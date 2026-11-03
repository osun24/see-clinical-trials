"""Closed-domain filter values.

Every case here failed in the release evaluation: the planner wrote a filter
value the way a person says it, ``filter`` casefolded both sides, nothing
matched, and the response reported an empty result for a plan that read
correctly. These assert the failure cannot recur silently.
"""

import pytest

from app.analysis import domains
from app.analysis.engine import execute, validate
from app.analysis.models import AnalysisPlan
from app.models.errors import ServiceError
from tests.conftest import make_study


def phase_plan(value: str, predicate: str = "equals") -> AnalysisPlan:
    return AnalysisPlan.model_validate({"steps": [
        {"op": "filter", "id": "chosen", "input": "trials", "field": "phase",
         "predicate": predicate, "value": value},
        {"op": "aggregate", "id": "counts", "input": "chosen",
         "group_by": ["overall_status"], "measure": "count_distinct_trials"},
    ]})


@pytest.fixture
def records():
    return [
        make_study("NCT1", designModule={"phases": ["PHASE4"]},
                  statusModule={"overallStatus": "RECRUITING"}),
        make_study("NCT2", designModule={"phases": ["PHASE2"]},
                  statusModule={"overallStatus": "COMPLETED"}),
    ]


@pytest.mark.parametrize("given,expected", [
    ("PHASE4", "PHASE4"),
    ("Phase 4", "PHASE4"),      # the exact value that emptied the live result
    ("phase 4", "PHASE4"),
    ("phase-4", "PHASE4"),
    ("Not Applicable", "NA"),   # the display label, not the stored value
    ("NA", "NA"),
])
def test_recognised_phase_spellings_canonicalise(given, expected):
    assert domains.canonical("phase", given) == expected


@pytest.mark.parametrize("column,given,expected", [
    ("overall_status", "Recruiting", "RECRUITING"),
    ("overall_status", "Active, not recruiting", "ACTIVE_NOT_RECRUITING"),
    ("study_type", "Interventional", "INTERVENTIONAL"),
    ("enrollment_type", "Estimated", "ESTIMATED"),
    ("sponsor_type", "Industry", "INDUSTRY"),
])
def test_other_closed_columns_canonicalise(column, given, expected):
    assert domains.canonical(column, given) == expected


def test_open_columns_pass_through_unchanged():
    assert domains.canonical("lead_sponsor", "Pfizer") == "Pfizer"
    assert domains.canonical("condition", "non-small cell lung cancer") == (
        "non-small cell lung cancer"
    )


def test_value_outside_a_closed_domain_is_rejected_with_the_allowed_values():
    with pytest.raises(ServiceError) as caught:
        validate(phase_plan("Phase 9"))
    assert caught.value.code == "query_not_interpretable"
    assert "PHASE4" in caught.value.message


@pytest.mark.asyncio
async def test_prose_phase_filter_matches_the_stored_enum(records):
    result = await execute(phase_plan("Phase 4"), records, "phase 4 trials")
    # A custom plan groups by the stored value; display labelling belongs to the
    # registered-dimension path and is asserted there.
    assert [(b.label, b.value) for b in result.buckets] == [("RECRUITING", 1)]
    assert result.metadata["filtered_rows"] == 1


@pytest.mark.asyncio
async def test_canonical_and_prose_spellings_agree(records):
    prose = await execute(phase_plan("Phase 4"), records, "q")
    raw = await execute(phase_plan("PHASE4"), records, "q")
    assert [b.model_dump() for b in prose.buckets] == [b.model_dump() for b in raw.buckets]


def test_canonicalisation_is_recorded_as_provenance():
    assert validate(phase_plan("Phase 4"))["filter_canonicalisation"] == [
        {"step": "chosen", "field": "phase", "given": "Phase 4", "applied": "PHASE4"}
    ]
    assert validate(phase_plan("PHASE4"))["filter_canonicalisation"] == []


@pytest.mark.asyncio
async def test_not_equals_also_canonicalises(records):
    result = await execute(phase_plan("Phase 4", predicate="not_equals"), records, "q")
    assert [(b.label, b.value) for b in result.buckets] == [("COMPLETED", 1)]


def test_contains_keeps_substring_semantics_without_rejecting():
    # "PHASE" is a prefix of several stored values, not a value itself; a
    # substring match must stay usable rather than be refused.
    plan = phase_plan("PHASE", predicate="contains")
    assert validate(plan)["filter_canonicalisation"] == []
