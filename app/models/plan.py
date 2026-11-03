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
from datetime import date
from app.analysis.models import AnalysisPlan

from pydantic import BaseModel, Field, model_validator

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

GraphEntity = Literal[
    "intervention_name",
    "lead_sponsor",
    "condition_mesh",
    "country",
    "trial",
]


class GraphLevel(BaseModel):
    """One evidence-backed entity level below a graph's root."""

    model_config = {"extra": "forbid"}

    entity: GraphEntity
    relationship: Literal["co_occurs_in_trial"] = "co_occurs_in_trial"


class GraphPlan(BaseModel):
    """A constrained semantic graph proposal; never contains generated data."""

    model_config = {"extra": "forbid"}

    #: ``population`` roots the graph in the search itself rather than a named
    #: subject: "sponsors and drugs for NSCLC trials" relates two recorded
    #: entity types to each other across every matching trial, so there is no
    #: single root node and ``root_value`` is unused.
    root_entity: Literal["person", "search_subject", "population"]
    root_value: str = ""
    levels: list[GraphLevel]
    edge_measure: Literal["trial_count"] = "trial_count"
    node_limit: int = 30

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


class SearchRequest(BaseModel):
    """Supported upstream search parameters; transport policy remains server-owned."""

    model_config = {"extra": "forbid", "populate_by_name": True}
    intervention: str | None = Field(default=None, alias="query.intr", description="Only an explicitly requested drug/intervention. Never a disease or person name.")
    condition: str | None = Field(default=None, alias="query.cond", description="Requested disease/condition. Do not also put it in intervention.")
    sponsor: str | None = Field(default=None, alias="query.spons")
    location: str | None = Field(default=None, alias="query.locn")
    term: str | None = Field(default=None, alias="query.term", description="Full-text search for a named person/subject or otherwise unrepresented concept.")
    advanced: str | None = Field(default=None, alias="filter.advanced")
    status: str | None = Field(default=None, alias="filter.overallStatus")
    date_field: Literal["StartDate", "CompletionDate"] = "StartDate"
    start_date: date | None = None
    end_date: date | None = None

    @model_validator(mode="after")
    def validate_search(self):
        for name in ("intervention", "condition", "sponsor", "location", "term", "advanced", "status"):
            value = getattr(self, name)
            if value is not None and (not value.strip() or len(value) > 2000 or any(ord(c) < 32 for c in value)):
                raise ValueError(f"Invalid search parameter: {name}")
        if self.start_date and self.end_date and self.start_date > self.end_date:
            raise ValueError("Search date bounds are reversed")
        return self


class QueryPlan(BaseModel):
    """The model's interpretation of the question. Structured Outputs schema."""

    model_config = {"extra": "forbid"}

    # Any entry here refuses the whole request, so the field needs a hard
    # boundary: a planner that used it for commentary ("...which is supported")
    # turned an answerable question into a refusal.
    unsupported_requirements: list[str] = Field(
        default_factory=list,
        description=(
            "Requirements of the question that CANNOT be met from trial-record "
            "fields and deterministic calculations — medical classification, "
            "drug class or mechanism, ownership, approval status, efficacy "
            "judgments. Any entry here refuses the entire request, so leave it "
            "EMPTY whenever the question is answerable, including when you also "
            "supply an analysis or search. Never use it for notes, caveats, "
            "assumptions, restatements, or to say that something IS supported: "
            "assumptions belong in assumptions."
        ),
    )
    analysis: AnalysisPlan | None = None
    search: SearchRequest | None = None
    category_selection: str | None = None

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
    graph: GraphPlan | None = Field(
        default=None,
        description=(
            "Constrained entity path for relationship questions. Contains semantics only; "
            "the server derives all nodes and edges from trial records."
        ),
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

    unsupported_requirements: list[str] = Field(default_factory=list)
    analysis: AnalysisPlan | None = None
    search: SearchRequest | None = None
    category_selection: str | None = None

    is_clinical_trials_question: bool
    intent: Intent
    interpretation: str
    group_by: str | None = None
    series_by: str | None = None
    chart_hint: str | None = None
    graph: GraphPlan | None = None
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

    #: Every clamp or defensive correction applied to the model's plan.
    adjustments: list[str] = Field(default_factory=list)
