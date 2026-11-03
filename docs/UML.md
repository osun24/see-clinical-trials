# Repository UML

These diagrams show the repository's stable architectural relationships rather
than every Pydantic field or private helper. The class diagram is the structural
map; the sequence diagram shows which modules collaborate during `POST /query`.

## Core class diagram

```mermaid
classDiagram
    direction LR

    class FastAPIApp {
        <<boundary>>
        +query(QueryRequest) AnswerResponse
        +plan(QueryRequest) dict
        +dimensions() dict
        +healthz() dict
    }

    class QueryRequest {
        <<input_model>>
        +str query
    }

    class QueryPlan {
        <<model_output>>
        +bool is_clinical_trials_question
        +Intent intent
        +PlanDimension group_by
        +PlanDimension series_by
        +ChartHint chart_hint
        +GraphPlan graph
    }

    class ResolvedPlan {
        <<execution_plan>>
        +str intent
        +str group_by
        +str series_by
        +list adjustments
    }

    class GraphPlan {
        +str root_entity
        +str root_value
        +list~GraphLevel~ levels
        +str layout
        +int node_limit
    }

    class CtgovQuerySpec {
        +dict params
        +tuple fields_mask
        +with_overrides(dict) CtgovQuerySpec
    }

    class CtgovClient {
        <<gateway>>
        +get_json(str, dict) dict
        +data_timestamp() str
    }

    class FetchResult {
        +list~NormalizedStudy~ studies
        +int total_matching
        +int pages_fetched
        +str sampling_method
        +bool is_sample
    }

    class NormalizedStudy {
        +str nct_id
        +str brief_title
        +dict raw
        +from_api(dict) NormalizedStudy
        +str url
    }

    class DimensionDef {
        <<registry_definition>>
        +str id
        +str path
        +str datatype
        +str semantics
        +str missing_policy
        +bool is_exact_capable
    }

    class BucketSet {
        +str dimension
        +str series_dimension
        +list~Bucket~ buckets
        +int records_analyzed
        +bool counts_are_exact
    }

    class Bucket {
        +str key
        +str label
        +int value
        +str series
        +bool value_is_exact
        +list~Citation~ citations
    }

    class Citation {
        +str nct_id
        +str field_path
        +str field_value
        +str evidence_kind
        +str excerpt
        +str source_url
    }

    class Invariant {
        +str kind
        +str statement
        +int assignments_total
        +int records_analyzed
    }

    class Exclusions {
        +int missing_dimension_value
        +int imprecise_for_granularity
    }

    class Truncation {
        +str dimension
        +int shown
        +int total_distinct
        +int collapsed_into_other
    }

    class VizDecision {
        <<policy_result>>
        +str type
        +str rationale
        +bool needed
        +str orientation
        +bool drop_series
        +override(str, str, str)
    }

    class VisualizationSpec {
        +VizType type
        +str title
        +Encoding encoding
        +object data
    }

    class Encoding {
        +Channel x
        +Channel y
        +Channel series
        +Channel theta
        +Channel color
        +str orientation
    }

    class Datum {
        +str label
        +float value
        +str series
        +float share
        +list~Citation~ citations
    }

    class NetworkData {
        +list nodes
        +list edges
        +OmittedFromGraph omitted
    }

    class AnswerResponse {
        <<output_model>>
        +VisualizationSpec visualization
        +Meta meta
    }

    class Meta {
        +QueryInterpretation query_interpretation
        +str intent
        +Invariant invariant
        +Coverage coverage
        +Notes notes
    }

    class Coverage {
        +int total_matching_studies
        +int records_fetched
        +int records_analyzed
        +bool is_sample
        +float coverage_ratio
        +bool counts_are_exact
    }

    class ServiceError {
        <<exception>>
        +str code
        +str message
        +int status_code
        +bool retryable
        +to_response() ErrorResponse
    }

    FastAPIApp ..> QueryRequest : validates
    FastAPIApp ..> CtgovClient : acquires
    FastAPIApp ..> AnswerResponse : returns
    FastAPIApp ..> ServiceError : handles

    QueryRequest ..> QueryPlan : interpret()
    QueryPlan *-- GraphPlan
    QueryPlan ..> ResolvedPlan : resolve()
    ResolvedPlan ..> CtgovQuerySpec : build_query()

    CtgovClient ..> CtgovQuerySpec : sends
    CtgovClient ..> FetchResult : fetch strategies produce
    FetchResult *-- NormalizedStudy

    DimensionDef ..> NormalizedStudy : extracts bucket keys
    FetchResult ..> BucketSet : bucketize()
    BucketSet *-- Bucket
    BucketSet *-- Invariant
    BucketSet *-- Exclusions
    BucketSet *-- Truncation
    Bucket *-- Citation
    Citation --> NormalizedStudy : evidence from raw record

    ResolvedPlan ..> VizDecision : intent and hint
    BucketSet ..> VizDecision : realized shape
    VizDecision ..> VisualizationSpec : selects type
    BucketSet ..> VisualizationSpec : encoders serialize
    VisualizationSpec *-- Encoding
    VisualizationSpec *-- Datum : flat chart data
    VisualizationSpec *-- NetworkData : graph data

    AnswerResponse *-- VisualizationSpec
    AnswerResponse *-- Meta
    Meta *-- Coverage
    Meta *-- Invariant
    Coverage *-- Exclusions
```

`QueryRequest` contains only the question. The language model proposes a
`QueryPlan`; `resolve()` then clamps and cross-validates it before any upstream
request is built. Everything after that boundary is deterministic.

## `POST /query` sequence diagram

```mermaid
sequenceDiagram
    autonumber
    actor Caller
    participant API as app.main
    participant Pipeline as app.pipeline
    participant Planner as app.interpret
    participant OpenAI
    participant Params as app.ctgov.params
    participant Fetch as app.ctgov client + strategies
    participant CTGov as ClinicalTrials.gov v2
    participant Aggregate as app.aggregate
    participant Viz as app.viz

    Caller->>API: POST /query with QueryRequest JSON
    API->>Pipeline: answer(request, client)
    Pipeline->>Planner: interpret(request)
    Planner->>OpenAI: strict QueryPlan schema + question
    OpenAI-->>Planner: QueryPlan
    Planner-->>Pipeline: QueryPlan
    Pipeline->>Planner: resolve(plan, request)
    Planner-->>Pipeline: ResolvedPlan
    Pipeline->>Params: build_query(resolved)
    Params-->>Pipeline: CtgovQuerySpec

    alt named comparison
        Pipeline->>Fetch: fetch_comparison()
        loop each comparison arm, concurrently
            Fetch->>CTGov: scoped /studies query
            CTGov-->>Fetch: studies + totalCount
        end
        Fetch-->>Pipeline: FetchResult + BucketSet + overlap facts
    else exact-capable dimension
        Pipeline->>Fetch: fetch_flat(spec, cap=1)
        Fetch->>CTGov: probe /studies with countTotal
        CTGov-->>Fetch: matching universe size
        Fetch-->>Pipeline: one-record FetchResult
        Pipeline->>Fetch: fetch_stratified()
        loop each known bucket, concurrently
            Fetch->>CTGov: bucket filter + countTotal
            CTGov-->>Fetch: exact count + citation sample
        end
        Fetch-->>Pipeline: FetchResult + exact BucketSet
    else ordinary capped fetch
        Pipeline->>Fetch: fetch_flat()
        loop nextPageToken until complete, cap, or deadline
            Fetch->>CTGov: /studies page
            CTGov-->>Fetch: studies + nextPageToken
        end
        Fetch-->>Pipeline: FetchResult
        Pipeline->>Aggregate: from_sample(studies, dimensions)
        Aggregate-->>Pipeline: BucketSet
    end

    Pipeline->>Viz: resolve(resolved, bucket_set)
    Viz-->>Pipeline: VizDecision
    Pipeline->>Viz: encode selected visualization
    Viz-->>Pipeline: VisualizationSpec data + encoding
    Pipeline->>Fetch: data_timestamp()
    Fetch->>CTGov: GET /version
    CTGov-->>Fetch: data timestamp
    Fetch-->>Pipeline: data timestamp
    Pipeline-->>API: AnswerResponse
    API-->>Caller: visualization + meta
```

The alternatives converge on `FetchResult` and `BucketSet`. That common
boundary lets coverage reporting and visualization selection remain unaware of
whether the data came from comparison arms, exact strata, or ordinary paging.
