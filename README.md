# Clinical Trials Visualization API

An AI-enabled backend that answers natural-language questions about clinical trials.
It interprets the question with the OpenAI API, retrieves matching trials from the
[ClinicalTrials.gov v2 Data API](https://clinicaltrials.gov/data-api/api), decides
whether a visualization is warranted and which type fits the data, and returns a
renderable **visualization specification** in which every plotted value carries
citations back to the trial records that produced it.

```
POST /query  {"query": "How many pembrolizumab trials are there by phase?"}
   -> {"visualization": {type, title, subtitle, encoding, data}, "meta": {...}}
```

---

## Quick start

```bash
uv sync
cp .env.example .env          # then set OPENAI_API_KEY
uv run uvicorn app.main:app --reload
```

Open <http://localhost:8000> for the demo renderer, or <http://localhost:8000/docs>
for the generated OpenAPI documentation.

```bash
uv run pytest        # 166 tests, fully offline
uv run pytest -m live   # 10 additional tests that hit the real API
```

---

## Two design decisions worth knowing up front

**The model produces intent, not output.** The OpenAI call picks a grouping dimension
from a closed enum, extracts filters, and states how it read the question. It never
produces a count, a bucket label, a chart title, a quotation, or the final chart type.
Everything a viewer could be misled by is computed deterministically from real API data
and is unit-tested. There is no fallback planner: a missing API key is a loud
misconfiguration (HTTP 500 `llm_misconfigured`), not a reason to quietly answer a
different question.

**Every number is computed locally, so the response says what it counted.**
ClinicalTrials.gov's `/stats/field/values` endpoint rejects any scoped query, so there
is no way to ask the server for "phase distribution *for pembrolizumab*". All
aggregation therefore happens over records this service fetched — which is also exactly
what makes per-datum citations possible. The default cap is sized so a specific drug or
condition is counted **completely**, but a broad query cannot be, so sampling is treated
as a normal outcome and reported in three redundant places:
`visualization.subtitle` (always present), `meta.coverage`, and `value_is_exact` on
every datum.

---

## Request schema

`POST /query` and `POST /plan` take the same body. Only `query` is required.

| Field | Type | Required | Validation | Notes |
|---|---|---|---|---|
| `query` | string | **yes** | 1–500 chars, not blank | The natural-language question. |
| `drug_name` | string | no | ≤ 200 chars | Maps to `query.intr`. |
| `condition` | string | no | ≤ 200 chars | Maps to `query.cond`. |
| `sponsor` | string | no | ≤ 200 chars | Maps to `query.spons`. Fuzzy text match, not an exact organisation. |
| `country` | string | no | ≤ 100 chars | Maps to `query.locn`. |
| `trial_phase` | string[] | no | each in `EARLY_PHASE1 PHASE1 PHASE2 PHASE3 PHASE4 NA` | Case-insensitive on input. |
| `overall_status` | string[] | no | each a valid `OverallStatus` | e.g. `["RECRUITING"]`. |
| `start_year` | integer | no | 1990 ≤ y ≤ current+10 | Earliest trial start. |
| `end_year` | integer | no | ≥ `start_year` | Latest trial start. |
| `group_by` | string | no | a dimension id from `GET /dimensions` | Forces the grouping. |
| `limit` | integer | no | 1–50 | Categories before `Other` collapsing. |
| `max_records` | integer | no | 1–10000 | Overrides the fetch cap (default 5000). |

### Comparisons

"Compare A against B" is the question shape an executive asks most, and it cannot be
expressed as one ClinicalTrials.gov query. When the model sets `compare_by` (one of
`sponsor`, `condition`, `drug_name`, `country`) and two to five `compare_values`, each
value becomes its own scoped query, run concurrently, and its own series on the chart.

```
"Compare the phase distribution of Pfizer against Merck."
  -> grouped_bar, group_by=phase, series=compare:sponsor
  -> filters_applied: {"Pfizer / query.spons": "Pfizer", "Merck / query.spons": "Merck"}
```

Because arms are independent queries, one trial can satisfy several of them. That overlap
is **measured and reported** (`meta.notes.general`, and `invariant.kind` becomes
`multi_label`) rather than assumed away. Arm totals and any arm that matched nothing are
reported too. A comparison always renders grouped, never stacked — stacking would hide
the difference the question asked about.

Unknown fields are rejected. Validation failures return **422** `invalid_request`.

**Precedence.** A structured field you supply always wins over what the model inferred,
because you asserted it and the model guessed it. The model still *sees* your fields, so
follow-on phrasing is interpreted relative to them. Every override is reported in
`meta.query_interpretation.overridden_fields`.

```json
{"query": "How has this drug's trial count changed over time?",
 "drug_name": "Pembrolizumab", "start_year": 2015}
```

---

## Response schema

```json
{
  "visualization": {
    "type": "bar_chart",
    "title": "Pembrolizumab Trials by Phase",
    "subtitle": "Based on 2,000 of 2,999 matching trials (first 2,000 by API relevance order); counts are approximate.",
    "encoding": {
      "x": {"field": "label", "type": "nominal", "axis_title": "Trial Phase", "sort": "none"},
      "y": {"field": "value", "type": "quantitative", "axis_title": "Number of Trials", "unit": "trials"},
      "orientation": "vertical",
      "tooltip": ["label", "value"]
    },
    "data": [
      {
        "label": "Phase 1/Phase 2",
        "value": 308,
        "value_is_exact": false,
        "citation_total": 308,
        "citations_truncated": true,
        "citations": [
          {
            "nct_id": "NCT04956640",
            "brief_title": "Study of LY3537982 in Cancer Patients With a Specific Genetic Mutation",
            "field_path": "protocolSection.designModule.phases",
            "field_value": "[\"PHASE1\",\"PHASE2\"]",
            "evidence_kind": "field_value",
            "excerpt": null,
            "source_url": "https://clinicaltrials.gov/study/NCT04956640"
          }
        ]
      }
    ]
  },
  "meta": { "...": "see below" }
}
```

### `visualization`

| Field | Type | Notes |
|---|---|---|
| `type` | enum | One of the 9 types below. Never null. |
| `title` | string | Human-readable. Never mutated by coverage. |
| `subtitle` | string | **Always present. The renderer contract requires displaying it.** States what the numbers are based on. |
| `encoding` | object | Channel → field mapping. See the per-type table. |
| `data` | array\|object | Flat array of rows, except `network_graph`. |

### Two guarantees that make a renderer a pure `switch (type)`

1. **`data` is always a flat array of uniform row objects.** The single documented
   exception is `network_graph`, which is `{"nodes": [...], "edges": [...]}`.
2. **Every `encoding` channel's `field` names a key present on every row.** You never
   have to guess which property holds the value.

### Encoding by type

| `type` | `encoding` keys | `data` row shape |
|---|---|---|
| `stat` | `value`, `label` | one row: `{label, value}` |
| `bar_chart` | `x`, `y`, `orientation`, `tooltip` | `{label, value}` |
| `grouped_bar` | above + `series`, `stack: false` | `{label, series, value}` |
| `stacked_bar` | above + `stack: true \| "normalize"` | `{label, series, value}` |
| `time_series` | `x` (`type: temporal`, `granularity`), `y`, `point_markers`, optional `series` | `{bucket, label, value}` — **zero-filled**, no gaps |
| `pie_chart` | `theta`, `color`, `total` | `{label, value, share}` where `share` is 0–1 |
| `scatter_plot` | `x`, `y`, optional `size`/`color` | one row per trial |
| `network_graph` | `node`, `edge`, `layout_hint` | `{nodes: [{id,label,group,size,level}], edges: [{source,target,weight,citations}], omitted?}` |
| `none` | `{}` | `[]` — read `meta.answer_text` and `meta.suggestions` |

The bundled demo implements all nine types: `bar_chart` and `grouped_bar` as bars,
`stacked_bar` as a true 100% stack when `stack: "normalize"`, `time_series` and
`scatter_plot` as SVG line and point plots with axes and gridlines, `pie_chart` as an
SVG pie with a legend, `network_graph` as a labeled hierarchical SVG graph, plus `stat`
and `none`. A test asserts every member of the type enum has a renderer branch.

`network_graph` is the one non-flat shape, and it carries its own invariant because it
has no `data[].value`:

```json
"invariant": {"kind": "multi_label",
  "statement": "sum(data.edges[].weight) + data.omitted.trials (2,114) >= meta.coverage.records_analyzed (a trial can span several edges)"}
```

A collapsed `Other` bucket has no second-dimension value, so there is no edge to place it
on. Rather than drop those trials, the graph reports them in `data.omitted`
(`{trials, distinct_values_collapsed, reason}`) and in `meta.notes.general`, so edge
weights always reconcile against `records_analyzed`.

### Row fields

| Field | Type | Notes |
|---|---|---|
| `label` | string | Display label for the bucket. |
| `value` | number | The count. |
| `value_is_exact` | boolean | `true` when the count came from a server-side count rather than the sample. |
| `sample_value` | integer | Records actually inspected, present alongside an exact `value`. |
| `series` | string | Present for grouped/stacked types. |
| `share` | number | Present for `pie_chart`, 0–1. |
| `is_other` | boolean | This row is the collapsed tail. |
| `is_missing` | boolean | This row is the "no value reported" bucket. |
| `distinct_values_collapsed` | integer | How many values folded into `Other`. |
| `citations` | Citation[] | Up to `citation_limit` (default 5). |
| `citation_total` | integer | The true number of contributing trials. |
| `citations_truncated` | boolean | `citation_total > citations.length`. |
| `citation_sample` | boolean | The `Other` bucket's citations are a token sample. |
| `citation_basis` | string | Prose describing where the citations came from. |

### `meta`

| Field | Notes |
|---|---|
| `query_interpretation.model_interpretation` | One sentence restating the question as read. |
| `query_interpretation.filters_applied` | The exact ClinicalTrials.gov parameters sent. |
| `query_interpretation.overridden_fields` | Where your structured fields beat the model. |
| `query_interpretation.assumptions` | Guesses the model had to make ("recent" → 2020+). |
| `query_interpretation.plan_adjustments` | Clamps and drops applied to the plan. |
| `intent` | `count_total`, `distribution`, `trend`, `comparison`, `ranking`, `composition`, `relationship`, `lookup`. |
| `group_by` / `series_by` | Dimension ids. |
| `measure` / `unit` / `sort` | `trial_count`, `trials`, sort spec. |
| `invariant` | **What the values sum to.** See below. |
| `coverage` | What was actually counted. See below. |
| `truncation` | `{dimension, rule, shown, total_distinct, collapsed_into_other}` or null. |
| `visualization_needed` | `false` for `stat` and `none`. |
| `visualization_rationale` | Why this chart type was chosen. |
| `answer_text` | A direct prose answer, for `stat` and `none`. |
| `suggestions` | Filter relaxations to try, when nothing matched. |
| `warnings` | e.g. the query is extremely broad. |
| `notes.counting_rule` | Plain-language version of the invariant. |
| `notes.overrides` | Chart types that were requested and refused, with reasons. |
| `notes.general` | Caveats specific to this dimension and these filters. |
| `citation_policy` | Statement that citations are mechanical. |
| `source` / `api_version` / `data_timestamp` | Provenance. |
| `request_id` / `elapsed_ms` | Diagnostics. |

---

## The two things that make these numbers trustworthy

### 1. `meta.invariant` — what the bars sum to

A bar chart of trial counts carries an unspoken promise: the bars add up to the number
of trials. For some dimensions that is true and for others it cannot be, so the response
states which:

```json
{"kind": "partition", "statement": "sum(data[].value) == meta.coverage.records_analyzed"}
```

```json
{"kind": "multi_label",
 "statement": "a trial may appear in several buckets; sum(data[].value) >= records_analyzed",
 "assignments_total": 2624, "records_analyzed": 2000}
```

A trial with sites in nine countries genuinely belongs in nine country buckets. Rather
than hide that, `country`, `condition`, `intervention_type`, `age_group`, and
`phase_any` are declared `multi_label`. **This single field is what stops a frontend
from innocently summing a multi-label axis and reporting a bogus total.** It is also
enforced: a pie chart over a `multi_label` dimension is structurally refused, because
slices that do not sum to the whole misrepresent proportion.

Two source quirks are resolved explicitly, because getting them wrong produces a chart
that is wrong in a way nobody can see:

- **`phases` is an array.** A `["PHASE1","PHASE2"]` trial lands in **one** combined
  `"Phase 1/Phase 2"` bucket, not one in each, which preserves the partition invariant.
  ClinicalTrials.gov's own UI labels it the same way. If you genuinely want a trial
  counted under every phase it includes, group by `phase_any` instead — a separate
  dimension with its own declared invariant, so the two are never silently swapped.
- **`locations[].country` repeats once per site.** The fixture contains one trial with
  **963 site entries**. Countries are de-duplicated per trial first, so that trial
  contributes 1 to the United States, not 963. Site counts answer a different question
  than trial counts.

**Top-N ranks the dimension, not the cells.** With a `series_by` dimension a key spans
several buckets, so ranking buckets directly would keep "the biggest cells" — which can be
five bars of one country — rather than the five biggest countries the caller asked for.
Ranking is always by the primary key's total, and `truncation.total_distinct` counts keys
rather than cells. High-cardinality dimensions keep the top `limit` keys and fold the rest
into one `Other` bucket, which carries a labelled sample of citations rather than hundreds
or none.

Missing data is shown, not dropped: `phase` distinguishes `"Not Applicable"` (the API's
`NA` — a real editorial answer meaning a non-phased study, 240,302 studies) from
`"Not Specified"` (no phase field at all, 144,616 studies). Merging those would destroy
the most common finding in the dataset. Dimensions with no honest axis position for
"unknown" — `start_year`, `completion_year`, `enrollment_bucket`, `duration_bucket` —
exclude such trials and report the count in `coverage.excluded`.

### 2. `meta.coverage` — what was actually counted

```json
{
  "total_matching_studies": 2999,
  "records_fetched": 2000,
  "records_analyzed": 1987,
  "is_sample": true,
  "coverage_ratio": 0.6669,
  "sampling_method": "first_n_by_api_relevance_order",
  "sampling_bias_risk": "high",
  "fetch_cap": 2000,
  "pages_fetched": 2,
  "truncated_reason": "fetch_cap_reached",
  "counts_are_exact": false,
  "excluded": {"missing_dimension_value": 13, "imprecise_for_granularity": 0}
}
```

`sampling_bias_risk: "high"` is stated rather than glossed. The API's default ordering
is relevance, which is **not** random, so a 67% sample is not a statistically clean 67%
sample. (`sort=NCTId` is rejected by the API with a 400, and registration order would
only trade an unknown bias for a known one toward older trials.)

**Why the cap is 5,000.** Fetching costs about 0.85 s per 1,000 records and pagination
is strictly sequential, so the cap is a direct latency trade. It is set where it is
because the queries this tool exists for land just under it — pembrolizumab is 2,253
trials, melanoma 3,781, glioblastoma 2,281 — and those become **complete censuses** in
2–4 s rather than relevance-ordered samples. That is a qualitative difference, not a
marginal one: a census has no sampling bias at all, whereas a 53 % relevance-ordered
sample has bias of unknown direction. A ceiling is still necessary, because breadth is
genuinely unbounded (`cancer` is ~124,000 trials, roughly 106 s), so broad queries
sample and say so. Raise `max_records` up to 10,000 per request to convert a larger
universe into a census.

The cap barely affects the exact-capable dimensions below, where counts come from
`countTotal` and fetched records only supply citations. Its real effect falls on the
sampled dimensions — precisely where bias would distort the answer.

**Exact counts, where they are possible.** For dimensions whose buckets are expressible
as a server-side filter — `overall_status`, `start_year`, `completion_year`,
`study_type`, `sponsor_type`, `phase_any`, `results_posted` — the service issues one
scoped `countTotal` request per bucket, concurrently. This yields `counts_are_exact:
true`, citations drawn evenly from every bucket rather than concentrated wherever
relevance ranking happened to look, and real parallelism, which the token-chained flat
fetch structurally cannot have. In practice it is both exact *and* faster: 965 ms for 14
parallel requests versus 1,832 ms for 2 sequential pages.

It is deliberately **not** applied to `lead_sponsor`, `condition`, `country`, or
`intervention_name`, whose buckets are fuzzy string matches and whose `Other` bucket is
unexpressible in principle. Those report sampled counts with full disclosure.
`sex` is also excluded: `AREA[Sex](ALL)` was verified to return the entire result set
rather than only `sex=ALL` trials, and a live test documents that so it can be promoted
if the API changes.

---

## Deep citations

Every plotted value carries references to the trial records behind it — on each datum
for flat types, on each **edge** for `network_graph`, and on the single row for `stat`.

```json
{
  "nct_id": "NCT04956640",
  "brief_title": "Study of LY3537982 in Cancer Patients...",
  "field_path": "protocolSection.designModule.phases",
  "field_value": "[\"PHASE1\",\"PHASE2\"]",
  "evidence_kind": "field_value",
  "excerpt": null,
  "source_url": "https://clinicaltrials.gov/study/NCT04956640"
}
```

`field_value` is the raw value at `field_path`, serialised exactly as the API returned
it. `excerpt`, when present, is a **verbatim substring** of a fetched text field;
when the value does not literally appear in the text, `excerpt` is `null` rather than a
paraphrase, so an excerpt is always checkable against its source.

**The language model never touches citations.** An LLM-authored "excerpt" would be a
fabricated quote, which is the worst available failure mode for a traceability feature,
so it is never given the opportunity. This is enforced by a property test
(`tests/test_citations.py`) that walks every bucket of every dimension over 300 real
trial records and asserts each cited value literally appears at the cited path in the
cited record — 289 citations checked, zero tolerance for a mismatch.

---

## Choosing the visualization

The model supplies a `chart_hint`; deterministic code decides. The decision depends on
facts that do not exist when the model is called — how many buckets survived top-N
collapsing, whether the dimension double-counts trials, whether anything matched at all
— so the hint is advisory input to a resolver, never the output. **Every refusal is
reported** in `meta.notes.overrides`:

```json
{"requested": "pie_chart", "applied": "bar_chart",
 "reason": "A trial can appear in several country buckets, so the slices would not sum to the total."}
```

Ordered rules, first match wins:

| Condition | Result |
|---|---|
| no matching trials | `none` |
| not a clinical-trials question | `none` (HTTP 400 `out_of_scope`) |
| `intent = count_total`, or no grouping | `stat` |
| `intent = lookup` | `none` with a suggestion to ask for a count or grouping |
| temporal dimension, ≥ 3 buckets | `time_series` |
| temporal dimension, exactly 2 buckets | `bar_chart` — two points cannot support a trend line |
| `intent = relationship`, two categorical dimensions | `network_graph` (> 60 nodes → primary-dimension `bar_chart`) |
| `pie_chart` hint + partition + 2–6 buckets + < 40% missing | `pie_chart` |
| two dimensions, ≤ 60 cells | `stacked_bar` if `composition`, else `grouped_bar` |
| otherwise | `bar_chart` |

Bar orientation is decided server-side (horizontal for long-label dimensions or > 8
buckets), so no label-length heuristics leak into the frontend.

**"No visualization needed" is a first-class answer.** `visualization` is never null —
a frontend should not have to special-case its absence — but `meta.visualization_needed`
is `false` and `meta.visualization_rationale` explains why, for the `stat` and `none`
paths.

---

## Supporting endpoints

| Route | Purpose |
|---|---|
| `POST /query` | The main endpoint. |
| `POST /plan` | **Interpretation dry run.** Returns the plan and the exact ClinicalTrials.gov parameters that *would* be sent, without fetching. The fastest way to see how a question was read. |
| `GET /dimensions` | The dimension registry, with each one's counting rule and whether exact counts are available. Self-documenting. |
| `GET /healthz` | Liveness and configuration. |
| `GET /docs` | Generated OpenAPI documentation. |
| `GET /` | Demo renderer. |

---

## Errors

All errors share one envelope:

```json
{"error": {"code": "upstream_unavailable", "message": "...", "retryable": true, "detail": {}}}
```

| Condition | HTTP | `code` | Retryable |
|---|---|---|---|
| Request failed validation | 422 | `invalid_request` | no |
| No usable dimension or filter, or the model refused | 400 | `query_not_interpretable` | no |
| Not a clinical-trials question | 400 | `out_of_scope` | no |
| `OPENAI_API_KEY` missing or rejected | 500 | `llm_misconfigured` | no |
| OpenAI timeout / 5xx / rate limit after retries | 503 | `llm_unavailable` | **yes** |
| ClinicalTrials.gov unreachable, zero pages fetched | 502 | `upstream_unavailable` | **yes** |
| ClinicalTrials.gov rejected a filter *we* built | 400 | `upstream_rejected_query` | no |

**Zero results and over-broad queries are 200s, not errors.** "No trials match that" is a
correct answer to a question; the response carries `type: "none"`, `meta.answer_text`,
and concrete `meta.suggestions` for relaxing filters. A query matching more than 50,000
trials returns a sampled answer with a `meta.warnings` entry rather than refusing.

A `400` from ClinicalTrials.gov is treated as **our** bug, not a transient fault, so it
is never retried and the offending parameters are attached to the error detail.

---

## Architecture

```
app/
  main.py          routes, exception handlers, static mount
  pipeline.py      the whole flow in one readable place
  config.py        settings (env / .env)
  dimensions/      registry.py  <- single source of truth
                   extractors.py  labels.py  bins.py
  interpret/       planner.py (the one OpenAI call)  prompt.py  merge.py
  ctgov/           client.py  params.py  fetch.py  stratified.py
  aggregate/       keys.py  bucketize.py  topn.py  citations.py
  viz/             resolver.py  encoders.py  titles.py
  models/          request  plan  study  buckets  viz  response  errors
  static/index.html  spec-driven demo renderer, no build step
```

```
QueryRequest -> QueryPlan -> ResolvedPlan -> CtgovQuerySpec
  -> FetchResult -> BucketSet -> VisualizationSpec -> AnswerResponse
```

`dimensions/registry.py` is the single source of truth. Each dimension declares its API
field, JSON path, datatype, counting semantics, cardinality rule, missing-value policy,
and exact-count filter expression in one place, so adding a dimension is one entry
rather than edits spread across the planner, aggregator, and resolver. A test asserts the
registry and the model's dimension enum cannot drift apart.

A dimension may instead be **derived** from several fields: `duration_bucket` computes
months between the start and completion dates. Its citations quote both source dates
verbatim plus the computed result, since the derived number appears nowhere in the record
and quoting it alone would be unverifiable.

### Validation happens in three layers

1. **`models/request.py`** — Pydantic on the inbound request → 422.
2. **`interpret/merge.py`** — Structured Outputs with `strict: true` already guarantees
   enum membership and shape, which is the main reason to use it: an invalid dimension
   is structurally impossible rather than something to catch later. But strict mode
   **ignores numeric bounds** (`minimum`, `maximum`) and cannot express a constraint
   spanning two fields, so all clamping and combination validation lives here —
   including dropping a second `multi_label` axis, which would otherwise put one trial
   in dozens of cells at once. Every change is recorded in `plan_adjustments`.
3. **`viz/resolver.py`** — the chart type, validated against the realised data.

---

## Testing

```bash
uv run pytest           # 166 tests, no network, no API key
uv run pytest -m live   # 10 tests against the real API
```

The offline suite runs against **300 real trial records** captured from the API
(`tests/fixtures/`), which happen to contain every edge case that matters: 63
multi-phase trials, 5 with `NA`, 26 with no phase field, 2 with no start date, 77
multi-country trials, and one trial with 963 site entries.

The load-bearing tests:

| Test | What it protects |
|---|---|
| `test_phase_composite_keeps_partition_invariant` | Bars sum to the trials analyzed. |
| `test_na_phase_is_distinct_from_missing_phase` | "Not Applicable" ≠ "Not Specified". |
| `test_country_dedupes_sites_not_studies` | 7 US sites + 1 CA site → US=1, CA=1. |
| `test_every_citation_matches_the_raw_payload` | Citations are derived, not generated. |
| `test_excerpts_are_verbatim_substrings` | An excerpt is a real quote or absent. |
| `test_pie_over_multi_label_is_structurally_refused` | No misleading part-to-whole charts. |
| `test_planner_failure_never_reaches_clinicaltrials_gov` | No fetch when interpretation failed. |
| `test_subtitle_states_the_sampling_basis` | The honesty signal is actually populated. |
| `test_plan_literal_matches_registry` | The model cannot pick a dimension we can't aggregate. |
| `test_top_n_keeps_n_distinct_keys_not_n_cells` | "Top 5 countries" means 5 countries. |
| `test_collapsed_other_is_reported_not_dropped` | Graph edge weights reconcile with the trial count. |
| `test_network_graph_invariant_describes_the_shape_it_returns` | The invariant names fields that exist. |
| `test_every_type_has_a_renderer` | No type silently degrades to a different chart. |
| `test_time_series_draws_a_line_not_percentage_height_bars` | The trend chart actually renders. |

The `live` suite exists because the registry encodes claims about the upstream API
(which filter expressions work, that `AREA[Sex]` over-matches). If ClinicalTrials.gov
changes, those claims should fail loudly rather than quietly produce wrong counts.

---

## Known limitations

- **Sponsor names are not entity-resolved.** `query.spons=Merck` matches
  "EMD Serono Research & Development Institute"; "Merck Sharp & Dohme LLC" and
  "Merck KGaA" count as different sponsors. Surfaced in `meta.notes.general`.
- **A truncated fetch is relevance-ordered, not random.** Proportions are more reliable
  than absolute counts whenever `counts_are_exact` is `false`. A universe within the
  5,000-record cap avoids this entirely; a broad one (`cancer`, `diabetes`) does not.
- **`condition` is sponsor free text**, so spelling and specificity vary.
  `condition_mesh` groups by normalised vocabulary and is usually the better choice.
- **`intervention_name` is disabled by default** — names embed arm-specific dosing
  ("Pembrolizumab 200mg Q3W"), making grouping unreliable.
- **Enrollment counts mix planned and actual figures**; the API's `EnrollmentType` is
  not yet used to separate them.
- **`scatter_plot` is renderable but not yet produced** by the resolver: no current
  intent maps to two numeric per-trial measures, so the type exists in the schema and the
  demo renders it, but nothing emits it today.
- **The graph layout is hierarchical, not force-directed.** Named-subject sponsor queries
  render three labeled levels: subject root → sponsor-type branches → lead-sponsor leaves.
- **Co-sponsorship is not available.** The sponsor leaves are lead sponsors because
  collaborator records are not currently fetched.
- **"Our pipeline" cannot be resolved.** The service has no notion of who is asking, so a
  first-person question is answered across all sponsors with that stated in
  `assumptions`. Pass `sponsor` explicitly to scope it.
- **Growth rates are not a measure.** "Which countries are growing fastest?" returns
  current volume by country, not a rate of change; `measure` only supports `trial_count`.
- **Durations use estimated completion dates** for trials still running, and are
  month-precision because the API mixes date precisions in the same field.
- **No persistent cache.** The 15-minute cache is per-process.
