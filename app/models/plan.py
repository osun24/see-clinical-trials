"""What the model is allowed to decide, and what it becomes after validation.

``QueryPlan`` is the raw Structured Outputs result. Its *shape* is guaranteed by
the strict JSON schema — an invalid dimension or chart type is structurally
impossible rather than something to catch later. What strict mode does NOT
enforce is numeric range (``minimum``/``maximum`` are ignored) or any constraint
spanning two fields, so all clamping and combination validation happens in
``app.interpret.merge`` and produces a ``ResolvedPlan`` plus an audit trail of
what was changed.

The model never produces counts, labels, titles, excerpts, or the final chart
type. It produces intent.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

#: Filter slots that can be split into side-by-side comparison arms.
CompareBy = Literal["sponsor", "condition", "drug_name", "country"]

Intent = Literal[
    "count_total",   # a single number: "how many trials for X?"
    "distribution",  # spread across one dimension
    "trend",         # change over time
    "comparison",    # one dimension split by another
    "ranking",       # top-N of a high-cardinality dimension
    "composition",   # parts of a whole
    "relationship",  # co-occurrence between two dimensions
    "lookup",        # list specific trials
]

ChartHint = Literal[
    "stat",
    "bar_chart",
    "grouped_bar",
    "stacked_bar",
    "time_series",
    "pie_chart",
    "scatter_plot",
    "network_graph",
]

# Mirrors registry.ENABLED_DIMENSIONS. Kept as an explicit Literal because the
# strict schema needs a closed enum at class-definition time; a registry drift
# test keeps the two in sync.
PlanDimension = Literal[
    "phase",
    "phase_any",
    "overall_status",
    "start_year",
    "completion_year",
    "lead_sponsor",
    "sponsor_type",
    "study_type",
    "country",
    "condition",
    "condition_mesh",
    "intervention_type",
    "enrollment_bucket",
    "duration_bucket",
    "sex",
    "age_group",
    "results_posted",
]


class QueryPlan(BaseModel):
    """The model's interpretation of the question. Structured Outputs schema."""

    model_config = {"extra": "forbid"}

    is_clinical_trials_question: bool = Field(
        description="False if the question is not about clinical trials at all."
    )
    intent: Intent = Field(description="What kind of answer the question asks for.")
    interpretation: str = Field(
        description="One sentence restating the question as you understood it."
    )

    group_by: PlanDimension | None = Field(
        default=None, description="Primary grouping dimension. Null for a single total."
    )
    series_by: PlanDimension | None = Field(
        default=None, description="Secondary dimension to split each group by. Usually null."
    )
    chart_hint: ChartHint | None = Field(
        default=None,
        description="Suggested chart type. Advisory only; the server overrides it if the data shape cannot support it.",
    )
    limit: int | None = Field(
        default=None, description="Requested number of categories before collapsing to 'Other'."
    )

    search_terms: str | None = Field(
        default=None,
        description="Free-text terms for a general search, when no specific field applies.",
    )
    drug_name: str | None = Field(default=None, description="Intervention or drug name.")
    condition: str | None = Field(default=None, description="Condition or disease.")
    sponsor: str | None = Field(default=None, description="Sponsor organisation name.")
    country: str | None = Field(default=None, description="Country with a trial site.")
    trial_phase: list[str] | None = Field(
        default=None, description="Phase filter, e.g. ['PHASE2','PHASE3']."
    )
    overall_status: list[str] | None = Field(
        default=None, description="Status filter, e.g. ['RECRUITING']."
    )
    start_year: int | None = Field(default=None, description="Earliest trial start year.")
    end_year: int | None = Field(default=None, description="Latest trial start year.")

    compare_by: CompareBy | None = Field(
        default=None,
        description=(
            "Set when the question compares two or more named things side by side "
            "('our pipeline versus Merck's', 'Keytruda vs Opdivo'). Names which filter "
            "the comparison splits on."
        ),
    )
    compare_values: list[str] | None = Field(
        default=None,
        description=(
            "The two to five values to compare, e.g. ['Pfizer', 'Merck']. Each becomes "
            "one series. Requires compare_by."
        ),
    )

    assumptions: list[str] = Field(
        default_factory=list,
        description="Any interpretation you had to guess at, e.g. what 'recent' means.",
    )


class ResolvedPlan(BaseModel):
    """A ``QueryPlan`` after clamping and cross-field validation."""

    model_config = {"extra": "forbid"}

    is_clinical_trials_question: bool
    intent: Intent
    interpretation: str
    group_by: str | None = None
    series_by: str | None = None
    chart_hint: str | None = None
    network_root_label: str | None = None
    limit: int
    search_terms: str | None = None
    drug_name: str | None = None
    condition: str | None = None
    sponsor: str | None = None
    country: str | None = None
    trial_phase: list[str] = Field(default_factory=list)
    overall_status: list[str] = Field(default_factory=list)
    start_year: int | None = None
    end_year: int | None = None
    compare_by: str | None = None
    compare_values: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)

    #: Every clamp, drop, or override applied to the model's plan.
    adjustments: list[str] = Field(default_factory=list)
    #: Structured request fields that overrode a model inference.
    overridden_fields: list[dict[str, object]] = Field(default_factory=list)
