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

from app.config import settings
from app.interpret.prompt import build_messages
from app.models.errors import ServiceError
from app.models.plan import QueryPlan
from app.models.request import QueryRequest


def _client() -> AsyncOpenAI:
    if not settings.openai_api_key:
        raise ServiceError(
            "llm_misconfigured",
            "OPENAI_API_KEY is not set. The interpretation layer cannot run without it.",
            detail={"fix": "Copy .env.example to .env and set OPENAI_API_KEY."},
        )
    return AsyncOpenAI(
        api_key=settings.openai_api_key, timeout=settings.openai_timeout_s
    )


async def interpret(request: QueryRequest) -> QueryPlan:
    """Ask the model for a strict intent plan and map provider failures to API errors."""
    client = _client()

    try:
        completion = await client.chat.completions.parse(
            model=settings.openai_model,
            messages=build_messages(request),  # type: ignore[arg-type]
            response_format=QueryPlan,
            temperature=0,
        )
    except (APITimeoutError, APIConnectionError, RateLimitError) as exc:
        raise ServiceError(
            "llm_unavailable",
            f"The interpretation service is unavailable: {type(exc).__name__}.",
        ) from exc
    except APIStatusError as exc:
        if exc.status_code in (401, 403):
            raise ServiceError(
                "llm_misconfigured",
                "OPENAI_API_KEY was rejected by OpenAI.",
            ) from exc
        raise ServiceError(
            "llm_unavailable",
            f"The interpretation service returned HTTP {exc.status_code}.",
        ) from exc

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
    return plan
