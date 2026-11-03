# Codebase guide

This guide is for reading and changing the implementation. The
[README](../README.md) documents the public HTTP API, response format, counting
rules, and operational limits. Here, the same system is described in terms of
Python modules and the values passed between them.

## Start here

Read these files in this order for the shortest useful tour:

1. [`app/models/request.py`](../app/models/request.py) defines accepted input.
2. [`app/pipeline.py`](../app/pipeline.py) shows the complete request flow.
3. [`app/models/plan.py`](../app/models/plan.py) separates model output from the
   plan the server actually executes.
4. [`app/dimensions/registry.py`](../app/dimensions/registry.py) defines every
   supported grouping dimension and its counting semantics.
5. [`app/aggregate/bucketize.py`](../app/aggregate/bucketize.py) turns studies
   into cited counts.
6. [`app/viz/resolver.py`](../app/viz/resolver.py) chooses a valid chart from
   the realized data; [`app/viz/encoders.py`](../app/viz/encoders.py) serializes
   it.
7. [`app/main.py`](../app/main.py) is the thin HTTP boundary.

The central flow is:

```text
HTTP JSON
  -> QueryRequest
  -> QueryPlan                 model interpretation
  -> ResolvedPlan              validated, auditable execution plan
  -> CtgovQuerySpec            upstream parameters and field mask
  -> FetchResult               normalized ClinicalTrials.gov studies
  -> BucketSet                 counts, citations, invariant, exclusions
  -> VisualizationSpec         chart-independent render contract
  -> AnswerResponse            visualization plus provenance and caveats
```

Almost every bug can be localized by asking which arrow first produced an
incorrect value.

For a visual version of these relationships, see the
[UML class and sequence diagrams](UML.md).

## One request, end to end

`POST /query` enters at `app.main.query()`. FastAPI has already parsed and
validated the body as a `QueryRequest`. The route borrows the process-wide
`httpx.AsyncClient`, wraps it in `CtgovClient`, and calls `pipeline.answer()`.

`answer()` has four phases.

### 1. Interpret and resolve

`interpret.planner.interpret()` makes the only OpenAI call. Structured Outputs
must produce a `QueryPlan`: intent, filters, dimensions, and an advisory chart
hint. The model does not produce counts, labels, titles, citations, or the
final chart type.

`interpret.merge.resolve()` converts that model proposal into a
`ResolvedPlan`. This is the trust boundary between probabilistic
interpretation and deterministic execution. It:

- clamps numeric limits and normalizes date ranges;
- removes unsupported or contradictory dimension combinations;
- validates comparison arms and graph paths;
- salvages useful search words if no explicit filter was found; and
- records every correction in `ResolvedPlan.adjustments`.

When interpretation looks wrong, call `POST /plan`. It runs this phase and
returns the resolved plan and exact upstream parameters without fetching trial
records.

### 2. Fetch data

`pipeline._gather_data()` selects one of three paths:

```text
comparison requested?
  yes -> ctgov.compare.fetch_comparison()
         one independent query per named arm
  no  -> exact-capable dimension with a small known bucket universe?
           yes -> ctgov.stratified.fetch_stratified()
                  one count request per bucket, concurrently
           no  -> ctgov.fetch.fetch_flat()
                  sequential nextPageToken pagination up to cap/deadline
```

All paths use `ctgov.params.build_query()` to translate a `ResolvedPlan` into
ClinicalTrials.gov v2 parameters. `CtgovQuerySpec.fields_mask` restricts the
payload to fields needed by registered dimensions and citations.

`CtgovClient` owns retry and cache policy. A client passed by FastAPI shares
the application's connection pool; a standalone client creates and closes its
own pool. The in-memory cache stores successful JSON responses for the
configured TTL and is local to one process.

Raw API records become `NormalizedStudy` objects. Normalization deliberately
extracts only the NCT ID and title while retaining the complete raw record.
Aggregation and citations therefore read the same source data.

### 3. Aggregate and choose a visualization

For ordinary fetches, `aggregate.bucketize.from_sample()` does the counting.
For stratified fetches, `from_strata()` uses server-reported exact values while
using returned records only as citation examples. Both return the same
`BucketSet` contract.

The aggregation path is:

```text
NormalizedStudy.raw
  -> dimensions.extractors.extract_raw()
  -> aggregate.keys.bucket_keys()
  -> group memberships by (primary key, optional series key)
  -> Bucket objects with mechanical citations
  -> aggregate.topn.apply_top_n()
  -> BucketSet
```

`viz.resolver.resolve()` then chooses a chart based on the realized bucket
count, the declared counting invariant, and the request intent. The model's
`chart_hint` can be accepted or refused, but it cannot bypass these checks. A
refusal is included in `meta.notes.overrides`.

`viz.encoders` turns buckets into the public row shape and channel mappings.
Encoders contain no planning logic: by the time an encoder runs, the chart
type is already valid for the data.

### 4. Explain and return

The rest of `pipeline.answer()` builds the response around the visualization:

- `_coverage()` says how much of the matching universe was inspected;
- `viz.titles` creates deterministic titles and the required coverage subtitle;
- `_graph_invariant()` rewrites flat-array invariants in graph terminology;
- `_general_notes()` surfaces dimension-specific and truncation caveats;
- `_filters_applied()` reports the upstream filters actually used; and
- `CtgovClient.data_timestamp()` adds upstream provenance when available.

The returned `AnswerResponse` is strict (`extra="forbid"`) all the way down.
An unexpected field or invalid shape fails close to its source instead of
leaking into a renderer.

## The important data contracts

### `QueryRequest`

Caller input. It contains only the natural-language `query`; filters, grouping,
and presentation intent are derived by the planner. Extra fields are rejected,
so the HTTP contract matches the single text input exposed by the UI.

### `QueryPlan` and `ResolvedPlan`

`QueryPlan` is the strict schema returned by the model. Its enums keep the
model inside concepts the server knows how to execute.

`ResolvedPlan` is the executable version. Downstream code should never consume
`QueryPlan` directly. The resolved form has normalized lists, a concrete
category limit, and an audit trail of defensive adjustments.

### `DimensionDef`

A dimension is configuration plus executable behavior. Its definition states:

- the JSON path or derive function used to read a trial;
- whether one trial has one value or many values;
- whether buckets form a `partition` or are `multi_label`;
- how raw values become stable keys and display labels;
- how missing values are shown or excluded;
- how values are ordered and collapsed to top-N; and
- optionally, how a bucket becomes an exact upstream filter.

The registry is the single source of truth. Planner options, field masks,
aggregation, labels, exact-count eligibility, endpoint documentation, and
chart safety all depend on it.

Do not confuse `cardinality` with `semantics`:

- cardinality describes how many source values a trial may have;
- semantics describes whether the resulting buckets are mutually exclusive.

For example, the API stores phase as a list, but the `phase` dimension joins
that list into one composite key and is therefore a partition. `phase_any`
explodes the same list and is multi-label.

### `FetchResult`

The common result of all fetch strategies. `studies` may be a census, a
relevance-ordered prefix, or citation samples from per-bucket requests.
`total_matching` always refers to the upstream matching universe.
`sampling_method`, `truncated_reason`, and `is_sample` explain the relationship
between the two.

### `BucketSet`

The aggregation boundary. It contains display-ready buckets, but no chart
choice. Its most important fields are:

- `invariant`: whether bucket values partition the analyzed trials or may
  count a trial several times;
- `records_analyzed`: the denominator against which the invariant is checked;
- `exclusions`: records omitted because the dimension has no honest location
  for a missing value;
- `truncation`: the high-cardinality tail folded into `Other`; and
- `counts_are_exact`: whether values came from upstream per-bucket totals.

### `VisualizationSpec`

The renderer contract. Most chart types carry a flat list of rows and an
`Encoding` whose field names exist on every row. `network_graph` is the only
exception and carries `NetworkData` with nodes, edges, and an optional omission
record. `none` is an explicit visualization type rather than a null value.

## Package map

### `app/main.py` — HTTP boundary

Creates the FastAPI application, shared HTTP connection pool, routes, and error
handlers. Keep business logic out of this file. Route handlers should validate,
acquire dependencies, and call pipeline functions.

### `app/pipeline.py` — orchestration

Connects the layers and constructs response metadata. It is the best place to
understand control flow and the wrong place for field extraction or chart
rendering rules.

### `app/interpret/` — natural language to intent

- `prompt.py` builds the system and user messages from the live dimension
  registry.
- `planner.py` owns OpenAI error mapping and strict parsing.
- `merge.py` turns the proposal into a safe, auditable execution plan.

Only this package should depend on the model's judgment.

### `app/ctgov/` — upstream access

- `params.py` maps plans to ClinicalTrials.gov parameters.
- `client.py` provides cached, retried JSON GET requests.
- `fetch.py` performs capped sequential pagination.
- `stratified.py` performs concurrent exact per-bucket counts.
- `compare.py` performs independent named-arm queries and measures overlap.

Upstream response quirks should be contained here or in dimension extraction,
not spread into visualization code.

### `app/dimensions/` — meaning of a grouping

- `registry.py` declares available dimensions.
- `extractors.py` walks dotted paths, including `[]` list segments.
- `labels.py` maps stored enums to reader-facing labels.
- `bins.py` defines numeric ranges.
- `derive.py` computes dimensions that require several source fields.

### `app/aggregate/` — records to cited counts

- `keys.py` produces canonical keys and raw citation values.
- `bucketize.py` groups studies and declares counting invariants.
- `topn.py` ranks primary dimension keys and constructs `Other`.
- `citations.py` creates evidence objects directly from raw records.

Aggregation counts trials, never sites or raw list entries, unless a dimension
explicitly declares multi-label membership. Values are de-duplicated within a
trial before counting.

### `app/viz/` — counts to a renderer contract

- `resolver.py` selects a truthful chart type and orientation.
- `encoders.py` builds rows, channel mappings, and network data.
- `titles.py` creates deterministic titles and coverage subtitles.

The resolver owns chart policy; encoders own serialization. Keeping that split
prevents an encoder from silently accepting a misleading shape.

### `app/models/` — boundaries between layers

Pydantic models make internal contracts executable. `extra="forbid"` is
intentional: schema drift should cause a test failure, not an undocumented
field in production.

### `app/static/index.html` — reference renderer

A no-build demo client. It is both a usable UI and an executable example of
the visualization contract. When adding a visualization type or changing an
encoding, update this file and the renderer contract tests together.

## Trace an example

For:

```json
{"query": "How many pembrolizumab trials are there by phase?"}
```

look for these values in order:

1. `QueryPlan(group_by="phase", drug_name="pembrolizumab", ...)` from the
   planner.
2. The same effective filters in `ResolvedPlan`, plus any adjustments.
3. `CtgovQuerySpec.params["query.intr"] == "pembrolizumab"`.
4. `FetchResult.total_matching` and its normalized studies.
5. Composite phase keys from `bucket_keys()`; a Phase 1/Phase 2 study produces
   one key, not two.
6. A partition `BucketSet` whose values sum to `records_analyzed`.
7. A `VizDecision` such as `bar_chart`.
8. Rows from `encode_bar()` with `label`, `value`, exactness, and citations.
9. Coverage, filters, invariant, and notes in the final `meta` object.

This is also a practical debugging checklist. `POST /plan` covers steps 1–3;
unit tests can exercise every later step without an OpenAI call.

## Finalized execution model

See [FINALIZATION.md](FINALIZATION.md) for scope and [RESPONSE_SCHEMA.md](RESPONSE_SCHEMA.md)
for the renderer contract. `aggregate.bucketize.from_sample` is now a compatibility
adapter: it compiles the registry dimension through `analysis.presets`, then runs
`analysis.engine.execute_sync`. Custom plans use that same executor. Comparison
arms reuse the preset adapter. `from_strata` only materializes server-reported
counts and citations; it is not a second local aggregation algorithm.

The executable operation schema, source catalog, collection catalog, and dimension
registry generate planner capabilities. `interpret.planner.PlannerOutput` exposes
one search representation; old internal filter fields remain only for compatibility.
No classification/enrichment operation exists. Unsupported requirements become
`unsupported_query` before fetching. One invalid plan can be repaired once.

`analysis.response` adapts generic aggregate/relationship results into existing
visualization schemas. Temporal grouping types survive expansion. Registered preset
presentation retains its natural labels and order. Graph serialization is specialized
and limited to full-text trial-record associations.

Provider errors are sanitized in `interpret.provider_errors`; the configured SDK
has one retry, independently of the one semantic plan-repair attempt. Public error
codes distinguish quota, rate limit, timeout, authentication and schema failures.

Extend capabilities by adding a reusable typed operation or source definition and
its deterministic executor semantics, not question-specific regex corrections.
Add fixtures and renderer-contract tests, then update the fixed live evaluation.
