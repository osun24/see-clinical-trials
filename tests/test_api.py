"""End-to-end through the HTTP layer, with both upstreams mocked."""

from __future__ import annotations

import json
import pathlib
from unittest.mock import patch

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from app.config import settings
from app.ctgov.client import clear_cache
from app.main import app
from app.models.errors import ServiceError
from app.models.plan import QueryPlan

BASE = settings.ctgov_base_url
FIXTURES = pathlib.Path(__file__).parent / "fixtures"


@pytest.fixture(autouse=True)
def _clear_cache():
    clear_cache()
    yield
    clear_cache()


@pytest.fixture
def client():
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture(scope="session")
def payload() -> dict:
    return json.loads((FIXTURES / "studies_pembrolizumab.json").read_text())


def mock_ctgov(payload: dict) -> None:
    respx.get(f"{BASE}/version").mock(
        return_value=httpx.Response(
            200, json={"apiVersion": "2.0.5", "dataTimestamp": "2026-11-02T10:00:05"}
        )
    )
    respx.get(f"{BASE}/studies").mock(return_value=httpx.Response(200, json=payload))


def make_plan(**kwargs) -> QueryPlan:
    base = dict(
        is_clinical_trials_question=True,
        intent="distribution",
        interpretation="Pembrolizumab trials grouped by phase",
        drug_name="pembrolizumab",
    )
    return QueryPlan(**{**base, **kwargs})


# --- the happy path -------------------------------------------------------

@respx.mock
def test_query_returns_a_renderable_spec(client, payload):
    mock_ctgov(payload)
    with patch("app.pipeline.interpret", return_value=make_plan(group_by="phase")):
        response = client.post("/query", json={"query": "pembrolizumab trials by phase"})

    assert response.status_code == 200
    body = response.json()
    viz, meta = body["visualization"], body["meta"]

    assert viz["type"] == "bar_chart"
    assert viz["title"] and viz["subtitle"]

    # Every channel must name a key present on every row, or a renderer breaks.
    x, y = viz["encoding"]["x"]["field"], viz["encoding"]["y"]["field"]
    assert viz["data"]
    for row in viz["data"]:
        assert x in row and y in row

    assert meta["invariant"]["kind"] == "partition"
    assert sum(r["value"] for r in viz["data"]) == meta["coverage"]["records_analyzed"]
    assert meta["group_by"] == "phase"
    assert meta["citation_policy"]


@respx.mock
def test_subtitle_states_the_sampling_basis(client, payload):
    """The subtitle is the one honesty signal a frontend cannot forget."""
    sampled = {**payload, "totalCount": 3139}
    mock_ctgov(sampled)
    # max_records is set explicitly so the assertion tests sampling disclosure
    # rather than whichever default cap happens to be configured.
    with patch("app.pipeline.interpret", return_value=make_plan(group_by="phase")):
        body = client.post(
            "/query", json={"query": "q", "max_records": 600}
        ).json()

    coverage = body["meta"]["coverage"]
    assert coverage["is_sample"] is True
    assert coverage["counts_are_exact"] is False
    assert coverage["sampling_bias_risk"] == "high"

    subtitle = body["visualization"]["subtitle"]
    assert "3,139" in subtitle
    assert f"{coverage['records_analyzed']:,}" in subtitle
    assert "approximate" in subtitle
    assert body["meta"]["notes"]["coverage_warning"] == subtitle


@respx.mock
def test_every_datum_carries_citations(client, payload):
    mock_ctgov(payload)
    with patch("app.pipeline.interpret", return_value=make_plan(group_by="phase")):
        body = client.post("/query", json={"query": "q"}).json()

    nct_ids = {
        s["protocolSection"]["identificationModule"]["nctId"] for s in payload["studies"]
    }
    for row in body["visualization"]["data"]:
        assert row["citation_total"] >= 1
        assert row["citations"], f"{row['label']} has no citations"
        for citation in row["citations"]:
            assert citation["nct_id"] in nct_ids
            assert citation["field_path"].startswith("protocolSection.")
            assert citation["source_url"].endswith(citation["nct_id"])


@respx.mock
def test_multi_label_dimension_declares_its_invariant(client, payload):
    mock_ctgov(payload)
    with patch("app.pipeline.interpret", return_value=make_plan(group_by="country", intent="ranking")):
        body = client.post("/query", json={"query": "which countries"}).json()

    invariant = body["meta"]["invariant"]
    assert invariant["kind"] == "multi_label"
    assert invariant["assignments_total"] > invariant["records_analyzed"]
    assert "several categories" in body["meta"]["notes"]["counting_rule"]


@respx.mock
def test_chart_override_is_reported_to_the_client(client, payload):
    mock_ctgov(payload)
    plan = make_plan(group_by="lead_sponsor", chart_hint="pie_chart", intent="composition", limit=25)
    with patch("app.pipeline.interpret", return_value=plan):
        body = client.post("/query", json={"query": "who sponsors"}).json()

    assert body["visualization"]["type"] == "bar_chart"
    overrides = body["meta"]["notes"]["overrides"]
    assert overrides and overrides[0]["requested"] == "pie_chart"
    assert overrides[0]["applied"] == "bar_chart"


@respx.mock
def test_zero_results_is_a_200_with_an_explanation(client):
    mock_ctgov({"totalCount": 0, "studies": []})
    with patch("app.pipeline.interpret", return_value=make_plan(group_by="phase", start_year=2024)):
        response = client.post("/query", json={"query": "q"})

    assert response.status_code == 200
    body = response.json()
    assert body["visualization"]["type"] == "none"
    assert body["visualization"]["data"] == []
    assert body["meta"]["visualization_needed"] is False
    assert body["meta"]["answer_text"] == "No trials match those filters."
    assert body["meta"]["suggestions"]


@respx.mock
def test_lookup_returns_no_visualization_instead_of_a_table(client, payload):
    mock_ctgov(payload)
    plan = make_plan(intent="lookup", group_by=None, chart_hint=None)
    with patch("app.pipeline.interpret", return_value=plan):
        response = client.post("/query", json={"query": "show recruiting trials"})

    assert response.status_code == 200
    body = response.json()
    assert body["visualization"]["type"] == "none"
    assert "not a supported visualization" in body["meta"]["answer_text"]
    assert body["meta"]["suggestions"]


@respx.mock
def test_named_subject_sponsor_association_returns_network_graph(client, payload):
    mock_ctgov(payload)
    misclassified = make_plan(
        intent="lookup", group_by=None, sponsor="Sharon Sha", chart_hint=None
    )
    with patch("app.pipeline.interpret", return_value=misclassified):
        response = client.post(
            "/query",
            json={"query": "What sponsors is Sharon Sha associated with?"},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["visualization"]["type"] == "network_graph"
    assert body["visualization"]["title"] == "Sharon Sha — Sponsor Relationships"
    assert body["meta"]["intent"] == "relationship"
    assert body["meta"]["group_by"] == "lead_sponsor"
    assert body["meta"]["series_by"] == "sponsor_type"
    assert body["meta"]["query_interpretation"]["filters_applied"] == {
        "query.term": "Sharon Sha"
    }
    nodes = body["visualization"]["data"]["nodes"]
    edges = body["visualization"]["data"]["edges"]
    root = next(node for node in nodes if node["level"] == 0)
    branches = {node["id"] for node in nodes if node["level"] == 1}
    leaves = {node["id"] for node in nodes if node["level"] == 2}
    assert root["label"] == "Sharon Sha"
    assert branches and leaves
    assert all(
        (edge["source"] == root["id"] and edge["target"] in branches)
        or (edge["source"] in branches and edge["target"] in leaves)
        for edge in edges
    )


# --- error paths ----------------------------------------------------------

@respx.mock
def test_out_of_scope_question_is_rejected(client, payload):
    mock_ctgov(payload)
    plan = QueryPlan(
        is_clinical_trials_question=False, intent="count_total", interpretation="weather"
    )
    with patch("app.pipeline.interpret", return_value=plan):
        response = client.post("/query", json={"query": "weather in Paris?"})

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "out_of_scope"


def test_blank_query_is_a_422(client):
    response = client.post("/query", json={"query": "   "})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_request"


def test_unknown_field_is_rejected(client):
    response = client.post("/query", json={"query": "q", "nonsense": 1})
    assert response.status_code == 422


def test_invalid_enum_is_rejected_with_the_allowed_values(client):
    response = client.post("/query", json={"query": "q", "trial_phase": ["PHASE9"]})
    assert response.status_code == 422
    assert "PHASE1" in json.dumps(response.json())


def test_reversed_years_are_rejected(client):
    response = client.post("/query", json={"query": "q", "start_year": 2024, "end_year": 2020})
    assert response.status_code == 422


@respx.mock
def test_planner_failure_never_reaches_clinicaltrials_gov(client):
    """No fetch should be attempted when interpretation failed."""
    studies = respx.get(f"{BASE}/studies").mock(
        return_value=httpx.Response(200, json={"totalCount": 0, "studies": []})
    )
    error = ServiceError("llm_unavailable", "OpenAI is down.")
    with patch("app.pipeline.interpret", side_effect=error):
        response = client.post("/query", json={"query": "q"})

    assert response.status_code == 503
    assert response.json()["error"]["retryable"] is True
    assert studies.call_count == 0


@respx.mock
def test_missing_api_key_is_a_500_and_fetches_nothing(client, monkeypatch):
    monkeypatch.setattr("app.interpret.planner.settings.openai_api_key", None)
    studies = respx.get(f"{BASE}/studies").mock(
        return_value=httpx.Response(200, json={"totalCount": 0, "studies": []})
    )
    response = client.post("/query", json={"query": "q"})

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "llm_misconfigured"
    assert studies.call_count == 0


# --- supporting routes -----------------------------------------------------

def test_dimensions_route_documents_the_registry(client):
    body = client.get("/dimensions").json()
    ids = {d["id"] for d in body["dimensions"]}
    assert {"phase", "country", "overall_status"} <= ids
    assert body["counting_rules"]["partition"]
    for dimension in body["dimensions"]:
        assert dimension["counting_rule"]
        assert "counts_can_be_exact" in dimension


def test_plan_route_shows_params_without_fetching(client):
    with patch("app.pipeline.interpret", return_value=make_plan(group_by="phase")):
        body = client.post("/plan", json={"query": "pembrolizumab by phase"}).json()

    assert body["ctgov_params"]["query.intr"] == "pembrolizumab"
    assert body["plan"]["group_by"] == "phase"
    assert "NCTId" in body["fields_mask"]


def test_healthz_reports_configuration(client):
    body = client.get("/healthz").json()
    assert body["status"] == "ok"
    assert "openai_configured" in body


def test_openapi_documents_the_response_schema(client):
    schema = client.get("/openapi.json").json()
    assert "/query" in schema["paths"]
    assert "AnswerResponse" in schema["components"]["schemas"]
    assert "Citation" in schema["components"]["schemas"]


def test_demo_page_is_served(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "visualization.type" in response.text


@respx.mock
def test_a_universe_within_the_cap_is_reported_as_a_census(client, payload):
    """The point of the default cap: a specific drug or condition is counted
    completely, not sampled. A census must claim no sampling bias."""
    complete = {k: v for k, v in payload.items() if k != "nextPageToken"}
    complete["totalCount"] = len(complete["studies"])
    mock_ctgov(complete)

    with patch("app.pipeline.interpret", return_value=make_plan(group_by="phase")):
        body = client.post("/query", json={"query": "q"}).json()

    coverage = body["meta"]["coverage"]
    assert coverage["is_sample"] is False
    assert coverage["sampling_method"] == "census"
    assert coverage["sampling_bias_risk"] == "none"
    # Null fields are stripped from the response, so absence is the signal.
    assert coverage.get("truncated_reason") is None
    assert coverage["coverage_ratio"] == 1.0
    assert "all" in body["visualization"]["subtitle"]
    assert body["meta"]["notes"].get("coverage_warning") is None


@respx.mock
def test_max_records_override_is_honoured(client, payload):
    mock_ctgov({**payload, "totalCount": 3139})
    with patch("app.pipeline.interpret", return_value=make_plan(group_by="phase")):
        body = client.post("/query", json={"query": "q", "max_records": 900}).json()

    coverage = body["meta"]["coverage"]
    assert coverage["fetch_cap"] == 900
    assert coverage["records_fetched"] <= 900
    assert coverage["is_sample"] is True


def test_max_records_above_the_hard_cap_is_rejected(client):
    assert client.post("/query", json={"query": "q", "max_records": 99999}).status_code == 422


@respx.mock
def test_network_graph_invariant_describes_the_shape_it_returns(client, payload):
    """A graph has no data[].value, so the flat-array statement would send a
    frontend looking for a field that does not exist."""
    mock_ctgov(payload)
    plan = make_plan(
        intent="relationship", group_by="country", series_by="phase", chart_hint="network_graph"
    )
    with patch("app.pipeline.interpret", return_value=plan):
        body = client.post("/query", json={"query": "country vs phase"}).json()

    viz, meta = body["visualization"], body["meta"]
    assert viz["type"] == "network_graph"

    statement = meta["invariant"]["statement"]
    assert "data.edges[].weight" in statement
    assert "data[].value" not in statement

    # And the stated relationship must actually hold.
    data = viz["data"]
    edge_total = sum(e["weight"] for e in data["edges"])
    omitted = (data.get("omitted") or {}).get("trials", 0)
    analyzed = meta["coverage"]["records_analyzed"]

    if omitted:
        assert "data.omitted.trials" in statement
    if meta["invariant"]["kind"] == "partition":
        assert edge_total + omitted == analyzed
    else:
        assert edge_total + omitted >= analyzed


@respx.mock
def test_network_graph_omission_is_surfaced_in_notes(client, payload):
    mock_ctgov(payload)
    plan = make_plan(
        intent="relationship", group_by="lead_sponsor", series_by="sponsor_type", limit=3
    )
    with patch("app.pipeline.interpret", return_value=plan):
        body = client.post("/query", json={"query": "sponsors vs type"}).json()

    omitted = body["visualization"]["data"].get("omitted")
    assert omitted and omitted["trials"] > 0
    assert any("not drawn on the graph" in n for n in body["meta"]["notes"]["general"])
