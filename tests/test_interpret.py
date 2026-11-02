"""Plan resolution: clamp and validate model output deterministically."""

from __future__ import annotations

import pytest
from openai import APITimeoutError

from app.interpret.merge import resolve
from app.interpret.planner import interpret
from app.models.errors import ServiceError
from app.models.plan import GraphLevel, GraphPlan, QueryPlan
from app.models.request import QueryRequest


def make_plan(**kwargs) -> QueryPlan:
    base = dict(
        is_clinical_trials_question=True, intent="distribution", interpretation="test"
    )
    return QueryPlan(**{**base, **kwargs})


# --- clamping (strict mode does not enforce numeric bounds) ----------------

def test_limit_is_clamped_and_reported():
    resolved = resolve(make_plan(limit=400, group_by="phase"), QueryRequest(query="q"))
    assert resolved.limit == 25
    assert any("Clamped limit" in a for a in resolved.adjustments)


def test_reversed_years_are_swapped():
    resolved = resolve(
        make_plan(start_year=2024, end_year=2020, group_by="phase"), QueryRequest(query="q")
    )
    assert (resolved.start_year, resolved.end_year) == (2020, 2024)
    assert any("Swapped" in a for a in resolved.adjustments)


def test_unknown_enum_values_are_dropped():
    resolved = resolve(
        make_plan(trial_phase=["PHASE2", "BOGUS"], overall_status=["RECRUITING", "NOPE"]),
        QueryRequest(query="q"),
    )
    assert resolved.trial_phase == ["PHASE2"]
    assert resolved.overall_status == ["RECRUITING"]
    assert sum("Dropped unrecognised" in a for a in resolved.adjustments) == 2


# --- combination validation -----------------------------------------------

def test_two_multi_label_axes_drop_the_series():
    """One trial in 9 countries and 6 conditions would land in 54 cells."""
    resolved = resolve(
        make_plan(group_by="country", series_by="condition"), QueryRequest(query="q")
    )
    assert resolved.series_by is None
    assert any("many cells" in a for a in resolved.adjustments)


def test_series_matching_group_is_dropped():
    resolved = resolve(
        make_plan(group_by="phase", series_by="phase"), QueryRequest(query="q")
    )
    assert resolved.series_by is None
    assert any("matched group_by" in a for a in resolved.adjustments)


def test_one_multi_label_axis_is_allowed():
    resolved = resolve(
        make_plan(group_by="country", series_by="phase"), QueryRequest(query="q")
    )
    assert resolved.series_by == "phase"


def test_named_subject_sponsor_association_becomes_a_network_relationship():
    """Association wording must not degrade into an unsupported trial lookup."""
    resolved = resolve(
        make_plan(intent="lookup", sponsor="Sharon Sha"),
        QueryRequest(query="What sponsors is Sharon Sha associated with?"),
    )

    assert resolved.intent == "relationship"
    assert resolved.group_by == "lead_sponsor"
    assert resolved.series_by is None
    assert resolved.chart_hint == "network_graph"
    assert resolved.graph is not None
    assert resolved.graph.root_entity == "person"
    assert resolved.graph.root_value == "Sharon Sha"
    assert [level.entity for level in resolved.graph.levels] == ["lead_sponsor", "trial"]
    assert resolved.search_terms == "Sharon Sha"
    assert resolved.sponsor is None
    assert any("evidence-backed" in a for a in resolved.adjustments)


def test_named_subject_drug_association_uses_actual_intervention_names():
    resolved = resolve(
        make_plan(intent="lookup"),
        QueryRequest(query="What drugs are Sharon Sha associated with?"),
    )

    assert resolved.group_by == "intervention_name"
    assert resolved.graph is not None
    assert [level.entity for level in resolved.graph.levels] == [
        "intervention_name",
        "trial",
    ]


def test_valid_model_proposed_graph_is_constrained_and_accepted():
    resolved = resolve(
        make_plan(
            graph=GraphPlan(
                root_entity="person",
                root_value="Sharon Sha",
                levels=[
                    GraphLevel(entity="intervention_name"),
                    GraphLevel(entity="trial"),
                ],
                node_limit=999,
            )
        ),
        QueryRequest(query="Show relationships between Sharon Sha and trial drugs"),
    )

    assert resolved.graph is not None
    assert resolved.group_by == "intervention_name"
    assert resolved.graph.node_limit == 60
    assert resolved.intent == "relationship"


def test_model_cannot_invent_a_graph_root_absent_from_the_question():
    resolved = resolve(
        make_plan(
            graph=GraphPlan(
                root_entity="person",
                root_value="Someone Else",
                levels=[GraphLevel(entity="lead_sponsor")],
            )
        ),
        QueryRequest(query="Show relationships for Sharon Sha"),
    )

    assert resolved.graph is None
    assert any("root was not present" in item for item in resolved.adjustments)


def test_unsupported_model_graph_path_is_dropped():
    resolved = resolve(
        make_plan(
            graph=GraphPlan(
                root_entity="person",
                root_value="Sharon Sha",
                levels=[GraphLevel(entity="trial")],
            )
        ),
        QueryRequest(query="Show Sharon Sha trial relationships"),
    )

    assert resolved.graph is None
    assert any("unsupported graph path" in item for item in resolved.adjustments)


def test_a_filterless_plan_searches_only_the_meaningful_words():
    """Sending the whole question as a full-text term matches nothing.

    ClinicalTrials.gov matches the phrase literally, so a sentence like
    "Compare the phase distribution of our pipeline against Merck's" returned
    zero trials. Only words that could name a drug, disease, or company are kept.
    """
    resolved = resolve(
        make_plan(group_by="phase"),
        QueryRequest(query="Top academic centers running immunotherapy trials."),
    )
    assert resolved.search_terms == "academic immunotherapy"
    assert "top" not in (resolved.search_terms or "")
    assert "trials" not in (resolved.search_terms or "")
    assert any("free text" in a for a in resolved.adjustments)


def test_first_person_questions_do_not_get_an_invented_sponsor():
    """The service cannot know who "our" is, so it must not pretend to.

    The model had been filling sponsor with "our organisation", which matched
    three trials by literal text and looked like a real answer.
    """
    resolved = resolve(
        make_plan(group_by="overall_status", sponsor="our organisation", condition="oncology"),
        QueryRequest(query="What is the status breakdown of our oncology portfolio?"),
    )
    assert resolved.sponsor is None
    assert any("cannot identify" in a for a in resolved.adjustments)
    assert any("your own organisation" in a for a in resolved.assumptions)


# --- planner failure modes -------------------------------------------------

async def test_missing_api_key_fails_loudly(monkeypatch):
    """There is no fallback planner by design, so this must not degrade."""
    monkeypatch.setattr("app.interpret.planner.settings.openai_api_key", None)
    with pytest.raises(ServiceError) as excinfo:
        await interpret(QueryRequest(query="q"))
    assert excinfo.value.code == "llm_misconfigured"
    assert excinfo.value.status_code == 500


async def test_openai_timeout_maps_to_retryable_503(monkeypatch):
    monkeypatch.setattr("app.interpret.planner.settings.openai_api_key", "sk-test")

    class _Completions:
        async def parse(self, **_):
            raise APITimeoutError(request=None)  # type: ignore[arg-type]

    class _Chat:
        completions = _Completions()

    class _Client:
        chat = _Chat()

    monkeypatch.setattr("app.interpret.planner.AsyncOpenAI", lambda **_: _Client())

    with pytest.raises(ServiceError) as excinfo:
        await interpret(QueryRequest(query="q"))
    assert excinfo.value.code == "llm_unavailable"
    assert excinfo.value.status_code == 503
    assert excinfo.value.retryable is True


async def test_refusal_is_not_treated_as_a_plan(monkeypatch):
    """A refusal must never be read as an empty plan and answered anyway."""
    monkeypatch.setattr("app.interpret.planner.settings.openai_api_key", "sk-test")

    class _Message:
        refusal = "I cannot help with that."
        parsed = None

    class _Choice:
        message = _Message()

    class _Completion:
        choices = [_Choice()]

    class _Completions:
        async def parse(self, **_):
            return _Completion()

    class _Chat:
        completions = _Completions()

    class _Client:
        chat = _Chat()

    monkeypatch.setattr("app.interpret.planner.AsyncOpenAI", lambda **_: _Client())

    with pytest.raises(ServiceError) as excinfo:
        await interpret(QueryRequest(query="q"))
    assert excinfo.value.code == "query_not_interpretable"
