"""Comparing arm totals, with no breakdown inside each arm.

"Compare the number of trials across breast cancer, Alzheimer's and heart
disease" parsed its arms correctly and then dropped them: the pipeline only ran
a comparison when a grouping dimension was present. The arms were discarded,
the base query was left with no constraints at all, and the response reported
609,811 trials — effectively the whole registry — as a single confident number.

Each arm's own ``totalCount`` is the answer here, and it is exact.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from app.config import settings
from app.ctgov.client import CtgovClient, clear_cache
from app.ctgov.compare import fetch_comparison, should_compare
from app.ctgov.params import build_query
from app.models.plan import ResolvedPlan
from app.viz.resolver import resolve as resolve_viz

BASE = settings.ctgov_base_url


@pytest.fixture(autouse=True)
def _clear_cache():
    clear_cache()
    yield
    clear_cache()


def plan(**kwargs) -> ResolvedPlan:
    base = dict(is_clinical_trials_question=True, intent="comparison",
                interpretation="Compare trial counts.", limit=10)
    return ResolvedPlan(**{**base, **kwargs})


def _page(prefix: str, total: int, n: int = 3) -> dict:
    return {
        "totalCount": total,
        "studies": [
            {"protocolSection": {
                "identificationModule": {"nctId": f"{prefix}{i}", "briefTitle": "S"}}}
            for i in range(n)
        ],
    }


ARMS = ["breast cancer", "Alzheimer's disease", "heart disease"]


def totals_plan() -> ResolvedPlan:
    return plan(compare_by="condition", compare_values=ARMS, group_by=None)


def test_a_totals_comparison_is_still_a_comparison():
    """The gate must not depend on having a grouping dimension."""
    assert should_compare(totals_plan())


@respx.mock
async def test_each_arm_reports_its_own_exact_total():
    route = respx.get(f"{BASE}/studies")
    route.side_effect = [
        httpx.Response(200, json=_page("A", 16979)),
        httpx.Response(200, json=_page("B", 4291)),
        httpx.Response(200, json=_page("C", 34073)),
    ]
    async with CtgovClient() as client:
        result = await fetch_comparison(client, totals_plan(), None, cap=10000)

    values = [(b.label, b.value) for b in result.bucket_set.buckets]
    assert values == [
        ("breast cancer", 16979),
        ("Alzheimer's disease", 4291),
        ("heart disease", 34073),
    ]
    # Server-reported, not counted from the handful of records fetched to cite.
    assert all(b.value_is_exact for b in result.bucket_set.buckets)
    assert result.bucket_set.counts_are_exact


@respx.mock
async def test_arm_totals_are_not_presented_as_additive():
    route = respx.get(f"{BASE}/studies")
    route.side_effect = [
        httpx.Response(200, json=_page("A", 10)),
        httpx.Response(200, json=_page("B", 20)),
        httpx.Response(200, json=_page("C", 30)),
    ]
    async with CtgovClient() as client:
        result = await fetch_comparison(client, totals_plan(), None, cap=10000)

    invariant = result.bucket_set.invariant
    assert invariant.kind == "multi_label"
    assert "not deduplicated" in invariant.statement
    assert "must not be summed" in invariant.statement


@respx.mock
async def test_every_arm_carries_evidence():
    route = respx.get(f"{BASE}/studies")
    route.side_effect = [
        httpx.Response(200, json=_page("A", 10)),
        httpx.Response(200, json=_page("B", 20)),
        httpx.Response(200, json=_page("C", 30)),
    ]
    async with CtgovClient() as client:
        result = await fetch_comparison(client, totals_plan(), None, cap=10000)

    for bucket in result.bucket_set.buckets:
        assert bucket.citations, bucket.label
        assert bucket.citation_total == bucket.value
        assert bucket.citation_sample is True


@respx.mock
async def test_arms_keep_the_order_the_question_named_them():
    route = respx.get(f"{BASE}/studies")
    route.side_effect = [
        httpx.Response(200, json=_page("A", 1)),
        httpx.Response(200, json=_page("B", 999)),
        httpx.Response(200, json=_page("C", 50)),
    ]
    async with CtgovClient() as client:
        result = await fetch_comparison(client, totals_plan(), None, cap=10000)
    assert [b.label for b in result.bucket_set.buckets] == ARMS


def test_the_arms_are_charted_as_bars_not_collapsed_into_one_number():
    """The reported failure rendered three arms as a single stat card."""
    from app.models.buckets import Bucket, BucketSet, Invariant

    bucket_set = BucketSet(
        dimension="compare:condition",
        buckets=[Bucket(key=a, label=a, value=v) for a, v in zip(ARMS, (1, 2, 3))],
        invariant=Invariant(kind="multi_label", statement="t"),
    )
    decision = resolve_viz(totals_plan(), bucket_set, records_fetched=9)
    assert decision.type == "bar_chart"
    assert decision.needed


def test_a_comparison_never_leaves_the_query_unconstrained():
    """An empty base query would scan the whole registry.

    It is tolerated only because each arm supplies its own filter, so the arms
    must actually run.
    """
    resolved = totals_plan()
    assert should_compare(resolved), "arms must execute; otherwise the base query runs bare"
    params = build_query(resolved).params
    assert not any(k.startswith("query.cond") for k in params)
