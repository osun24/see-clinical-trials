"""Per-bucket fetch that returns exact counts instead of sampled ones.

For dimensions whose buckets are expressible as a server-side filter, asking
the API once per bucket is strictly better than counting a relevance-ordered
sample. It buys three things at once:

1. **Exact counts** — ``totalCount`` per bucket is the real number.
2. **Evenly distributed citations** — real records from every bucket, instead
   of citations concentrated wherever relevance ranking happened to look.
3. **Parallelism** — unlike the token-chained flat fetch, these requests are
   independent and run concurrently.

It is gated on bucket count, and deliberately does not apply to sponsor,
condition, country, or intervention, whose buckets are fuzzy string matches and
whose "Other" bucket is unexpressible in principle.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from pydantic import BaseModel, Field

from app.config import settings
from app.ctgov.client import CtgovClient
from app.ctgov.fetch import FetchResult, parse_studies
from app.ctgov.params import CtgovQuerySpec, year_universe
from app.dimensions.registry import DimensionDef
from app.models.errors import ServiceError
from app.models.plan import ResolvedPlan
from app.models.study import NormalizedStudy


class Stratum(BaseModel):
    """One exact bucket count and a bounded sample of supporting studies."""

    model_config = {"extra": "forbid"}

    key: str
    exact_count: int
    studies: list[NormalizedStudy] = Field(default_factory=list)


class StratifiedResult(BaseModel):
    """Results of the independent exact-count requests for one dimension."""

    model_config = {"extra": "forbid"}

    strata: list[Stratum] = Field(default_factory=list)
    total_matching: int = 0
    requests_made: int = 0

    @property
    def counted_total(self) -> int:
        """Sum the exact counts returned for non-empty strata."""
        return sum(s.exact_count for s in self.strata)


def bucket_universe(dimension: DimensionDef, plan: ResolvedPlan) -> list[str]:
    """The bucket keys to request, when they are knowable without the data."""
    if dimension.enum_values:
        return list(dimension.enum_values)
    if dimension.datatype == "temporal":
        return year_universe(plan)
    return []


def should_stratify(dimension: DimensionDef, plan: ResolvedPlan) -> bool:
    """Return whether exact per-bucket requests are safe and bounded."""
    if not settings.exact_counts or not dimension.is_exact_capable:
        return False
    if plan.series_by:
        # A second axis would multiply the request count; sample instead.
        return False
    universe = bucket_universe(dimension, plan)
    return 0 < len(universe) <= settings.exact_max_buckets


async def fetch_stratified(
    client: CtgovClient,
    spec: CtgovQuerySpec,
    dimension: DimensionDef,
    plan: ResolvedPlan,
    *,
    total_matching: int,
) -> StratifiedResult:
    """One scoped request per bucket, run concurrently."""
    assert dimension.exact_filter is not None
    universe = bucket_universe(dimension, plan)
    semaphore = asyncio.Semaphore(settings.exact_concurrency)
    deadline = time.monotonic() + settings.fetch_budget_s

    async def one(key: str) -> Stratum | None:
        if time.monotonic() > deadline:
            return None
        scoped = spec.with_overrides(dimension.exact_filter(key))
        params: dict[str, Any] = {
            **scoped.params,
            "fields": ",".join(scoped.fields_mask),
            "pageSize": str(settings.exact_page_size),
            "countTotal": "true",
        }
        async with semaphore:
            try:
                payload = await client.get_json("/studies", params)
            except ServiceError:
                # One unlucky bucket must not sink the whole answer; it is
                # reported as zero-with-no-citations rather than a 502.
                return None
        return Stratum(
            key=key,
            exact_count=int(payload.get("totalCount", 0)),
            studies=parse_studies(payload),
        )

    results = await asyncio.gather(*(one(k) for k in universe))
    strata = [s for s in results if s is not None and s.exact_count > 0]

    return StratifiedResult(
        strata=strata,
        total_matching=total_matching,
        requests_made=len([r for r in results if r is not None]),
    )


def to_fetch_result(stratified: StratifiedResult) -> FetchResult:
    """Flatten strata into a FetchResult for uniform coverage reporting."""
    seen: dict[str, NormalizedStudy] = {}
    for stratum in stratified.strata:
        for study in stratum.studies:
            seen.setdefault(study.nct_id, study)
    return FetchResult(
        studies=list(seen.values()),
        total_matching=stratified.total_matching,
        pages_fetched=stratified.requests_made,
        truncated_reason=None,
        exact_counts={s.key: s.exact_count for s in stratified.strata},
        sampling_method="stratified_by_bucket",
    )
