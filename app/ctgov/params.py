"""Turn a resolved plan into ClinicalTrials.gov query parameters."""

from __future__ import annotations

from datetime import datetime, timezone

from pydantic import BaseModel, Field

from app.dimensions.registry import ALL_API_FIELDS
from app.models.plan import ResolvedPlan


class CtgovQuerySpec(BaseModel):
    """Validated outbound query. ``params`` is sent verbatim."""

    model_config = {"extra": "forbid"}

    params: dict[str, str] = Field(default_factory=dict)
    fields_mask: tuple[str, ...] = ALL_API_FIELDS

    def with_overrides(self, extra: dict[str, str]) -> CtgovQuerySpec:
        """Layer bucket-scoped filters on top, merging advanced expressions with AND."""
        merged = dict(self.params)
        for key, value in extra.items():
            if key == "filter.advanced" and merged.get("filter.advanced"):
                merged[key] = f"{merged['filter.advanced']} AND {value}"
            else:
                merged[key] = value
        return CtgovQuerySpec(params=merged, fields_mask=self.fields_mask)


def _date_range_expression(start_year: int | None, end_year: int | None) -> str | None:
    if start_year is None and end_year is None:
        return None
    low = f"{start_year}-01-01" if start_year else "MIN"
    high = f"{end_year}-12-31" if end_year else "MAX"
    return f"AREA[StartDate]RANGE[{low},{high}]"


def build_query(plan: ResolvedPlan) -> CtgovQuerySpec:
    """Map plan filters onto the API's search areas and filters.

    Each filter lands in its own parameter, so a drug plus a condition narrow
    together rather than competing. The free-text terms are sent as
    ``query.term`` to keep relevance ranking meaningful.
    """
    params: dict[str, str] = {}
    advanced: list[str] = []

    if plan.drug_name:
        params["query.intr"] = plan.drug_name
    if plan.condition:
        params["query.cond"] = plan.condition
    if plan.sponsor:
        params["query.spons"] = plan.sponsor
    if plan.country:
        params["query.locn"] = plan.country
    if plan.search_terms:
        params["query.term"] = plan.search_terms

    if plan.overall_status:
        params["filter.overallStatus"] = "|".join(plan.overall_status)

    if plan.trial_phase:
        phases = " OR ".join(f"AREA[Phase]({p})" for p in plan.trial_phase)
        advanced.append(f"({phases})" if len(plan.trial_phase) > 1 else phases)

    date_expr = _date_range_expression(plan.start_year, plan.end_year)
    if date_expr:
        advanced.append(date_expr)

    if advanced:
        params["filter.advanced"] = " AND ".join(advanced)

    # A request with no filter at all would scan the entire registry; fall back
    # to the user's own words rather than returning a meaningless global count.
    if not params:
        params["query.term"] = plan.search_terms or plan.interpretation

    return CtgovQuerySpec(params=params, fields_mask=ALL_API_FIELDS)


def year_universe(plan: ResolvedPlan, fallback_span: int = 15) -> list[str]:
    """Bucket keys for a temporal dimension's exact-count fetch."""
    this_year = datetime.now(timezone.utc).year
    start = plan.start_year or (this_year - fallback_span)
    end = min(plan.end_year or this_year, this_year)
    if start > end:
        start, end = end, start
    return [str(y) for y in range(start, end + 1)]
