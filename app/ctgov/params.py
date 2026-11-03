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
                merged[key] = f"({merged['filter.advanced']}) AND ({value})"
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
    spec = _build_query(plan)
    if plan.analysis is not None:
        from app.analysis.engine import validate
        spec = spec.model_copy(update={"fields_mask": tuple(validate(plan.analysis)["fields_mask"])})
    return spec


def _build_query(plan: ResolvedPlan) -> CtgovQuerySpec:
    """Map plan filters onto the API's search areas and filters.

    Each filter lands in its own parameter, so a drug plus a condition narrow
    together rather than competing. The free-text terms are sent as
    ``query.term`` to keep relevance ranking meaningful.
    """
    if plan.search is not None:
        search = plan.search
        params = search.model_dump(by_alias=True, exclude_none=True, exclude={"start_date", "end_date", "date_field"})
        spec = CtgovQuerySpec(params=params)
        if plan.trial_phase:
            phases = " OR ".join(f"AREA[Phase]({p})" for p in plan.trial_phase)
            spec = spec.with_overrides({"filter.advanced": f"({phases})"})
        if plan.overall_status:
            statuses = " OR ".join(f"AREA[OverallStatus]({status})" for status in plan.overall_status)
            spec = spec.with_overrides({"filter.advanced": f"({statuses})"})
        if search.start_date or search.end_date:
            expression = f"AREA[{search.date_field}]RANGE[{search.start_date or 'MIN'},{search.end_date or 'MAX'}]"
            spec = spec.with_overrides({"filter.advanced": expression})
        if not spec.params and not plan.compare_values:
            from app.models.errors import ServiceError
            raise ServiceError("query_not_interpretable", "The generated search has no constraints.")
        return spec

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
    """Bucket keys for a temporal dimension's exact-count fetch.

    ``resolve`` moves a search-path plan's period into ``search.start_date`` /
    ``end_date`` and nulls the plan's own years, so reading only the latter
    would silently substitute a default 15-year window for the period the
    question actually asked for.
    """
    this_year = datetime.now(timezone.utc).year
    start_year, end_year = plan.start_year, plan.end_year
    if plan.search is not None:
        if start_year is None and plan.search.start_date is not None:
            start_year = plan.search.start_date.year
        if end_year is None and plan.search.end_date is not None:
            end_year = plan.search.end_date.year
    start = start_year or (this_year - fallback_span)
    end = min(end_year or this_year, this_year)
    if start > end:
        start, end = end, start
    return [str(y) for y in range(start, end + 1)]
