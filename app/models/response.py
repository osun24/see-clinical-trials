"""The public response schema for POST /query."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from app.models.buckets import Exclusions, Invariant, Truncation
from app.models.viz import VisualizationSpec


class ComparisonArmCoverage(BaseModel):
    """Coverage for one independently queried comparison arm."""

    model_config = {"extra": "forbid"}

    total_matching: int
    records_fetched: int
    is_sample: bool


class Coverage(BaseModel):
    """What the numbers are actually based on.

    ClinicalTrials.gov cannot aggregate every filtered query server-side, so
    counts may be computed locally over a bounded fetch. This object records
    how much of the matching universe the response represents.
    """

    model_config = {"extra": "forbid"}

    total_matching_studies: int
    records_fetched: int
    records_analyzed: int
    is_sample: bool
    coverage_ratio: float
    sampling_method: Literal["census", "first_n_by_api_relevance_order", "stratified_by_bucket"]
    sampling_bias_risk: Literal["none", "low", "high"]
    fetch_cap: int
    pages_fetched: int
    truncated_reason: Literal["fetch_cap_reached", "time_budget_exceeded"] | None = None
    counts_are_exact: bool = False
    excluded: Exclusions = Field(default_factory=Exclusions)
    comparison_arms: dict[str, ComparisonArmCoverage] = Field(default_factory=dict)


class QueryInterpretation(BaseModel):
    """Effective filters and defensive adjustments to the model plan."""

    model_config = {"extra": "forbid"}

    model_interpretation: str
    filters_applied: dict[str, str] = Field(default_factory=dict)
    assumptions: list[str] = Field(default_factory=list)
    plan_adjustments: list[str] = Field(default_factory=list)


class Notes(BaseModel):
    """Human-readable caveats that a client should display with the result."""

    model_config = {"extra": "forbid"}

    coverage_warning: str | None = None
    counting_rule: str | None = None
    overrides: list[dict[str, str]] = Field(default_factory=list)
    general: list[str] = Field(default_factory=list)


class Meta(BaseModel):
    """Provenance, coverage, invariants, and decision details for an answer."""

    model_config = {"extra": "forbid"}

    analysis: dict | None = None
    query_interpretation: QueryInterpretation
    intent: str
    group_by: str | None = None
    series_by: str | None = None
    measure: str = "trial_count"
    unit: str = "trials"
    sort: dict[str, str] | None = None
    invariant: Invariant
    coverage: Coverage
    truncation: Truncation | None = None

    visualization_needed: bool = True
    visualization_rationale: str = ""

    answer_text: str | None = None
    suggestions: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    notes: Notes = Field(default_factory=Notes)

    citation_policy: str = (
        "Citations are extracted mechanically from the ClinicalTrials.gov API response. "
        "Field values and excerpts are verbatim; the language model never writes them."
    )
    source: str = "clinicaltrials.gov"
    api_version: str = "2.0.5"
    data_timestamp: str | None = None
    request_id: str | None = None
    elapsed_ms: int | None = None


class AnswerResponse(BaseModel):
    """Top-level successful response returned by ``POST /query``."""

    model_config = {"extra": "forbid"}

    visualization: VisualizationSpec
    meta: Meta
