from datetime import date

import pytest

from app.aggregate.bucketize import from_sample
from app.ctgov.params import build_query
from app.dimensions.registry import DIMENSIONS
from app.interpret.categories import CategoryDecision, validate_decisions
from app.interpret.merge import resolve
from app.models.errors import ServiceError
from app.models.plan import QueryPlan, SearchRequest
from app.models.request import QueryRequest
from tests.conftest import make_study


def test_search_preserves_precise_dates_and_constraint():
    plan = QueryPlan(is_clinical_trials_question=True, intent="distribution", interpretation="Cancer trials", group_by="condition",
                     search=SearchRequest(**{"query.spons": "Pfizer", "query.cond": "cancer", "start_date": "2025-11-02", "end_date": "2026-11-02"}))
    resolved = resolve(plan, QueryRequest(query="What cancers has Pfizer launched trials for in the past year?"))
    params = build_query(resolved).params
    assert params == {"query.spons": "Pfizer", "query.cond": "cancer", "filter.advanced": "AREA[StartDate]RANGE[2025-11-02,2026-11-02]"}
    assert resolved.condition == "cancer"


def test_selection_filters_memberships_before_topn_and_keeps_evidence():
    studies = [make_study("NCT00000001", conditionsModule={"conditions": ["Cancer", "Obesity", "Cancer"]}),
               make_study("NCT00000002", conditionsModule={"conditions": ["Obesity"]}),
               make_study("NCT00000003")]
    dimension = DIMENSIONS["condition"]
    from app.aggregate.keys import bucket_keys
    key = next(k for k, raw in bucket_keys(studies[0].raw, dimension) if raw == "Cancer")
    result = from_sample(studies, dimension, approved_keys={key})
    assert len(result.buckets) == 1
    assert result.buckets[0].value == 1
    assert result.buckets[0].citations[0].nct_id == "NCT00000001"
    assert result.records_analyzed == 1
    assert result.exclusions.no_selected_category == 1
    assert result.exclusions.missing_dimension_value == 1
    assert result.exclusions.memberships_removed == 2


@pytest.mark.parametrize("decisions", [[], [CategoryDecision(key="invented", decision="include", reason="x")],
    [CategoryDecision(key="a", decision="uncertain", reason="x")],
    [CategoryDecision(key="a", decision="include", reason="x")] * 2])
def test_invalid_selection_is_not_silently_accepted(decisions):
    with pytest.raises(ServiceError):
        validate_decisions({"a"}, decisions)


@pytest.mark.asyncio
async def test_pipeline_selection_and_empty_result(monkeypatch):
    from app import pipeline
    from app.ctgov.fetch import FetchResult
    from app.interpret.categories import CategorySelection
    study = make_study(conditionsModule={"conditions": ["Obesity"]})
    plan = QueryPlan(is_clinical_trials_question=True, intent="distribution", interpretation="Cancer conditions",
                     group_by="condition", category_selection="Only cancers", search=SearchRequest(condition="cancer"))
    async def interpret(_):
        return plan
    async def gather(*args):
        return FetchResult(studies=[study], total_matching=1, sampling_method="census"), None, None
    async def select(question, instruction, terms):
        return CategorySelection(instruction=instruction, decisions=[CategoryDecision(key=k, decision="exclude", reason="Not cancer") for k in terms])
    class Client:
        async def data_timestamp(self):
            return None
    monkeypatch.setattr(pipeline, "interpret", interpret)
    monkeypatch.setattr(pipeline, "_gather_data", gather)
    monkeypatch.setattr(pipeline, "select_categories", select)
    response = await pipeline.answer(QueryRequest(query="Which cancers?"), Client())
    assert response.visualization.type == "none"
    assert response.meta.coverage.excluded.no_selected_category == 1
    assert response.meta.category_selection.decisions[0].decision == "exclude"
    assert "qualifying" in response.meta.answer_text


@pytest.mark.parametrize("group,series", [("phase", None), ("phase", "condition_mesh"), ("condition_mesh", "phase")])
def test_cancers_by_phase_retains_both_dimensions(group, series):
    plan = QueryPlan(is_clinical_trials_question=True, intent="distribution", interpretation="Cancers by phase",
                     group_by=group, series_by=series, category_selection="Only cancer types")
    result = resolve(plan, QueryRequest(query="What cancers has Pfizer launched trials for in the past year, by phase?"))
    assert result.group_by == "condition_mesh"
    assert result.series_by == "phase"


def test_selected_conditions_are_split_by_phase():
    from app.aggregate.keys import bucket_keys
    dimension = DIMENSIONS["condition"]
    studies = [make_study(conditionsModule={"conditions": ["Cancer", "Obesity"]}, designModule={"phases": ["PHASE2"]})]
    key = next(k for k, raw in bucket_keys(studies[0].raw, dimension) if raw == "Cancer")
    result = from_sample(studies, dimension, series_dimension=DIMENSIONS["phase"], approved_keys={key})
    assert len(result.buckets) == 1
    assert result.buckets[0].label == "Cancer"
    assert result.buckets[0].series == "Phase 2"
    assert result.buckets[0].value == 1


def test_direct_search_keeps_descriptive_phase_and_existing_expression():
    plan = QueryPlan(is_clinical_trials_question=True, intent="distribution", interpretation="Phase four cancer trials",
                     group_by="country", trial_phase=["PHASE4"],
                     search=SearchRequest(sponsor="Pfizer", condition="cancer", advanced="AREA[StudyType](INTERVENTIONAL)",
                                          start_date=date(2025, 1, 1), end_date=date(2026, 1, 1)))
    result = resolve(plan, QueryRequest(query="Pfizer phase 4 cancer trials by country"))
    params = build_query(result).params
    assert "AREA[Phase](PHASE4)" in params["filter.advanced"]
    assert "AREA[StudyType](INTERVENTIONAL)" in params["filter.advanced"]
    assert "AREA[StartDate]RANGE[2025-01-01,2026-01-01]" in params["filter.advanced"]
    assert params["query.spons"] == "Pfizer"


def test_city_question_does_not_silently_become_country_chart():
    plan = QueryPlan(is_clinical_trials_question=True, intent="distribution", interpretation="US cities",
                     group_by="country", search=SearchRequest(location="United States"))
    with pytest.raises(ServiceError, match="City-level grouping"):
        resolve(plan, QueryRequest(query="Which U.S. cities does Pfizer have clinical trial sites for phase 4 cancer trials?"))
