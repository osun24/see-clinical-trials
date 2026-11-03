"""Per-request planner token accounting.

The release gate scores cost, and the planner is the only metered component:
ClinicalTrials.gov is free, and everything else is local arithmetic. Usage is
read from the provider's own response after the fact, so this module cannot
change what is sent or what the planner returns.

Prices are per million tokens and must be set deliberately — a stale default
reported as fact would be worse than reporting tokens alone, so an unpriced
model yields token counts with a null cost.
"""

from __future__ import annotations

from contextvars import ContextVar

#: USD per million tokens (input, output), per model id. Verify before quoting.
PRICES: dict[str, tuple[float, float]] = {
    "gpt-4.1-mini": (0.40, 1.60),
}

_CALLS: ContextVar[list[dict[str, int]]] = ContextVar("planner_calls")


def start() -> None:
    """Begin accounting for one API request."""
    _CALLS.set([])


def record(usage: object) -> None:
    """Record one planner completion's usage, ignoring a provider that omits it."""
    if usage is None:
        return
    try:
        calls = _CALLS.get()
    except LookupError:
        return
    calls.append({
        "prompt_tokens": int(getattr(usage, "prompt_tokens", 0) or 0),
        "completion_tokens": int(getattr(usage, "completion_tokens", 0) or 0),
        "cached_tokens": int(
            getattr(getattr(usage, "prompt_tokens_details", None), "cached_tokens", 0) or 0
        ),
    })


def summary(model: str) -> dict | None:
    """Planner calls, tokens, and cost for the current request."""
    try:
        calls = _CALLS.get()
    except LookupError:
        return None
    if not calls:
        return None
    prompt = sum(c["prompt_tokens"] for c in calls)
    completion = sum(c["completion_tokens"] for c in calls)
    price = PRICES.get(model)
    cost = None
    if price is not None:
        cost = round(prompt / 1e6 * price[0] + completion / 1e6 * price[1], 6)
    return {
        "planner_calls": len(calls),
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "cached_prompt_tokens": sum(c["cached_tokens"] for c in calls),
        "model": model,
        "usd": cost,
        "pricing_basis": (
            f"{price[0]}/{price[1]} USD per million input/output tokens"
            if price is not None
            else "No price configured for this model; tokens only."
        ),
    }
