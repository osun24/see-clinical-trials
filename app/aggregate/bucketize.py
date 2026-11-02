"""Turn fetched records into buckets with citations.

Two entry points produce the same ``BucketSet``:

``from_sample``  counts locally over fetched records.
``from_strata``  uses exact per-bucket counts from a stratified fetch, with
                 citations drawn from each bucket's own records.
"""

from __future__ import annotations

from typing import Any

from app.aggregate import topn
from app.aggregate.citations import build_citation
from app.aggregate.keys import MISSING_KEY, bucket_keys, display_label
from app.dimensions.registry import DimensionDef
from app.models.buckets import Bucket, BucketSet, Exclusions, Invariant
from app.models.study import NormalizedStudy

PARTITION_STATEMENT = "sum(data[].value) == meta.coverage.records_analyzed"
MULTI_LABEL_STATEMENT = (
    "a trial may appear in several buckets; sum(data[].value) >= records_analyzed"
)


def _invariant(
    dimension: DimensionDef,
    series_dimension: DimensionDef | None,
    buckets: list[Bucket],
    records_analyzed: int,
) -> Invariant:
    multi = dimension.semantics == "multi_label" or (
        series_dimension is not None and series_dimension.semantics == "multi_label"
    )
    if not multi:
        return Invariant(kind="partition", statement=PARTITION_STATEMENT)
    return Invariant(
        kind="multi_label",
        statement=MULTI_LABEL_STATEMENT,
        assignments_total=sum(b.value for b in buckets),
        records_analyzed=records_analyzed,
    )


def _collect(
    studies: list[NormalizedStudy],
    dimension: DimensionDef,
    series_dimension: DimensionDef | None,
) -> tuple[dict[tuple[str, str | None], list[tuple[NormalizedStudy, Any]]], Exclusions]:
    grouped: dict[tuple[str, str | None], list[tuple[NormalizedStudy, Any]]] = {}
    exclusions = Exclusions()

    for study in studies:
        pairs = bucket_keys(study.raw, dimension)

        if dimension.missing_policy == "exclude" and any(
            key == MISSING_KEY for key, _ in pairs
        ):
            exclusions.missing_dimension_value += 1
            continue

        series_pairs: list[tuple[str | None, Any]] = [(None, None)]
        if series_dimension is not None:
            series_keys = bucket_keys(study.raw, series_dimension)
            if series_dimension.missing_policy == "exclude" and any(
                key == MISSING_KEY for key, _ in series_keys
            ):
                exclusions.missing_dimension_value += 1
                continue
            series_pairs = [(k, v) for k, v in series_keys]

        for key, raw in pairs:
            for series_key, _ in series_pairs:
                grouped.setdefault((key, series_key), []).append((study, raw))

    return grouped, exclusions


def _bucket_from_members(
    key: str,
    series_key: str | None,
    members: list[tuple[NormalizedStudy, Any]],
    dimension: DimensionDef,
    series_dimension: DimensionDef | None,
    citation_limit: int,
) -> Bucket:
    citations = [
        build_citation(study, dimension, raw) for study, raw in members[:citation_limit]
    ]
    return Bucket(
        key=key,
        label=display_label(key, dimension),
        series=(
            display_label(series_key, series_dimension)
            if series_key is not None and series_dimension is not None
            else None
        ),
        value=len(members),
        is_missing=key == MISSING_KEY,
        citations=citations,
        citation_total=len(members),
        citations_truncated=len(members) > len(citations),
    )


def from_sample(
    studies: list[NormalizedStudy],
    dimension: DimensionDef,
    *,
    series_dimension: DimensionDef | None = None,
    limit: int = 10,
    citation_limit: int = 5,
) -> BucketSet:
    grouped, exclusions = _collect(studies, dimension, series_dimension)
    records_analyzed = len(studies) - exclusions.missing_dimension_value

    buckets = [
        _bucket_from_members(
            key, series_key, members, dimension, series_dimension, citation_limit
        )
        for (key, series_key), members in grouped.items()
    ]
    buckets, truncation = topn.apply_top_n(buckets, dimension, limit, citation_limit)
    buckets = topn.sort_buckets(buckets, dimension)

    return BucketSet(
        dimension=dimension.id,
        series_dimension=series_dimension.id if series_dimension else None,
        buckets=buckets,
        invariant=_invariant(dimension, series_dimension, buckets, records_analyzed),
        exclusions=exclusions,
        truncation=truncation,
        records_analyzed=records_analyzed,
        counts_are_exact=False,
    )


def from_strata(
    strata: list[Any],
    dimension: DimensionDef,
    *,
    limit: int = 10,
    citation_limit: int = 5,
) -> BucketSet:
    """Build buckets from exact per-bucket counts.

    ``value`` is the server's own count. ``sample_value`` keeps the number of
    records actually inspected, so the gap between the two is visible rather
    than implied.
    """
    buckets: list[Bucket] = []
    records_analyzed = 0

    for stratum in strata:
        members = [
            (study, next(iter(raws), None))
            for study in stratum.studies
            for raws in [[raw for _, raw in bucket_keys(study.raw, dimension)]]
        ]
        citations = [
            build_citation(study, dimension, raw) for study, raw in members[:citation_limit]
        ]
        records_analyzed += len(stratum.studies)
        buckets.append(
            Bucket(
                key=stratum.key,
                label=display_label(stratum.key, dimension),
                value=stratum.exact_count,
                value_is_exact=True,
                sample_value=len(stratum.studies),
                citations=citations,
                citation_total=stratum.exact_count,
                citations_truncated=stratum.exact_count > len(citations),
                citation_basis=(
                    f"Citations drawn from {len(stratum.studies)} of "
                    f"{stratum.exact_count} trials in this bucket."
                ),
            )
        )

    buckets, truncation = topn.apply_top_n(buckets, dimension, limit, citation_limit)
    buckets = topn.sort_buckets(buckets, dimension)

    return BucketSet(
        dimension=dimension.id,
        buckets=buckets,
        invariant=_invariant(dimension, None, buckets, records_analyzed),
        truncation=truncation,
        records_analyzed=records_analyzed,
        counts_are_exact=True,
    )
