"""Regressions for the network_graph path.

Four bugs lived here at once: the demo drew no graph, the invariant promised a
field the data has no equivalent of, the collapsed "Other" bucket vanished
without trace, and top-N ranked individual cells instead of the dimension the
caller asked to rank.
"""

from __future__ import annotations

from app.aggregate.bucketize import from_sample
from app.dimensions.registry import get
from app.models.plan import GraphLevel, GraphPlan
from app.viz.encoders import encode_network, encode_semantic_graph
from tests.conftest import make_study


def _sponsor_study(nct_id: str, name: str, sponsor_class: str):
    return make_study(
        nct_id,
        sponsorCollaboratorsModule={"leadSponsor": {"name": name, "class": sponsor_class}},
    )


# --- top-N must rank the dimension, not the cells -------------------------

def test_top_n_keeps_n_distinct_keys_not_n_cells(studies):
    """With a series dimension, a key spans several buckets.

    Ranking cells directly kept "the biggest cells" — which was five bars of
    one country — instead of the five biggest countries.
    """
    limit = 5
    result = from_sample(
        studies, get("country"), series_dimension=get("phase"), limit=limit
    )
    kept = {b.key for b in result.buckets if not b.is_other and not b.is_missing}
    assert len(kept) == limit


def test_truncation_counts_distinct_keys_not_cells(studies):
    grouped = from_sample(
        studies, get("country"), series_dimension=get("phase"), limit=5
    )
    flat = from_sample(studies, get("country"), limit=5)
    # The number of distinct countries does not change just because a second
    # dimension was added.
    assert grouped.truncation is not None and flat.truncation is not None
    assert grouped.truncation.total_distinct == flat.truncation.total_distinct


def test_top_n_still_accounts_for_every_assignment(studies):
    result = from_sample(
        studies, get("country"), series_dimension=get("phase"), limit=5
    )
    assert sum(b.value for b in result.buckets) == result.invariant.assignments_total


# --- the graph must account for every analyzed trial ----------------------

def test_collapsed_other_is_reported_not_dropped():
    """An 'Other' bucket has no series value, so it has no edge to sit on.

    Dropping it silently made the edge weights fail to reconcile with
    records_analyzed, with nothing in the response to explain the shortfall.
    """
    studies = (
        [_sponsor_study(f"NCT1{i}", "BigCo", "INDUSTRY") for i in range(20)]
        + [_sponsor_study(f"NCT2{i}", f"Tiny{i}", "OTHER") for i in range(30)]
    )
    result = from_sample(
        studies, get("lead_sponsor"), series_dimension=get("sponsor_type"), limit=3
    )
    _, graph = encode_network(result)

    edge_total = sum(e["weight"] for e in graph["edges"])
    omitted = graph.get("omitted", {}).get("trials", 0)

    assert omitted > 0, "the collapsed tail must be reported"
    assert edge_total + omitted == result.records_analyzed
    assert graph["omitted"]["distinct_values_collapsed"] > 0
    assert "Other" in graph["omitted"]["reason"]


def test_graph_without_truncation_has_no_omission_record():
    studies = [
        _sponsor_study("NCT1", "BigCo", "INDUSTRY"),
        _sponsor_study("NCT2", "Uni", "OTHER"),
    ]
    result = from_sample(
        studies, get("lead_sponsor"), series_dimension=get("sponsor_type"), limit=10
    )
    _, graph = encode_network(result)
    assert "omitted" not in graph
    assert sum(e["weight"] for e in graph["edges"]) == result.records_analyzed


def test_nodes_and_edges_reference_each_other_consistently(studies):
    result = from_sample(
        studies, get("country"), series_dimension=get("phase"), limit=6
    )
    encoding, graph = encode_network(result)

    ids = {n["id"] for n in graph["nodes"]}
    assert ids, "a graph needs nodes"
    for edge in graph["edges"]:
        # A renderer resolves endpoints by id; a dangling reference draws nothing.
        assert edge["source"] in ids
        assert edge["target"] in ids

    # Every field the encoding names must exist on every node and edge.
    for node in graph["nodes"]:
        for key in encoding.node.values():
            assert key in node
    for edge in graph["edges"]:
        for key in encoding.edge.values():
            assert key in edge


def test_node_size_is_the_sum_of_its_edges(studies):
    result = from_sample(
        studies, get("country"), series_dimension=get("phase"), limit=6
    )
    _, graph = encode_network(result)
    sizes = {n["id"]: n["size"] for n in graph["nodes"]}

    expected: dict[str, int] = {}
    for edge in graph["edges"]:
        expected[edge["source"]] = expected.get(edge["source"], 0) + edge["weight"]
        expected[edge["target"]] = expected.get(edge["target"], 0) + edge["weight"]
    assert sizes == expected


def test_graph_edges_carry_citations(studies):
    result = from_sample(
        studies, get("country"), series_dimension=get("phase"), limit=5, citation_limit=3
    )
    _, graph = encode_network(result)
    cited = [e for e in graph["edges"] if e["citations"]]
    assert cited, "edges are where citations attach for a graph"
    for edge in cited:
        assert edge["citation_total"] >= len(edge["citations"])
        assert all(c["nct_id"].startswith("NCT") for c in edge["citations"])


def test_semantic_drug_graph_has_root_entity_and_trial_evidence_levels():
    studies = [
        make_study(
            "NCT1",
            title="Trial One",
            contactsLocationsModule={
                "overallOfficials": [{"name": "Sharon Sha", "role": "PRINCIPAL_INVESTIGATOR"}]
            },
            armsInterventionsModule={
                "interventions": [
                    {"type": "DRUG", "name": "Pembrolizumab"},
                    {"type": "BIOLOGICAL", "name": "Vaccine"},
                ]
            },
        ),
        make_study(
            "NCT2",
            title="Trial Two",
            contactsLocationsModule={
                "overallOfficials": [{"name": "Sharon Sha, MD", "role": "STUDY_CHAIR"}]
            },
            armsInterventionsModule={
                "interventions": [{"type": "DRUG", "name": "Nivolumab"}]
            },
        ),
    ]
    plan = GraphPlan(
        root_entity="person",
        root_value="Sharon Sha",
        levels=[
            GraphLevel(entity="intervention_name"),
            GraphLevel(entity="trial"),
        ],
    )
    encoding, graph = encode_semantic_graph(studies, plan, citation_limit=5)

    assert encoding.node["level_field"] == "level"
    root = next(node for node in graph["nodes"] if node["level"] == 0)
    branches = {node["id"] for node in graph["nodes"] if node["level"] == 1}
    leaves = {node["id"] for node in graph["nodes"] if node["level"] == 2}
    assert root["label"] == "Sharon Sha"
    assert root["verification_basis"] == "overall_official_name_exact_match"
    assert {node["label"] for node in graph["nodes"] if node["level"] == 1} == {
        "Pembrolizumab",
        "Nivolumab",
    }
    assert {node["label"] for node in graph["nodes"] if node["level"] == 2} == {
        "Trial One",
        "Trial Two",
    }
    assert all(
        (edge["source"] == root["id"] and edge["target"] in branches)
        or (edge["source"] in branches and edge["target"] in leaves)
        for edge in graph["edges"]
    )
    assert all(
        "overallOfficials[].name" in edge["citations"][0]["field_path"]
        and "intervention_name=" in edge["citations"][0]["field_value"]
        for edge in graph["edges"]
    )
