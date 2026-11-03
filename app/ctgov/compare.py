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
import time

from pydantic import BaseModel, Field

from app.aggregate import bucketize, topn
from app.config import settings
from app.ctgov.client import CtgovClient
from app.ctgov.fetch import FetchResult, fetch_flat, fetch_total_only
from app.ctgov.params import build_query
from app.ctgov.stratified import (
    bucket_universe,
    fetch_stratified,
    should_stratify,
)
from app.dimensions.registry import DimensionDef
from app.models.buckets import Bucket, BucketSet, Citation, Exclusions, Invariant
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

#: Per-arm bucketing keeps everything; ranking happens on the merged set.
_NO_ARM_LIMIT = 10_000


class ComparisonResult(BaseModel):
    """Combined buckets, fetch metadata, and overlap facts for comparison arms."""

    model_config = {"extra": "forbid"}

    bucket_set: BucketSet
    fetch: FetchResult
    arm_totals: dict[str, int] = Field(default_factory=dict)
    arm_fetched: dict[str, int] = Field(default_factory=dict)
    overlapping_trials: int = 0
    empty_arms: list[str] = Field(default_factory=list)


def should_compare(plan: ResolvedPlan) -> bool:
    """Return whether the plan contains a supported multi-arm comparison."""
    return bool(
        plan.compare_by
        and plan.compare_by in COMPARE_FIELD
        and len(plan.compare_values) >= MIN_ARMS
    )


def _arm_plan(plan: ResolvedPlan, field: str, value: str) -> ResolvedPlan:
    """Apply one comparison value without retaining the shared arm filter."""
    arm_plan = plan.model_copy(update={field: value, "compare_values": []})
    if plan.search is not None:
        slot = {
            "sponsor": "sponsor",
            "condition": "condition",
            "drug_name": "intervention",
            "country": "location",
        }[field]
        arm_plan = arm_plan.model_copy(
            update={"search": plan.search.model_copy(update={slot: value})}
        )
    return arm_plan


def _allocate_arm_caps(arm_totals: dict[str, int], cap: int) -> dict[str, int]:
    """Fairly divide a global cap while reusing capacity from small arms.

    This is an equal water-fill allocation: arms smaller than the current fair
    share receive their full population, and their unused share is repeatedly
    redistributed to the remaining arms.
    """
    if sum(arm_totals.values()) <= cap:
        return dict(arm_totals)

    allocated = {name: 0 for name in arm_totals}
    pending = {name for name, total in arm_totals.items() if total > 0}
    remaining = cap
    while pending and remaining > 0:
        share = remaining // len(pending)
        if share == 0:
            for name in sorted(pending)[:remaining]:
                allocated[name] += 1
            break
        completed = {
            name for name in pending
            if arm_totals[name] - allocated[name] <= share
        }
        if completed:
            for name in completed:
                amount = arm_totals[name] - allocated[name]
                allocated[name] += amount
                remaining -= amount
            pending -= completed
            continue
        for name in pending:
            allocated[name] += share
            remaining -= share
        for name in sorted(pending):
            if remaining <= 0:
                break
            allocated[name] += 1
            remaining -= 1
        break
    return allocated


async def fetch_comparison(
    client: CtgovClient,
    plan: ResolvedPlan,
    dimension: DimensionDef | None,
    *,
    cap: int,
    prefer_exact: bool = False,
    adaptive_caps: bool = False,
) -> ComparisonResult:
    """One scoped fetch per arm, bucketed by the primary dimension.

    With no dimension the question is a comparison of totals — "compare the
    number of trials across A, B and C" — and each arm's own ``totalCount`` is
    the answer. Those are server-reported and exact, so only enough records to
    cite are fetched. This shape used to be dropped for having no dimension,
    which left the arms unapplied and the base query unconstrained: the
    response reported the whole registry as though it were the answer.
    """
    field = COMPARE_FIELD[str(plan.compare_by)]
    arms = plan.compare_values[:MAX_ARMS]
    totals_only = dimension is None
    default_per_arm_cap = (
        max(settings.citation_limit, 10) if totals_only else max(200, cap // len(arms))
    )
    arm_plans = {value: _arm_plan(plan, field, value) for value in arms}

    exact_matrix = bool(
        prefer_exact
        and dimension is not None
        and all(should_stratify(dimension, arm_plan) for arm_plan in arm_plans.values())
        and len(arms) * len(bucket_universe(dimension, plan))
        <= settings.exact_max_buckets
    )

    arm_totals: dict[str, int] = {}
    if exact_matrix or (adaptive_caps and not totals_only):
        totals = await asyncio.gather(*(
            fetch_total_only(client, build_query(arm_plans[value])) for value in arms
        ))
        arm_totals = dict(zip(arms, totals))

    if exact_matrix:
        shared_slots = asyncio.Semaphore(settings.exact_concurrency)
        deadline = time.monotonic() + settings.fetch_budget_s
        stratified_results = await asyncio.gather(*(
            fetch_stratified(
                client,
                build_query(arm_plans[value]),
                dimension,
                arm_plans[value],
                total_matching=arm_totals[value],
                semaphore=shared_slots,
                deadline=deadline,
            )
            for value in arms
        ))
        expected = len(bucket_universe(dimension, plan))
        # A deadline may stop unstarted cells. Never certify a partial matrix
        # as exact; fall back to bounded record fetching instead.
        exact_matrix = all(r.requests_made == expected for r in stratified_results)
    else:
        stratified_results = []

    if exact_matrix:
        results = []
    else:
        arm_caps = (
            _allocate_arm_caps(arm_totals, cap)
            if arm_totals
            else {value: default_per_arm_cap for value in arms}
        )

        async def one(value: str):
            if arm_caps[value] <= 0:
                return value, FetchResult(total_matching=arm_totals.get(value, 0))
            fetched = await fetch_flat(
                client, build_query(arm_plans[value]), cap=arm_caps[value]
            )
            return value, fetched

        results = await asyncio.gather(*(one(v) for v in arms))

    buckets: list[Bucket] = []
    arm_fetched: dict[str, int] = {}
    empty_arms: list[str] = []
    seen_ids: dict[str, set[str]] = {}
    exclusions = Exclusions()
    all_studies = []
    pages = 0

    if exact_matrix:
        for value, stratified in zip(arms, stratified_results):
            pages += stratified.requests_made
            if arm_totals[value] == 0:
                empty_arms.append(value)
            arm_fetched[value] = sum(len(s.studies) for s in stratified.strata)
            seen_ids[value] = {
                study.nct_id
                for stratum in stratified.strata
                for study in stratum.studies
            }
            for stratum in stratified.strata:
                all_studies.extend(stratum.studies)
            arm_bucket_set = bucketize.from_strata(
                stratified.strata,
                dimension,
                limit=_NO_ARM_LIMIT,
                citation_limit=settings.citation_limit,
            )
            buckets.extend(
                bucket.model_copy(update={"series": value})
                for bucket in arm_bucket_set.buckets
            )

    for value, fetched in results:
        pages += fetched.pages_fetched
        arm_totals[value] = fetched.total_matching
        arm_fetched[value] = len(fetched.studies)
        if not fetched.studies:
            empty_arms.append(value)
            continue

        seen_ids[value] = {s.nct_id for s in fetched.studies}
        all_studies.extend(fetched.studies)

        if totals_only:
            # One bar per arm. The value is the arm's own server-reported
            # total, not a count of the records fetched to cite it.
            citations = [
                Citation(nct_id=study.nct_id, brief_title=study.brief_title,
                         field_path="protocolSection.identificationModule.nctId",
                         field_value=study.nct_id, source_url=study.url)
                for study in fetched.studies[:settings.citation_limit]
            ]
            buckets.append(Bucket(
                key=value, label=value, value=fetched.total_matching,
                value_is_exact=True, sample_value=len(fetched.studies),
                citations=citations, citation_total=fetched.total_matching,
                citations_truncated=fetched.total_matching > len(citations),
                citation_sample=True,
                citation_basis=(
                    f"{len(citations)} example trials from the "
                    f"{fetched.total_matching:,} matching this arm."
                ),
            ))
            continue

        # Rank and order the arms together, below, not here. Truncating an arm
        # against only its own values keeps each arm's separate top-N, so a key
        # can be present in one arm and silently absent from another rather
        # than shown as the lower value it is.
        arm = bucketize.from_sample(
            fetched.studies,
            dimension,
            limit=_NO_ARM_LIMIT,
            citation_limit=settings.citation_limit,
        )
        exclusions.missing_dimension_value += arm.exclusions.missing_dimension_value
        for bucket in arm.buckets:
            buckets.append(bucket.model_copy(update={"series": value}))

    # Each arm arrived sorted within itself and was then concatenated, so the
    # merged list ran arm by arm. On a time axis the renderer plots that order
    # literally, and a year present only in a later arm landed after the first
    # arm's range — an axis reading "… 2024, 1981, 1977" with a cliff at the
    # join that no data supports. Rank and order the merged set once.
    truncation = None
    if dimension is not None:
        buckets, truncation = topn.apply_top_n(
            buckets, dimension, plan.limit, settings.citation_limit
        )
        buckets = topn.sort_buckets(buckets, dimension)
    else:
        # Arm order is the order the question named them.
        buckets.sort(key=lambda b: arms.index(b.key))

    # A trial can satisfy several arms, since each arm is its own query.
    counted: dict[str, int] = {}
    for ids in seen_ids.values():
        for nct_id in ids:
            counted[nct_id] = counted.get(nct_id, 0) + 1
    overlapping = sum(1 for n in counted.values() if n > 1)

    analyzed = len(counted)
    multi_label = bool(overlapping) or exact_matrix or (
        dimension is not None and dimension.semantics == "multi_label"
    )
    if totals_only:
        # Values are each arm's server-reported total, so they are exact. They
        # are not additive: the arms are independent queries and a trial
        # matching two conditions is counted in both.
        invariant = Invariant(
            kind="multi_label",
            statement=(
                "Each value is one arm's own server-reported total. Arms are "
                "independent queries and are not deduplicated against each "
                "other, so values must not be summed into a population."
            ),
            assignments_total=sum(b.value for b in buckets),
            records_analyzed=analyzed,
        )
    else:
        invariant = Invariant(
            kind="multi_label" if multi_label else "partition",
            statement=(
                "sum(data[].value) == meta.coverage.records_analyzed"
                if not multi_label
                else "a trial can match more than one comparison arm; "
                "sum(data[].value) >= records_analyzed"
            ),
            assignments_total=sum(b.value for b in buckets),
            records_analyzed=analyzed,
        )

    bucket_set = BucketSet(
        dimension=dimension.id if dimension is not None else f"compare:{plan.compare_by}",
        series_dimension=f"compare:{plan.compare_by}" if dimension is not None else None,
        buckets=buckets,
        invariant=invariant,
        exclusions=exclusions,
        truncation=truncation,
        records_analyzed=analyzed,
        counts_are_exact=totals_only or exact_matrix,
    )

    fetch_result = FetchResult(
        studies=list({s.nct_id: s for s in all_studies}.values()),
        total_matching=sum(arm_totals.values()),
        pages_fetched=pages,
        truncated_reason=None if exact_matrix else next(
            (f.truncated_reason for _, f in results if f.truncated_reason), None
        ),
        sampling_method=(
            "stratified_by_bucket" if exact_matrix
            else "first_n_by_api_relevance_order"
        ),
        exact_counts=(
            {
                f"{value}:{stratum.key}": stratum.exact_count
                for value, stratified in zip(arms, stratified_results)
                for stratum in stratified.strata
            }
            if exact_matrix else None
        ),
        duplicate_arm_matches=sum(n - 1 for n in counted.values() if n > 1),
    )

    return ComparisonResult(
        bucket_set=bucket_set,
        fetch=fetch_result,
        arm_totals=arm_totals,
        arm_fetched=arm_fetched,
        overlapping_trials=overlapping,
        empty_arms=empty_arms,
    )
