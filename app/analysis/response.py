"""Adapter from generic result tables to the existing chart contract."""
import time
from app.analysis.engine import execute
from app.ctgov.fetch import fetch_flat
from app.ctgov.params import build_query
from app.dimensions.registry import DimensionDef
from app.models.buckets import BucketSet, Invariant
from app.models.response import AnswerResponse, Meta, Notes, QueryInterpretation
from app.models.viz import VisualizationSpec
from app.viz import encoders, titles
from app.viz.resolver import resolve


async def answer_analysis(request, plan, client, cap, request_id, started):
    from app.pipeline import _coverage
    from app.config import settings
    fetched = await fetch_flat(client, build_query(plan), cap=cap)
    result = await execute(plan.analysis, fetched.studies, request.query, settings.citation_limit)
    if result.points is not None:
        return await relationship_response(plan, client, fetched, result, cap, request_id, started)
    aggregate = next(s for s in plan.analysis.steps if s.op == "aggregate")
    dimension_name = " / ".join(aggregate.group_by)
    invariant = Invariant(kind="multi_label", statement="Values count category memberships or collection entries; they must not be summed to infer unique trials.")
    buckets = BucketSet(dimension=dimension_name, buckets=result.buckets,
                        invariant=invariant, records_analyzed=len(fetched.studies))
    definition = DimensionDef(id=dimension_name, api_fields=(), path="",
                             datatype="nominal_open", semantics="multi_label", description=dimension_name)
    from app.analysis.temporal import adapt
    temporal = adapt(result, aggregate)
    series_definition = None
    if temporal:
        year, series, temporal_buckets, temporal_meta = temporal
        result.metadata["temporal"] = temporal_meta
        buckets = buckets.model_copy(update={"dimension": year, "series_dimension": series, "buckets": temporal_buckets})
        definition = DimensionDef(id=year, api_fields=(), path="", datatype="temporal", semantics="multi_label", description=year)
        if series:
            series_definition = DimensionDef(id=series, api_fields=(), path="", datatype="nominal_open", semantics="multi_label", description=series)
    presentation = plan.model_copy(update={"group_by": dimension_name, "series_by": None, "intent": "distribution"})
    decision = resolve(presentation, buckets, records_fetched=len(fetched.studies) if result.buckets else 0,
                       dimension_definition=definition, series_definition=series_definition)
    coverage = _coverage(fetched, buckets, cap)
    coverage_message = titles.build_coverage_message(total_matching=fetched.total_matching, records_analyzed=len(fetched.studies),
        is_sample=coverage.is_sample, counts_are_exact=False, sampling_method=fetched.sampling_method)
    methodology = []
    methodology.append("Counts represent distinct trials per category." if result.unit == "trials" else "Counts represent collection entries, not unique trials.")
    methodology.append(f"{result.metadata['filtered_rows']} rows filtered; {result.metadata['missing_group_rows']} rows excluded for missing grouping values.")
    if temporal:
        methodology.append("Grouped by study start year; zeros indicate no observed memberships in fetched records.")
        omitted = result.metadata["temporal"]["omitted_series"]
        if omitted:
            methodology.append(f"Showing the top 6 series by summed trial-year memberships; {len(omitted)} additional series are retained in analysis metadata.")
    if decision.type == "time_series":
        encoding, data = encoders.encode_time_series(buckets)
    else:
        encoding, data = encoders.encode_bar(buckets, orientation=decision.orientation or "horizontal")
    encoding.y.unit = result.unit
    encoding.y.axis_title = "Number of Trials" if result.unit == "trials" else "Number of Entries"
    if decision.type == "none":
        from app.models.viz import Encoding
        encoding, data = Encoding(), []
    return AnswerResponse(visualization=VisualizationSpec(type=decision.type,
        title=f"{plan.interpretation.rstrip('.')} — by {dimension_name}", encoding=encoding, data=data),
        meta=Meta(analysis=result.metadata, query_interpretation=QueryInterpretation(
            model_interpretation=plan.interpretation, filters_applied=build_query(plan).params,
            assumptions=plan.assumptions, plan_adjustments=plan.adjustments),
            intent=plan.intent, group_by=dimension_name, measure=aggregate.measure, unit=result.unit,
            invariant=invariant, coverage=coverage, visualization_needed=decision.needed,
            visualization_rationale=decision.rationale, answer_text="No rows satisfy the analysis." if not data else None,
            notes=Notes(coverage_warning=coverage_message if coverage.is_sample else None,
                        counting_rule=invariant.statement, overrides=decision.overrides,
                        general=[result.metadata["evidence_basis"], *methodology]),
            data_timestamp=await client.data_timestamp(), request_id=request_id,
            elapsed_ms=int((time.monotonic()-started)*1000)))


async def relationship_response(plan, client, fetched, result, cap, request_id, started):
    from app.pipeline import _coverage
    from app.models.viz import Channel, Encoding
    from app.analysis.numeric import SOURCES
    relation = plan.analysis.steps[-1]
    summary = result.metadata["numeric_summary"]
    points = result.points
    coverage = _coverage(fetched, None, cap)
    coverage.records_analyzed = len(points)
    coverage.excluded.invalid_numeric_pair = result.metadata["invalid_pairs"]
    coverage_message = titles.build_coverage_message(total_matching=fetched.total_matching, records_analyzed=len(fetched.studies),
        is_sample=coverage.is_sample, counts_are_exact=False, sampling_method=fetched.sampling_method)
    methodology = [
        f"{result.metadata['filtered_rows']} rows excluded by analysis filters.",
        f"{len(points)} complete trial pairs; {result.metadata['invalid_pairs']} excluded for missing, imprecise, or invalid numeric/date values.",
    ]
    if any(s.op == "derive" and s.transform == "months_between" for s in plan.analysis.steps):
        methodology.append("Duration is start-to-completion calendar months; days are ignored and year-only dates are excluded. Completion dates may be estimated.")
    methodology.append(f"Pearson r = {summary['coefficient']:.3f}." if summary["coefficient"] is not None else summary["undefined_reason"])
    methodology.append("Descriptive association, not causation or a significance test.")
    answer_text = " ".join([coverage_message, *methodology])
    def channel(field, column):
        source = SOURCES.get(column)
        is_months = any(s.op == "derive" and s.output == column and s.transform == "months_between" for s in plan.analysis.steps)
        return Channel(field=field, type="quantitative", axis_title=column.replace("_", " ").title(),
                       unit="months" if is_months else source[3] if source else None)
    encoding = Encoding(x=channel("x", relation.x), y=channel("y", relation.y), tooltip=["label", "trial_id", "x", "y"]) if points else Encoding()
    invariant = Invariant(kind="partition", statement="Each point represents one trial with a complete numeric pair; points are not additive.")
    return AnswerResponse(visualization=VisualizationSpec(type="scatter_plot" if points else "none",
        title=plan.interpretation.rstrip("."), encoding=encoding, data=points),
        meta=Meta(analysis=result.metadata, query_interpretation=QueryInterpretation(model_interpretation=plan.interpretation,
            filters_applied=build_query(plan).params, assumptions=plan.assumptions, plan_adjustments=plan.adjustments),
            intent="relationship", measure="relationship", unit="trials", invariant=invariant, coverage=coverage,
            visualization_needed=bool(points), visualization_rationale="Two numeric trial variables are represented by one point per complete trial.",
            answer_text=answer_text, notes=Notes(coverage_warning=coverage_message if coverage.is_sample else None,
                counting_rule=invariant.statement, general=[summary["interpretation"], *methodology]),
            data_timestamp=await client.data_timestamp(), request_id=request_id, elapsed_ms=int((time.monotonic()-started)*1000)))
