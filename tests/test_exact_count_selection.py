"""When exact per-bucket counting is chosen over a sampled or census fetch.

Exact counting was unreachable in practice: `should_stratify` refused whenever
the plan carried a typed `search`, and consolidating onto that search object
made it the path every query takes. It fired in 0 of 267 live gate runs, so a
question like "study types for diabetes trials" sampled 5,000 of 24,588
records when three requests would have counted all of them exactly.
"""

from datetime import date

import pytest

from app.ctgov.params import year_universe
from app.ctgov.stratified import bucket_universe, should_stratify
from app.dimensions.registry import DIMENSIONS
from app.interpret.merge import resolve
from app.models.plan import QueryPlan
from app.models.request import QueryRequest


def plan(group: str, question: str, **search):
    raw = QueryPlan.model_validate({
        "is_clinical_trials_question": True,
        "intent": "distribution",
        "interpretation": question,
        "group_by": group,
        "chart_hint": "bar_chart",
        "search": search or {"query.cond": "melanoma"},
    })
    return resolve(raw, QueryRequest(query=question))


@pytest.mark.parametrize("group", [
    "study_type", "overall_status", "phase_any", "sponsor_type", "results_posted",
])
def test_closed_value_groupings_stratify_on_the_search_path(group):
    assert should_stratify(DIMENSIONS[group], plan(group, "A question."))


@pytest.mark.parametrize("group", [
    "lead_sponsor", "condition", "country", "intervention_type", "enrollment_bucket",
])
def test_open_ended_groupings_still_sample(group):
    assert not should_stratify(DIMENSIONS[group], plan(group, "A question."))


def test_a_filter_on_the_grouped_field_blocks_stratification():
    """A bucket filter replaces `filter.overallStatus` rather than narrowing it.

    Stratifying here would answer for all statuses, discarding the user's own
    recruiting constraint.
    """
    constrained = plan(
        "overall_status", "What is the status of recruiting melanoma trials?",
        **{"query.cond": "melanoma", "filter.overallStatus": "RECRUITING"},
    )
    assert not should_stratify(DIMENSIONS["overall_status"], constrained)


def test_an_advanced_filter_on_another_field_does_not_block_it():
    """`filter.advanced` merges with AND, so it narrows and stays correct."""
    scoped = plan(
        "study_type", "Study types for phase 4 melanoma trials.",
        **{"query.cond": "melanoma", "filter.advanced": "AREA[Phase](PHASE4)"},
    )
    assert should_stratify(DIMENSIONS["study_type"], scoped)


def test_a_second_axis_blocks_stratification():
    raw = QueryPlan.model_validate({
        "is_clinical_trials_question": True, "intent": "distribution",
        "interpretation": "Status by sponsor type.", "group_by": "overall_status",
        "series_by": "sponsor_type", "chart_hint": "grouped_bar",
        "search": {"query.cond": "melanoma"},
    })
    resolved = resolve(raw, QueryRequest(query="Status by sponsor type."))
    assert not should_stratify(DIMENSIONS["overall_status"], resolved)


def test_temporal_universe_comes_from_the_search_period():
    """`resolve` nulls plan years on the search path; the dates hold the period.

    Reading only the plan years substituted a default 15-year window for the
    period the question asked for.
    """
    resolved = plan(
        "start_year", "How have melanoma trial starts changed since 2015?",
        **{"query.cond": "melanoma",
           "start_date": date(2015, 1, 1), "end_date": date(2024, 12, 31)},
    )
    assert resolved.start_year is None and resolved.search.start_date is not None
    assert year_universe(resolved) == [str(y) for y in range(2015, 2025)]
    assert bucket_universe(DIMENSIONS["start_year"], resolved) == year_universe(resolved)


def test_bucket_universes_stay_within_the_request_budget():
    from app.config import settings

    for name, dimension in DIMENSIONS.items():
        if not dimension.is_exact_capable:
            continue
        universe = bucket_universe(dimension, plan("study_type", "A question."))
        assert len(universe) <= settings.exact_max_buckets, name
