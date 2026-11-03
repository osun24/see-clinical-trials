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
    assert viz["title"]

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
def test_coverage_warning_states_the_sampling_basis(client, payload):
    sampled = {**payload, "totalCount": 3139}
    mock_ctgov(sampled)
    with (
        patch("app.pipeline.settings.max_records", 600),
        patch("app.pipeline.interpret", return_value=make_plan(group_by="phase")),
    ):
        body = client.post("/query", json={"query": "q"}).json()

    coverage = body["meta"]["coverage"]
    assert coverage["is_sample"] is True
    assert coverage["counts_are_exact"] is False
    assert coverage["sampling_bias_risk"] == "high"

    warning = body["meta"]["notes"]["coverage_warning"]
    assert "3,139" in warning
    assert f"{coverage['records_analyzed']:,}" in warning
    assert "approximate" in warning


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
    assert body["visualization"]["title"] == "Sharon Sha — Associated Sponsors"
    assert body["meta"]["intent"] == "relationship"
    assert body["meta"]["group_by"] == "lead_sponsor"
    assert body["meta"].get("series_by") is None
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


@respx.mock
def test_named_person_drug_graph_uses_drug_names_and_trial_evidence(client):
    studies = []
    for nct_id, title, official, interventions in (
        (
            "NCT00000001",
            "Trial One",
            "Sharon Sha",
            [
                {"type": "DRUG", "name": "Pembrolizumab"},
                {"type": "BIOLOGICAL", "name": "Cancer vaccine"},
            ],
        ),
        (
            "NCT00000002",
            "Trial Two",
            "Sharon Sha, MD",
            [{"type": "DRUG", "name": "Nivolumab"}],
        ),
    ):
        studies.append(
            {
                "protocolSection": {
                    "identificationModule": {"nctId": nct_id, "briefTitle": title},
                    "contactsLocationsModule": {
                        "overallOfficials": [{"name": official}]
                    },
                    "armsInterventionsModule": {"interventions": interventions},
                }
            }
        )
    mock_ctgov({"totalCount": 2, "studies": studies})
    misclassified = make_plan(
        intent="lookup", group_by=None, drug_name=None, chart_hint=None
    )

    with patch("app.pipeline.interpret", return_value=misclassified):
        response = client.post(
            "/query", json={"query": "What drugs are Sharon Sha associated with?"}
        )

    assert response.status_code == 200
    body = response.json()
    visualization = body["visualization"]
    assert visualization["type"] == "network_graph"
    assert visualization["title"] == "Sharon Sha — Associated Drugs"
    assert any(
        "all 2 trials returned by a full-text search for Sharon Sha" in note
        for note in body["meta"]["notes"]["general"]
    )

    nodes = visualization["data"]["nodes"]
    branches = {node["label"] for node in nodes if node["level"] == 1}
    leaves = {node["label"] for node in nodes if node["level"] == 2}
    assert branches == {"Pembrolizumab", "Nivolumab"}
    assert "Cancer vaccine" not in branches
    assert leaves == {"Trial One", "Trial Two"}
    assert all(edge["citations"] for edge in visualization["data"]["edges"])
    assert "must not be summed across levels" in body["meta"]["invariant"][
        "statement"
    ]
    assert any(
        "OverallOfficialName" in note for note in body["meta"]["notes"]["general"]
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


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("drug_name", "pembrolizumab"),
        ("condition", "melanoma"),
        ("sponsor", "Pfizer"),
        ("country", "Canada"),
        ("trial_phase", ["PHASE2"]),
        ("overall_status", ["RECRUITING"]),
        ("start_year", 2020),
        ("end_year", 2024),
        ("group_by", "phase"),
        ("limit", 5),
        ("max_records", 900),
    ],
)
def test_removed_structured_request_fields_are_rejected(client, field, value):
    response = client.post("/query", json={"query": "q", field: value})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_request"


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

def test_removed_internal_routes_are_not_exposed(client):
    assert client.post("/plan", json={"query": "q"}).status_code == 404
    assert client.get("/dimensions").status_code == 404


def test_healthz_reports_configuration(client):
    body = client.get("/healthz").json()
    assert body["status"] == "ok"
    assert "openai_configured" in body


def test_openapi_documents_the_response_schema(client):
    schema = client.get("/openapi.json").json()
    assert "/query" in schema["paths"]
    assert "/plan" not in schema["paths"]
    assert "/dimensions" not in schema["paths"]
    assert "AnswerResponse" in schema["components"]["schemas"]
    assert "Citation" in schema["components"]["schemas"]
    request_schema = schema["components"]["schemas"]["QueryRequest"]
    assert set(request_schema["properties"]) == {"query"}
    assert request_schema["required"] == ["query"]


def test_demo_page_is_served(client):
    response = client.get("/")
    assert response.status_code == 200
    # The renderer still switches on the response type; assert the behaviour
    # rather than the prose, which is editorial and was removed.
    assert "const DRAW" in response.text
    # Assert structure, not the heading copy, which is editorial.
    assert "<h1>" in response.text


def test_demo_page_is_not_cached(client):
    """The renderer ships inside the page, so a stale copy draws old charts
    from current data with nothing on screen to show the mismatch."""
    response = client.get("/")
    assert "no-store" in response.headers.get("cache-control", "")


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
    assert body["meta"]["notes"].get("coverage_warning") is None


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
