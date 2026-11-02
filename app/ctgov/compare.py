"""Side-by-side comparison arms: "our pipeline versus Merck's".

Comparing two named things is the question shape an executive asks most, and it
cannot be expressed as a single ClinicalTrials.gov query. Each arm is therefore
its own scoped query, run concurrently, and the arm's own value becomes the
series on the chart.

Because arms are independent queries, one trial can satisfy more than one of
them (a trial listing two conditions, say). That overlap is measured and
reported rather than assumed away.
"""

from __future__ import annotations

import asyncio

from pydantic import BaseModel, Field

from app.aggregate import bucketize
from app.config import settings
from app.ctgov.client import CtgovClient
from app.ctgov.fetch import FetchResult, fetch_flat
from app.ctgov.params import build_query
from app.dimensions.registry import DimensionDef
from app.models.buckets import Bucket, BucketSet, Exclusions, Invariant
from app.models.plan import ResolvedPlan

#: Comparison slot -> the ResolvedPlan field it overrides.
COMPARE_FIELD: dict[str, str] = {
    "sponsor": "sponsor",
    "condition": "condition",
    "drug_name": "drug_name",
    "country": "country",
}

MIN_ARMS = 2
MAX_ARMS = 5


class ComparisonResult(BaseModel):
    """Combined buckets, fetch metadata, and overlap facts for comparison arms."""

    model_config = {"extra": "forbid"}

    bucket_set: BucketSet
    fetch: FetchResult
    arm_totals: dict[str, int] = Field(default_factory=dict)
    overlapping_trials: int = 0
    empty_arms: list[str] = Field(default_factory=list)


def should_compare(plan: ResolvedPlan) -> bool:
    """Return whether the plan contains a supported multi-arm comparison."""
    return bool(
        plan.compare_by
        and plan.compare_by in COMPARE_FIELD
        and len(plan.compare_values) >= MIN_ARMS
    )


async def fetch_comparison(
    client: CtgovClient,
    plan: ResolvedPlan,
    dimension: DimensionDef,
    *,
    cap: int,
) -> ComparisonResult:
    """One scoped fetch per arm, bucketed by the primary dimension."""
    field = COMPARE_FIELD[str(plan.compare_by)]
    arms = plan.compare_values[:MAX_ARMS]
    per_arm_cap = max(200, cap // len(arms))

    async def one(value: str):
        arm_plan = plan.model_copy(update={field: value, "compare_values": []})
        return value, await fetch_flat(client, build_query(arm_plan), cap=per_arm_cap)

    results = await asyncio.gather(*(one(v) for v in arms))

    buckets: list[Bucket] = []
    arm_totals: dict[str, int] = {}
    empty_arms: list[str] = []
    seen_ids: dict[str, set[str]] = {}
    exclusions = Exclusions()
    all_studies = []
    pages = 0

    for value, fetched in results:
        pages += fetched.pages_fetched
        arm_totals[value] = fetched.total_matching
        if not fetched.studies:
            empty_arms.append(value)
            continue

        seen_ids[value] = {s.nct_id for s in fetched.studies}
        all_studies.extend(fetched.studies)

        arm = bucketize.from_sample(
            fetched.studies,
            dimension,
            limit=plan.limit,
            citation_limit=settings.citation_limit,
        )
        exclusions.missing_dimension_value += arm.exclusions.missing_dimension_value
        for bucket in arm.buckets:
            buckets.append(bucket.model_copy(update={"series": value}))

    # A trial can satisfy several arms, since each arm is its own query.
    counted: dict[str, int] = {}
    for ids in seen_ids.values():
        for nct_id in ids:
            counted[nct_id] = counted.get(nct_id, 0) + 1
    overlapping = sum(1 for n in counted.values() if n > 1)

    analyzed = len(counted)
    bucket_set = BucketSet(
        dimension=dimension.id,
        series_dimension=f"compare:{plan.compare_by}",
        buckets=buckets,
        invariant=Invariant(
            kind="partition" if not overlapping else "multi_label",
            statement=(
                "sum(data[].value) == meta.coverage.records_analyzed"
                if not overlapping
                else "a trial can match more than one comparison arm; "
                "sum(data[].value) >= records_analyzed"
            ),
            assignments_total=sum(b.value for b in buckets),
            records_analyzed=analyzed,
        ),
        exclusions=exclusions,
        records_analyzed=analyzed,
        counts_are_exact=False,
    )

    fetch_result = FetchResult(
        studies=list({s.nct_id: s for s in all_studies}.values()),
        total_matching=sum(arm_totals.values()),
        pages_fetched=pages,
        truncated_reason=next(
            (f.truncated_reason for _, f in results if f.truncated_reason), None
        ),
        sampling_method="first_n_by_api_relevance_order",
    )

    return ComparisonResult(
        bucket_set=bucket_set,
        fetch=fetch_result,
        arm_totals=arm_totals,
        overlapping_trials=overlapping,
        empty_arms=empty_arms,
    )
