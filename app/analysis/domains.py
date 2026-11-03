"""Closed value domains for analysis filter columns.

The planner writes a filter value the way a person says it ("Phase 4"); records
store the API's raw enum ("PHASE4"). ``filter`` compares both sides casefolded,
so those two never match and every row is filtered out — a plan that reads
correctly returns an empty result and the response degrades to "no rows satisfy
the analysis". That is a silent wrong answer, so the domain is enforced here.

The vocabulary comes from the dimension registry's own ``enum_values`` and
``label_fn``, never from model memory: this module only recognises spellings of
values ClinicalTrials.gov already defines.
"""

from __future__ import annotations

from app.dimensions.registry import DIMENSIONS

#: Scalar record fields with a closed API vocabulary but no groupable dimension.
_SCALAR_DOMAINS: dict[str, tuple[str, ...]] = {
    "enrollment_type": ("ACTUAL", "ESTIMATED"),
    "start_date_type": ("ACTUAL", "ESTIMATED"),
    "completion_date_type": ("ACTUAL", "ESTIMATED"),
}


def _normalise(value: object) -> str:
    """Collapse spelling differences that carry no meaning for an enum."""
    return "".join(c for c in str(value).casefold() if c.isalnum())


def _build() -> dict[str, tuple[str, ...]]:
    domains = {
        name: tuple(dimension.enum_values)
        for name, dimension in DIMENSIONS.items()
        if dimension.enum_values
    }
    # ``phase`` is a composite dimension with no enum of its own, but the column
    # the engine reads holds the per-record phase enum.
    domains.setdefault("phase", tuple(DIMENSIONS["phase_any"].enum_values))
    domains.update(_SCALAR_DOMAINS)
    return domains


#: Filter column -> the closed universe of raw values it can hold.
DOMAINS = _build()


def aliases(column: str) -> dict[str, str]:
    """Normalised spelling -> canonical raw value, including display labels."""
    values = DOMAINS.get(column)
    if not values:
        return {}
    dimension = DIMENSIONS.get(column)
    table: dict[str, str] = {}
    for raw in values:
        table[_normalise(raw)] = raw
        if dimension is not None:
            # "Not Applicable" is how the label reads; "NA" is how it is stored.
            table.setdefault(_normalise(dimension.label_fn(raw)), raw)
    return table


def canonical(column: str, value: str) -> str | None:
    """The stored value a filter should compare against.

    Returns ``value`` unchanged for an open column, the canonical raw value for
    a recognised spelling, and ``None`` when the column is closed and the value
    is outside it.
    """
    table = aliases(column)
    if not table:
        return value
    return table.get(_normalise(value))


def describe(column: str) -> str:
    """The allowed values, for an actionable validation error."""
    return ", ".join(DOMAINS.get(column, ()))
