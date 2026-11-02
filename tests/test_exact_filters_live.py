"""Live checks against the real API. Excluded from the default run.

Run with ``uv run pytest -m live``. These assert the facts the registry depends
on, so if ClinicalTrials.gov changes a filter expression the registry's
``exact_filter`` entries stop being trustworthy and this is how you find out.
"""

from __future__ import annotations

import pytest

from app.ctgov.client import CtgovClient
from app.ctgov.fetch import fetch_total_only
from app.ctgov.params import CtgovQuerySpec
from app.dimensions.registry import DIMENSIONS
from app.models.plan import ResolvedPlan

pytestmark = pytest.mark.live

BASE_SPEC = CtgovQuerySpec(params={"query.intr": "pembrolizumab"})


def _plan() -> ResolvedPlan:
    return ResolvedPlan(
        is_clinical_trials_question=True,
        intent="distribution",
        interpretation="live check",
        limit=10,
        drug_name="pembrolizumab",
        start_year=2020,
        end_year=2022,
    )


@pytest.mark.parametrize(
    "dimension_id",
    [d.id for d in DIMENSIONS.values() if d.is_exact_capable],
)
async def test_exact_filter_expressions_are_accepted(dimension_id):
    from app.ctgov.stratified import bucket_universe

    dimension = DIMENSIONS[dimension_id]
    assert dimension.exact_filter is not None
    key = bucket_universe(dimension, _plan())[0]

    async with CtgovClient() as client:
        total = await fetch_total_only(
            client, BASE_SPEC.with_overrides(dimension.exact_filter(key))
        )
    assert total >= 0


async def test_status_strata_sum_to_the_universe():
    """A partition's exact counts should account for every matching trial."""
    dimension = DIMENSIONS["overall_status"]
    async with CtgovClient() as client:
        total = await fetch_total_only(client, BASE_SPEC)
        counted = 0
        for value in dimension.enum_values:
            counted += await fetch_total_only(
                client, BASE_SPEC.with_overrides(dimension.exact_filter(value))  # type: ignore[misc]
            )
    assert counted == total


async def test_sex_filter_still_over_matches():
    """Documents why `sex` is excluded from exact counting.

    AREA[Sex](ALL) returns the whole result set rather than only sex=ALL
    trials. If this ever starts behaving, `sex` can be promoted.
    """
    async with CtgovClient() as client:
        total = await fetch_total_only(client, BASE_SPEC)
        all_sex = await fetch_total_only(
            client, BASE_SPEC.with_overrides({"filter.advanced": "AREA[Sex](ALL)"})
        )
        female = await fetch_total_only(
            client, BASE_SPEC.with_overrides({"filter.advanced": "AREA[Sex](FEMALE)"})
        )
    assert all_sex == total and female > 0, (
        "AREA[Sex] may now filter correctly; re-evaluate registry.sex.exact_filter"
    )


async def test_full_field_mask_is_accepted():
    from app.dimensions.registry import ALL_API_FIELDS

    async with CtgovClient() as client:
        payload = await client.get_json(
            "/studies",
            {
                "query.intr": "pembrolizumab",
                "fields": ",".join(ALL_API_FIELDS),
                "pageSize": "1",
            },
        )
    assert payload["studies"]
