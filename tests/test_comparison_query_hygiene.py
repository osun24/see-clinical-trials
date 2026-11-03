"""Base-query hygiene for comparisons, and what counts as a sample.

"Compare phases for trials involving semaglutide vs. tirzepatide" returned a
different answer on every click: 1,106 trials, then 876, then none at all.
Three separate defects, all in how the base query and the arms combine.
"""

from __future__ import annotations

import pytest

from app.ctgov.fetch import FetchResult
from app.interpret.advanced import strip_compared_area
from app.interpret.merge import resolve
from app.models.plan import QueryPlan
from app.models.request import QueryRequest
from tests.conftest import make_study

QUESTION = "Compare phases for trials involving semaglutide vs. tirzepatide."
BOTH_DRUGS = "(AREA[InterventionName](semaglutide) OR AREA[InterventionName](tirzepatide))"


# --- 1. a base filter on the compared field narrows every arm -------------

def test_a_base_filter_on_the_compared_field_is_dropped():
    """Arms replace their own slot, but `filter.advanced` merges with AND.

    Keeping it intersected each arm with the union of all arms, cutting the
    semaglutide arm from 796 trials to 632.
    """
    stripped, removed = strip_compared_area(BOTH_DRUGS, "drug_name")
    assert removed and stripped is None


def test_unrelated_clauses_survive():
    stripped, removed = strip_compared_area(
        f"(AREA[Phase](PHASE4)) AND {BOTH_DRUGS}", "drug_name"
    )
    assert removed and stripped == "(AREA[Phase](PHASE4))"


@pytest.mark.parametrize("expression,compare_by", [
    ("AREA[Phase](PHASE4)", "drug_name"),          # different field
    (BOTH_DRUGS, "condition"),                      # comparing something else
    (BOTH_DRUGS, None),                             # not a comparison
    (None, "drug_name"),
])
def test_nothing_else_is_touched(expression, compare_by):
    stripped, removed = strip_compared_area(expression, compare_by)
    assert stripped == expression and not removed


def test_the_executed_arms_lose_the_shared_filter():
    from app.ctgov.params import build_query

    plan = QueryPlan.model_validate({
        "is_clinical_trials_question": True, "intent": "comparison",
        "interpretation": "Compare phases.", "group_by": "phase",
        "chart_hint": "grouped_bar", "compare_by": "drug_name",
        "compare_values": ["semaglutide", "tirzepatide"],
        "search": {"filter.advanced": BOTH_DRUGS},
    })
    resolved = resolve(plan, QueryRequest(query=QUESTION))
    assert "filter.advanced" not in build_query(resolved).params
    assert any("compared field" in a for a in resolved.adjustments)


# --- 2. salvage must not fire on an already-constrained query -------------

def test_an_advanced_only_query_is_not_treated_as_unfiltered():
    """`advanced` lives only on the search object and was never checked.

    A salvaged phrase was ANDed onto a valid filter, so a question with real
    constraints returned zero trials.
    """
    plan = QueryPlan.model_validate({
        "is_clinical_trials_question": True, "intent": "distribution",
        "interpretation": "Phases for two drugs.", "group_by": "phase",
        "chart_hint": "bar_chart",
        "search": {"filter.advanced": BOTH_DRUGS},
    })
    resolved = resolve(plan, QueryRequest(query=QUESTION))
    assert resolved.search_terms is None
    assert not any("No specific filter" in a for a in resolved.adjustments)


def test_a_status_only_query_is_not_treated_as_unfiltered():
    plan = QueryPlan.model_validate({
        "is_clinical_trials_question": True, "intent": "distribution",
        "interpretation": "Recruiting trials by phase.", "group_by": "phase",
        "chart_hint": "bar_chart",
        "search": {"filter.overallStatus": "RECRUITING"},
    })
    resolved = resolve(plan, QueryRequest(query="What phases are recruiting trials in?"))
    assert resolved.search_terms is None


def test_a_genuinely_unfiltered_query_still_gets_salvaged():
    plan = QueryPlan.model_validate({
        "is_clinical_trials_question": True, "intent": "distribution",
        "interpretation": "Melanoma trials.", "group_by": "phase",
        "chart_hint": "bar_chart", "search": {},
    })
    resolved = resolve(plan, QueryRequest(query="How many melanoma trials are there?"))
    assert resolved.search_terms == "melanoma"


# --- 3. overlap is not sampling -------------------------------------------

def test_overlapping_arms_are_not_reported_as_a_sample():
    """A comparison's total is a sum of arm totals, so it double-counts.

    837 unique records against a summed 876 was reported as a 95% sample with
    "counts are approximate", though every matching trial had been fetched.
    """
    studies = [make_study(f"NCT{i}") for i in range(3)]
    complete = FetchResult(studies=studies, total_matching=4, duplicate_arm_matches=1)
    assert not complete.is_sample, "3 unique + 1 double-counted == the summed total"


def test_a_genuinely_short_fetch_is_still_a_sample():
    studies = [make_study(f"NCT{i}") for i in range(3)]
    short = FetchResult(studies=studies, total_matching=10, duplicate_arm_matches=1)
    assert short.is_sample


def test_overlap_never_hides_a_truncated_arm():
    studies = [make_study(f"NCT{i}") for i in range(3)]
    truncated = FetchResult(
        studies=studies, total_matching=50, duplicate_arm_matches=2,
        truncated_reason="fetch_cap_reached",
    )
    assert truncated.is_sample
