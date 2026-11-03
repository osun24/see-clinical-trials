"""Association-question handling.

The release evaluation showed two defects here: the deterministic backstop
matched none of the real "<entity> and trials is <person> associated with"
phrasings, and when a graph path was dropped the rest of the plan still ran, so
an unanswerable association question returned a confident sponsor count instead
of a limitation.
"""

import pytest

from app.interpret.merge import entity_association, resolve
from app.models.errors import ServiceError
from app.models.plan import QueryPlan
from app.models.request import QueryRequest


#: The graph questions exactly as the release evaluation asks them.
RELEASE_QUESTIONS = [
    ("Which organizations and trials is Sharon Sha associated with?", "lead_sponsor", "Sharon Sha"),
    ("Which organizations and trials is Crystal Mackall associated with?", "lead_sponsor", "Crystal Mackall"),
    ("What drugs and trials is Sharon Sha associated with?", "intervention_name", "Sharon Sha"),
    ("What countries and trials is Crystal Mackall associated with?", "country", "Crystal Mackall"),
    ("What conditions and trials is Sharon Sha associated with?", "condition_mesh", "Sharon Sha"),
]


@pytest.mark.parametrize("question,target,subject", RELEASE_QUESTIONS)
def test_release_phrasings_resolve_deterministically(question, target, subject):
    assert entity_association(question) == (target, subject)


@pytest.mark.parametrize("question,target", [
    ("What drugs is Sharon Sha associated with?", "intervention_name"),
    ("Which medications and studies is Sharon Sha associated with?", "intervention_name"),
    ("Which companies is Sharon Sha affiliated with?", "lead_sponsor"),
    ("Which institutions and trials is Sharon Sha connected to?", "lead_sponsor"),
    ("Which diseases is Sharon Sha linked to?", "condition_mesh"),
    ("Which countries and trials are Sharon Sha associated with?", "country"),
])
def test_supported_entity_nouns_and_verbs(question, target):
    found = entity_association(question)
    assert found is not None and found[0] == target


@pytest.mark.parametrize("question", [
    "How many trials are recruiting for lung cancer?",
    "Which lead sponsors run the most Alzheimer's trials?",
    # A numeric relationship says "associated with" too, and must keep its own
    # execution path rather than being read as a graph.
    "Is planned enrollment size associated with trial duration for melanoma trials?",
    "Is enrollment associated with trial duration for diabetes trials?",
])
def test_non_association_questions_are_untouched(question):
    assert entity_association(question) is None


def _plan(**overrides) -> QueryPlan:
    data = {
        "is_clinical_trials_question": True,
        "intent": "distribution",
        "interpretation": "A question.",
        "group_by": "lead_sponsor",
        "chart_hint": "bar_chart",
    }
    data.update(overrides)
    return QueryPlan.model_validate(data)


def test_unsupported_association_is_refused_not_substituted():
    """The exact live failure: a dropped graph fell through to a sponsor count."""
    question = "What widgets and trials is Sharon Sha associated with?"
    with pytest.raises(ServiceError) as caught:
        resolve(_plan(), QueryRequest(query=question))
    assert caught.value.code == "unsupported_query"
    assert caught.value.detail["requirements"]
    assert caught.value.detail["alternative"]


def test_graph_with_an_unsupported_path_is_refused():
    # An entity noun the deterministic backstop does not cover, so the model's
    # own graph is the only candidate and its path is not supported.
    question = "What widgets and trials is Sharon Sha associated with?"
    graph = {
        "root_entity": "person",
        "root_value": "Sharon Sha",
        "levels": [{"entity": "trial", "relationship": "co_occurs_in_trial"}],
    }
    with pytest.raises(ServiceError) as caught:
        resolve(_plan(graph=graph), QueryRequest(query=question))
    assert caught.value.code == "unsupported_query"


def test_graph_root_absent_from_the_question_is_refused():
    question = "What widgets and trials is Sharon Sha associated with?"
    graph = {
        "root_entity": "person",
        "root_value": "Jane Fabricated",
        "levels": [{"entity": "intervention_name", "relationship": "co_occurs_in_trial"}],
    }
    with pytest.raises(ServiceError) as caught:
        resolve(_plan(graph=graph), QueryRequest(query=question))
    assert caught.value.code == "unsupported_query"
    assert "Jane Fabricated" in caught.value.detail["requirements"][0]


def test_the_backstop_overrides_a_models_wrong_graph_for_a_covered_phrasing():
    """A covered phrasing is repaired deterministically rather than refused."""
    graph = {"root_entity": "person", "root_value": "Sharon Sha",
             "levels": [{"entity": "trial", "relationship": "co_occurs_in_trial"}]}
    plan = resolve(_plan(graph=graph),
                   QueryRequest(query="What drugs and trials is Sharon Sha associated with?"))
    assert [level.entity for level in plan.graph.levels] == ["intervention_name", "trial"]


def test_a_supported_association_still_resolves_to_a_graph():
    question = "What drugs and trials is Sharon Sha associated with?"
    plan = resolve(_plan(), QueryRequest(query=question))
    assert plan.graph is not None
    assert plan.graph.root_value == "Sharon Sha"
    assert plan.graph.root_entity == "person"
    assert [level.entity for level in plan.graph.levels] == ["intervention_name", "trial"]
    assert plan.intent == "relationship"
    assert plan.chart_hint == "network_graph"


def test_a_dropped_graph_on_a_non_association_question_still_answers():
    """Refusing is scoped to association questions, not to every dropped graph."""
    graph = {"root_entity": "person", "root_value": "Nobody",
             "levels": [{"entity": "trial", "relationship": "co_occurs_in_trial"}]}
    plan = resolve(_plan(graph=graph, condition="melanoma"),
                   QueryRequest(query="What is the phase distribution of melanoma trials?"))
    assert plan.graph is None
    assert any("graph" in a for a in plan.adjustments)
