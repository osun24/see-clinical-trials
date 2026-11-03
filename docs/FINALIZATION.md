# Supported capabilities and release decisions

The model proposes search and analysis; Python validates and executes. All facts
come from ClinicalTrials.gov records. No external enrichment or model-inferred
medical categories are supported.

| Family | Supported | Boundary |
|---|---|---|
| Counts/distributions/rankings | Registered fields; deterministic extraction and bins | Multi-label categories overlap |
| Comparisons | Two to five independently searched arms, with or without a breakdown inside each arm | Upstream arm totals are not a deduplicated union. With no breakdown the bars are each arm's exact server-reported total and must not be summed |
| Nested analysis | Locations, interventions, conditions, officials | At most one item expansion; retain parent context |
| Temporal trends | Recorded dates or derived year, optional series | Date event and interval disclosed |
| Numeric relationships | Enrollment and calendar-month duration, descriptive Pearson | No significance/causality; current estimated enrollment cannot recover historical plans |
| Association graphs | Named subject to recorded sponsors/organizations, drugs, conditions, or countries, optionally through trials | Full-text associations; no verified employment, ownership, or identity claims; an unsupported entity is refused, not substituted |
| Co-occurrence graphs | Two recorded entity types across a searched population (sponsor/drug, country/condition) | An edge means both values appear on one trial record: not a partnership, supply relationship, or combination therapy. Unreported values are not drawn. Both axes are capped; dropped values are omitted, never pooled into 'Other' |

Unsupported: therapeutic classes/mechanisms, independent cancer classification,
company ownership, approval status, and treatment-benefit judgments. Return
`unsupported_query` with the unmet requirements and an optional alternative.
Never silently execute that alternative.

An association question whose entity is not a recorded field is refused the same
way. A graph that cannot be built — an unsupported entity level, or a root that
does not occur in the question — returns `unsupported_query` rather than falling
through to whatever else the plan happened to contain. "Organizations" and
"companies" read as the recorded lead sponsor, which is a registered name on a
trial record, not a verified employer or owner.

## Deterministic value handling

Filter values on a closed column are canonicalized against the registry's own
enum universe before execution, so a plan filtering `"Phase 4"` matches the
stored `PHASE4` instead of matching nothing and reporting an empty result. A
value outside the domain is rejected with the allowed values, which the single
planning repair can act on. Every rewrite is reported in
`meta.analysis.schema.filter_canonicalisation`.

Enum values inside a model-supplied `search.advanced` expression are validated
against the same registry universe before any request is sent, so
`AREA[Phase](PHASE1_PHASE2)` is a planning error the repair can act on rather
than an upstream 400 earned at the cost of a live request. A recognised spelling
(`AREA[Phase](Phase 4)`) is rewritten and disclosed; an unrecognised value is
rejected with the allowed values. Areas without a closed enum — sponsor names,
dates, conditions — are left untouched.

Condition abbreviations (`NSCLC`, `GBM`, `TNBC`, …) expand to an explicit OR of
the abbreviation and the full name before reaching `query.cond`. This is a
retrieval alias, not a medical claim: it changes which records are searched,
never what a record is said to contain, and it is disclosed in the response's
assumptions. The table is deterministic and lives in `app/interpret/merge.py`.

## Architecture

Registered dimensions are presets compiled to `memberships` and `aggregate`
operations by `app.analysis.presets`. Both presets and custom linear plans use
`app.analysis.engine`; there is no second local counting implementation.
A grouping whose values form a closed, server-filterable set is counted exactly
per bucket instead of from fetched records, at any population size, and is
chosen over a census only when it needs fewer requests or when the population
exceeds the fetch cap. It is skipped when the question already filters on the
grouped field, because a bucket filter would replace that constraint rather
than narrow it. Comparison arms still sample past the cap: per-arm exact counts
would cost the overlap measurement, which needs real record identifiers.

Comparison arms reuse those presets. Exact-count stratification is an upstream
optimization, not a separate model-generated analysis. Graph serialization remains
a specialized adapter using shared search, evidence, and response models.

One source catalog, operation schema, and dimension registry generate the model's
capability catalog. Compatibility filter fields are not exposed to the planner:
there is one authoritative typed search object. Python controls hosts, endpoint,
field mask, budgets, pagination, retries, citations, arithmetic, and chart policy.

The planner gets one repair attempt after deterministic validation fails. Provider
retries are bounded separately. Authentication, quota, rate limits, timeouts,
provider-schema rejection, upstream errors, and unsupported analyses are distinct.

The demo is optional: a minimal chart with units and coverage remains visible;
assumptions, diagnostics, and raw output are expandable. The complete structured
response is the primary deliverable. See [RESPONSE_SCHEMA.md](RESPONSE_SCHEMA.md).

## Release gate

30 questions (five per family), each repeated three times: 90 full live runs.
At least 90% semantic correctness overall and 80% per family, plus all offline
correctness/contract tests, nonzero evidence, negative tests, and diagnostic checks.
An HTTP 200 is not a semantic pass. Automated assertions measure explicit constraints
and contract properties; report their limitations and audit results independently.
No release claim is made until the evidence satisfies the full gate.

Results, run history, and remaining limitations: [EVALUATION.md](EVALUATION.md).
