# Renderer contract

`POST /query` accepts `{"query":"…"}` (1–500 nonblank characters, no extra
fields). A successful answer contains `visualization` and `meta`. Do not interpret
the original query or the analysis plan to render the result.

## Visualization

`type` selects the branch and `title` is human-readable. `encoding` maps named
data fields to visual channels. `data` is a flat array except for `network_graph`,
which contains `nodes`, `edges`, and optional `omitted`. Sampling disclosure is
provided by `meta.notes.coverage_warning` when `meta.coverage.is_sample` is true.

| Type | Channels | Required datum fields |
|---|---|---|
| `stat` | `label`, `value` | `label`, `value` |
| `bar_chart` | `x`, `y`, `orientation`, `tooltip` | `label`, `value` |
| `grouped_bar` | `x`, `y`, `series`, `stack:false` | `label`, `series`, `value` |
| `stacked_bar` | `x`, `y`, `series`, `stack:true` or `"normalize"` | `label`, `series`, `value` |
| `time_series` | temporal `x`, quantitative `y`, optional `series`, `point_markers` | `bucket`, `label`, `value`; `series` when encoded |
| `pie_chart` | `theta`, `color`, `total` | `label`, `value`, `share` in [0,1] |
| `scatter_plot` | quantitative `x`, `y`, `tooltip` | `trial_id`, `label`, numeric `x`, `y` |
| `network_graph` | `node`, `edge`, `layout_hint` | Nodes: `id,label,group,size,level`; edges: `source,target,weight,citations` |
| `none` | empty object | empty array; explanation in `meta.answer_text` |

Each channel has `field` and `type` (`nominal`, `quantitative`, `temporal`), plus
optional `axis_title`, `legend_title`, `unit`, `sort`, `scale`, `format`, and
`granularity`. Honor field mappings rather than assuming x always means label.
For bars, `orientation=horizontal` changes layout without changing field roles.
Array order is presentation order when sort is `none`. Normalized stacks divide
by the within-group sum. Time buckets are year strings; missing generated buckets
mean zero observed memberships, not proof of no studies in an unfetched universe.

A `network_graph` is either subject-rooted (a named person or subject at
`level` 0, entities below) or a co-occurrence graph between two recorded entity
types, where `level` 0 and `level` 1 are the two sides and `group` names the
dimension each node came from. A co-occurrence edge means both values are
recorded on the same trial; it is not a verified real-world relationship.
Unreported values are never drawn as nodes — those trials appear in
`data.omitted` instead.

Node encoding identifies `id_field`, `label_field`, `group_field`, `size_field`,
and `level_field`. Edge encoding identifies `source_field`, `target_field`, and
`weight_field`. `layout_hint` reports the layout the response is drawn with;
it is `force`, and node positions carry no meaning beyond connectedness. Node
`level` is retained as data — it says which side of a co-occurrence a node sits
on, or how far from a subject root — but it does not pin a position. IDs are opaque; all edge endpoints reference existing nodes.
Graph layer weights cannot be summed across levels. Optional `data.omitted`
reports trials absent from the drawing, with a reason.

Bucket labels from a registered dimension are display text (`Phase 4`,
`Active, not recruiting`). A model-authored analysis plan groups by a record
column instead, so its labels are the values as stored (`PHASE4`,
`ACTIVE_NOT_RECRUITING`); `meta.analysis.schema.group_types` names those
columns. Render labels as given rather than re-deriving them.

Optional datum annotations include `is_other`, `is_missing`,
`distinct_values_collapsed`, `sample_value`, and citation information. They may
be omitted; treat omitted booleans as false. Every **encoded** field must exist on
every row. Sparse optional annotations are not visual channels.

## Evidence

Nonzero rows carry `citations`; graph edges carry them instead. A citation contains
`nct_id`, optional `brief_title`, `field_path`, `field_value`, `evidence_kind`,
optional `excerpt`, and `source_url`. Indexed collection paths identify source
items. Combined or derived evidence can cite multiple paths and values.

`citation_total` counts contributors (or declared category memberships in a
collapsed tail), while the citation array is bounded. `citations_truncated`,
`citation_sample`, and `citation_basis` describe that gap. Do not treat citation
array length as the plotted value. Zero-filled points may have no citations.
A stat's count can be exact while its citations are examples from fetched records.

## Metadata

- `query_interpretation`: interpretation, exact executed search filters, assumptions,
  and defensive plan adjustments. Comparison filters are prefixed by arm.
- `measure`, `unit`, `group_by`, `series_by`, `sort`: analysis semantics.
- `invariant`: partition versus overlapping/multi-label values; do not sum
  multi-label categories to infer unique trial counts.
- `coverage`: total matching, fetched/analyzed counts, coverage ratio, sampling
  method/risk, fetch cap, pages, truncation reason, exact-count flag, and exclusions.
  Comparisons use a sum of arm totals, not a deduplicated population denominator.
- `truncation`: top-N category collapse; `notes` discloses chart overrides and caveats.
- `analysis`: optional replayable execution plan, typed result schema, input
  fingerprint, trace counts and exclusions; numeric summaries or temporal omissions.
  `schema.filter_canonicalisation` lists every filter value rewritten to the
  stored vocabulary (`given` -> `applied`), so a reader can tell that a plan
  filtering `"Phase 4"` executed against `PHASE4`. This is audit information,
  not a rendering dependency.
- `answer_text`, `suggestions`: prose result and optional alternatives.
- `source`, `api_version`, `data_timestamp`, `request_id`, `elapsed_ms`: provenance.

A record census can yield exact local arithmetic although `value_is_exact` is
reserved for server-reported values. `coverage.counts_are_exact` identifies the
exact per-bucket fetch strategy. An analyzed subset may be smaller than fetched
records because of missing values or explicit analysis predicates.

## Errors and empty results

No matches is a successful `none` answer. Errors have the envelope:

```json
{"error":{"code":"unsupported_query","message":"…","retryable":false,"detail":{"requirements":["…"]}}}
```

422: invalid request or unsupported query. 400: uninterpretable plan or rejected
upstream query. 429: provider rate limit. 504: provider timeout. 503: connectivity,
provider outage or quota (quota is not automatically retryable). 500: missing or
rejected credentials, provider schema rejection, or internal failure. 502:
ClinicalTrials.gov failure. Provider diagnostic detail contains sanitized codes,
status, and request ID; never secrets. See the generated JSON schemas and example
responses in this directory.
