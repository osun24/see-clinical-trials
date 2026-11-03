"""Ordering and ranking across merged comparison arms.

Arms were bucketed and sorted individually and then concatenated, so the merged
list ran arm by arm. On a time axis the renderer plots that order literally: a
three-condition comparison produced an x-axis reading
"1987 1992 ... 2020 2024 1981 1977 1985", because the early years existed only
in the second and third arms and were appended after the first arm's range. The
join looked like a cliff down to zero that no data supported.

Ranking had the same shape of bug: each arm kept its own top-N, so a key could
be shown in one arm and silently absent from another rather than shown at the
lower value it actually had.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from app.config import settings
from app.ctgov.client import CtgovClient, clear_cache
from app.ctgov.compare import fetch_comparison
from app.dimensions.registry import get
from app.models.plan import ResolvedPlan

BASE = settings.ctgov_base_url


@pytest.fixture(autouse=True)
def _clear_cache():
    clear_cache()
    yield
    clear_cache()


def plan(**kwargs) -> ResolvedPlan:
    base = dict(is_clinical_trials_question=True, intent="comparison",
                interpretation="t", limit=10)
    return ResolvedPlan(**{**base, **kwargs})


def _years_page(prefix: str, years: list[str]) -> dict:
    return {
        "totalCount": len(years),
        "studies": [
            {"protocolSection": {
                "identificationModule": {"nctId": f"{prefix}{i}", "briefTitle": "S"},
                "statusModule": {"startDateStruct": {"date": f"{year}-01-01"}},
            }}
            for i, year in enumerate(years)
        ],
    }


def _sponsor_page(prefix: str, sponsors: list[str]) -> dict:
    return {
        "totalCount": len(sponsors),
        "studies": [
            {"protocolSection": {
                "identificationModule": {"nctId": f"{prefix}{i}", "briefTitle": "S"},
                "sponsorCollaboratorsModule": {"leadSponsor": {"name": name}},
            }}
            for i, name in enumerate(sponsors)
        ],
    }


@respx.mock
async def test_merged_time_axis_is_chronological():
    """The reported failure: later arms carry years earlier than the first arm."""
    route = respx.get(f"{BASE}/studies")
    route.side_effect = [
        httpx.Response(200, json=_years_page("A", ["1990", "2000", "2010"])),
        httpx.Response(200, json=_years_page("B", ["1981", "2000", "2020"])),
        httpx.Response(200, json=_years_page("C", ["1977", "1985", "2010"])),
    ]
    resolved = plan(compare_by="condition",
                    compare_values=["breast cancer", "Alzheimer's disease", "heart disease"],
                    group_by="start_year")

    async with CtgovClient() as client:
        result = await fetch_comparison(client, resolved, get("start_year"), cap=1000)

    order: list[str] = []
    for bucket in result.bucket_set.buckets:
        if bucket.key not in order:
            order.append(bucket.key)

    assert order == sorted(order), f"x-axis not chronological: {order}"
    assert order[0] == "1977", order
    assert order[-1] == "2020", order


@respx.mock
async def test_each_arm_still_becomes_its_own_series():
    route = respx.get(f"{BASE}/studies")
    route.side_effect = [
        httpx.Response(200, json=_years_page("A", ["2000"])),
        httpx.Response(200, json=_years_page("B", ["1990"])),
    ]
    resolved = plan(compare_by="condition", compare_values=["x", "y"], group_by="start_year")

    async with CtgovClient() as client:
        result = await fetch_comparison(client, resolved, get("start_year"), cap=1000)

    assert {b.series for b in result.bucket_set.buckets} == {"x", "y"}
    assert [b.key for b in result.bucket_set.buckets] == ["1990", "2000"]


@respx.mock
async def test_ranking_uses_the_combined_total_not_each_arm_separately():
    """A key strong in one arm must not be dropped from the other's view."""
    route = respx.get(f"{BASE}/studies")
    route.side_effect = [
        # Arm A: Acme dominates, Globex appears once.
        httpx.Response(200, json=_sponsor_page("A", ["Acme"] * 5 + ["Globex"])),
        # Arm B: Globex dominates, Acme appears once.
        httpx.Response(200, json=_sponsor_page("B", ["Globex"] * 5 + ["Acme"])),
    ]
    resolved = plan(compare_by="condition", compare_values=["x", "y"],
                    group_by="lead_sponsor", limit=2)

    async with CtgovClient() as client:
        result = await fetch_comparison(client, resolved, get("lead_sponsor"), cap=1000)

    cells = {(b.key, b.series): b.value for b in result.bucket_set.buckets}
    # Both sponsors clear a combined top-2, so every arm reports both values
    # rather than hiding the one that is small within that arm.
    assert cells.get(("Acme", "x")) == 5 and cells.get(("Acme", "y")) == 1
    assert cells.get(("Globex", "x")) == 1 and cells.get(("Globex", "y")) == 5
