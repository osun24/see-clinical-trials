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


def from_sample(
    studies: list[NormalizedStudy], dimension: DimensionDef, *,
    series_dimension: DimensionDef | None = None, limit: int = 10, citation_limit: int = 5,
) -> BucketSet:
    """Compatibility entry point: compile a preset and use the shared executor."""
    from app.analysis.presets import run_preset
    return run_preset(studies, dimension, series_dimension, limit, citation_limit)


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
        invariant=Invariant(kind=dimension.semantics, statement=("sum(data[].value) equals the sum of server-reported bucket counts; records_analyzed counts citation records." if dimension.semantics == "partition" else "Server-reported bucket counts overlap; values must not be summed to infer unique trials.")),
        truncation=truncation,
        records_analyzed=records_analyzed,
        counts_are_exact=True,
    )
