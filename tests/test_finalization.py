"""Release scope, shared execution, diagnostics, and public contract checks."""
import json
from pathlib import Path
import httpx
import pytest
from openai import RateLimitError, BadRequestError, AuthenticationError
from pydantic import ValidationError
from app.analysis.models import AnalysisPlan
from app.interpret.provider_errors import provider_error
from app.interpret.merge import resolve
from app.models.plan import QueryPlan, SearchRequest
from app.models.request import QueryRequest
from app.models.response import AnswerResponse
from app.models.errors import ServiceError
from app.viz.contract import check_response


@pytest.mark.parametrize("op", ["classify", "categorize"])
def test_enrichment_operations_are_not_in_executable_schema(op):
    with pytest.raises(ValidationError):
        AnalysisPlan.model_validate({"steps":[{"op":op,"id":"x","input":"trials","field":"name","instruction":"infer classes"}]})


def test_unsupported_requirements_fail_before_fetch():
    proposal = QueryPlan(is_clinical_trials_question=True, intent="distribution", interpretation="Drug classes",
                         search=SearchRequest(condition="NSCLC"), unsupported_requirements=["drug-to-class mapping"])
    with pytest.raises(ServiceError) as error:
        resolve(proposal,QueryRequest(query="Which drug classes?"))
    assert error.value.code == "unsupported_query"
    assert not error.value.retryable


@pytest.mark.parametrize("exception,code,status,retry", [
    (RateLimitError,"insufficient_quota",503,False),
    (RateLimitError,"rate_limit_exceeded",429,True),
    (BadRequestError,"invalid_schema",500,False),
    (AuthenticationError,"invalid_api_key",500,False)])
def test_provider_diagnostics_are_distinct_and_sanitized(exception,code,status,retry):
    response=httpx.Response(429 if exception is RateLimitError else 400 if exception is BadRequestError else 401,
                            request=httpx.Request("POST","https://api.openai.com/v1/chat/completions"),headers={"x-request-id":"req-test"})
    error=provider_error(exception("Provider error",response=response,body={"code":code,"type":"provider_error","message":"secret request must not escape"}))
    assert error.status_code == status
    assert error.retryable == retry
    assert error.detail["provider_code"] == code
    assert error.detail["request_id"] == "req-test"
    assert "secret" not in json.dumps(error.to_response().model_dump())


def test_presets_use_shared_executor(monkeypatch,studies):
    from app.analysis import presets
    from app.aggregate.bucketize import from_sample
    from app.dimensions.registry import DIMENSIONS
    calls=[]
    original=presets.execute_sync
    def record(plan,*args,**kwargs):
        calls.append(plan)
        return original(plan,*args,**kwargs)
    monkeypatch.setattr(presets,"execute_sync",record)
    result=from_sample(studies,DIMENSIONS["phase"])
    assert [s.op for s in calls[0].steps] == ["memberships","aggregate"]
    assert sum(b.value for b in result.buckets) == len(studies)


@pytest.mark.parametrize("file", sorted((Path(__file__).parents[1]/"docs"/"examples").glob("*.json")))
def test_documented_responses_validate_and_render(file):
    check_response(AnswerResponse.model_validate_json(file.read_text()))


def test_documentation_covers_every_visualization():
    from typing import get_args
    from app.models.viz import VizType
    directory=Path(__file__).parents[1]/"docs"/"examples"
    assert {p.stem for p in directory.glob("*.json")} == set(get_args(VizType))


def test_planner_catalog_tracks_source_and_operation_definitions():
    from app.interpret.prompt import capability_catalog
    from app.analysis.numeric import SOURCES
    catalog=json.loads(capability_catalog())
    assert set(SOURCES) <= set(catalog["root_columns"])
    assert "categorize" not in catalog["operations"]
    assert "memberships" in catalog["operations"]


def test_empty_comparison_search_is_valid_until_arms_are_scoped():
    from app.ctgov.params import build_query
    proposal=QueryPlan(is_clinical_trials_question=True,intent="comparison",interpretation="Compare phase",
                       search=SearchRequest(),group_by="phase",compare_by="sponsor",compare_values=["Pfizer","Merck"])
    resolved=resolve(proposal,QueryRequest(query="Compare Pfizer and Merck phases"))
    assert build_query(resolved).params == {}
