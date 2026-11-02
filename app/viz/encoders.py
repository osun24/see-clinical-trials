"""Assemble the per-type encoding and data rows.

Two guarantees a renderer can rely on:

* ``data`` is a flat array of uniform row objects, except ``network_graph``
  which is ``{"nodes": [...], "edges": [...]}``.
* Every channel's ``field`` names a key present on every row.
"""

from __future__ import annotations

import re
from typing import Any

from app.aggregate.citations import build_citation, serialise_value
from app.aggregate.keys import bucket_keys, display_label
from app.dimensions.extractors import extract_raw
from app.dimensions.registry import DIMENSIONS
from app.models.buckets import Bucket, BucketSet
from app.models.plan import GraphPlan
from app.models.study import NormalizedStudy
from app.models.viz import Channel, Encoding


def _axis_title(dimension_id: str | None) -> str:
    if not dimension_id:
        return "Category"
    # Comparison arms are a synthetic series, not a registry dimension.
    if dimension_id.startswith("compare:"):
        return dimension_id.split(":", 1)[1].replace("_", " ").title()
    dimension = DIMENSIONS.get(dimension_id)
    if dimension is None:
        return dimension_id.replace("_", " ").title()
    return {
        "phase": "Trial Phase",
        "phase_any": "Trial Phase",
        "overall_status": "Recruitment Status",
        "start_year": "Start Year",
        "completion_year": "Completion Year",
        "lead_sponsor": "Lead Sponsor",
        "sponsor_type": "Sponsor Type",
        "study_type": "Study Type",
        "country": "Country",
        "condition": "Condition",
        "condition_mesh": "Condition (MeSH)",
        "intervention_type": "Intervention Type",
        "intervention_name": "Intervention",
        "enrollment_bucket": "Enrollment Size",
        "sex": "Eligible Sex",
        "age_group": "Age Group",
        "results_posted": "Results Posted",
    }.get(dimension_id, dimension_id.replace("_", " ").title())


def _row(bucket: Bucket) -> dict[str, Any]:
    """The common row shape, shared by every flat visualization type."""
    row: dict[str, Any] = {
        "label": bucket.label,
        "value": bucket.value,
        "value_is_exact": bucket.value_is_exact,
        "citations": [c.model_dump() for c in bucket.citations],
        "citation_total": bucket.citation_total,
        "citations_truncated": bucket.citations_truncated,
    }
    if bucket.series is not None:
        row["series"] = bucket.series
    if bucket.sample_value is not None:
        row["sample_value"] = bucket.sample_value
    if bucket.is_other:
        row["is_other"] = True
        row["distinct_values_collapsed"] = bucket.distinct_values_collapsed
    if bucket.is_missing:
        row["is_missing"] = True
    if bucket.citation_sample:
        row["citation_sample"] = True
    if bucket.citation_basis:
        row["citation_basis"] = bucket.citation_basis
    return row


def _tooltip(bucket_set: BucketSet) -> list[str]:
    tooltip = ["label", "value"]
    if bucket_set.series_dimension:
        tooltip.insert(1, "series")
    return tooltip


def encode_bar(
    bucket_set: BucketSet,
    *,
    orientation: str,
    stack: bool | str | None = None,
    with_share: bool = False,
) -> tuple[Encoding, list[dict[str, Any]]]:
    """Encode bucket rows as a bar, grouped-bar, or stacked-bar contract."""
    encoding = Encoding(
        x=Channel(
            field="label",
            type="nominal",
            axis_title=_axis_title(bucket_set.dimension),
            sort="none",
        ),
        y=Channel(
            field="value",
            type="quantitative",
            axis_title="Number of Trials",
            unit="trials",
        ),
        orientation=orientation,  # type: ignore[arg-type]
        tooltip=_tooltip(bucket_set),
    )
    if bucket_set.series_dimension:
        encoding.series = Channel(
            field="series",
            type="nominal",
            legend_title=_axis_title(bucket_set.series_dimension),
        )
        encoding.stack = stack

    rows = [_row(b) for b in bucket_set.buckets]

    # A "what share" question still wants proportions even when too many
    # categories ruled out a pie chart.
    if with_share and bucket_set.invariant.kind == "partition":
        total = sum(r["value"] for r in rows) or 1
        for row in rows:
            row["share"] = round(row["value"] / total, 4)
        encoding.total = total
        encoding.tooltip = [*encoding.tooltip, "share"]

    return encoding, rows


def encode_time_series(bucket_set: BucketSet) -> tuple[Encoding, list[dict[str, Any]]]:
    """Zero-fill the axis so a gap reads as zero rather than as missing data."""
    rows = [_row(b) for b in bucket_set.buckets if not b.is_missing]
    for row in rows:
        row["bucket"] = row["label"]

    numeric = [int(r["bucket"]) for r in rows if str(r["bucket"]).isdigit()]
    if numeric and not bucket_set.series_dimension:
        present = {int(r["bucket"]): r for r in rows if str(r["bucket"]).isdigit()}
        rows = []
        for year in range(min(numeric), max(numeric) + 1):
            rows.append(
                present.get(
                    year,
                    {
                        "bucket": str(year),
                        "label": str(year),
                        "value": 0,
                        "value_is_exact": bucket_set.counts_are_exact,
                        "citations": [],
                        "citation_total": 0,
                        "citations_truncated": False,
                    },
                )
            )

    encoding = Encoding(
        x=Channel(
            field="bucket",
            type="temporal",
            granularity="year",
            axis_title=_axis_title(bucket_set.dimension),
        ),
        y=Channel(
            field="value",
            type="quantitative",
            axis_title="Number of Trials",
            unit="trials",
        ),
        point_markers=True,
        tooltip=_tooltip(bucket_set),
    )
    if bucket_set.series_dimension:
        encoding.series = Channel(
            field="series",
            type="nominal",
            legend_title=_axis_title(bucket_set.series_dimension),
        )
    return encoding, rows


def encode_pie(bucket_set: BucketSet) -> tuple[Encoding, list[dict[str, Any]]]:
    """Encode a resolver-approved partition as slices with fractional shares."""
    rows = [_row(b) for b in bucket_set.buckets]
    total = sum(r["value"] for r in rows) or 1
    for row in rows:
        row["share"] = round(row["value"] / total, 4)
    encoding = Encoding(
        theta=Channel(field="value", type="quantitative", unit="trials"),
        color=Channel(
            field="label", type="nominal", legend_title=_axis_title(bucket_set.dimension)
        ),
        total=total,
        tooltip=["label", "value", "share"],
    )
    return encoding, rows


def encode_stat(
    bucket_set: BucketSet | None, *, total: int, exact: bool
) -> tuple[Encoding, list[dict[str, Any]]]:
    """Encode one total and any available supporting citations."""
    encoding = Encoding(
        value=Channel(
            field="value", type="quantitative", unit="trials", format="integer"
        ),
        label=Channel(field="label", type="nominal"),
    )
    citations: list[dict[str, Any]] = []
    citation_total = 0
    if bucket_set and bucket_set.buckets:
        citations = [c.model_dump() for b in bucket_set.buckets[:1] for c in b.citations]
        citation_total = total
    return encoding, [
        {
            "label": "Matching trials",
            "value": total,
            "value_is_exact": exact,
            "citations": citations,
            "citation_total": citation_total,
            "citations_truncated": citation_total > len(citations),
        }
    ]


def encode_network(bucket_set: BucketSet) -> tuple[Encoding, dict[str, Any]]:
    """The one type whose ``data`` is not a flat array."""
    nodes: dict[str, dict[str, Any]] = {}
    edges: list[dict[str, Any]] = []
    omitted_trials = 0
    omitted_values: int | None = None
    omitted_reason = ""

    for bucket in bucket_set.buckets:
        if bucket.series is None:
            # No second-dimension value, so there is no edge to place this on.
            # Record it instead of dropping it: the edge weights must still
            # add up to something the client can reconcile.
            if bucket.value:
                omitted_trials += bucket.value
                if bucket.is_other:
                    omitted_values = bucket.distinct_values_collapsed
                    shown = (
                        bucket_set.truncation.shown if bucket_set.truncation else "N"
                    )
                    omitted_reason = (
                        f"Collapsed into 'Other' by the top-{shown} limit, so it has "
                        f"no single {bucket_set.series_dimension} value to link to."
                    )
                elif bucket.is_missing:
                    omitted_reason = (
                        f"No {bucket_set.series_dimension} value recorded, "
                        "so there is no edge to place these trials on."
                    )
                else:
                    omitted_reason = "No second-dimension value to link to."
            continue
        source = f"{bucket_set.dimension}:{bucket.label}"
        target = f"{bucket_set.series_dimension}:{bucket.series}"
        nodes.setdefault(
            source,
            {
                "id": source,
                "label": bucket.label,
                "group": bucket_set.dimension,
                "size": 0,
                "level": 0,
            },
        )
        nodes.setdefault(
            target,
            {
                "id": target,
                "label": bucket.series,
                "group": bucket_set.series_dimension,
                "size": 0,
                "level": 1,
            },
        )
        nodes[source]["size"] += bucket.value
        nodes[target]["size"] += bucket.value
        edges.append(
            {
                "source": source,
                "target": target,
                "weight": bucket.value,
                "citations": [c.model_dump() for c in bucket.citations],
                "citation_total": bucket.citation_total,
                "citations_truncated": bucket.citations_truncated,
            }
        )

    encoding = _network_encoding()
    graph: dict[str, Any] = {"nodes": list(nodes.values()), "edges": edges}
    if omitted_trials:
        graph["omitted"] = {
            "trials": omitted_trials,
            "distinct_values_collapsed": omitted_values,
            "reason": omitted_reason,
        }
    return encoding, graph


def encode_semantic_graph(
    studies: list[NormalizedStudy],
    plan: GraphPlan,
    *,
    citation_limit: int,
) -> tuple[Encoding, dict[str, Any]]:
    """Materialize a validated graph plan exclusively from fetched records."""
    target = plan.levels[0].entity
    include_trials = len(plan.levels) > 1 and plan.levels[1].entity == "trial"
    graph_studies, verification = _studies_for_root(studies, plan)
    associations = _graph_associations(graph_studies, target, plan)

    root_id = f"{plan.root_entity}:{plan.root_value}"
    nodes: dict[str, dict[str, Any]] = {
        root_id: {
            "id": root_id,
            "label": plan.root_value,
            "group": plan.root_entity,
            "size": len(graph_studies),
            "level": 0,
            "verification_basis": verification,
        }
    }
    edges: list[dict[str, Any]] = []
    node_limit = max(5, min(plan.node_limit, 60))

    ranked = sorted(
        associations.items(),
        key=lambda item: (-len(item[1]["studies"]), item[1]["label"].casefold()),
    )
    entity_cap = node_limit - 1
    if include_trials:
        entity_cap = max(1, (node_limit - 1) // 2)
    kept = ranked[:entity_cap]
    omitted_study_ids: set[str] = set()

    for key, association in ranked[entity_cap:]:
        del key
        omitted_study_ids.update(association["studies"])

    entity_members: dict[str, list[str]] = {}
    for key, association in kept:
        entity_id = f"{target}:{key}"
        study_ids = sorted(association["studies"])
        entity_members[entity_id] = study_ids
        nodes[entity_id] = {
            "id": entity_id,
            "label": association["label"],
            "group": target,
            "size": len(study_ids),
            "level": 1,
        }
        citations = [association["citations"][nct_id] for nct_id in study_ids]
        edges.append(
            {
                "source": root_id,
                "target": entity_id,
                "weight": len(study_ids),
                "citations": citations[:citation_limit],
                "citation_total": len(study_ids),
                "citations_truncated": len(study_ids) > citation_limit,
            }
        )

    if include_trials:
        by_nct = {study.nct_id: study for study in graph_studies}
        queues = {entity_id: list(ids) for entity_id, ids in entity_members.items()}
        while len(nodes) < node_limit and any(queues.values()):
            progressed = False
            for entity_id, queue in queues.items():
                if not queue or len(nodes) >= node_limit:
                    continue
                nct_id = queue.pop(0)
                study = by_nct[nct_id]
                trial_id = f"trial:{nct_id}"
                nodes.setdefault(
                    trial_id,
                    {
                        "id": trial_id,
                        "label": study.brief_title or nct_id,
                        "group": "trial",
                        "size": 1,
                        "level": 2,
                        "nct_id": nct_id,
                        "url": study.url,
                    },
                )
                association_key = entity_id.split(":", 1)[1]
                citation = associations[association_key]["citations"][nct_id]
                edges.append(
                    {
                        "source": entity_id,
                        "target": trial_id,
                        "weight": 1,
                        "citations": [citation],
                        "citation_total": 1,
                        "citations_truncated": False,
                    }
                )
                progressed = True
            if not progressed:
                break
        for queue in queues.values():
            omitted_study_ids.update(queue)

    graph: dict[str, Any] = {"nodes": list(nodes.values()), "edges": edges}
    if omitted_study_ids:
        graph["omitted"] = {
            "trials": len(omitted_study_ids),
            "distinct_values_collapsed": max(0, len(ranked) - len(kept)),
            "reason": f"The graph was limited to {node_limit} nodes.",
        }
    return _network_encoding(), graph


def _studies_for_root(
    studies: list[NormalizedStudy], plan: GraphPlan
) -> tuple[list[NormalizedStudy], str]:
    if plan.root_entity != "person":
        return studies, "full_text_query"
    with_officials = [
        study
        for study in studies
        if extract_raw(
            study.raw,
            "protocolSection.contactsLocationsModule.overallOfficials[].name",
        )
    ]
    if not with_officials:
        return studies, "full_text_query_official_field_unavailable"
    wanted = _normalise_person(plan.root_value)
    verified = [
        study
        for study in with_officials
        if any(
            _normalise_person(str(name)) == wanted
            for name in extract_raw(
                study.raw,
                "protocolSection.contactsLocationsModule.overallOfficials[].name",
            )
        )
    ]
    return verified, "overall_official_name_exact_match"


def _normalise_person(value: str) -> str:
    ignored = {"md", "phd", "mph", "do", "m", "d"}
    tokens = re.findall(r"[a-z0-9]+", value.casefold())
    return " ".join(token for token in tokens if token not in ignored)


def _graph_associations(
    studies: list[NormalizedStudy], target: str, plan: GraphPlan
) -> dict[str, dict[str, Any]]:
    dimension = DIMENSIONS[target]
    associations: dict[str, dict[str, Any]] = {}
    for study in studies:
        values: list[tuple[str, Any]]
        if target == "intervention_name":
            values = []
            for intervention in extract_raw(
                study.raw,
                "protocolSection.armsInterventionsModule.interventions[]",
            ):
                if not isinstance(intervention, dict) or intervention.get("type") != "DRUG":
                    continue
                name = str(intervention.get("name") or "").strip()
                if name:
                    values.append((name.casefold(), name))
        else:
            values = bucket_keys(study.raw, dimension)

        seen: set[str] = set()
        for key, raw in values:
            normalized = " ".join(str(key).casefold().split())
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            association = associations.setdefault(
                normalized,
                {
                    "label": (
                        str(raw).strip()
                        if target == "intervention_name"
                        else display_label(str(key), dimension)
                    ),
                    "studies": set(),
                    "citations": {},
                },
            )
            association["studies"].add(study.nct_id)
            citation = build_citation(study, dimension, raw)
            if plan.root_entity == "person":
                matching_official = next(
                    (
                        name
                        for name in extract_raw(
                            study.raw,
                            "protocolSection.contactsLocationsModule.overallOfficials[].name",
                        )
                        if _normalise_person(str(name))
                        == _normalise_person(plan.root_value)
                    ),
                    None,
                )
                if matching_official is not None:
                    citation.field_path = (
                        "protocolSection.contactsLocationsModule.overallOfficials[].name"
                        f" + {dimension.path}"
                    )
                    citation.field_value = (
                        f"overallOfficialName={serialise_value(matching_official)}; "
                        f"{target}={serialise_value(raw)}"
                    )
                    citation.evidence_kind = "field_value"
                    citation.excerpt = None
            association["citations"][study.nct_id] = citation.model_dump()
    return associations


def _network_encoding() -> Encoding:
    return Encoding(
        node={
            "id_field": "id",
            "label_field": "label",
            "group_field": "group",
            "size_field": "size",
            "level_field": "level",
        },
        edge={"source_field": "source", "target_field": "target", "weight_field": "weight"},
        layout_hint="hierarchical",
    )
