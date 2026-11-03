"""Population co-occurrence graphs: "a network of sponsors and drugs for X".

These have no named subject, so the subject-rooted graph adapter cannot express
them and the request previously fell through to a stat card reporting a trial
count — a different question, answered confidently. The planner now states the
shape as a population graph and `resolve` lowers it to the ordinary
two-dimension form, which already has bucketing, citations and a network
encoder behind it.

An edge asserts only that both values are recorded on the same trial.
"""

from __future__ import annotations

import pytest

from app.aggregate import topn
from app.dimensions.registry import ENABLED_DIMENSIONS, get
from app.interpret.merge import resolve
from app.models.buckets import Bucket, BucketSet, Invariant
from app.models.errors import ServiceError
from app.models.plan import QueryPlan
from app.models.request import QueryRequest
from app.viz import encoders

QUESTION = "Show a network of sponsors and drugs for non-small cell lung cancer trials"


def population_plan(entities: list[str], **overrides) -> QueryPlan:
    data = {
        "is_clinical_trials_question": True,
        "intent": "relationship",
        "interpretation": "Sponsors and drugs for NSCLC trials.",
        "chart_hint": "network_graph",
        "search": {"query.cond": "non-small cell lung cancer"},
        "graph": {
            "root_entity": "population",
            "root_value": "",
            "levels": [{"entity": e, "relationship": "co_occurs_in_trial"} for e in entities],
        },
    }
    data.update(overrides)
    return QueryPlan.model_validate(data)


def test_a_population_graph_lowers_to_two_dimensions():
    resolved = resolve(
        population_plan(["lead_sponsor", "intervention_name"]), QueryRequest(query=QUESTION)
    )
    assert resolved.group_by == "lead_sponsor"
    assert resolved.series_by == "intervention_name"
    assert resolved.intent == "relationship"
    assert resolved.chart_hint == "network_graph"
    # Lowered, so the subject-rooted adapter is not involved.
    assert resolved.graph is None
    assert any("co-occurrence" in a for a in resolved.adjustments)
    assert any("same trial" in a for a in resolved.adjustments)


def test_the_entity_order_of_the_question_is_kept():
    resolved = resolve(
        population_plan(["country", "condition_mesh"]),
        QueryRequest(query="Which countries and conditions co-occur in CAR-T trials?"),
    )
    assert (resolved.group_by, resolved.series_by) == ("country", "condition_mesh")


def test_a_trial_level_is_ignored_rather_than_counted_as_an_entity():
    resolved = resolve(
        population_plan(["lead_sponsor", "trial", "intervention_name"]),
        QueryRequest(query=QUESTION),
    )
    assert (resolved.group_by, resolved.series_by) == ("lead_sponsor", "intervention_name")


@pytest.mark.parametrize("entities", [
    ["lead_sponsor"],                              # only one entity
    ["lead_sponsor", "lead_sponsor"],              # not a relationship
    ["trial"],                                     # no entity at all
])
def test_an_unbuildable_population_graph_is_refused_not_substituted(entities):
    with pytest.raises(ServiceError) as caught:
        resolve(population_plan(entities), QueryRequest(query=QUESTION))
    assert caught.value.code == "unsupported_query"


def test_intervention_name_stays_out_of_ordinary_grouping():
    """It is allowed as a graph axis, not as a plain distribution."""
    assert "intervention_name" not in ENABLED_DIMENSIONS


def test_two_multi_label_axes_survive_for_a_graph():
    """The cross-product guard must not strip the very edge being asked for."""
    resolved = resolve(
        population_plan(["country", "condition_mesh"]),
        QueryRequest(query="Which countries and conditions co-occur in CAR-T trials?"),
    )
    assert get("country").semantics == "multi_label"
    assert get("condition_mesh").semantics == "multi_label"
    assert resolved.series_by == "condition_mesh"


# --- encoding -------------------------------------------------------------

def _bucket_set(buckets: list[Bucket]) -> BucketSet:
    return BucketSet(
        dimension="lead_sponsor",
        series_dimension="intervention_name",
        buckets=buckets,
        invariant=Invariant(kind="multi_label", statement="t"),
    )


def test_an_unreported_value_is_not_drawn_as_a_node():
    """"Not Reported" is the absence of a value, not an entity to link to."""
    buckets = [
        Bucket(key="Acme", label="Acme", value=3, series="Pembrolizumab"),
        Bucket(key="Acme", label="Acme", value=7, series="Not Reported",
               series_is_missing=True),
    ]
    _, graph = encoders.encode_network(_bucket_set(buckets))

    labels = {n["label"] for n in graph["nodes"]}
    assert "Not Reported" not in labels
    assert len(graph["edges"]) == 1
    # The trials are reported as undrawn rather than silently dropped.
    assert graph["omitted"]["trials"] == 7
    assert "no entity to place" in graph["omitted"]["reason"]


def test_a_missing_primary_value_is_also_undrawn():
    buckets = [
        Bucket(key="Acme", label="Acme", value=2, series="Pembrolizumab"),
        Bucket(key="__missing__", label="Not Reported", value=5,
               series="Pembrolizumab", is_missing=True),
    ]
    _, graph = encoders.encode_network(_bucket_set(buckets))
    assert len(graph["edges"]) == 1
    assert graph["omitted"]["trials"] == 5


def test_every_drawn_edge_references_real_nodes():
    buckets = [
        Bucket(key="Acme", label="Acme", value=3, series="A"),
        Bucket(key="Globex", label="Globex", value=4, series="B"),
    ]
    _, graph = encoders.encode_network(_bucket_set(buckets))
    ids = {n["id"] for n in graph["nodes"]}
    assert all(e["source"] in ids and e["target"] in ids for e in graph["edges"])


# --- node budget ----------------------------------------------------------

def test_the_series_axis_is_bounded_so_the_graph_is_not_downgraded():
    """`apply_top_n` bounds only the primary axis; both axes are nodes here."""
    buckets = [
        Bucket(key="Acme", label="Acme", value=value, series=f"drug{value}")
        for value in range(1, 21)
    ]
    trimmed, dropped = topn.limit_series(buckets, 5)
    assert dropped == 15
    kept = {b.series for b in trimmed}
    assert kept == {f"drug{v}" for v in range(16, 21)}, kept


def test_series_trimming_keeps_everything_when_it_already_fits():
    buckets = [Bucket(key="Acme", label="Acme", value=1, series=s) for s in "abc"]
    trimmed, dropped = topn.limit_series(buckets, 10)
    assert dropped == 0 and len(trimmed) == 3


def test_trimmed_series_are_dropped_not_pooled_into_other():
    """An edge to a synthetic "Other" node would assert a false co-occurrence."""
    buckets = [
        Bucket(key="Acme", label="Acme", value=v, series=f"d{v}") for v in range(1, 6)
    ]
    trimmed, _ = topn.limit_series(buckets, 2)
    assert all(not b.is_other for b in trimmed)
    assert "Other" not in {b.series for b in trimmed}
