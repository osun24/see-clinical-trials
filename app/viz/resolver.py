"""Choose the final chart type from the realised data.

The model supplies a ``chart_hint``; this module decides. That split is
deliberate. The decision depends on facts that do not exist when the model is
called — how many buckets actually came back after top-N collapsing, whether
the dimension double-counts trials, whether anything matched at all — so the
hint is advisory input to a deterministic resolver, never the output.

Every override is recorded and surfaced in the response.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.dimensions.registry import DIMENSIONS, DimensionDef
from app.models.buckets import BucketSet
from app.models.plan import ResolvedPlan

MAX_PIE_SLICES = 6
MIN_PIE_SLICES = 2
MAX_PIE_MISSING_SHARE = 0.4
MIN_TIME_SERIES_POINTS = 3
MAX_GRID_CELLS = 60
MAX_GRAPH_NODES = 60
#: Overlapping lines stop being distinguishable well before this.
MAX_SERIES_LINES = 6
WIDE_LABEL_DIMENSIONS = {"lead_sponsor", "condition", "condition_mesh", "country", "intervention_name"}


@dataclass
class VizDecision:
    """Final chart policy decision plus any user-visible overrides."""

    type: str
    rationale: str
    needed: bool = True
    overrides: list[dict[str, str]] = field(default_factory=list)
    orientation: str | None = None
    drop_series: bool = False

    def override(self, requested: str, applied: str, reason: str) -> None:
        """Record why an advisory chart hint could not be used."""
        self.overrides.append(
            {"requested": requested, "applied": applied, "reason": reason}
        )


def _orientation(dimension: DimensionDef | None, bucket_count: int) -> str:
    if dimension is not None and dimension.id in WIDE_LABEL_DIMENSIONS:
        return "horizontal"
    return "horizontal" if bucket_count > 8 else "vertical"


def resolve(
    plan: ResolvedPlan,
    bucket_set: BucketSet | None,
    *,
    records_fetched: int,
) -> VizDecision:
    """Choose a truthful visualization from intent and realized bucket shape."""
    hint = plan.chart_hint
    dimension = DIMENSIONS.get(bucket_set.dimension) if bucket_set else None
    series_dimension = (
        DIMENSIONS.get(bucket_set.series_dimension or "") if bucket_set else None
    )
    # Comparison arms are a synthetic series with no registry entry, but they
    # still need the grouped/stacked treatment rather than being flattened.
    is_comparison = bool(
        bucket_set and (bucket_set.series_dimension or "").startswith("compare:")
    )

    primary = [b for b in (bucket_set.buckets if bucket_set else []) if b.series is None or True]
    distinct_groups = len({b.key for b in primary})
    distinct_series = len({b.series for b in primary if b.series is not None})

    # 1. Nothing matched. Not an error — "no trials match that" answers the
    #    question, so it is a 200 with an explanation.
    if records_fetched == 0:
        return VizDecision(
            type="none",
            needed=False,
            rationale="No trials matched the filters, so there is nothing to plot.",
        )

    # 2. Not a clinical-trials question at all.
    if not plan.is_clinical_trials_question:
        return VizDecision(
            type="none",
            needed=False,
            rationale="The question is not about clinical trials.",
        )

    # 3. A single number.
    if plan.intent == "count_total" or (plan.group_by is None and plan.intent != "lookup"):
        decision = VizDecision(
            type="stat",
            needed=False,
            rationale="The question asks for a single total, which needs a number rather than a chart.",
        )
        if hint and hint not in ("stat", None):
            decision.override(hint, "stat", "The question asks for one total, not a distribution.")
        return decision

    # 4. A list of specific trials has no supported visualization. The API
    #    returns an explicit prose explanation instead of disguising the rows
    #    as a chart.
    if plan.intent == "lookup":
        decision = VizDecision(
            type="none",
            needed=False,
            rationale="The question asks for a trial list, which is not a supported visualization.",
        )
        if hint:
            decision.override(hint, "none", "The question asks to list trials, not to aggregate them.")
        return decision

    assert bucket_set is not None and dimension is not None

    # 5. Time.
    if dimension.datatype == "temporal":
        if distinct_groups >= MIN_TIME_SERIES_POINTS:
            # A line per series is only readable up to a handful of series; 40
            # overlapping lines is a hairball, not a trend. Drop the series and
            # say so rather than emitting something nobody can read.
            if (series_dimension is not None or is_comparison) and distinct_series > MAX_SERIES_LINES:
                decision = VizDecision(
                    type="time_series",
                    rationale=(
                        f"{distinct_groups} time buckets, but {distinct_series} "
                        f"series is too many lines to read; showing the overall trend."
                    ),
                    orientation=None,
                    drop_series=True,
                )
                series_name = (
                    series_dimension.id if series_dimension else bucket_set.series_dimension
                )
                decision.override(
                    f"time_series split by {series_name}",
                    "time_series",
                    f"{distinct_series} lines exceeds the {MAX_SERIES_LINES}-line "
                    f"limit, so the {series_name} breakdown was dropped.",
                )
                return decision
            decision = VizDecision(
                type="time_series",
                rationale=f"{distinct_groups} time buckets show a trend over time.",
                orientation=None,
            )
            if hint and hint not in ("time_series", "bar_chart"):
                decision.override(hint, "time_series", f"{dimension.id} is a time dimension.")
            return decision
        if distinct_groups == 2:
            # Two points joined by a line imply a trajectory that two
            # observations cannot support.
            decision = VizDecision(
                type="bar_chart",
                rationale="Only two time buckets, which a line would overstate as a trend.",
                orientation="vertical",
            )
            if hint == "time_series":
                decision.override(hint, "bar_chart", "Two points are not enough to draw a trend.")
            return decision
        return VizDecision(
            type="stat",
            needed=False,
            rationale="Only one time bucket has data, so there is no trend to show.",
        )

    # 6/7. Relationship between two dimensions.
    if plan.intent == "relationship" and plan.graph is not None:
        return VizDecision(
            type="network_graph",
            rationale="The validated graph plan asks how evidence-backed entities relate through trials.",
        )

    if plan.intent == "relationship" and series_dimension is not None:
        nodes = distinct_groups + distinct_series
        if nodes > MAX_GRAPH_NODES:
            decision = VizDecision(
                type="bar_chart",
                rationale=(
                    f"{nodes} nodes is past the point a graph stays readable; "
                    "showing the primary dimension only."
                ),
                orientation=_orientation(dimension, distinct_groups),
                drop_series=True,
            )
            decision.override(
                hint or "network_graph",
                "bar_chart",
                f"{nodes} nodes exceeds the {MAX_GRAPH_NODES}-node limit.",
            )
            return decision
        return VizDecision(
            type="network_graph",
            rationale="The question is about how two dimensions co-occur.",
        )

    # 8. Parts of a whole.
    if hint == "pie_chart":
        missing_share = 0.0
        total = sum(b.value for b in bucket_set.buckets) or 1
        missing_share = sum(b.value for b in bucket_set.buckets if b.is_missing) / total

        if dimension.semantics == "multi_label":
            # Slices that do not sum to the whole are a lie about proportion.
            decision = VizDecision(
                type="bar_chart",
                rationale="Counts overlap between categories, so proportions of a whole would mislead.",
                orientation=_orientation(dimension, distinct_groups),
            )
            decision.override(
                "pie_chart",
                "bar_chart",
                f"A trial can appear in several {dimension.id} buckets, so the slices would not sum to the total.",
            )
            return decision
        if not (MIN_PIE_SLICES <= distinct_groups <= MAX_PIE_SLICES):
            decision = VizDecision(
                type="bar_chart",
                rationale=f"{distinct_groups} categories is too many to read as slices.",
                orientation=_orientation(dimension, distinct_groups),
            )
            decision.override(
                "pie_chart",
                "bar_chart",
                f"A pie chart needs {MIN_PIE_SLICES}-{MAX_PIE_SLICES} categories; this has {distinct_groups}.",
            )
            return decision
        if missing_share > MAX_PIE_MISSING_SHARE:
            decision = VizDecision(
                type="bar_chart",
                rationale="Too much of the data is unreported to show as proportions.",
                orientation=_orientation(dimension, distinct_groups),
            )
            decision.override(
                "pie_chart",
                "bar_chart",
                f"{missing_share:.0%} of trials have no value for {dimension.id}.",
            )
            return decision
        return VizDecision(
            type="pie_chart",
            rationale=f"{distinct_groups} mutually exclusive categories read well as proportions.",
        )

    # 9. Two dimensions, or a comparison.
    if (series_dimension is not None or is_comparison) and distinct_series > 0:
        cells = distinct_groups * distinct_series
        if cells <= MAX_GRID_CELLS:
            # A comparison is read side by side; stacking would hide the very
            # difference the question asked about.
            chart = (
                "stacked_bar"
                if plan.intent == "composition" and not is_comparison
                else "grouped_bar"
            )
            return VizDecision(
                type=chart,
                rationale=f"{distinct_groups} groups split by {distinct_series} series.",
                orientation=_orientation(dimension, distinct_groups),
            )
        decision = VizDecision(
            type="bar_chart",
            rationale=f"{cells} cells is too dense to compare; showing the primary dimension only.",
            orientation=_orientation(dimension, distinct_groups),
            drop_series=True,
        )
        decision.override(
            hint or "grouped_bar",
            "bar_chart",
            f"{distinct_groups}x{distinct_series} cells exceeds the {MAX_GRID_CELLS}-cell limit.",
        )
        return decision

    # 10. Default.
    decision = VizDecision(
        type="bar_chart",
        rationale=f"A count across {distinct_groups} categories compares best as bars.",
        orientation=_orientation(dimension, distinct_groups),
    )
    if hint and hint not in ("bar_chart", None):
        decision.override(hint, "bar_chart", f"{hint} does not suit a plain categorical count.")
    return decision
