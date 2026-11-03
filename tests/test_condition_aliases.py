"""Condition abbreviation expansion.

The planner spelled the same NSCLC question two ways across evaluation runs,
which moved both recall and the constraint the response reports. The expansion
is a retrieval alias: it changes which records are searched, never what a record
is said to contain, and it is disclosed in the response.
"""

import pytest

from app.ctgov.params import build_query
from app.interpret.merge import expand_condition, resolve
from app.models.plan import QueryPlan
from app.models.request import QueryRequest


@pytest.mark.parametrize("given,expected_substring", [
    ("NSCLC", "non-small cell lung cancer"),
    ("nsclc", "non-small cell lung cancer"),
    (" NSCLC ", "non-small cell lung cancer"),
    ("SCLC", "small cell lung cancer"),
    ("GBM", "glioblastoma"),
    ("TNBC", "triple negative breast cancer"),
])
def test_abbreviations_expand_and_keep_the_abbreviation(given, expected_substring):
    expanded, disclosure = expand_condition(given)
    assert expected_substring in expanded
    assert given.strip().upper() in expanded.upper()
    assert " OR " in expanded
    assert disclosure and "retrieval only" in disclosure


@pytest.mark.parametrize("given", [
    "melanoma",
    "non-small cell lung cancer",
    "glioblastoma",
    "Alzheimer",
    None,
    "",
])
def test_unabbreviated_conditions_are_left_alone(given):
    expanded, disclosure = expand_condition(given)
    assert expanded == given
    assert disclosure is None


def _plan(**overrides) -> QueryPlan:
    data = {
        "is_clinical_trials_question": True,
        "intent": "distribution",
        "interpretation": "Intervention names in phase 4 NSCLC trials.",
        "group_by": "intervention_type",
        "chart_hint": "bar_chart",
        "condition": "NSCLC",
    }
    data.update(overrides)
    return QueryPlan.model_validate(data)


def test_the_executed_query_carries_the_expansion_and_discloses_it():
    plan = resolve(_plan(), QueryRequest(query="What intervention names are recorded in phase 4 NSCLC trials?"))
    condition = build_query(plan).params["query.cond"]
    assert "lung" in condition.lower()
    assert "NSCLC" in condition
    assert any("NSCLC" in a for a in plan.assumptions)
    assert any("NSCLC" in a for a in plan.adjustments)


def test_expansion_also_applies_on_the_direct_search_path():
    plan = resolve(
        _plan(search={"query.cond": "NSCLC", "query.intr": None}),
        QueryRequest(query="Which phase 4 NSCLC trials are recruiting?"),
    )
    assert "lung" in build_query(plan).params["query.cond"].lower()


def test_an_unabbreviated_question_adds_no_assumption():
    plan = resolve(
        _plan(condition="melanoma"),
        QueryRequest(query="What intervention names are recorded in melanoma trials?"),
    )
    assert build_query(plan).params["query.cond"] == "melanoma"
    assert not any("retrieval only" in a for a in plan.assumptions)
