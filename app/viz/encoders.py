"""Assemble the per-type encoding and data rows.

Two guarantees a renderer can rely on:

* ``data`` is a flat array of uniform row objects, except ``network_graph``
  which is ``{"nodes": [...], "edges": [...]}``.
* Every channel's ``field`` names a key present on every row.
"""

from __future__ import annotations

from typing import Any

from app.dimensions.registry import DIMENSIONS
from app.models.buckets import Bucket, BucketSet
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


def encode_network(
    bucket_set: BucketSet, *, root_label: str | None = None
) -> tuple[Encoding, dict[str, Any]]:
    """The one type whose ``data`` is not a flat array."""
    if root_label and bucket_set.series_dimension:
        return _encode_rooted_network(bucket_set, root_label)

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


def _encode_rooted_network(
    bucket_set: BucketSet, root_label: str
) -> tuple[Encoding, dict[str, Any]]:
    """Build root → series → category levels for named-subject relationships."""
    root_id = f"subject:{root_label}"
    represented = [bucket for bucket in bucket_set.buckets if bucket.series is not None]
    nodes: dict[str, dict[str, Any]] = {
        root_id: {
            "id": root_id,
            "label": root_label,
            "group": "subject",
            "size": sum(bucket.value for bucket in represented),
            "level": 0,
        }
    }
    leaf_edges: list[dict[str, Any]] = []
    root_edges: dict[str, dict[str, Any]] = {}

    for bucket in represented:
        branch_id = f"{bucket_set.series_dimension}:{bucket.series}"
        leaf_id = f"{bucket_set.dimension}:{bucket.label}"
        nodes.setdefault(
            branch_id,
            {
                "id": branch_id,
                "label": bucket.series,
                "group": bucket_set.series_dimension,
                "size": 0,
                "level": 1,
            },
        )
        nodes.setdefault(
            leaf_id,
            {
                "id": leaf_id,
                "label": bucket.label,
                "group": bucket_set.dimension,
                "size": 0,
                "level": 2,
            },
        )
        nodes[branch_id]["size"] += bucket.value
        nodes[leaf_id]["size"] += bucket.value

        branch_edge = root_edges.setdefault(
            branch_id,
            {
                "source": root_id,
                "target": branch_id,
                "weight": 0,
                "citations": [],
                "citation_total": 0,
                "citations_truncated": False,
            },
        )
        branch_edge["weight"] += bucket.value
        branch_edge["citations"].extend(c.model_dump() for c in bucket.citations)
        branch_edge["citation_total"] += bucket.citation_total
        branch_edge["citations_truncated"] = (
            branch_edge["citations_truncated"] or bucket.citations_truncated
        )

        leaf_edges.append(
            {
                "source": branch_id,
                "target": leaf_id,
                "weight": bucket.value,
                "citations": [c.model_dump() for c in bucket.citations],
                "citation_total": bucket.citation_total,
                "citations_truncated": bucket.citations_truncated,
            }
        )

    graph: dict[str, Any] = {
        "nodes": list(nodes.values()),
        "edges": [*root_edges.values(), *leaf_edges],
    }
    omitted = [bucket for bucket in bucket_set.buckets if bucket.series is None]
    omitted_trials = sum(bucket.value for bucket in omitted)
    if omitted_trials:
        other = next((bucket for bucket in omitted if bucket.is_other), None)
        graph["omitted"] = {
            "trials": omitted_trials,
            "distinct_values_collapsed": (
                other.distinct_values_collapsed if other is not None else None
            ),
            "reason": (
                "Collapsed categories or records without a sponsor type cannot be "
                "placed on a branch."
            ),
        }
    return _network_encoding(), graph


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
