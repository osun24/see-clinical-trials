"""Outbound query construction, pagination, and retry behaviour."""

from __future__ import annotations

import httpx
import pytest
import respx

from app.config import settings
from app.ctgov.client import CtgovClient, clear_cache
from app.ctgov.fetch import fetch_flat
from app.ctgov.params import build_query, year_universe
from app.dimensions.registry import get
from app.models.errors import ServiceError
from app.models.plan import ResolvedPlan
from app.ctgov.stratified import bucket_universe, should_stratify

BASE = settings.ctgov_base_url


def plan(**kwargs) -> ResolvedPlan:
    base = dict(
        is_clinical_trials_question=True,
        intent="distribution",
        interpretation="test",
        limit=10,
    )
    return ResolvedPlan(**{**base, **kwargs})


@pytest.fixture(autouse=True)
def _clear_cache():
    clear_cache()
    yield
    clear_cache()


def _page(count: int, total: int, token: str | None = None) -> dict:
    payload = {
        "totalCount": total,
        "studies": [
            {"protocolSection": {"identificationModule": {"nctId": f"NCT{i:08d}"}}}
            for i in range(count)
        ],
    }
    if token:
        payload["nextPageToken"] = token
    return payload


# --- parameter building ---------------------------------------------------

def test_each_filter_lands_in_its_own_parameter():
    spec = build_query(
        plan(drug_name="pembrolizumab", condition="melanoma", sponsor="Merck", country="Germany")
    )
    assert spec.params["query.intr"] == "pembrolizumab"
    assert spec.params["query.cond"] == "melanoma"
    assert spec.params["query.spons"] == "Merck"
    assert spec.params["query.locn"] == "Germany"


def test_phase_and_date_filters_combine_with_and():
    spec = build_query(plan(trial_phase=["PHASE2", "PHASE3"], start_year=2020, end_year=2022))
    advanced = spec.params["filter.advanced"]
    assert "(AREA[Phase](PHASE2) OR AREA[Phase](PHASE3))" in advanced
    assert "AREA[StartDate]RANGE[2020-01-01,2022-12-31]" in advanced
    assert " AND " in advanced


def test_open_ended_date_ranges_use_sentinels():
    assert "RANGE[MIN,2015-12-31]" in build_query(plan(end_year=2015)).params["filter.advanced"]
    assert "RANGE[2020-01-01,MAX]" in build_query(plan(start_year=2020)).params["filter.advanced"]


def test_multiple_statuses_join_with_a_pipe():
    spec = build_query(plan(overall_status=["RECRUITING", "COMPLETED"]))
    assert spec.params["filter.overallStatus"] == "RECRUITING|COMPLETED"


def test_bucket_override_merges_into_the_existing_advanced_filter():
    spec = build_query(plan(drug_name="x", start_year=2020))
    scoped = spec.with_overrides({"filter.advanced": "AREA[Phase](PHASE1)"})
    advanced = scoped.params["filter.advanced"]
    assert "AREA[StartDate]RANGE" in advanced and "AREA[Phase](PHASE1)" in advanced
    # The base filter must survive; replacing it would widen the bucket query.
    assert advanced.count("AND") >= 1
    assert spec.params["filter.advanced"] != advanced


def test_a_filterless_plan_falls_back_to_the_question_text():
    spec = build_query(plan(interpretation="CAR-T trials"))
    assert "query.term" in spec.params


# --- stratification gating -------------------------------------------------

def test_year_universe_follows_the_requested_range():
    assert year_universe(plan(start_year=2020, end_year=2023)) == ["2020", "2021", "2022", "2023"]


def test_stratify_only_for_exact_capable_dimensions():
    assert should_stratify(get("overall_status"), plan())
    assert should_stratify(get("phase_any"), plan())
    # Fuzzy string buckets cannot be expressed as a server-side filter.
    assert not should_stratify(get("lead_sponsor"), plan())
    assert not should_stratify(get("country"), plan())
    assert not should_stratify(get("phase"), plan())


def test_stratify_is_skipped_when_the_universe_is_too_large():
    wide = plan(start_year=1990, end_year=2030)
    assert len(bucket_universe(get("start_year"), wide)) > settings.exact_max_buckets
    assert not should_stratify(get("start_year"), wide)


def test_stratify_is_skipped_for_two_axis_plans():
    assert not should_stratify(get("overall_status"), plan(series_by="phase"))


# --- pagination -------------------------------------------------------------

@respx.mock
async def test_pagination_follows_tokens_until_the_cap():
    route = respx.get(f"{BASE}/studies")
    route.side_effect = [
        httpx.Response(200, json=_page(1000, 2500, token="t1")),
        httpx.Response(200, json=_page(1000, 2500, token="t2")),
        httpx.Response(200, json=_page(500, 2500)),
    ]
    async with CtgovClient() as client:
        result = await fetch_flat(client, build_query(plan(drug_name="x")), cap=2000)

    assert len(result.studies) == 2000
    assert result.pages_fetched == 2
    assert result.truncated_reason == "fetch_cap_reached"
    assert result.is_sample is True
    assert result.sampling_method == "first_n_by_api_relevance_order"


@respx.mock
async def test_a_complete_fetch_is_reported_as_a_census():
    respx.get(f"{BASE}/studies").mock(
        return_value=httpx.Response(200, json=_page(120, 120))
    )
    async with CtgovClient() as client:
        result = await fetch_flat(client, build_query(plan(drug_name="x")), cap=2000)

    assert result.is_sample is False
    assert result.truncated_reason is None
    assert result.sampling_method == "census"


@respx.mock
async def test_count_total_is_requested_once():
    route = respx.get(f"{BASE}/studies")
    route.side_effect = [
        httpx.Response(200, json=_page(1000, 1500, token="t1")),
        httpx.Response(200, json=_page(500, 1500)),
    ]
    async with CtgovClient() as client:
        await fetch_flat(client, build_query(plan(drug_name="x")), cap=2000)

    assert "countTotal" in route.calls[0].request.url.params
    assert "countTotal" not in route.calls[1].request.url.params
    assert route.calls[1].request.url.params["pageToken"] == "t1"


# --- error handling ----------------------------------------------------------

@respx.mock
async def test_a_400_is_our_bug_and_is_not_retried():
    route = respx.get(f"{BASE}/studies").mock(
        return_value=httpx.Response(400, text="bad filter expression")
    )
    async with CtgovClient() as client:
        with pytest.raises(ServiceError) as excinfo:
            await fetch_flat(client, build_query(plan(drug_name="x")))

    assert excinfo.value.code == "upstream_rejected_query"
    assert excinfo.value.status_code == 400
    assert route.call_count == 1, "retrying our own malformed query cannot help"


@respx.mock
async def test_transient_failures_are_retried_then_succeed():
    route = respx.get(f"{BASE}/studies")
    route.side_effect = [
        httpx.Response(503),
        httpx.Response(200, json=_page(10, 10)),
    ]
    async with CtgovClient() as client:
        result = await fetch_flat(client, build_query(plan(drug_name="x")))

    assert len(result.studies) == 10
    assert route.call_count == 2


@respx.mock
async def test_persistent_5xx_becomes_a_retryable_502():
    respx.get(f"{BASE}/studies").mock(return_value=httpx.Response(503))
    async with CtgovClient() as client:
        with pytest.raises(ServiceError) as excinfo:
            await fetch_flat(client, build_query(plan(drug_name="x")))

    assert excinfo.value.code == "upstream_unavailable"
    assert excinfo.value.status_code == 502
    assert excinfo.value.retryable is True


@respx.mock
async def test_identical_requests_are_served_from_cache():
    route = respx.get(f"{BASE}/studies").mock(
        return_value=httpx.Response(200, json=_page(5, 5))
    )
    spec = build_query(plan(drug_name="x"))
    async with CtgovClient() as client:
        await fetch_flat(client, spec)
        await fetch_flat(client, spec)

    assert route.call_count == 1
