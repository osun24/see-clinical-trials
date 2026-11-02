"""The model proposes a chart type; the resolver disposes, visibly."""

from __future__ import annotations

from app.aggregate.bucketize import from_sample
from app.dimensions.registry import get
from app.models.plan import ResolvedPlan
from app.viz.resolver import resolve
from tests.conftest import make_study


def plan(**kwargs) -> ResolvedPlan:
    base = dict(
        is_clinical_trials_question=True,
        intent="distribution",
        interpretation="test",
        limit=10,
    )
    return ResolvedPlan(**{**base, **kwargs})


def buckets_for(dimension_id: str, studies, limit: int = 10):
    return from_sample(studies, get(dimension_id), limit=limit)


# --- overrides ------------------------------------------------------------

def test_pie_over_too_many_categories_becomes_bar(studies):
    bucket_set = buckets_for("lead_sponsor", studies, limit=25)
    decision = resolve(
        plan(group_by="lead_sponsor", chart_hint="pie_chart", intent="composition"),
        bucket_set,
        records_fetched=len(studies),
    )
    assert decision.type == "bar_chart"
    assert decision.overrides[0]["requested"] == "pie_chart"
    assert decision.overrides[0]["applied"] == "bar_chart"
    assert "categories" in decision.overrides[0]["reason"]


def test_pie_over_multi_label_is_structurally_refused(studies):
    """Slices that do not sum to the whole misrepresent proportion."""
    bucket_set = buckets_for("country", studies, limit=5)
    decision = resolve(
        plan(group_by="country", chart_hint="pie_chart", intent="composition"),
        bucket_set,
        records_fetched=len(studies),
    )
    assert decision.type == "bar_chart"
    assert "several country buckets" in decision.overrides[0]["reason"]


def test_pie_is_allowed_for_a_small_partition():
    studies = [
        make_study(f"NCT{i}", designModule={"studyType": t})
        for i, t in enumerate(["INTERVENTIONAL"] * 8 + ["OBSERVATIONAL"] * 4)
    ]
    decision = resolve(
        plan(group_by="study_type", chart_hint="pie_chart", intent="composition"),
        buckets_for("study_type", studies),
        records_fetched=len(studies),
    )
    assert decision.type == "pie_chart"
    assert decision.overrides == []


def test_time_series_hint_over_categorical_becomes_bar(studies):
    decision = resolve(
        plan(group_by="overall_status", chart_hint="time_series"),
        buckets_for("overall_status", studies),
        records_fetched=len(studies),
    )
    assert decision.type == "bar_chart"
    assert decision.overrides[0]["requested"] == "time_series"


def test_two_time_points_do_not_become_a_trend_line():
    studies = [
        make_study("NCT1", statusModule={"startDateStruct": {"date": "2020-01-01"}}),
        make_study("NCT2", statusModule={"startDateStruct": {"date": "2021-01-01"}}),
    ]
    decision = resolve(
        plan(group_by="start_year", chart_hint="time_series", intent="trend"),
        buckets_for("start_year", studies),
        records_fetched=2,
    )
    assert decision.type == "bar_chart"
    assert "trend" in decision.overrides[0]["reason"]


def test_country_ranking_is_a_bar_chart(studies):
    countries = buckets_for("country", studies, limit=20)
    decision = resolve(
        plan(group_by="country", intent="ranking"),
        countries,
        records_fetched=len(studies),
    )
    assert decision.type == "bar_chart"
    assert decision.orientation == "horizontal"


# --- non-chart paths ------------------------------------------------------

def test_zero_results_is_a_none_not_an_error():
    decision = resolve(plan(group_by="phase"), None, records_fetched=0)
    assert decision.type == "none"
    assert decision.needed is False
    assert "No trials matched" in decision.rationale


def test_out_of_scope_short_circuits():
    decision = resolve(
        plan(is_clinical_trials_question=False, intent="count_total"),
        None,
        records_fetched=10,
    )
    assert decision.type == "none"
    assert decision.needed is False


def test_count_total_is_a_stat_not_a_chart(studies):
    decision = resolve(
        plan(intent="count_total", group_by=None, chart_hint="bar_chart"),
        None,
        records_fetched=len(studies),
    )
    assert decision.type == "stat"
    assert decision.needed is False
    assert decision.overrides[0]["applied"] == "stat"


def test_lookup_has_no_visualization(studies):
    decision = resolve(
        plan(intent="lookup", group_by=None, chart_hint="pie_chart"),
        None,
        records_fetched=len(studies),
    )
    assert decision.type == "none"
    assert decision.needed is False
    assert decision.overrides[0]["applied"] == "none"


def test_many_categories_remain_a_bar_chart():
    studies = [
        make_study(f"NCT{i}", eligibilityModule={"sex": f"CAT{i}"}) for i in range(40)
    ]
    decision = resolve(
        plan(group_by="sex"), buckets_for("sex", studies, limit=25), records_fetched=40
    )
    assert decision.type == "bar_chart"


# --- orientation ----------------------------------------------------------

def test_long_label_dimensions_get_horizontal_bars(studies):
    decision = resolve(
        plan(group_by="lead_sponsor", intent="ranking"),
        buckets_for("lead_sponsor", studies, limit=8),
        records_fetched=len(studies),
    )
    assert decision.orientation == "horizontal"


def test_short_label_dimensions_stay_vertical():
    studies = [
        make_study(f"NCT{i}", designModule={"studyType": t})
        for i, t in enumerate(["INTERVENTIONAL"] * 5 + ["OBSERVATIONAL"] * 3)
    ]
    decision = resolve(
        plan(group_by="study_type"), buckets_for("study_type", studies), records_fetched=8
    )
    assert decision.orientation == "vertical"
