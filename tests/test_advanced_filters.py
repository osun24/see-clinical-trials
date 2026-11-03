"""Enum validation inside a model-supplied advanced filter expression.

The release evaluation spent a live request on
``AREA[Phase](PHASE1_PHASE2)`` and got a 400 back, which was reported as an
upstream rejection rather than the planning error it was.
"""

import pytest

from app.interpret.advanced import validate_advanced
from app.interpret.merge import resolve
from app.models.errors import ServiceError
from app.models.plan import QueryPlan
from app.models.request import QueryRequest

#: The exact expression ClinicalTrials.gov rejected during the gate run.
REJECTED_LIVE = (
    "(AREA[Phase](PHASE1) OR AREA[Phase](PHASE1_PHASE2) OR AREA[Phase](PHASE2)"
    " OR AREA[Phase](PHASE2_PHASE3) OR AREA[Phase](PHASE3) OR AREA[Phase](PHASE4))"
)


def test_the_expression_that_failed_live_is_rejected_before_fetching():
    with pytest.raises(ServiceError) as caught:
        validate_advanced(REJECTED_LIVE)
    assert caught.value.code == "query_not_interpretable"
    # The repair needs the allowed values to act on.
    assert "PHASE4" in caught.value.message
    assert "PHASE1_PHASE2" in caught.value.message


@pytest.mark.parametrize("expression", [
    "AREA[Phase](PHASE4)",
    "AREA[Phase](PHASE2 OR PHASE3)",
    "(AREA[Phase](PHASE4)) AND (AREA[StartDate]RANGE[2021-01-01,MAX])",
    "AREA[OverallStatus](RECRUITING)",
    "AREA[StudyType](INTERVENTIONAL)",
])
def test_valid_expressions_pass_through_unchanged(expression):
    checked, adjustments = validate_advanced(expression)
    assert checked == expression
    assert adjustments == []


@pytest.mark.parametrize("expression", [
    # Areas with no closed enum must not be touched.
    "AREA[LeadSponsorName](Pfizer)",
    "AREA[StartDate]RANGE[2020-01-01,MAX]",
    "AREA[Condition](melanoma)",
    None,
    "",
])
def test_non_enum_areas_are_left_alone(expression):
    checked, adjustments = validate_advanced(expression)
    assert checked == expression
    assert adjustments == []


def test_recognised_spellings_are_rewritten_and_disclosed():
    checked, adjustments = validate_advanced(
        "(AREA[Phase](Phase 4)) AND (AREA[OverallStatus](Recruiting))"
    )
    assert checked == "(AREA[Phase](PHASE4)) AND (AREA[OverallStatus](RECRUITING))"
    assert len(adjustments) == 2
    assert all("Rewrote" in a for a in adjustments)


@pytest.mark.parametrize("expression,bad", [
    ("AREA[Phase](PHASE9)", "PHASE9"),
    ("AREA[OverallStatus](FINISHED)", "FINISHED"),
    ("AREA[StudyType](EXPERIMENTAL)", "EXPERIMENTAL"),
])
def test_values_outside_the_api_enum_are_rejected(expression, bad):
    with pytest.raises(ServiceError) as caught:
        validate_advanced(expression)
    assert bad in caught.value.message


def _plan(advanced: str) -> QueryPlan:
    return QueryPlan.model_validate({
        "is_clinical_trials_question": True,
        "intent": "distribution",
        "interpretation": "Compare melanoma trial phases.",
        "group_by": "phase",
        "chart_hint": "grouped_bar",
        "search": {"query.cond": "melanoma", "filter.advanced": advanced},
    })


def test_resolve_rejects_the_invalid_expression_so_the_repair_can_run():
    with pytest.raises(ServiceError) as caught:
        resolve(_plan(REJECTED_LIVE), QueryRequest(query="Compare melanoma trial phases."))
    # This specific code is what triggers the single planning repair.
    assert caught.value.code == "query_not_interpretable"


def test_resolve_carries_the_rewrite_into_the_executed_search():
    from app.ctgov.params import build_query

    plan = resolve(_plan("AREA[Phase](Phase 4)"),
                   QueryRequest(query="Compare melanoma phase 4 trials."))
    assert build_query(plan).params["filter.advanced"] == "AREA[Phase](PHASE4)"
    assert any("Rewrote" in a for a in plan.adjustments)
