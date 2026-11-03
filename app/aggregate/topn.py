"""Collapse a high-cardinality dimension's tail into a single 'Other' bucket."""

from __future__ import annotations

from collections import defaultdict

from app.dimensions.registry import DimensionDef
from app.models.buckets import Bucket, Truncation

OTHER_KEY = "__other__"


def sort_buckets(buckets: list[Bucket], dimension: DimensionDef) -> list[Bucket]:
    """Order buckets, keeping 'Other' and the missing bucket pinned last.

    Ties break on label ascending so output is byte-stable across runs — a
    snapshot test cannot pass otherwise.
    """
    natural = {value: index for index, value in enumerate(dimension.natural_order)}

    def key(bucket: Bucket):
        if dimension.sort_mode == "natural":
            return (natural.get(bucket.key, len(natural)), bucket.label, bucket.series or "")
        if dimension.sort_mode == "label_asc":
            return (bucket.label, bucket.series or "")
        return (-bucket.value, bucket.label, bucket.series or "")

    ordinary = [b for b in buckets if not b.is_other and not b.is_missing]
    other = [b for b in buckets if b.is_other]
    missing = [b for b in buckets if b.is_missing]
    return sorted(ordinary, key=key) + other + missing


def apply_top_n(
    buckets: list[Bucket],
    dimension: DimensionDef,
    limit: int,
    citation_limit: int,
) -> tuple[list[Bucket], Truncation | None]:
    """Keep the ``limit`` largest values of the dimension; fold the rest into 'Other'.

    Ranking is always by the *primary dimension's* total, never by individual
    cells. With a series dimension a key occupies several buckets, so ranking
    cells directly would keep "the biggest cells" — which can be five bars of
    one country — rather than the biggest countries the caller asked for.
    """
    if dimension.cardinality_rule != "top_n":
        return buckets, None

    ordinary = [b for b in buckets if not b.is_missing]
    missing = [b for b in buckets if b.is_missing]

    totals: dict[str, int] = defaultdict(int)
    for bucket in ordinary:
        totals[bucket.key] += bucket.value

    if len(totals) <= limit:
        return buckets, None

    ranked_keys = sorted(totals, key=lambda k: (-totals[k], k))
    kept_keys = set(ranked_keys[:limit])

    kept = [b for b in ordinary if b.key in kept_keys]
    tail = [b for b in ordinary if b.key not in kept_keys]

    collapsed = sum(b.value for b in tail)
    other = Bucket(
        key=OTHER_KEY,
        label="Other",
        value=collapsed,
        is_other=True,
        distinct_values_collapsed=len(totals) - len(kept_keys),
        # Hundreds of references would be unusable and zero would be opaque, so
        # carry a token sample and say that is what it is.
        citations=[c for b in tail[:3] for c in b.citations[:1]],
        citation_total=collapsed,
        citations_truncated=True,
        citation_sample=True,
        citation_basis=(
            f"A sample of the {len(totals) - len(kept_keys)} "
            f"{dimension.id} values collapsed into 'Other'."
        ),
    )

    return kept + [other] + missing, Truncation(
        dimension=dimension.id,
        shown=len(kept_keys),
        total_distinct=len(totals),
        collapsed_into_other=collapsed,
    )


def limit_series(buckets: list[Bucket], limit: int) -> tuple[list[Bucket], int]:
    """Keep the ``limit`` largest series values, dropping the rest.

    ``apply_top_n`` bounds only the primary axis. On a co-occurrence graph the
    second axis is a node count too, so an unbounded series turns "sponsors and
    drugs" into thousands of nodes and the resolver quietly downgrades the
    whole thing to a bar chart. Ranking by summed value keeps the edges that
    carry the relationship and reports how many values were left out.

    Dropped cells are removed rather than folded into an "Other" node: an edge
    to a synthetic node would assert a co-occurrence that no trial records.
    """
    totals: dict[str, int] = defaultdict(int)
    for bucket in buckets:
        if bucket.series is not None:
            totals[bucket.series] += bucket.value
    if len(totals) <= limit:
        return buckets, 0
    kept = set(sorted(totals, key=lambda s: (-totals[s], s))[:limit])
    return (
        [b for b in buckets if b.series is None or b.series in kept],
        len(totals) - len(kept),
    )
