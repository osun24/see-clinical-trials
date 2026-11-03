from datetime import date

import pytest

from app.aggregate.bucketize import from_sample
from app.ctgov.params import build_query
from app.dimensions.registry import DIMENSIONS
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


