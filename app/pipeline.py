"""The request pipeline, in one readable place.

    QueryRequest -> QueryPlan -> ResolvedPlan -> CtgovQuerySpec
      -> FetchResult -> BucketSet -> VisualizationSpec -> AnswerResponse
"""

from __future__ import annotations

import time
import uuid
from typing import Any

from app.aggregate import bucketize, topn
from app.aggregate.keys import bucket_keys, display_label, MISSING_KEY
from app.config import settings
from app.ctgov.client import CtgovClient
from app.ctgov.compare import fetch_comparison, should_compare
from app.ctgov.fetch import FetchResult, fetch_flat
from app.ctgov.params import build_query
from app.ctgov.stratified import (
    bucket_universe,
    fetch_stratified,
    should_stratify,
    to_fetch_result,
)
from app.dimensions.registry import DIMENSIONS
from app.interpret.merge import resolve
from app.interpret.planner import interpret
from app.models.buckets import Bucket, BucketSet, Exclusions, Invariant
from app.models.errors import ServiceError
from app.models.plan import ResolvedPlan
from app.models.request import QueryRequest
from app.models.response import (
    AnswerResponse,
    Coverage,
    Meta,
    Notes,
    QueryInterpretation,
)
from app.models.viz import Encoding, VisualizationSpec
from app.viz import encoders, titles
from app.viz.resolver import resolve as resolve_viz

BROAD_QUERY_THRESHOLD = 50_000


#: Failures worth re-planning: the plan was defective, not the question
#: unsupported. A capability refusal is deliberately absent — see
#: ``settings.retry_capability_refusals``.
_RETRYABLE_PLAN_ERRORS = frozenset({
    "query_not_interpretable",
    "upstream_rejected_query",
})

_EMPTY_RESULT_FEEDBACK = (
    "That plan matched no trials at all. The filters were probably too narrow or "
    "placed in the wrong field — a drug name in the sponsor slot, several drug "
    "names in one field, or a constraint repeated locally that the search already "
    "applies. Re-read the question and produce simpler, correctly assigned filters."
)


def _should_retry_error(exc: ServiceError) -> bool:
    if exc.code in _RETRYABLE_PLAN_ERRORS:
        return True
    return exc.code == "unsupported_query" and settings.retry_capability_refusals


async def _interpret_resolved(
    request: QueryRequest,
    *,
    prior: Any = None,
    feedback: str | None = None,
) -> tuple[ResolvedPlan, Any]:
    """Plan and validate, with one in-attempt repair. Returns the raw plan too.

    The raw plan is handed back so an outer retry can show the planner what it
    produced last time; without it a resample at temperature 0 just repeats
    itself.
    """
    # A first attempt has nothing to show the planner, so it is an ordinary
    # call; only a retry carries the previous plan and why it failed.
    raw = (
        await interpret(request, previous=prior, feedback=feedback)
        if feedback
        else await interpret(request)
    )
    try:
        resolved = resolve(raw, request)
    except ServiceError as exc:
        if exc.code != "query_not_interpretable":
            raise
        repaired = await interpret(request, previous=raw, feedback=exc.message)
        resolved = resolve(repaired, request)
        resolved.adjustments.append("The planning model revised its plan once after deterministic validation failed: " + exc.message)
        return resolved, repaired
    return resolved, raw


async def _gather_data(
    client: CtgovClient, resolved: ResolvedPlan, cap: int
) -> tuple[FetchResult, BucketSet | None, Any]:
    spec = build_query(resolved)

    dimension = DIMENSIONS.get(resolved.group_by) if resolved.group_by else None
    series_dimension = DIMENSIONS.get(resolved.series_by) if resolved.series_by else None

    # "Compare A against B" cannot be one query; each arm is its own. This is
    # not gated on having a grouping dimension: "compare the number of trials
    # across A, B and C" compares the arm totals themselves. Requiring a
    # dimension dropped the arms and left the base query unconstrained, so the
    # answer became a count of the entire registry.
    if should_compare(resolved):
        comparison = await fetch_comparison(
            client,
            resolved,
            dimension,
            cap=cap,
            prefer_exact=True,
            adaptive_caps=True,
        )
        return comparison.fetch, comparison.bucket_set, comparison

    if dimension is not None and should_stratify(dimension, resolved):
        # Establish the universe size first, then count each bucket exactly.
        probe = await fetch_flat(client, spec, cap=1)
        # A census is exact too, and returns whole records rather than a
        # per-bucket slice, so it is the better buy whenever the population
        # fits and costs no more requests. Stratify when the census cannot be
        # exact at all, or when one request per bucket is the cheaper route:
        # bucket requests run concurrently, page requests are token-chained.
        pages_needed = -(-min(probe.total_matching, cap) // settings.page_size)
        buckets_needed = len(bucket_universe(dimension, resolved))
        if probe.total_matching > cap or buckets_needed < pages_needed:
            stratified = await fetch_stratified(
                client, spec, dimension, resolved, total_matching=probe.total_matching
            )
        else:
            stratified = None
        if stratified is not None and stratified.strata:
            fetch_result = to_fetch_result(stratified)
            bucket_set = bucketize.from_strata(
                stratified.strata,
                dimension,
                limit=resolved.limit,
                citation_limit=settings.citation_limit,
            )
            return fetch_result, bucket_set, None

    fetch_result = await fetch_flat(client, spec, cap=cap)

    if dimension is None:
        return fetch_result, None, None

    bucket_set = _bucket_sample(fetch_result, resolved, dimension, series_dimension)
    return fetch_result, bucket_set, None


def _bucket_sample(fetch_result, resolved, dimension, series_dimension):
    """Bucket fetched records, bounding both axes when a graph is intended."""
    bucket_set = bucketize.from_sample(
        fetch_result.studies,
        dimension,
        series_dimension=series_dimension,
        limit=resolved.limit,
        citation_limit=settings.citation_limit,
    )
    if series_dimension is None or resolved.intent != "relationship":
        return bucket_set

    # Both axes of a co-occurrence graph are nodes, so the series needs its own
    # budget; without one the node count blows past the readable limit and the
    # resolver downgrades the requested network to a bar chart.
    trimmed, dropped = topn.limit_series(bucket_set.buckets, resolved.limit)
    if not dropped:
        return bucket_set
    return bucket_set.model_copy(update={
        "buckets": trimmed,
        "series_truncated": dropped,
    })


def _coverage(
    fetch_result: FetchResult,
    bucket_set: BucketSet | None,
    fetch_cap: int,
    comparison: Any = None,
) -> Coverage:
    analyzed = bucket_set.records_analyzed if bucket_set else len(fetch_result.studies)
    total = max(fetch_result.total_matching, 1)
    exact = bool(bucket_set and bucket_set.counts_are_exact)
    is_sample = fetch_result.is_sample and not exact

    return Coverage(
        total_matching_studies=fetch_result.total_matching,
        records_fetched=len(fetch_result.studies),
        records_analyzed=analyzed,
        is_sample=is_sample,
        coverage_ratio=round(len(fetch_result.studies) / total, 4),
        sampling_method=fetch_result.sampling_method,
        sampling_bias_risk=(
            "none"
            if not fetch_result.is_sample
            else ("low" if exact else "high")
        ),
        fetch_cap=fetch_cap,
        pages_fetched=fetch_result.pages_fetched,
        truncated_reason=fetch_result.truncated_reason,
        counts_are_exact=exact,
        excluded=bucket_set.exclusions if bucket_set else Exclusions(),
        comparison_arms=(
            {
                name: {
                    "total_matching": total,
                    "records_fetched": comparison.arm_fetched.get(name, 0),
                    "is_sample": (
                        not exact
                        and comparison.arm_fetched.get(name, 0) < total
                    ),
                }
                for name, total in comparison.arm_totals.items()
            }
            if comparison is not None else {}
        ),
    )


def _build_visualization(
    resolved: ResolvedPlan,
    bucket_set: BucketSet | None,
    fetch_result: FetchResult,
    decision_type: str,
    orientation: str | None,
) -> VisualizationSpec:
    title = titles.build_title(resolved, bucket_set)

    if decision_type == "none":
        return VisualizationSpec(type="none", title=title, data=[])

    if decision_type == "stat":
        if bucket_set is None and fetch_result.studies:
            from app.models.buckets import Citation
            citations = [Citation(nct_id=s.nct_id, brief_title=s.brief_title,
                field_path="protocolSection.identificationModule.nctId", field_value=s.nct_id, source_url=s.url)
                for s in fetch_result.studies[:settings.citation_limit]]
            bucket_set = BucketSet(dimension="total", buckets=[Bucket(key="total", label="Matching trials",
                value=fetch_result.total_matching, citations=citations, citation_total=fetch_result.total_matching)],
                invariant=Invariant(kind="partition", statement="Single API-reported total."), records_analyzed=len(fetch_result.studies))
        encoding, data = encoders.encode_stat(
            bucket_set,
            total=fetch_result.total_matching,
            exact=True,  # totalCount is the API's own count, always exact
        )
    elif decision_type == "time_series":
        encoding, data = encoders.encode_time_series(bucket_set)  # type: ignore[arg-type]
    elif decision_type == "pie_chart":
        encoding, data = encoders.encode_pie(bucket_set)  # type: ignore[arg-type]
    elif decision_type == "network_graph":
        if resolved.graph is not None:
            encoding, graph = encoders.encode_semantic_graph(
                fetch_result.studies,
                resolved.graph,
                citation_limit=settings.citation_limit,
            )
        else:
            encoding, graph = encoders.encode_network(bucket_set)  # type: ignore[arg-type]
        return VisualizationSpec(
            type="network_graph",
            title=title,
            encoding=encoding,
            data=graph,
        )
    elif decision_type in ("grouped_bar", "stacked_bar"):
        encoding, data = encoders.encode_bar(
            bucket_set,  # type: ignore[arg-type]
            orientation=orientation or "vertical",
            stack="normalize" if decision_type == "stacked_bar" else False,
        )
    else:
        encoding, data = encoders.encode_bar(
            bucket_set,  # type: ignore[arg-type]
            orientation=orientation or "vertical",
            with_share=resolved.intent == "composition",
        )

    return VisualizationSpec(
        type=decision_type,  # type: ignore[arg-type]
        title=title,
        encoding=encoding,
        data=data,
    )


async def answer(request: QueryRequest, client: CtgovClient) -> AnswerResponse:
    """Execute the full question-to-cited-visualization pipeline."""
    started = time.monotonic()
    request_id = uuid.uuid4().hex[:12]
    from app.interpret import usage as usage_meter

    usage_meter.start()

    deadline = started + settings.plan_retry_budget_s
    prior: Any = None
    feedback: str | None = None
    attempts = 0

    while True:
        attempts += 1
        budget_left = (
            time.monotonic() < deadline and attempts < settings.max_plan_attempts
        )
        try:
            response, prior = await _attempt(
                request, client, request_id, started, prior, feedback, attempts
            )
        except ServiceError as exc:
            if budget_left and _should_retry_error(exc):
                feedback = exc.message
                continue
            raise
        # An empty result can mean "no trials match that" or a mis-built plan;
        # the response cannot tell them apart, so try once more and keep the
        # empty answer if the next plan agrees.
        if (
            budget_left
            and response.visualization.type == "none"
            and response.meta.coverage.total_matching_studies == 0
            and response.meta.intent != "lookup"
        ):
            feedback = _EMPTY_RESULT_FEEDBACK
            continue
        return response


async def _attempt(
    request: QueryRequest,
    client: CtgovClient,
    request_id: str,
    started: float,
    prior: Any,
    feedback: str | None,
    attempts: int,
) -> tuple[AnswerResponse, Any]:
    """One full planning-and-execution pass."""
    resolved, raw_plan = await _interpret_resolved(
        request, prior=prior, feedback=feedback
    )
    if attempts > 1:
        resolved.adjustments.append(
            f"Attempt {attempts}: the previous plan was retried after "
            f"{'an empty result' if feedback == _EMPTY_RESULT_FEEDBACK else 'a validation failure'}."
        )

    if not resolved.is_clinical_trials_question:
        raise ServiceError(
            "out_of_scope",
            "That question does not appear to be about clinical trials.",
            detail={"interpretation": resolved.interpretation},
        )

    cap = settings.record_cap
    if resolved.analysis is not None:
        from app.analysis.response import answer_analysis
        return (
            await answer_analysis(request, resolved, client, cap, request_id, started),
            raw_plan,
        )
    fetch_result, bucket_set, comparison = await _gather_data(client, resolved, cap)

    decision = resolve_viz(
        resolved, bucket_set, records_fetched=len(fetch_result.studies)
    )
    if decision.drop_series and bucket_set is not None:
        bucket_set = bucketize.from_sample(
            fetch_result.studies,
            DIMENSIONS[bucket_set.dimension],
            limit=resolved.limit,
            citation_limit=settings.citation_limit,
        )

    coverage = _coverage(fetch_result, bucket_set, cap, comparison)
    coverage_message = titles.build_coverage_message(
        total_matching=coverage.total_matching_studies,
        records_analyzed=coverage.records_analyzed,
        is_sample=coverage.is_sample,
        counts_are_exact=coverage.counts_are_exact,
        sampling_method=coverage.sampling_method,
        excluded_missing=coverage.excluded.missing_dimension_value,
        comparison_arms=len(coverage.comparison_arms),
    )

    visualization = _build_visualization(
        resolved, bucket_set, fetch_result, decision.type, decision.orientation
    )

    graph_coverage_note: str | None = None
    graph_provenance_note: str | None = None
    if resolved.graph is not None and decision.type == "network_graph":
        graph_coverage_note, graph_provenance_note = _semantic_graph_provenance(
            resolved, visualization, coverage
        )
        coverage_message = graph_coverage_note

    invariant = (
        bucket_set.invariant
        if bucket_set
        else Invariant(
            kind="partition",
            statement="A single total; nothing is bucketed.",
        )
    )
    if decision.type == "network_graph":
        invariant = (
            _semantic_graph_invariant(visualization)
            if resolved.graph is not None
            else _graph_invariant(invariant, visualization)
        )

    notes = Notes(
        coverage_warning=coverage_message if coverage.is_sample else None,
        counting_rule=(
            "Each trial is counted once."
            if invariant.kind == "partition"
            else "A trial can be counted in several categories, so the values sum to more than the number of trials."
        ),
        overrides=decision.overrides,
        general=list(_general_notes(resolved, bucket_set, coverage)),
    )
    if graph_provenance_note:
        notes.general.append(graph_provenance_note)
    if graph_coverage_note and not coverage.is_sample:
        notes.general.append(graph_coverage_note)

    if comparison is not None:
        arms = ", ".join(
            f"{name} ({total:,})" for name, total in comparison.arm_totals.items()
        )
        notes.general.append(f"Comparison arms and their matching trials: {arms}.")
        if comparison.overlapping_trials:
            notes.general.append(
                f"{comparison.overlapping_trials:,} trials match more than one arm, so "
                "the series overlap and the bars sum to more than the trials analyzed."
            )
        if comparison.empty_arms:
            notes.general.append(
                f"No trials matched these arms: {', '.join(comparison.empty_arms)}. "
                "They are absent from the chart."
            )

    omitted_record = _omitted_from_graph(visualization)
    if omitted_record and omitted_record.get("trials"):
        notes.general.append(
            f"{omitted_record['trials']:,} trials are not drawn on the graph. "
            f"{omitted_record['reason']} They are reported in data.omitted."
        )

    warnings: list[str] = []
    if coverage.total_matching_studies > BROAD_QUERY_THRESHOLD:
        warnings.append(
            f"This query matches {coverage.total_matching_studies:,} trials. "
            "Consider narrowing it with a condition, drug, or date range."
        )

    answer_text = None
    suggestions: list[str] = []
    if decision.type == "none":
        if resolved.intent == "lookup" and coverage.total_matching_studies > 0:
            answer_text = (
                "Trial-level lists are not a supported visualization. "
                f"{coverage.total_matching_studies:,} trials match; ask to group or count them instead."
            )
            suggestions = [
                "Ask for a total count.",
                "Group the trials by phase, status, sponsor, or country.",
            ]
        else:
            answer_text = "No trials match those filters."
            suggestions = _relaxation_suggestions(resolved)
    elif decision.type == "stat":
        interpretation = resolved.interpretation.rstrip(".")
        answer_text = (
            f"{coverage.total_matching_studies:,} trials match this query "
            f"({interpretation})."
        )

    meta = Meta(
        analysis=bucket_set.extra.get("analysis") if bucket_set else None,
        query_interpretation=QueryInterpretation(
            model_interpretation=resolved.interpretation,
            filters_applied=_filters_applied(resolved, comparison),
            assumptions=resolved.assumptions,
            plan_adjustments=resolved.adjustments,
        ),
        intent=resolved.intent,
        group_by=bucket_set.dimension if bucket_set else None,
        series_by=bucket_set.series_dimension if bucket_set else None,
        sort=(
            {
                # A totals comparison has no registry dimension: the arms keep
                # the order the question named them in.
                "by": (
                    DIMENSIONS[bucket_set.dimension].sort_mode
                    if bucket_set.dimension in DIMENSIONS
                    else "natural"
                ),
                "direction": "desc",
            }
            if bucket_set
            else None
        ),
        invariant=invariant,
        coverage=coverage,
        truncation=bucket_set.truncation if bucket_set else None,
        visualization_needed=decision.needed,
        visualization_rationale=decision.rationale,
        answer_text=answer_text,
        suggestions=suggestions,
        warnings=warnings,
        notes=notes,
        data_timestamp=await client.data_timestamp(),
        request_id=request_id,
        elapsed_ms=int((time.monotonic() - started) * 1000),
    )

    return AnswerResponse(visualization=visualization, meta=meta), raw_plan


def _filters_applied(resolved: ResolvedPlan, comparison: Any) -> dict[str, str]:
    """The parameters actually sent, including one entry per comparison arm.

    A comparison issues a separate query per arm, so reporting the base plan's
    parameters would show a full-text fallback that was never used for any of
    them.
    """
    if comparison is None:
        return build_query(resolved).params

    from app.ctgov.compare import COMPARE_FIELD

    field = COMPARE_FIELD[str(resolved.compare_by)]
    applied: dict[str, str] = {}
    for value in resolved.compare_values:
        arm = resolved.model_copy(update={field: value, "compare_values": []})
        if resolved.search is not None:
            slot = {"sponsor": "sponsor", "condition": "condition", "drug_name": "intervention", "country": "location"}[field]
            arm = arm.model_copy(update={"search": resolved.search.model_copy(update={slot: value})})
        for key, param in build_query(arm).params.items():
            applied[f"{value} / {key}"] = param
    return applied


def _omitted_from_graph(visualization: VisualizationSpec) -> dict[str, Any] | None:
    """Read the graph's omission record, whether data is still a dict or a model.

    ``VisualizationSpec`` validates ``data`` into ``NetworkData``, so attribute
    access is needed once it has been through the model; the dict branch covers
    the spec as first assembled.
    """
    data = visualization.data
    if isinstance(data, dict):
        record = data.get("omitted")
    else:
        record = getattr(data, "omitted", None)
    if record is None:
        return None
    return record if isinstance(record, dict) else record.model_dump()


def _graph_root(visualization: VisualizationSpec) -> dict[str, Any] | None:
    data = visualization.data
    nodes = data.get("nodes", []) if isinstance(data, dict) else getattr(data, "nodes", [])
    return next((node for node in nodes if node.get("level") == 0), None)


def _semantic_graph_provenance(
    resolved: ResolvedPlan,
    visualization: VisualizationSpec,
    coverage: Coverage,
) -> tuple[str, str]:
    """Put person-resolution evidence next to the graph, not in hidden metadata."""
    root = _graph_root(visualization) or {}
    basis = root.get("verification_basis")
    subject = resolved.graph.root_value if resolved.graph is not None else "the subject"

    if basis == "full_text_query_person_association":
        if coverage.is_sample:
            coverage_message = (
                f"Based on {coverage.records_fetched:,} of {coverage.total_matching_studies:,} "
                f"trials returned by a full-text search for {subject} "
                "(relevance-ordered sample)."
            )
        else:
            coverage_message = (
                f"Based on all {coverage.total_matching_studies:,} trials returned "
                f"by a full-text search for {subject}."
            )
        return coverage_message, (
            "These are full-text associations, not independently confirmed person-level links. "
            "Exact OverallOfficialName matches provide additional citation evidence where "
            "available; missing or different official names do not exclude search results."
        )

    return titles.build_coverage_message(
        total_matching=coverage.total_matching_studies,
        records_analyzed=coverage.records_analyzed,
        is_sample=coverage.is_sample,
        counts_are_exact=coverage.counts_are_exact,
        sampling_method=coverage.sampling_method,
    ), (
        "The graph root comes from the question; nodes and edges are deterministically "
        "derived from matching trial records, not generated by the language model."
    )


def _semantic_graph_invariant(visualization: VisualizationSpec) -> Invariant:
    root = _graph_root(visualization) or {}
    verified = int(root.get("size") or 0)
    return Invariant(
        kind="multi_label",
        statement=(
            "Each edge is backed by cited trial records. Edge weights are per relationship "
            "layer and must not be summed across levels."
        ),
        records_analyzed=verified,
    )


def _graph_invariant(invariant: Invariant, visualization: VisualizationSpec) -> Invariant:
    """Restate the invariant in terms of edge weights.

    A graph has no ``data[].value``, so the flat-array statement would send a
    client looking for a field that does not exist. Edge weights are the
    graph's equivalent, and any trial that could not be placed on an edge is
    named explicitly rather than left as an unexplained shortfall.
    """
    omitted_record = _omitted_from_graph(visualization)
    omitted = omitted_record.get("trials", 0) if omitted_record else 0

    statement = "sum(data.edges[].weight)"
    if omitted:
        statement += f" + data.omitted.trials ({omitted:,})"
    statement += (
        " == meta.coverage.records_analyzed"
        if invariant.kind == "partition"
        else " >= meta.coverage.records_analyzed (a trial can span several edges)"
    )

    return Invariant(
        kind=invariant.kind,
        statement=statement,
        assignments_total=invariant.assignments_total,
        records_analyzed=invariant.records_analyzed,
    )


def _general_notes(
    resolved: ResolvedPlan,
    bucket_set: BucketSet | None,
    coverage: Coverage,
) -> list[str]:
    notes: list[str] = []
    if bucket_set is None:
        return notes

    # A totals comparison groups by the arms themselves, which are a synthetic
    # axis with no registry entry.
    dimension = DIMENSIONS.get(bucket_set.dimension)
    if dimension is None:
        notes.append(
            "Each bar is one arm's own total from a separate query. The arms are "
            "not deduplicated against each other, so a trial matching two of them "
            "is counted in both and the bars must not be added together."
        )
        return notes
    notes.extend(dimension.notes)

    if resolved.country and dimension.id == "country":
        notes.append(
            f"Grouping by country while filtered to {resolved.country}: trials are "
            "counted for every country they run in, not only that one."
        )
    if resolved.sponsor:
        notes.append(
            "Sponsor search matches text rather than an exact organisation, so "
            "related subsidiaries can appear."
        )
    if bucket_set.truncation:
        t = bucket_set.truncation
        notes.append(
            f"Showing the top {t.shown} of {t.total_distinct} values; the remaining "
            f"{t.total_distinct - t.shown} are grouped into 'Other'."
        )
    if coverage.excluded.missing_dimension_value:
        notes.append(
            f"{coverage.excluded.missing_dimension_value} trials had no "
            f"{dimension.id} value and were excluded from the chart."
        )
    return notes


def _relaxation_suggestions(resolved: ResolvedPlan) -> list[str]:
    suggestions = []
    if resolved.start_year or resolved.end_year:
        suggestions.append("Widen or remove the year range.")
    if resolved.trial_phase:
        suggestions.append("Allow any trial phase.")
    if resolved.overall_status:
        suggestions.append("Allow any recruitment status.")
    if resolved.country:
        suggestions.append(f"Remove the {resolved.country} location filter.")
    if resolved.sponsor:
        suggestions.append("Remove the sponsor filter.")
    if not suggestions:
        suggestions.append("Try a broader condition or drug name.")
    return suggestions
