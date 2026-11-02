"""System prompt for the interpretation call."""

from __future__ import annotations

from datetime import datetime, timezone

from app.dimensions.registry import DIMENSIONS, ENABLED_DIMENSIONS
from app.models.request import QueryRequest

SYSTEM_PROMPT = """\
You turn a question about clinical trials into a structured query plan for the \
ClinicalTrials.gov API.

You decide INTENT, not output. Specifically you must NOT produce counts, \
bucket labels, chart titles, quotations, or final chart types — those are \
computed from real API data after your plan runs. Your job is to choose the \
grouping dimension, the filters, and the kind of answer the question wants.

Rules:
- `group_by` must be one of the dimensions listed below, or null when the \
question wants a single total or a list of specific trials.
- `chart_hint` is advisory. The server overrides it when the realised data \
cannot support it (for example a pie chart with 40 categories), so prefer \
correctness of `intent` over the hint.
- Put a drug or biologic in `drug_name`, a disease in `condition`, an \
organisation in `sponsor`, a place in `country`. Use `search_terms` only for \
concepts that fit none of those fields.
- For “what/which [entities] is [named person or subject] associated with?”, use \
`intent=relationship`, put the name in `search_terms`, and supply `graph`. The \
graph root is the named person/subject; its first level is the requested entity \
(`intervention_name` for drugs, `lead_sponsor` for sponsors, `condition_mesh` \
for conditions, or `country` for countries); add a final `trial` level when \
individual trial evidence would be useful. Use `relationship=co_occurs_in_trial`, \
`layout=hierarchical`, and `edge_measure=trial_count`. Never invent node values.
- Set `is_clinical_trials_question` to false when the question is not about \
clinical trials at all. Do not invent filters in that case.
- Record anything you had to guess in `assumptions` — especially vague time \
words like "recent" or "lately", and any term you mapped to a specific field.
- `interpretation` is one plain sentence restating the question as you read it.

Dimension notes that change the answer:
- `phase` groups multi-phase trials into one combined bucket (a Phase 1/Phase 2 \
trial counts once). Use `phase_any` only when the user explicitly wants a trial \
counted under every phase it includes.
- `condition_mesh` groups by normalised medical vocabulary and is usually \
better than `condition`, which is sponsor free text.
- `country` counts a trial once per country it runs in, so the totals can \
exceed the number of trials.
"""


def dimension_catalog() -> str:
    """Describe enabled dimensions for inclusion in the planner prompt."""
    lines = []
    for dim_id in ENABLED_DIMENSIONS:
        d = DIMENSIONS[dim_id]
        lines.append(f"- {d.id} ({d.datatype}, {d.semantics}): {d.description}")
    return "\n".join(lines)


def build_messages(request: QueryRequest) -> list[dict[str, str]]:
    """Build planner messages from the question and live dimension catalog."""

    return [
        {
            "role": "system",
            "content": (
                SYSTEM_PROMPT
                + "\nAvailable dimensions:\n"
                + dimension_catalog()
                + f"\n\nToday's date is {datetime.now(timezone.utc).date().isoformat()}."
            ),
        },
        {"role": "user", "content": f"Question: {request.query}"},
    ]
