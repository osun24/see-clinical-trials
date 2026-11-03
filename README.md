# Clinical Trials Visualization API

## Run the application

### Requirements

- Python 3.12 or newer
- [uv](https://docs.astral.sh/uv/)
- OpenAI API key (set in `.env` file as `OPENAI_API_KEY`)

### Install

```bash
uv sync
```

### Configure

Create a local environment file and add your API key:

```bash
cp .env.example .env
```

```dotenv
OPENAI_API_KEY=sk-...
```

### Start

```bash
uv run uvicorn app.main:app --reload
```

The service is then available at:<http://localhost:8000>

##  Implementation reflection
## Tools used
- Codex
- Claude Code

## Validating correctness
- Adversarial review by other models from a different provider
- Implemented a suite of tests to ensure correctness
- Manual review of the generated responses for critical aspects of the implementation

## Design vs. adaptation
- Started with initial high-level planning by sketching a flowchart
 - Aimed to balance flexibility for query input and need for determinism and reliability in creating the visualization
 - To prevent hallucination and excess cost, I decided to have the API request and visualization run locally with Python. 
 - To ensure flexibility of visualization and usefulness, I decided to use a LLM to interpret the query and generate a plan for the visualization, including providing fields for the API request
 - Used structured model outputs to ensure that the output remained constrained and to reduce hallucination
![plan]("Initial high level planning.png")

- Adapted: 
  - Code was generated according to specifications in prompt and were continually iterated and adapted upon based on testing and review
  - Visualization renderer, initial tests, and metadata schema were generated and adapted 
  - Prototyped with allowing the LLM to enrich the output based on its knowledge (e.g., classify drugs into mechanistic classes or combine synonyms) but found that this disrupted downstream visualization, so I removed it.
  - Adapted with number of trials to fetch, achieving exact counts when possible, and sampling when it would take too much time to fetch all trials.

## Key design decisions and tradeoffs
1. Used only an unstructured input query as input, reducing friction for the user. However, this relies on the LLM to flexibly interpret the query, determining the intent of the user, and generate a plan for visualization. 
2. Used an LLM to generate fields for API request and visualization plan, but not to fetch trials or generate the visualization itself. This allows for flexibility in query input while ensuring that the output is deterministic and reliable. However, this sometimes can result in invalid fields for the API request or visualization plan, which is mitigated by validation and retry.
3. Used deterministic Python to select the visualization type and generate the visualization itself. Certain questions cannot be best visualized as there is no downstream enrichment or interpretation of the API output.  
 - One such question is "How have the drug classes for new NSCLC trials changed over time?" requires consolidating individual drugs into mechanistic classes, which is not possible with the current API output as it is not included in clinicaltrials.gov. 

## Future work 
1. Implement enrichment downstream to allow for richer visualization and expansion in the space of questions answerable by the visualizer.
2. Implement more complex visualizations, such as a Sankey diagram.

## Request schema

### `POST /query`

Interprets and answers a natural-language clinical-trials question.

```json
{
  "query": "Compare trial phases for pembrolizumab and nivolumab"
}
```

| Field | Type | Required | Validation |
|---|---|---:|---|
| `query` | string | Yes | Nonblank, 1–500 characters |

Example:

```bash
curl -X POST http://localhost:8000/query \
  -H 'Content-Type: application/json' \
  -d '{"query":"How many pembrolizumab trials are there by phase?"}'
```

## Response schema

A successful `POST /query` response contains a visualization specification and
response metadata:

```json
{
  "visualization": {
    "type": "bar_chart",
    "title": "Pembrolizumab Trials by Phase",
    "encoding": {
      "x": {"field": "label", "type": "nominal", "axis_title": "Trial Phase"},
      "y": {"field": "value", "type": "quantitative", "axis_title": "Trials", "unit": "trials"},
      "orientation": "vertical"
    },
    "data": [
      {
        "label": "Phase 2",
        "value": 1,
        "value_is_exact": false,
        "citation_total": 1,
        "citations_truncated": false,
        "citations": [
          {
            "nct_id": "NCT00000001",
            "brief_title": "Illustrative trial",
            "field_path": "protocolSection.designModule.phases",
            "field_value": "PHASE2",
            "evidence_kind": "field_value",
            "source_url": "https://clinicaltrials.gov/study/NCT00000001"
          }
        ]
      }
    ]
  },
  "meta": {
    "query_interpretation": {
      "model_interpretation": "Count pembrolizumab trials by phase.",
      "filters_applied": {"query.intr": "pembrolizumab"},
      "assumptions": [],
      "plan_adjustments": []
    },
    "intent": "distribution",
    "group_by": "phase",
    "measure": "trial_count",
    "unit": "trials",
    "invariant": {
      "kind": "partition",
      "statement": "sum(data[].value) == meta.coverage.records_analyzed"
    },
    "coverage": {
      "total_matching_studies": 1,
      "records_fetched": 1,
      "records_analyzed": 1,
      "is_sample": false,
      "coverage_ratio": 1.0,
      "sampling_method": "census",
      "sampling_bias_risk": "none",
      "fetch_cap": 10000,
      "pages_fetched": 1,
      "counts_are_exact": false,
      "excluded": {}
    }
  }
}
```

### Visualization specification

Treat the response as a display contract, not as data to reinterpret. A frontend
renderer should follow this order:

1. Validate that `visualization.type` is supported. Show a visible error for an
   unknown type; do not fall back to a different chart.
2. Display `visualization.title` as visible text.
3. Display coverage and general warnings using the policy below.
4. If `type` is `none`, render the documented empty state and stop.
5. Otherwise, read field names and presentation options from `encoding`, then
   render `data` with the matching type-specific branch.
6. Attach citations to the mark they came from: a flat-data row or a graph edge.

Do not re-interpret the question, use `meta` to select a chart, or infer field
names from labels. Every channel `field` names a property that exists on every
applicable row. `network_graph` is the only type whose `data` is not a flat
array.

| Field | Required | Renderer behavior |
|---|---:|---|
| `type` | Yes | Select exactly one renderer branch using the table below. Unknown values must produce a visible unsupported-type error. |
| `title` | Yes | Display as the visualization heading. |
| `encoding` | Yes | Read channel field names, types, titles, units, sorting, and presentation options from this object. |
| `data` | Yes | Flat row array for every type except `network_graph`; graph data is an object. |

#### Encoding and channel contract

The channel keys are `x`, `y`, `series`, `theta`, `color`, `size`, `value`, and
`label`. A channel object always has `field`; all other channel properties are
optional and are omitted when they do not apply:

| Field | Values | Behavior |
|---|---|---|
| `field` | string | Required row property to read. Never substitute a hard-coded property. |
| `type` | `nominal`, `quantitative`, `temporal` | Defaults to `nominal` if omitted. Select categorical, numeric, or time handling. |
| `axis_title` | string | Display as the axis heading. Omitted means no axis heading. |
| `legend_title` | string | Display as the legend heading. Omitted means no legend heading. |
| `unit` | string | Append to formatted values for this channel. This takes precedence over `meta.unit`. |
| `format` | string | Formatting hint. `integer` means locale-formatted with zero fractional digits. Ignore unknown hints without changing the value. |
| `granularity` | `year` or `month` | Time-axis bucket size. Values are already bucketed; do not aggregate them again. |
| `scale` | `linear` or `log` | Defaults to `linear`. A log scale changes position only, not the displayed raw value. |
| `sort` | string | `none` means preserve response order. If omitted, preserve response order. |

The encoding object can also contain:

- `orientation`: `vertical` or `horizontal`; change mark layout, not channel
  bindings. For example, `x.field` remains the category field for a horizontal
  bar chart even though it is drawn on the vertical screen axis.
- `stack`: `false`, `true`, or `normalize`. `normalize` divides each segment by
  its within-category total for geometry only; tooltips and labels still show
  the raw row value.
- `point_markers`: draw time-series point marks unless explicitly `false`.
- `total`: backend-supplied denominator for part-to-whole geometry. Do not
  recompute it from visible rows.
- `tooltip`: ordered row-field names. Include fields that exist on the row and
  silently skip any absent optional field.
- `layout_hint`: requested graph layout; currently `force`.

Render strings as untrusted text, including titles, labels, warning text,
citations, and graph fields. Never inject them as HTML. Numeric channel values
are JSON numbers; temporal bucket values are display-ready strings.

#### Visualization types

| `type` | Required encoding | Required data fields | Rendering rule |
|---|---|---|---|
| `stat` | `value`, `label` | Fields named by those channels; currently `label`, `value` | Render exactly the first row as one prominent value and supporting label. |
| `bar_chart` | `x`, `y`, `orientation` | Fields named by `x.field` and `y.field`; normally `label`, `value` | One bar per row. Preserve array order when channel sort is `none`. |
| `grouped_bar` | `x`, `y`, `series`, `stack:false` | Fields named by the three channels; currently `label`, `value`, `series` | Group rows with equal `x.field` values and draw one adjacent bar per `series.field` value. Missing x/series combinations mean no mark; do not synthesize zeros. |
| `stacked_bar` | `x`, `y`, `series`, `stack` | Fields named by the three channels; currently `label`, `value`, `series` | Stack series within equal `x.field` values. Normalize geometry only when `stack` is `normalize`. |
| `time_series` | temporal `x`, quantitative `y`, optional `series`, `point_markers` | Encoded fields; currently `bucket`, `value`, and optional `series` | Order x buckets chronologically and draw one line per series, or one line when no series channel exists. Generated zero buckets are already rows; do not zero-fill again. |
| `pie_chart` | `theta`, `color`, `total` | Encoded fields plus `share`; currently `value`, `label` | Use `theta.field / total` for angle and `color.field` for slice identity. Display `share` as a percentage; it is a fraction in `[0,1]`. |
| `scatter_plot` | quantitative `x`, quantitative `y`, optional `size`/`color` | Encoded fields plus `trial_id` and `label` | Draw one point per row. Apply channel scale, title, and unit independently. Use the ordered `tooltip` fields; `trial_id` is suitable as the record identifier. |
| `network_graph` | `node`, `edge`, `layout_hint` | See graph contract below | Render nodes and weighted edges with opaque IDs. |
| `none` | Empty object | Empty array | Render `meta.answer_text` as the primary message and each `meta.suggestions` string as a separate suggested next query. Do not allocate chart space. |

Labels are display-ready. Render them exactly as returned; do not convert enum
values, title-case them, or reconstruct them from `group_by`. Empty flat data for
a chart type is a valid no-points state: keep the title and warnings and
show “No data points”; do not change the type to `none`.

#### Network graph contract

For `network_graph`, data has this shape:

```json
{
  "nodes": [
    {"id": "opaque-id", "label": "Displayed name", "group": "lead_sponsor", "size": 12, "level": 0},
    {"id": "other-id", "label": "Drug name", "group": "intervention_name", "size": 4, "level": 1}
  ],
  "edges": [
    {
      "source": "opaque-id",
      "target": "other-id",
      "weight": 4,
      "citations": [
        {
          "nct_id": "NCT00000001",
          "field_path": "protocolSection.identificationModule.nctId",
          "field_value": "NCT00000001",
          "evidence_kind": "field_value",
          "source_url": "https://clinicaltrials.gov/study/NCT00000001"
        }
      ]
    }
  ],
  "omitted": {
    "trials": 2,
    "distinct_values_collapsed": 1,
    "reason": "Why these trials could not be drawn"
  }
}
```

`omitted` is optional. Never manufacture an “Unknown” node for omitted trials.
When present, show “`trials` trials omitted” and `reason` as visible text near
the graph. If `distinct_values_collapsed` is present, also show how many distinct
values were collapsed.

Read graph property names from the encoding:

```json
{
  "node": {
    "id_field": "id",
    "label_field": "label",
    "group_field": "group",
    "size_field": "size",
    "level_field": "level"
  },
  "edge": {
    "source_field": "source",
    "target_field": "target",
    "weight_field": "weight"
  },
  "layout_hint": "force"
}
```

Read all graph property names from these mappings rather than from the example
names. Node IDs are opaque strings: use them only for joins and never display or
parse them. Every edge endpoint references an existing node ID. Scale node marks
monotonically using the mapped size and edge thickness monotonically using the
mapped weight; the exact pixel scale is a frontend concern. Use the mapped group
for color and legend. The mapped level identifies graph layers but does not
prescribe screen position. Force-layout position and distance carry no semantic
meaning. Select an edge to show its citations; nodes do not own citations.

#### Common row annotations

Flat visualization rows can also contain:

| Field | Meaning |
|---|---|
| `value_is_exact` | `true` only when this value is a server-side count. It is not a formatting instruction. |
| `sample_value` | Number of fetched supporting records when `value` is an exact server-side count. Show as evidence context, not as a replacement value. |
| `is_other` | Row is a top-N tail collapsed into “Other”. Retain it as an ordinary mark. |
| `is_missing` | Row represents an unreported grouping value. Retain it as an ordinary mark. |
| `distinct_values_collapsed` | Number of distinct values represented by an `is_other` row. |
| `citations` | Bounded supporting trial records. |
| `citation_total` | Total represented contributors or memberships. It is independent of the plotted value and citation-array length. |
| `citations_truncated` | `true` means the citation array is incomplete. Show “Showing N of M supporting records.” |
| `citation_sample` | Citations are examples rather than the records used to calculate the value. When true, label the list “Example supporting records.” |
| `citation_basis` | Display-ready explanation of how citations were selected. Show with the citation list. |

Omitted optional booleans mean `false`; omitted optional arrays mean empty; and
omitted optional strings mean “do not render that element.” Zero-filled rows may
have no citations. For flat data, open citations from the selected row; for a
graph, open them from the selected edge. Do not disable a nonzero mark merely
because its citation array is empty.

A citation contains `nct_id`, optional `brief_title`, `field_path`, `field_value`,
`evidence_kind`, optional `excerpt`, and `source_url`. Render the link text as
`nct_id` (optionally followed by `brief_title`) and use `source_url` as its
destination with normal external-link protections. `field_path` and
`field_value` are audit detail; show them in the evidence panel. Show `excerpt`
only when present. Never infer value accuracy or completeness from the number of
citations returned.

#### Sampling and warning policy

Use `meta.coverage.is_sample` as the sole switch for a visually prominent
coverage warning:

- When `true`, show `meta.notes.coverage_warning`. If it is unexpectedly absent,
  show “Results are based on `records_analyzed` of `total_matching_studies`
  matching trials.” If `comparison_arms` is nonempty,
  list every arm as “name: `records_fetched` of `total_matching`” and identify
  arms whose `is_sample` is true. Do not calculate one combined comparison
  percentage.
- When `false`, do not show a coverage banner, including when
  `counts_are_exact` is `true`.
- Render every string in `meta.warnings` as its own additional visible warning,
  regardless of `is_sample`.

`counts_are_exact` describes the complete chart strategy. `value_is_exact`
describes one datum. A complete record census can have exact arithmetic while
`value_is_exact` remains false because the value was calculated locally.

### Response metadata

`meta` contains rendering context, audit information, and provenance:

- Required for every successful response: `query_interpretation`, `intent`,
  `invariant`, and `coverage`.
- Optional null properties are omitted from the JSON, and empty presentation
  properties may also be omitted. Apply the explicit defaults in this section;
  do not use truthiness for numeric fields because zero is meaningful.

| Field | Description |
|---|---|
| `query_interpretation` | `model_interpretation`, exact `filters_applied`, `assumptions`, and defensive `plan_adjustments`. Display in an optional “How this was interpreted” panel. |
| `intent` | Requested analytical intent. Audit context; the selected renderer is still `visualization.type`. |
| `group_by`, `series_by` | Dimensions represented by the chart. Use for context, not field lookup. |
| `measure`, `unit`, `sort` | Measurement semantics. Channel-level units and sorting control marks and axes. |
| `invariant` | `partition` means values are additive as described by `statement`; `multi_label` means a trial can appear in several values and values must not be summed into a unique population. Display `statement` in methodology details. |
| `coverage` | Population, sampling, exactness, request count, cap, per-arm coverage, and exclusions. |
| `truncation` | Top-N rule and how many categories were collapsed. When present, show this in methodology details; the collapsed mark itself is identified by `is_other`. |
| `analysis` | Optional replayable analysis plan and trace. Not required for rendering. |
| `notes` | Coverage warning, counting rule, renderer overrides, and general caveats. Display every `notes.general` string; expose counting rule and overrides in methodology details. |
| `warnings` | Warning strings that should be shown to the user. |
| `visualization_needed`, `visualization_rationale` | Why a chart was or was not selected. |
| `answer_text`, `suggestions` | Primary text and alternatives for `none`; `answer_text` may also supplement a `stat`. |
| `source`, `api_version`, `data_timestamp` | Data provenance. |
| `request_id`, `elapsed_ms` | Request tracing and timing. |

`coverage` contains:

| Field | Meaning |
|---|---|
| `total_matching_studies` | Upstream matching count. For comparisons this is the sum of arm totals, not a deduplicated population. |
| `records_fetched` | Distinct trial records fetched. |
| `records_analyzed` | Records directly inspected by local aggregation. For exact stratified counts, these are citation records rather than the full counted universe. |
| `is_sample` | Whether unfetched matching records could affect chart values. Controls the coverage banner. |
| `coverage_ratio` | `records_fetched / total_matching_studies`; do not use as a comparison-arm completeness measure. |
| `sampling_method` | `census`, `first_n_by_api_relevance_order`, or `stratified_by_bucket`. |
| `sampling_bias_risk` | `none`, `low`, or `high`. |
| `fetch_cap`, `pages_fetched`, `truncated_reason` | Fetch diagnostics. |
| `counts_are_exact` | All displayed category values are exact server-side counts. |
| `comparison_arms` | Map of arm label to `total_matching`, `records_fetched`, and `is_sample`. |
| `excluded` | Counts excluded for missing values, precision, selection, memberships, or invalid numeric pairs. |

Before shipping a renderer, verify these contract cases:

- All nine `type` values have distinct branches, including `stat` and `none`.
- A horizontal bar still reads category and value from the encoded x and y
  fields; only its screen layout changes.
- A normalized stack uses percentages for geometry but raw values in evidence
  and tooltips.
- A zero time-series row is drawn even when it has no citations.
- A graph omission is disclosed and never becomes a synthetic node.
- A sampled response and every `meta.warnings` entry are visibly disclosed.
- A `none` response renders text and suggestions without an empty plot.
- An unknown visualization type and malformed response produce a visible error.
- All response strings are escaped as text.

Detailed per-visualization rendering rules are documented in
[`docs/RESPONSE_SCHEMA.md`](docs/RESPONSE_SCHEMA.md). Machine-readable schemas
and complete examples are available in:

- [`docs/answer.schema.json`](docs/answer.schema.json)
- [`docs/error.schema.json`](docs/error.schema.json)
- [`docs/analysis.schema.json`](docs/analysis.schema.json)
- [`docs/examples/`](docs/examples/)
- [`examples/`](examples/) — five complete responses generated from live queries

### Error response

No matches is a successful response with `visualization.type = "none"`. Transport,
validation, provider, and upstream failures use one error envelope:

```json
{
  "error": {
    "code": "invalid_request",
    "message": "The request did not match the expected schema.",
    "retryable": false,
    "detail": null
  }
}
```

Handle errors as follows:

- Display `error.message` to the user.
- Use `error.code` for application logic and diagnostics.
- Offer a retry only when `error.retryable` is `true`.
- Treat `error.retryable` as authoritative; do not infer retry behavior from the
  HTTP status.
- Treat `error.detail` as optional diagnostic data; never require it for display.

Relevant statuses are 400 for an uninterpretable/rejected query, 422 for invalid
or unsupported input, 429 for provider rate limiting, 502 for ClinicalTrials.gov
failure, 503 for provider availability/quota, 504 for provider timeout, and 500
for configuration or internal failures.
