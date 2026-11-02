"""System prompt for the interpretation call."""

from __future__ import annotations

import json
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
- Treat “what/which sponsors is [named person or subject] associated with?” as \
a relationship, not a trial lookup: put the name in `search_terms`, use \
`group_by=lead_sponsor`, `series_by=sponsor_type`, and suggest `network_graph`.
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
    lines = []
    for dim_id in ENABLED_DIMENSIONS:
        d = DIMENSIONS[dim_id]
        lines.append(f"- {d.id} ({d.datatype}, {d.semantics}): {d.description}")
    return "\n".join(lines)


def build_messages(request: QueryRequest) -> list[dict[str, str]]:
    supplied = request.supplied_filter_fields()

    user_parts = [f"Question: {request.query}"]
    if supplied:
        # Given as context so follow-on phrasing ("narrow that to recruiting")
        # is interpreted relative to them. They are re-applied authoritatively
        # after the call regardless of what the model returns.
        user_parts.append(
            "Structured fields the user already supplied (these are facts, not "
            "guesses; they will be enforced):\n" + json.dumps(supplied, indent=2)
        )
    if request.group_by:
        user_parts.append(f"The user explicitly asked to group by: {request.group_by}")

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
        {"role": "user", "content": "\n\n".join(user_parts)},
    ]
