"""Exact comparison matrices and fair fallback record allocation."""

from __future__ import annotations

import re

import httpx
import pytest
import respx

from app.config import settings
from app.ctgov.client import CtgovClient, clear_cache
from app.ctgov.compare import _allocate_arm_caps, fetch_comparison
from app.dimensions.registry import get
from app.models.plan import ResolvedPlan
from app.pipeline import _coverage


BASE = settings.ctgov_base_url


@pytest.fixture(autouse=True)
def _clear_cache():
    clear_cache()
    yield
    clear_cache()


def plan(*, group_by: str, compare_by: str, values: list[str]) -> ResolvedPlan:
    return ResolvedPlan(
        is_clinical_trials_question=True,
        intent="comparison",
        interpretation="comparison",
        group_by=group_by,
        compare_by=compare_by,
        compare_values=values,
        limit=10,
    )


def study(nct_id: str, *, phase: str = "PHASE1", sponsor_class: str | None = None):
    protocol = {
        "identificationModule": {"nctId": nct_id, "briefTitle": nct_id},
        "designModule": {"phases": [phase]},
    }
    if sponsor_class:
        protocol["sponsorCollaboratorsModule"] = {
            "leadSponsor": {"name": "Sponsor", "class": sponsor_class}
        }
    return {"protocolSection": protocol}


def test_water_fill_reuses_capacity_from_small_arms():
    assert _allocate_arm_caps({"small": 2, "large": 8}, 10) == {
        "small": 2,
        "large": 8,
    }
    assert _allocate_arm_caps({"small": 2, "large": 20}, 10) == {
        "small": 2,
        "large": 8,
    }


@respx.mock
async def test_adaptive_fallback_fetches_a_complete_skewed_population_under_cap():
    totals = {"Alpha": 2, "Beta": 8}
    fetched_page_sizes: dict[str, int] = {}

    def respond(request: httpx.Request) -> httpx.Response:
        arm = request.url.params["query.spons"]
        page_size = int(request.url.params["pageSize"])
        if page_size == 1:
            return httpx.Response(200, json={"totalCount": totals[arm], "studies": []})
        fetched_page_sizes[arm] = page_size
        rows = [study(f"NCT{arm[0]}{i:06d}") for i in range(page_size)]
        return httpx.Response(200, json={"totalCount": totals[arm], "studies": rows})

    respx.get(f"{BASE}/studies").mock(side_effect=respond)
    resolved = plan(group_by="phase", compare_by="sponsor", values=["Alpha", "Beta"])
    async with CtgovClient() as client:
        result = await fetch_comparison(
            client,
            resolved,
            get("phase"),
            cap=10,
            adaptive_caps=True,
        )

    assert fetched_page_sizes == {"Alpha": 2, "Beta": 8}
    assert result.arm_fetched == totals
    assert not result.fetch.is_sample


@respx.mock
async def test_exact_comparison_counts_every_arm_bucket_cell():
    totals = {"Condition A": 10, "Condition B": 20}
    cell_counts = {
        ("Condition A", "INDUSTRY"): 5,
        ("Condition A", "OTHER"): 5,
        ("Condition B", "INDUSTRY"): 7,
        ("Condition B", "OTHER"): 13,
    }
    cell_requests = 0

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal cell_requests
        arm = request.url.params["query.cond"]
        advanced = request.url.params.get("filter.advanced")
        if not advanced:
            return httpx.Response(200, json={"totalCount": totals[arm], "studies": []})
        cell_requests += 1
        match = re.search(r"LeadSponsorClass\]\(([^)]+)\)", advanced)
        assert match
        key = match.group(1)
        count = cell_counts.get((arm, key), 0)
        rows = [study(f"NCT{arm[-1]}{key[:2]}0001", sponsor_class=key)] if count else []
        return httpx.Response(200, json={"totalCount": count, "studies": rows})

    respx.get(f"{BASE}/studies").mock(side_effect=respond)
    resolved = plan(
        group_by="sponsor_type",
        compare_by="condition",
        values=["Condition A", "Condition B"],
    )
    async with CtgovClient() as client:
        result = await fetch_comparison(
            client,
            resolved,
            get("sponsor_type"),
            cap=10_000,
            prefer_exact=True,
            adaptive_caps=True,
        )

    assert cell_requests == 2 * len(get("sponsor_type").enum_values)
    assert result.bucket_set.counts_are_exact
    assert result.fetch.sampling_method == "stratified_by_bucket"
    assert all(bucket.value_is_exact for bucket in result.bucket_set.buckets)
    assert {
        (bucket.series, bucket.key): bucket.value for bucket in result.bucket_set.buckets
    } == cell_counts

    coverage = _coverage(result.fetch, result.bucket_set, 10_000, result)
    assert coverage.counts_are_exact
    assert not coverage.is_sample
    assert not any(arm.is_sample for arm in coverage.comparison_arms.values())

