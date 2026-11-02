"""Side-by-side comparison arms.

"Compare A against B" is the question shape an executive asks most and cannot
be expressed as a single ClinicalTrials.gov query. Before this existed the
service silently answered a different question.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from app.config import settings
from app.ctgov.client import CtgovClient, clear_cache
from app.ctgov.compare import fetch_comparison, should_compare
from app.dimensions.registry import get
from app.interpret.merge import resolve
from app.models.plan import QueryPlan, ResolvedPlan
from app.models.request import QueryRequest

BASE = settings.ctgov_base_url


@pytest.fixture(autouse=True)
def _clear_cache():
    clear_cache()
    yield
    clear_cache()


def plan(**kwargs) -> ResolvedPlan:
    base = dict(
        is_clinical_trials_question=True,
        intent="comparison",
        interpretation="t",
        limit=10,
    )
    return ResolvedPlan(**{**base, **kwargs})


def make_plan(**kwargs) -> QueryPlan:
    base = dict(is_clinical_trials_question=True, intent="comparison", interpretation="t")
    return QueryPlan(**{**base, **kwargs})


# --- gating ---------------------------------------------------------------

def test_comparison_needs_a_slot_and_two_values():
    assert should_compare(plan(compare_by="sponsor", compare_values=["A", "B"]))
    assert not should_compare(plan(compare_by="sponsor", compare_values=["A"]))
    assert not should_compare(plan(compare_values=["A", "B"]))
    assert not should_compare(plan())


def test_unsupported_slot_is_structurally_impossible():
    """The strict schema's enum keeps an unaggregatable slot out entirely."""
    with pytest.raises(Exception):
        make_plan(compare_by="phase", compare_values=["A", "B"])


def test_unsupported_slot_is_still_dropped_defensively():
    """Belt and braces: if one ever reached merge, it must not be fetched."""
    from app.models.plan import ResolvedPlan

    resolved = ResolvedPlan(
        is_clinical_trials_question=True, intent="comparison", interpretation="t",
        limit=10, compare_by="phase", compare_values=["A", "B"],
    )
    assert not should_compare(resolved)


def test_a_single_value_is_not_a_comparison():
    resolved = resolve(
        make_plan(compare_by="sponsor", compare_values=["Pfizer"]), QueryRequest(query="q")
    )
    assert resolved.compare_by is None
    assert any("fewer than the 2" in a for a in resolved.adjustments)


def test_duplicate_arms_are_collapsed():
    resolved = resolve(
        make_plan(compare_by="sponsor", compare_values=["Pfizer", "pfizer", "Merck"]),
        QueryRequest(query="q"),
    )
    assert resolved.compare_values == ["Pfizer", "Merck"]
    assert any("duplicate" in a for a in resolved.adjustments)


def test_too_many_arms_are_trimmed():
    resolved = resolve(
        make_plan(compare_by="sponsor", compare_values=[f"S{i}" for i in range(9)]),
        QueryRequest(query="q"),
    )
    assert len(resolved.compare_values) == 5


def test_a_comparison_drops_a_competing_series():
    resolved = resolve(
        make_plan(compare_by="sponsor", compare_values=["A", "B"],
                  group_by="phase", series_by="country"),
        QueryRequest(query="q"),
    )
    assert resolved.series_by is None
    assert any("already splits the chart" in a for a in resolved.adjustments)


def test_free_text_is_dropped_so_it_cannot_narrow_every_arm():
    """A leftover phrase would be ANDed into each arm and match almost nothing."""
    resolved = resolve(
        make_plan(compare_by="sponsor", compare_values=["Pfizer", "Merck"],
                  search_terms="phase against merck"),
        QueryRequest(query="Compare the phase distribution of Pfizer against Merck."),
    )
    assert resolved.search_terms is None
    assert any("each comparison arm" in a for a in resolved.adjustments)


def test_comparison_suppresses_the_salvage_fallback():
    """compare_values are a filter, so the no-filter fallback must not fire."""
    resolved = resolve(
        make_plan(compare_by="sponsor", compare_values=["Pfizer", "Merck"], group_by="phase"),
        QueryRequest(query="Compare Pfizer against Merck by phase."),
    )
    assert resolved.search_terms is None
    assert not any("free text" in a for a in resolved.adjustments)


# --- fetching -------------------------------------------------------------

def _page(nct_ids: list[str], phase: str, total: int) -> dict:
    return {
        "totalCount": total,
        "studies": [
            {
                "protocolSection": {
                    "identificationModule": {"nctId": n, "briefTitle": f"Study {n}"},
                    "designModule": {"phases": [phase]},
                }
            }
            for n in nct_ids
        ],
    }


@respx.mock
async def test_each_arm_becomes_its_own_series():
    route = respx.get(f"{BASE}/studies")
    route.side_effect = [
        httpx.Response(200, json=_page(["NCT1", "NCT2"], "PHASE1", 2)),
        httpx.Response(200, json=_page(["NCT3"], "PHASE3", 1)),
    ]
    resolved = plan(compare_by="sponsor", compare_values=["Alpha", "Beta"], group_by="phase")

    async with CtgovClient() as client:
        result = await fetch_comparison(client, resolved, get("phase"), cap=1000)

    assert result.bucket_set.series_dimension == "compare:sponsor"
    assert {b.series for b in result.bucket_set.buckets} == {"Alpha", "Beta"}
    assert result.arm_totals == {"Alpha": 2, "Beta": 1}
    assert result.overlapping_trials == 0
    assert result.bucket_set.invariant.kind == "partition"


@respx.mock
async def test_each_arm_is_filtered_by_its_own_value():
    route = respx.get(f"{BASE}/studies")
    route.side_effect = [
        httpx.Response(200, json=_page(["NCT1"], "PHASE1", 1)),
        httpx.Response(200, json=_page(["NCT2"], "PHASE1", 1)),
    ]
    resolved = plan(compare_by="sponsor", compare_values=["Alpha", "Beta"], group_by="phase")

    async with CtgovClient() as client:
        await fetch_comparison(client, resolved, get("phase"), cap=1000)

    sponsors = {c.request.url.params.get("query.spons") for c in route.calls}
    assert sponsors == {"Alpha", "Beta"}


@respx.mock
async def test_overlapping_arms_are_measured_not_assumed_away():
    """Arms are independent queries, so one trial can satisfy several."""
    route = respx.get(f"{BASE}/studies")
    route.side_effect = [
        httpx.Response(200, json=_page(["NCT1", "NCT2"], "PHASE1", 2)),
        httpx.Response(200, json=_page(["NCT2", "NCT3"], "PHASE1", 2)),
    ]
    resolved = plan(compare_by="condition", compare_values=["X", "Y"], group_by="phase")

    async with CtgovClient() as client:
        result = await fetch_comparison(client, resolved, get("phase"), cap=1000)

    assert result.overlapping_trials == 1
    assert result.bucket_set.invariant.kind == "multi_label"
    assert result.bucket_set.records_analyzed == 3


@respx.mock
async def test_an_empty_arm_is_reported():
    route = respx.get(f"{BASE}/studies")
    route.side_effect = [
        httpx.Response(200, json=_page(["NCT1"], "PHASE1", 1)),
        httpx.Response(200, json={"totalCount": 0, "studies": []}),
    ]
    resolved = plan(compare_by="sponsor", compare_values=["Real", "Nonexistent"], group_by="phase")

    async with CtgovClient() as client:
        result = await fetch_comparison(client, resolved, get("phase"), cap=1000)

    assert result.empty_arms == ["Nonexistent"]
    assert {b.series for b in result.bucket_set.buckets} == {"Real"}


@respx.mock
async def test_reported_filters_name_each_arm():
    """The base plan has no filters of its own, so reporting it would show a
    full-text fallback that no arm ever used."""
    from unittest.mock import patch

    from fastapi.testclient import TestClient

    from app.main import app
    from app.models.plan import QueryPlan

    respx.get(f"{BASE}/version").mock(
        return_value=httpx.Response(200, json={"apiVersion": "2.0.5", "dataTimestamp": "x"})
    )
    respx.get(f"{BASE}/studies").mock(
        return_value=httpx.Response(200, json=_page(["NCT1"], "PHASE1", 1))
    )
    model_plan = QueryPlan(
        is_clinical_trials_question=True, intent="comparison",
        interpretation="Compare Alpha and Beta",
        group_by="phase", compare_by="sponsor", compare_values=["Alpha", "Beta"],
    )
    with patch("app.pipeline.interpret", return_value=model_plan):
        with TestClient(app) as client:
            body = client.post("/query", json={"query": "Compare Alpha and Beta"}).json()

    filters = body["meta"]["query_interpretation"]["filters_applied"]
    assert filters == {"Alpha / query.spons": "Alpha", "Beta / query.spons": "Beta"}
    # The interpretation sentence must never leak in as a search term.
    assert not any("Compare" in v for v in filters.values())


@respx.mock
async def test_comparison_renders_as_grouped_not_stacked():
    """Stacking hides the difference the comparison was asked about."""
    from unittest.mock import patch

    from fastapi.testclient import TestClient

    from app.main import app
    from app.models.plan import QueryPlan

    respx.get(f"{BASE}/version").mock(
        return_value=httpx.Response(200, json={"apiVersion": "2.0.5", "dataTimestamp": "x"})
    )
    route = respx.get(f"{BASE}/studies")
    route.side_effect = [
        httpx.Response(200, json=_page(["NCT1", "NCT2"], "PHASE1", 2)),
        httpx.Response(200, json=_page(["NCT3"], "PHASE2", 1)),
    ]
    model_plan = QueryPlan(
        is_clinical_trials_question=True, intent="composition",
        interpretation="share comparison", group_by="phase",
        compare_by="sponsor", compare_values=["Alpha", "Beta"],
    )
    with patch("app.pipeline.interpret", return_value=model_plan):
        with TestClient(app) as client:
            body = client.post("/query", json={"query": "Compare Alpha and Beta"}).json()

    assert body["visualization"]["type"] == "grouped_bar"
    assert body["visualization"]["encoding"]["stack"] is False
    assert body["visualization"]["encoding"]["series"]["legend_title"] == "Sponsor"
    assert body["meta"]["series_by"] == "compare:sponsor"
