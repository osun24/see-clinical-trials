import pytest
from app.analysis.models import AnalysisPlan
from app.analysis.engine import execute, validate
from app.models.errors import ServiceError
from tests.conftest import make_study


def plan(group="city", measure="count_distinct_trials"):
    return AnalysisPlan.model_validate({"steps": [
        {"op": "expand", "id": "sites", "input": "trials", "collection": "locations"},
        {"op": "filter", "id": "us", "input": "sites", "field": "country", "predicate": "equals", "value": "United States"},
        {"op": "aggregate", "id": "counts", "input": "us", "group_by": [group], "measure": measure},
        {"op": "sort", "id": "ranked", "input": "counts", "field": "value"},
    ]})


@pytest.fixture
def records():
    return [make_study("NCT1", contactsLocationsModule={"locations": [
        {"city": "Springfield", "state": "Illinois", "country": "United States", "facility": "A"},
        {"city": "Springfield", "state": "Illinois", "country": "United States", "facility": "B"},
        {"city": "Madrid", "state": "Madrid", "country": "Spain", "facility": "C"},
    ]}), make_study("NCT2", contactsLocationsModule={"locations": [
        {"city": "Boston", "state": "Massachusetts", "country": "United States", "facility": "D"},
    ]})]


@pytest.mark.asyncio
@pytest.mark.parametrize("group,expected", [("city", {"Springfield", "Boston"}), ("state", {"Illinois", "Massachusetts"}), ("facility", {"A", "B", "D"})])
async def test_composition_without_executor_changes(records, group, expected):
    result = await execute(plan(group), records, "locations")
    assert {b.label for b in result.buckets} == expected
    assert all(b.value == 1 for b in result.buckets)
    assert all(b.citations for b in result.buckets)
    assert all("locations[" in b.citations[0].field_path for b in result.buckets)
    assert result.metadata["filtered_rows"] == 1
    assert "LocationCity" in validate(plan())["fields_mask"]


@pytest.mark.asyncio
async def test_trial_vs_entry_counts_and_replay(records):
    trial = await execute(plan(), records, "cities")
    sites = await execute(plan(measure="count_rows"), records, "sites")
    assert next(b.value for b in trial.buckets if b.label == "Springfield") == 1
    assert next(b.value for b in sites.buckets if b.label == "Springfield") == 2
    replay = await execute(plan(), records, "cities", mappings={})
    assert replay == trial




@pytest.mark.parametrize("change", ["field", "dependency", "limit"])
def test_invalid_plans_rejected(change):
    data = plan().model_dump()
    if change == "field":
        data["steps"][1]["field"] = "secret"
    elif change == "dependency":
        data["steps"][2]["input"] = "trials"
    else:
        data["steps"].append({"op": "limit", "id": "tail", "input": "ranked", "count": 10000, "tail": "drop"})
    with pytest.raises(ServiceError):
        validate(AnalysisPlan.model_validate(data))


@pytest.mark.asyncio
async def test_pipeline_analysis_contract(monkeypatch, records):
    from app import pipeline
    from app.models.plan import QueryPlan, SearchRequest
    from app.models.request import QueryRequest
    from app.ctgov.fetch import FetchResult
    import app.analysis.response as adapter
    async def interpret(_):
        return QueryPlan(is_clinical_trials_question=True, intent="distribution", interpretation="US cities",
                         search=SearchRequest(sponsor="Pfizer"), analysis=plan())
    async def fetch(*args, **kwargs):
        return FetchResult(studies=records, total_matching=2, sampling_method="census")
    class Client:
        async def data_timestamp(self):
            return None
    monkeypatch.setattr(pipeline, "interpret", interpret)
    monkeypatch.setattr(adapter, "fetch_flat", fetch)
    response = await pipeline.answer(QueryRequest(query="Which US cities have Pfizer trials?"), Client())
    assert response.visualization.type == "bar_chart"
    assert response.meta.analysis["trace"][-1]["rows"] == 2
    assert response.meta.unit == "trials"
    assert response.visualization.encoding.y.unit == "trials"
    dry_run = await pipeline.plan_only(QueryRequest(query="Which US cities have Pfizer trials?"))
    assert dry_run["analysis_schema"]["columns"] == ["label", "value"]
    assert not dry_run["would_use_exact_counts"]


@pytest.mark.asyncio
async def test_derive_missing_and_tail_policies(records):
    data = plan().model_dump()
    data["steps"].insert(2, {"op": "derive", "id": "normalized", "input": "us", "field": "city", "output": "normalized_city", "transform": "lower"})
    data["steps"][3]["input"] = "normalized"
    data["steps"][3]["group_by"] = ["normalized_city", "state"]
    data["steps"].append({"op": "limit", "id": "top", "input": "ranked", "count": 1, "tail": "other"})
    result = await execute(AnalysisPlan.model_validate(data), records, "cities")
    assert len(result.buckets) == 2
    assert result.buckets[-1].is_other
    assert result.buckets[-1].value == 1
    assert result.buckets[-1].citations
    assert " / " in result.buckets[0].label


def test_uppercase_us_is_not_first_person():
    from app.interpret.merge import mentions_first_person
    assert not mentions_first_person("Which US cities?")
    assert mentions_first_person("Show us our trials")


def test_planner_schema_uses_supported_union_shape():
    from app.models.plan import QueryPlan
    def walk(value):
        if isinstance(value, dict):
            assert "oneOf" not in value
            assert "discriminator" not in value
            for nested in value.values():
                walk(nested)
        elif isinstance(value, list):
            for nested in value:
                walk(nested)
    walk(QueryPlan.model_json_schema())








def numeric_plan():
    return AnalysisPlan.model_validate({"steps": [
        {"op": "filter", "id": "planned", "input": "trials", "field": "enrollment_type", "predicate": "equals", "value": "ESTIMATED"},
        {"op": "derive", "id": "duration", "input": "planned", "field": "start_date", "other_field": "completion_date", "output": "duration_months", "transform": "months_between"},
        {"op": "relationship", "id": "association", "input": "duration", "x": "enrollment", "y": "duration_months"}]})


def numeric_study(i, enrollment, end, kind="ESTIMATED", start="2024-01-01"):
    return make_study(f"NCT{i}", designModule={"enrollmentInfo": {"count": enrollment, "type": kind}},
                      statusModule={"startDateStruct": {"date": start}, "completionDateStruct": {"date": end, "type": "ESTIMATED"}})


@pytest.mark.asyncio
async def test_numeric_relationship_filters_planned_and_excludes_invalid_pairs():
    records = [numeric_study(1, 10, "2024-02-01"), numeric_study(2, 20, "2024-03"),
               numeric_study(3, 30, "2024-04-01"), numeric_study(4, 40, "2025", start="2024"),
               numeric_study(5, 50, "2023-12-01"), numeric_study(6, 60, "2024-05-01", kind="ACTUAL")]
    result = await execute(numeric_plan(), records, "association")
    assert [(p["x"], p["y"]) for p in result.points] == [(10, 1), (20, 2), (30, 3)]
    assert result.metadata["numeric_summary"]["coefficient"] == pytest.approx(1)
    assert result.metadata["invalid_pairs"] == 2
    assert result.metadata["filtered_rows"] == 1
    assert "enrollmentInfo.count" in result.points[0]["citations"][0]["field_path"]
    assert "completionDateStruct.date" in result.points[0]["citations"][0]["field_path"]
    assert set(validate(numeric_plan())["fields_mask"]) >= {"EnrollmentCount", "EnrollmentType", "StartDate", "CompletionDate"}


@pytest.mark.asyncio
async def test_constant_or_small_numeric_samples_are_explicit():
    result = await execute(numeric_plan(), [numeric_study(i, 10, "2024-02-01") for i in range(3)], "association")
    assert result.metadata["numeric_summary"]["coefficient"] is None
    assert "constant" in result.metadata["numeric_summary"]["undefined_reason"]
    result = await execute(numeric_plan(), [numeric_study(1, 10, "2024-02-01")], "association")
    assert "three" in result.metadata["numeric_summary"]["undefined_reason"]


@pytest.mark.asyncio
async def test_numeric_response_contract(monkeypatch):
    from app import pipeline
    from app.models.plan import QueryPlan, SearchRequest
    from app.models.request import QueryRequest
    from app.ctgov.fetch import FetchResult
    import app.analysis.response as adapter
    async def interpret(_):
        return QueryPlan(is_clinical_trials_question=True, intent="relationship", interpretation="Enrollment vs duration",
                         search=SearchRequest(condition="melanoma"), analysis=numeric_plan())
    async def fetch(*args, **kwargs):
        return FetchResult(studies=[numeric_study(i, i*10, f"2024-0{i+1}-01") for i in range(1,4)], total_matching=3)
    class Client:
        async def data_timestamp(self):
            return None
    monkeypatch.setattr(pipeline, "interpret", interpret)
    monkeypatch.setattr(adapter, "fetch_flat", fetch)
    response = await pipeline.answer(QueryRequest(query="Enrollment vs duration for melanoma"), Client())
    assert response.visualization.type == "scatter_plot"
    assert response.visualization.encoding.x.unit == "participants"
    assert response.visualization.encoding.y.unit == "months"
    assert response.meta.coverage.records_analyzed == 3
    assert "Pearson" in response.visualization.subtitle


def test_relationship_rejects_text_axes():
    data = numeric_plan().model_dump()
    data["steps"][-1]["x"] = "title"
    with pytest.raises(ServiceError, match="numeric"):
        validate(AnalysisPlan.model_validate(data))


@pytest.mark.asyncio
async def test_root_condition_predicate_with_numeric_relationship():
    data = numeric_plan().model_dump()
    data["steps"].insert(0, {"op": "filter", "id": "disease", "input": "trials", "field": "condition", "predicate": "contains", "value": "melanoma"})
    data["steps"][1]["input"] = "disease"
    from app.models.study import NormalizedStudy
    record = numeric_study(1, 10, "2024-02-01").raw
    record["protocolSection"]["conditionsModule"] = {"conditions": ["Metastatic Melanoma", "Cancer"]}
    result = await execute(AnalysisPlan.model_validate(data), [NormalizedStudy.from_api(record)], "association")
    assert len(result.points) == 1
    assert "Condition" in validate(AnalysisPlan.model_validate(data))["fields_mask"]


def temporal_plan():
    return AnalysisPlan.model_validate({"steps": [
        {"op": "filter", "id": "phase4", "input": "trials", "field": "phase", "predicate": "equals", "value": "PHASE4"},
        {"op": "derive", "id": "dated", "input": "phase4", "field": "start_date", "output": "year", "transform": "year"},
        {"op": "expand", "id": "agents", "input": "dated", "collection": "interventions"},
                {"op": "aggregate", "id": "counts", "input": "agents", "group_by": ["year", "name"], "measure": "count_distinct_trials"}]})


@pytest.mark.asyncio
async def test_phase_filter_and_temporal_columns_preserved():
    from app.analysis.temporal import adapt
    records = [make_study(f"NCT{i}", designModule={"phases": [phase]},
                         statusModule={"startDateStruct": {"date": year}},
                         armsInterventionsModule={"interventions": [{"name": "Agent A", "type": "DRUG"}]})
               for i, (year, phase) in enumerate([("2007", "PHASE4"), ("2009", "PHASE4"), ("2008", "PHASE3")])]

    result = await execute(temporal_plan(), records, "trends")
    assert result.metadata["filtered_rows"] == 1
    assert "Phase" in result.metadata["schema"]["fields_mask"]
    assert result.metadata["schema"]["group_types"] == {"year": "year", "name": "string"}
    year, series, buckets, meta = adapt(result, temporal_plan().steps[-1])
    assert (year, series) == ("year", "name")
    assert [(b.key, b.series, b.value) for b in buckets] == [("2007", "Agent A", 1), ("2008", "Agent A", 0), ("2009", "Agent A", 1)]
    assert "startDateStruct.date" in buckets[0].citations[0].field_path


@pytest.mark.asyncio
async def test_temporal_pipeline_renders_series(monkeypatch):
    from app import pipeline
    from app.models.plan import QueryPlan, SearchRequest
    from app.models.request import QueryRequest
    from app.ctgov.fetch import FetchResult
    import app.analysis.response as adapter
    import app.analysis.engine as engine
    async def interpret(_):
        return QueryPlan(is_clinical_trials_question=True, intent="trend", interpretation="Drug classes over time",
                         search=SearchRequest(condition="NSCLC"), analysis=temporal_plan())
    async def fetch(*args, **kwargs):
        return FetchResult(total_matching=2, studies=[make_study(f"NCT{i}", designModule={"phases": ["PHASE4"]},
            statusModule={"startDateStruct": {"date": year}}, armsInterventionsModule={"interventions": [{"name": "Agent A"}]})
            for i, year in enumerate(["2007", "2009"])])
    class Client:
        async def data_timestamp(self):
            return None
    monkeypatch.setattr(pipeline, "interpret", interpret)
    monkeypatch.setattr(adapter, "fetch_flat", fetch)
    response = await pipeline.answer(QueryRequest(query="Classes over time"), Client())
    assert response.visualization.type == "time_series"
    assert response.visualization.encoding.series.field == "series"
    assert len(response.visualization.data) == 3
    assert "study start year" in response.visualization.subtitle


@pytest.mark.asyncio
async def test_validation_feedback_repairs_once(monkeypatch):
    from app import pipeline
    from app.models.plan import QueryPlan, SearchRequest
    from app.models.request import QueryRequest
    raw = plan().model_dump()
    raw["steps"][1]["input"] = "trials"
    invalid = QueryPlan(is_clinical_trials_question=True, intent="distribution", interpretation="Cities",
                        search=SearchRequest(condition="cancer"), analysis=AnalysisPlan.model_validate(raw))
    calls = []
    async def interpret(request, **kwargs):
        calls.append(kwargs)
        return invalid if not kwargs else invalid.model_copy(update={"analysis": plan()})
    monkeypatch.setattr(pipeline, "interpret", interpret)
    # Returns the raw plan too, so an outer retry can show the planner its
    # previous attempt.
    result, raw = await pipeline._interpret_resolved(QueryRequest(query="Cities for cancer trials"))
    assert len(calls) == 2
    assert raw is not None
    assert "input must be" in calls[1]["feedback"]
    assert result.analysis == plan()
    assert "revised its plan once" in result.adjustments[-1]


@pytest.mark.asyncio
async def test_invalid_plan_repair_is_bounded(monkeypatch):
    from app import pipeline
    from app.models.plan import QueryPlan, SearchRequest
    from app.models.request import QueryRequest
    data = plan().model_dump()
    data["steps"][1]["field"] = "imaginary"
    invalid = QueryPlan(is_clinical_trials_question=True, intent="distribution", interpretation="Cities",
                        search=SearchRequest(condition="cancer"), analysis=AnalysisPlan.model_validate(data))
    calls = []
    async def interpret(request, **kwargs):
        calls.append(kwargs)
        return invalid
    monkeypatch.setattr(pipeline, "interpret", interpret)
    with pytest.raises(ServiceError, match="imaginary"):
        await pipeline._interpret_resolved(QueryRequest(query="Cities"))
    assert len(calls) == 2
