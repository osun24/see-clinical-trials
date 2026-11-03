"""Registered dimensions compile to the same deterministic relational executor."""
import json
from app.analysis.models import AnalysisPlan, Memberships, Aggregate
from app.analysis.engine import execute_sync
from app.aggregate import topn
from app.aggregate.keys import display_label, MISSING_KEY
from app.models.buckets import BucketSet, Exclusions, Invariant


def compile_preset(dimension, series_dimension=None):
    steps = [Memberships(op="memberships", id="primary_rows", input="trials", dimension=dimension.id, output="primary")]
    groups = ["primary"]
    if series_dimension:
        steps.append(Memberships(op="memberships", id="series_rows", input="primary_rows", dimension=series_dimension.id, output="secondary"))
        groups.append("secondary")
    steps.append(Aggregate(op="aggregate", id="counts", input=steps[-1].id, group_by=groups, measure="count_distinct_trials", missing="bucket"))
    return AnalysisPlan(steps=steps)


def run_preset(studies, dimension, series_dimension=None, limit=10, citation_limit=5):
    plan = compile_preset(dimension, series_dimension)
    # Two membership axes cross-multiply before top-N trims them, so the
    # transient cell count is bounded by the executor's row budget, not by
    # the tighter budget a model-authored plan must stay inside.
    result = execute_sync(plan, studies, citation_limit, max_buckets=100_000)
    buckets = []
    for bucket in result.buckets:
        keys = json.loads(bucket.key)
        buckets.append(bucket.model_copy(update={"key": keys[0], "label": display_label(keys[0], dimension),
            "series": display_label(keys[1], series_dimension) if series_dimension else None,
            "is_missing": keys[0] == MISSING_KEY,
            "series_is_missing": bool(series_dimension) and keys[1] == MISSING_KEY}))
    excluded = len(result.metadata["excluded_trial_ids"])
    analyzed = len({s.nct_id for s in studies}) - excluded
    buckets, truncation = topn.apply_top_n(buckets, dimension, limit, citation_limit)
    buckets = topn.sort_buckets(buckets, dimension)
    multi = dimension.semantics == "multi_label" or bool(series_dimension and series_dimension.semantics == "multi_label")
    return BucketSet(dimension=dimension.id, series_dimension=series_dimension.id if series_dimension else None,
        buckets=buckets, invariant=Invariant(kind="multi_label" if multi else "partition",
            statement="a trial may appear in several buckets; sum(data[].value) >= records_analyzed" if multi else "sum(data[].value) == meta.coverage.records_analyzed",
            assignments_total=sum(b.value for b in buckets) if multi else None, records_analyzed=analyzed if multi else None),
        exclusions=Exclusions(missing_dimension_value=excluded), truncation=truncation, records_analyzed=analyzed,
        extra={"analysis": result.metadata})
