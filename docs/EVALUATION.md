# Release evaluation report

Thirty questions, five per supported family, each run three times against the
live ClinicalTrials.gov API and the live planner: **90 full runs**, plus five
negative cases. Questions were fixed in `evaluations/cases.py` before any run
and were not rewritten after observing output.

Reproduce with `uv run python -m evaluations.run --output evaluations/results.json`.

## Gate result

Release evidence is `evaluations/results.json` (run 6). Run 5 is the preceding
run on behaviourally identical code, kept as a consistency check.

| Threshold | Required | Run 6 | Run 5 |
|---|---|---|---|
| Semantic correctness overall | ≥ 90% | **98.9%** | 100% |
| Lowest family | ≥ 80% | **93.3%** (nested) | 100% |
| Deterministic correctness and contract tests | all pass | **288 pass** | 288 pass |
| Nonzero plotted result has traceable evidence | all | **0 violations** | 0 violations |
| Negative tests return limitations, not substitutes | all | **5/5 refused** | 5/5 refused |

Per family, run 6: counts 100%, comparisons 100%, nested 93.3%, trends 100%,
numeric 100%, graphs 100%.

Cost, latency and recovery, run 6: 96 planner calls over 90 runs (5 repairs),
443,719 prompt tokens of which 417,152 cached, 21,796 completion tokens,
**$0.212 total / $0.0024 per run** at 0.40/1.60 USD per million input/output
tokens. Latency median 3.9 s, p90 6.3 s, max 7.7 s. ClinicalTrials.gov is free
and all analysis is local, so the planner is the only metered component.

## What the automated score does and does not establish

Scoring asserts explicit constraints and contract properties: that each
requested search constraint appears in the executed query, that grouping,
measure and chart type match the question's shape, that encoded fields exist on
every row, that graph edges reference real nodes, and that every nonzero datum
carries evidence. It does **not** establish medical truth, and it is one-sided
about constraints: it verifies that requested constraints are present, never
that no extra constraint was invented. An unrequested `AREA[Phase](PHASE4)` was
observed once in ad-hoc probing; it appears in none of the 540 scored runs, so
it is a rare planner artifact rather than a systemic fault, but the harness
would not have caught it if it were. An HTTP 200 is not a semantic pass.

## Run history

Six gate runs were needed. Each failure was diagnosed to a deterministic cause
and fixed; no question or threshold was changed in response to a result.

| Run | Overall | Lowest family | Gate | Cause addressed afterwards |
|---|---|---|---|---|
| 1 | 86.7% | 66.7% trends | fail | Prose enum filter values; graph phrasing; condition abbreviation |
| 2 | 95.6% | 86.7% trends | pass | False refusal from planner commentary in `unsupported_requirements` |
| 3 | 92.2% | 73.3% trends | fail | Truncated analysis plans; missing intervention-name recipe |
| 4 | 96.7% | 86.7% comparisons | pass | Unvalidated enum values in `search.advanced` |
| 5 | 100% | 100% | pass | — (cost/latency telemetry added; behaviour unchanged) |
| 6 | 98.9% | 93.3% nested | pass | Release evidence |

Run 2 passed the thresholds but was not taken as the release result: it carried
a known false refusal, fixed in run 3. Fixing it perturbed the planner's output
distribution and exposed two further defects, which is why runs 3 and 4 regress
before recovering.

### Causes found and fixed

1. **Prose enum filter values.** `filter` compared both sides casefolded, so a
   planner filtering `"Phase 4"` never matched the stored `PHASE4`. Every row
   was filtered out and the response reported an empty result for a plan that
   read correctly — a silent wrong answer. Values are now canonicalized against
   the registry enum universe and out-of-domain values are rejected with the
   allowed list. (`app/analysis/domains.py`)
2. **Association graph phrasing.** The deterministic backstop matched none of
   the five release questions, and `organizations` was absent from the target
   table. Worse, a dropped graph left the rest of the plan running, so an
   unanswerable association returned an unrelated sponsor count. Unsupported
   associations now return `unsupported_query`. (`app/interpret/merge.py`)
3. **Condition abbreviation instability.** The planner spelled NSCLC two ways
   across runs, moving both recall and the reported constraint. Abbreviations
   now expand to an OR of abbreviation and full name, disclosed as an
   assumption. Verified live: 8,845 / 8,646 / 9,097 studies for abbreviation /
   full name / both.
4. **False refusal.** `unsupported_requirements` had no field description, so
   the planner used it for commentary — including a sentence asserting that the
   request *was* supported — and any entry there refuses the whole request. The
   field now states its boundary. The guard was deliberately left fail-closed:
   ignoring a stated requirement because an executable plan exists would
   substitute an answer. (`app/models/plan.py`)
5. **Truncated analysis plans.** The commonest planner failure is a plan that
   stops before its aggregate. The repair feedback was a bare sentence; it now
   names the step to attach to, the columns in scope, and the exact step shape.
   Standard trends are additionally routed to the registered preset, removing
   the model's opportunity to mis-author them. (`app/analysis/engine.py`,
   `app/interpret/prompt.py`)
6. **Unvalidated advanced expressions.** `search.advanced` was the one planner
   output reaching the API unchecked. `AREA[Phase](PHASE1_PHASE2)` spent a live
   request to earn a 400, surfacing as an upstream failure rather than the
   planning error it was. Enum values in advanced expressions are now validated
   before fetching. (`app/interpret/advanced.py`)

## What remains limited

- **Planner variance is the dominant residual risk.** The same 30 questions
  scored 86.7%, 95.6%, 92.2%, 96.7%, 100% and 98.9% at `temperature=0`. Real
  fixes drove most of that climb, but the spread is wider than the fixes alone
  explain. `gpt-4.1-mini` authoring multi-step plans is the variance source, and
  any schema or prompt edit shifts its output distribution. Two consecutive
  passing runs are better evidence than one, and still a sample rather than a
  guarantee. A single run's rate should not be quoted as the system's rate.
- **The residual failure mode is plan authoring, not analysis.** Run 6's one
  failure is a non-linear step chain the single repair did not fix. It fails
  closed with a diagnostic, never with a wrong chart.
- **One bounded repair by design.** A second attempt would likely convert most
  remaining failures into passes; the one-repair limit is a frozen scope
  decision, not an oversight.
- **Custom plans label buckets with stored values.** A registered dimension
  yields display text (`Phase 4`); a model-authored plan grouping by a record
  column yields the value as stored (`ACTIVE_NOT_RECRUITING`). Documented in the
  renderer contract rather than silently changed, because it alters response
  data for every custom-plan answer.
- **Comparison denominators are a sum of arm totals**, not a deduplicated
  union; overlap is measured and reported.
- **Multi-label categories overlap**, so bucket values must not be summed to
  infer unique trial counts. Enforced by `meta.invariant`.
- Medical truth, and semantic correctness beyond the asserted constraints,
  require independent audit.
