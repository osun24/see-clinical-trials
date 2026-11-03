"""The single OpenAI call: question in, typed QueryPlan out.

Uses Structured Outputs with a strict schema derived from the Pydantic model,
so the *shape* of the result is guaranteed — an invalid dimension or chart type
cannot come back at all. There is no fallback planner by design: a missing key
is a loud misconfiguration, not a reason to silently answer a different
question than the one asked.
"""

from __future__ import annotations

from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    AsyncOpenAI,
    RateLimitError,
)

from app.interpret import usage as usage_meter
from app.interpret.provider_errors import provider_error
from pydantic import create_model
from copy import deepcopy
from app.config import settings
from app.interpret.prompt import build_messages
from app.models.errors import ServiceError
from app.models.plan import QueryPlan
from app.models.request import QueryRequest


# Compatibility filter fields remain accepted internally, but the model has one
# authoritative search object instead of two competing ways to express filters.
_OUTPUT_FIELDS = {"unsupported_requirements", "analysis", "search", "is_clinical_trials_question", "intent", "interpretation",
                  "group_by", "series_by", "chart_hint", "graph", "limit", "compare_by", "compare_values", "assumptions"}
PlannerOutput = create_model("PlannerOutput", __config__={"extra": "forbid"},
    **{name: (field.annotation, deepcopy(field)) for name, field in QueryPlan.model_fields.items() if name in _OUTPUT_FIELDS})


def _client() -> AsyncOpenAI:
    if not settings.openai_api_key:
        raise ServiceError(
            "llm_misconfigured",
            "OPENAI_API_KEY is not set. The interpretation layer cannot run without it.",
            detail={"fix": "Copy .env.example to .env and set OPENAI_API_KEY."},
        )
    return AsyncOpenAI(
        api_key=settings.openai_api_key, timeout=settings.openai_timeout_s, max_retries=1
    )


async def interpret(request: QueryRequest, *, previous: QueryPlan | None = None, feedback: str | None = None) -> QueryPlan:
    """Ask the model for a strict intent plan and map provider failures to API errors."""
    client = _client()
    messages = build_messages(request)
    if previous is not None and feedback:
        messages.extend([
            {"role": "assistant", "content": previous.model_dump_json()},
            {"role": "user", "content": "The deterministic validator rejected this plan: " + feedback +
             ". Correct the plan while preserving every requirement of the original question. "
             "Every input must reference the immediately preceding step ID, starting with trials. "
             "Move supported search constraints into search instead of inventing columns. Do not substitute a different answer."},
        ])

    try:
        completion = await client.chat.completions.parse(
            model=settings.openai_model,
            messages=messages,  # type: ignore[arg-type]
            response_format=PlannerOutput,
            temperature=0,
        )
    except (APITimeoutError, APIConnectionError, APIStatusError) as exc:
        raise provider_error(exc) from exc
    finally:
        await client.close()

    # Telemetry only: read after the fact, it cannot affect the request.
    usage_meter.record(getattr(completion, "usage", None))

    message = completion.choices[0].message

    # A refusal is a failure to plan, never a plan. Treating it as one would
    # silently answer a question the user did not ask.
    if getattr(message, "refusal", None):
        raise ServiceError(
            "query_not_interpretable",
            "The interpretation service declined to plan this question.",
            detail={"refusal": message.refusal},
        )

    plan = message.parsed
    if plan is None:
        raise ServiceError(
            "query_not_interpretable",
            "The interpretation service returned no usable plan for this question.",
        )
    return plan if isinstance(plan, QueryPlan) else QueryPlan.model_validate(plan.model_dump())
