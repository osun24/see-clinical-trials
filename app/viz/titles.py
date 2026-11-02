"""Titles, and the subtitle that states what the numbers are based on.

The subtitle is the load-bearing honesty mechanism in this API. A boolean flag
a frontend has to remember to check will eventually be forgotten; a required
string sitting next to the title cannot be. ``VisualizationSpec.subtitle`` is
therefore never null and the renderer contract requires displaying it.
"""

from __future__ import annotations

from app.dimensions.registry import DIMENSIONS
from app.models.buckets import BucketSet
from app.models.plan import ResolvedPlan

_DIMENSION_PHRASES = {
    "phase": "by Phase",
    "phase_any": "by Phase (counted once per phase)",
    "overall_status": "by Recruitment Status",
    "start_year": "by Start Year",
    "completion_year": "by Completion Year",
    "lead_sponsor": "by Lead Sponsor",
    "sponsor_type": "by Sponsor Type",
    "study_type": "by Study Type",
    "country": "by Country",
    "condition": "by Condition",
    "condition_mesh": "by Condition",
    "intervention_type": "by Intervention Type",
    "intervention_name": "by Intervention",
    "enrollment_bucket": "by Enrollment Size",
    "sex": "by Eligible Sex",
    "age_group": "by Age Group",
    "results_posted": "by Results Posted",
}


def _subject(plan: ResolvedPlan) -> str:
    # The compared values carry the subject, so the per-arm filter would repeat it.
    if plan.compare_values:
        field = str(plan.compare_by)
        base = {"condition": "Trials", "drug_name": "Trials", "country": "Trials"}.get(
            field, "Trials"
        )
        if field != "condition" and plan.condition:
            return f"{plan.condition.title()} {base}"
        return base

    parts = []
    if plan.drug_name:
        parts.append(plan.drug_name.title())
    if plan.condition:
        parts.append(plan.condition.title())
    if not parts and plan.sponsor:
        parts.append(plan.sponsor)
    subject = " / ".join(parts)
    return f"{subject} Trials" if subject else "Clinical Trials"


def build_title(plan: ResolvedPlan, bucket_set: BucketSet | None) -> str:
    if plan.network_root_label and bucket_set is not None:
        return f"{plan.network_root_label} — Sponsor Relationships"

    title = _subject(plan)

    if bucket_set is not None:
        phrase = _DIMENSION_PHRASES.get(bucket_set.dimension)
        if phrase:
            title = f"{title} {phrase}"
        if bucket_set.series_dimension:
            series_phrase = _DIMENSION_PHRASES.get(bucket_set.series_dimension, "")
            if series_phrase:
                title = f"{title}, split {series_phrase}"

    if plan.compare_values:
        title = f"{title}: {' vs '.join(plan.compare_values)}"

    qualifiers = []
    if plan.overall_status:
        labels = [
            DIMENSIONS["overall_status"].label_fn(s).lower() for s in plan.overall_status
        ]
        qualifiers.append(" or ".join(labels))
    if plan.country:
        qualifiers.append(f"in {plan.country}")
    if plan.start_year and plan.end_year:
        qualifiers.append(f"{plan.start_year}-{plan.end_year}")
    elif plan.start_year:
        qualifiers.append(f"from {plan.start_year}")
    elif plan.end_year:
        qualifiers.append(f"through {plan.end_year}")

    return f"{title} ({', '.join(qualifiers)})" if qualifiers else title


def build_subtitle(
    *,
    total_matching: int,
    records_analyzed: int,
    is_sample: bool,
    counts_are_exact: bool,
    sampling_method: str,
    excluded_missing: int = 0,
) -> str:
    """One sentence a viewer cannot miss, stating the basis of the numbers."""
    if total_matching == 0:
        return "No trials matched these filters."
    if counts_are_exact:
        head = (
            f"Exact counts across all {total_matching:,} matching trials; "
            f"citations drawn from a sample."
        )
    elif not is_sample:
        head = f"Based on all {total_matching:,} matching trials."
    else:
        order = (
            "by API relevance order"
            if sampling_method == "first_n_by_api_relevance_order"
            else sampling_method.replace("_", " ")
        )
        head = (
            f"Based on {records_analyzed:,} of {total_matching:,} matching trials "
            f"(first {records_analyzed:,} {order}); counts are approximate."
        )

    if excluded_missing:
        head += f" {excluded_missing:,} trials were excluded for having no value."
    return head
