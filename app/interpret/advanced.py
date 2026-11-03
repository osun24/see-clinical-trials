"""Validate enum values inside a model-supplied advanced filter expression.

``search.advanced`` was the one planner output that reached ClinicalTrials.gov
unchecked. A plan carrying ``AREA[Phase](PHASE1_PHASE2)`` — a value the API's
enum does not define — spent a live request to earn a 400, which surfaced as an
upstream rejection rather than as the planning error it is.

The enum universe comes from the dimension registry, so this module recognises
exactly the values ClinicalTrials.gov defines and nothing else. A recognised
spelling is rewritten to the stored value; an unrecognised one is rejected
before fetching, which gives the single planning repair something to act on.
"""

from __future__ import annotations

import re

from app.analysis import domains

#: API search area -> the domain column holding its closed value set.
_AREA_DOMAINS: dict[str, str] = {
    "phase": "phase",
    "overallstatus": "overall_status",
    "studytype": "study_type",
    "leadsponsorclass": "sponsor_type",
    "interventiontype": "intervention_type",
}

#: Record fields the advanced filter accepts although the search-area catalog
#: omits them. Each verified live against the API.
_EXTRA_AREAS = frozenset({
    "CompletionDate", "EnrollmentCount", "HasResults", "OverallStatus",
    "Sex", "StartDate",
})

#: Planner near-misses mapped to the name the API actually resolves.
_AREA_ALIASES = {
    "country": "LocationCountry",
    "status": "OverallStatus",
    "sponsor": "LeadSponsorName",
    "city": "LocationCity",
    "state": "LocationState",
    "facility": "LocationFacility",
    "intervention": "InterventionName",
    "drug": "InterventionName",
    "condition": "Condition",
}


def _valid_areas() -> frozenset[str]:
    from app.dimensions.registry import ALL_API_FIELDS, SEARCH_AREA_NAMES

    return SEARCH_AREA_NAMES | frozenset(ALL_API_FIELDS) | _EXTRA_AREAS


def validate_area_names(expression: str | None) -> tuple[str | None, list[str]]:
    """Rewrite or reject area names the API does not resolve.

    ``AREA[Country](Canada)`` is a 400 ("Unknown area name"); the field is
    ``LocationCountry``. Catching it here turns a spent request and an upstream
    error into a planning error the single repair can act on.
    """
    if not expression:
        return expression, []
    valid = _valid_areas()
    by_fold = {name.casefold(): name for name in valid}
    adjustments: list[str] = []

    def rename(match: re.Match[str]) -> str:
        name = match.group(1)
        if name in valid:
            return match.group(0)
        corrected = by_fold.get(name.casefold()) or _AREA_ALIASES.get(name.casefold())
        if corrected is None:
            from app.models.errors import ServiceError

            raise ServiceError(
                "query_not_interpretable",
                f"AREA[{name}] is not a ClinicalTrials.gov search area. Use a "
                "recorded field name such as LocationCountry, Condition, "
                "InterventionName, LeadSponsorName, Phase or OverallStatus.",
            )
        adjustments.append(f"Rewrote AREA[{name}] as AREA[{corrected}].")
        return match.group(0).replace(f"[{name}]", f"[{corrected}]", 1)

    return _AREA_NAME.sub(rename, expression), adjustments


_AREA = re.compile(r"AREA\[(\w+)\]\(([^()]*)\)", re.IGNORECASE)
_AREA_NAME = re.compile(r"AREA\[(\w+)\]", re.IGNORECASE)
#: Boolean glue inside one AREA(...) group, kept so it can be re-emitted.
_SPLIT = re.compile(r"\s+(?:OR|AND|NOT)\s+", re.IGNORECASE)


def validate_advanced(expression: str | None) -> tuple[str | None, list[str]]:
    """Return the expression with canonical enum values, plus any adjustments.

    Raises ``ServiceError('query_not_interpretable')`` when a value is outside
    the API's enum for that area, naming the allowed values.
    """
    if not expression:
        return expression, []

    adjustments: list[str] = []

    def area(match: re.Match[str]) -> str:
        field, body = match.group(1), match.group(2)
        column = _AREA_DOMAINS.get(field.casefold())
        if column is None or not body.strip():
            return match.group(0)
        pieces = _SPLIT.split(body)
        separators = _SPLIT.findall(body)
        resolved: list[str] = []
        for piece in pieces:
            token = piece.strip()
            if not token:
                resolved.append(piece)
                continue
            canonical = domains.canonical(column, token)
            if canonical is None:
                from app.models.errors import ServiceError

                raise ServiceError(
                    "query_not_interpretable",
                    f"AREA[{field}]({token}) is not a ClinicalTrials.gov "
                    f"{field} value. Allowed values: {domains.describe(column)}. "
                    "Use only these values, or omit the filter.",
                )
            if canonical != token:
                adjustments.append(
                    f"Rewrote AREA[{field}]({token}) as AREA[{field}]({canonical}) "
                    "to match the recorded value."
                )
            resolved.append(canonical)
        rebuilt = resolved[0]
        for separator, piece in zip(separators, resolved[1:]):
            rebuilt += separator + piece
        return f"AREA[{field}]({rebuilt})"

    return _AREA.sub(area, expression), adjustments


#: Comparison slot -> the advanced-filter areas that constrain the same field.
_COMPARED_AREAS: dict[str, frozenset[str]] = {
    "drug_name": frozenset({"interventionname", "interventionothername", "basicsearch"}),
    "condition": frozenset({"condition", "conditionmeshterm", "conditionancestorterm"}),
    "sponsor": frozenset({"leadsponsorname", "collaboratorname", "sponsorsearch"}),
    "country": frozenset({"locationcountry"}),
}

#: Top-level AND between bracketed groups, which is how `with_overrides` joins.
_AND = re.compile(r"\s+AND\s+", re.IGNORECASE)


def strip_compared_area(expression: str | None, compare_by: str | None) -> tuple[str | None, bool]:
    """Remove base filter clauses that constrain the field being compared.

    Each comparison arm replaces its own slot (``query.intr`` and friends), but
    ``filter.advanced`` is merged with AND rather than replaced. A base
    expression naming every compared value therefore intersects each arm with
    the union of all arms. Seen live: a base
    ``(AREA[InterventionName](semaglutide) OR AREA[InterventionName](tirzepatide))``
    cut the semaglutide arm from 796 trials to 632, because the AREA match is
    stricter than the ``query.intr`` the arm sets, and the same question
    returned a different total on every run.

    Returns the expression with those clauses dropped, and whether any were.
    """
    areas = _COMPARED_AREAS.get(str(compare_by))
    if not expression or not areas:
        return expression, False
    kept = [
        clause for clause in _AND.split(expression)
        if not any(name.casefold() in areas for name in _AREA_NAME.findall(clause))
    ]
    if len(kept) == len(_AND.split(expression)):
        return expression, False
    return (" AND ".join(kept) or None), True
