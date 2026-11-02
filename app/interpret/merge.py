"""Clamp the model's plan and let explicit request fields win.

Strict Structured Outputs guarantees enum membership and shape, but it ignores
numeric bounds and cannot express a constraint spanning two fields. Everything
that needs real logic therefore lives here, and every change is recorded so the
response can show what was adjusted and why.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

from app.config import settings
from app.dimensions.registry import DIMENSIONS, ENABLED_DIMENSIONS
from app.models.plan import QueryPlan, ResolvedPlan
from app.models.request import QueryRequest

_MIN_YEAR = 1990

#: Request field -> plan field, where the request's value is authoritative.
_OVERRIDE_MAP: dict[str, str] = {
    "drug_name": "drug_name",
    "condition": "condition",
    "sponsor": "sponsor",
    "country": "country",
    "trial_phase": "trial_phase",
    "overall_status": "overall_status",
    "start_year": "start_year",
    "end_year": "end_year",
}


#: Words that carry no retrieval signal. Sending them to a full-text search
#: matches nothing, because the API matches the phrase rather than the ideas.
_STOPWORDS = frozenset("""
a an and are as at be been between by can compare comparison could distribution
do does each for from give growing had has have how i in into is it its lets
list many me most much my of on or our ours over please run running shifted show
split than that the their them there these they this those to top trend trends
us versus vs was we were what when where which who whom whose why will with
would year years activity breakdown centers number pipeline portfolio proportion
share size status study studies trial trials data chart graph plot
""".split())

_POSSESSIVE = ("our ", "my ", "we ", "us ", "ours ")

_SPONSOR_ASSOCIATION = re.compile(
    r"\b(?:what|which)\s+sponsors?\s+(?:is|are|was|were)\s+(.+?)\s+associated\s+with\b",
    re.IGNORECASE,
)


def mentions_first_person(text: str) -> bool:
    """Whether the question says "our"/"my" without naming an organisation."""
    lowered = f" {text.lower()} "
    return any(f" {p}" in lowered for p in _POSSESSIVE)


def sponsor_association_subject(text: str) -> str | None:
    """Return the named subject in “which sponsors is X associated with?”"""
    match = _SPONSOR_ASSOCIATION.search(text)
    return match.group(1).strip(" .,?!;:'\"") if match else None


def _salvage_search_terms(query: str) -> str | None:
    """Reduce a question to the words that could match a trial record."""
    words = [w.strip(".,?!;:'\"()").lower() for w in query.split()]
    kept = [
        w for w in words
        if w and w not in _STOPWORDS and not w.isdigit() and len(w) > 2
    ]
    return " ".join(kept[:6]) or None


def resolve(plan: QueryPlan, request: QueryRequest) -> ResolvedPlan:
    adjustments: list[str] = []
    overridden: list[dict[str, object]] = []

    data = plan.model_dump()
    data["trial_phase"] = data.get("trial_phase") or []
    data["overall_status"] = data.get("overall_status") or []

    # --- explicit request fields are facts, the model's are inferences ------
    for request_field, plan_field in _OVERRIDE_MAP.items():
        supplied = getattr(request, request_field)
        if supplied is None:
            continue
        inferred = data.get(plan_field)
        if inferred not in (None, [], "") and inferred != supplied:
            overridden.append(
                {
                    "field": plan_field,
                    "model_value": inferred,
                    "request_value": supplied,
                }
            )
        data[plan_field] = supplied

    if request.group_by:
        if data.get("group_by") and data["group_by"] != request.group_by:
            overridden.append(
                {
                    "field": "group_by",
                    "model_value": data["group_by"],
                    "request_value": request.group_by,
                }
            )
        data["group_by"] = request.group_by

    # A named-subject sponsor association is a relationship question, not a
    # request to list trials. ClinicalTrials.gov can search the subject as free
    # text; the graph then connects matching lead sponsors to their sponsor
    # types. Keep explicit structured filters, but discard model-inferred slots
    # that commonly mistake a person's name for an organisation or condition.
    association_subject = sponsor_association_subject(request.query)
    if association_subject and request.group_by is None:
        for field in ("drug_name", "condition", "sponsor", "country"):
            if getattr(request, field) is None:
                data[field] = None
        data.update(
            intent="relationship",
            group_by="lead_sponsor",
            series_by="sponsor_type",
            chart_hint="network_graph",
            network_root_label=association_subject,
            search_terms=association_subject,
        )
        adjustments.append(
            f"Interpreted sponsor association for {association_subject!r} as a "
            "lead-sponsor network split by sponsor type."
        )

    # --- dimension sanity ---------------------------------------------------
    for field in ("group_by", "series_by"):
        value = data.get(field)
        if value and value not in ENABLED_DIMENSIONS:
            adjustments.append(f"Dropped unsupported {field} {value!r}.")
            data[field] = None

    if data.get("series_by") and data["series_by"] == data.get("group_by"):
        adjustments.append(
            f"Dropped series_by {data['series_by']!r} because it matched group_by."
        )
        data["series_by"] = None

    # Two compounding multi-label axes produce cells nobody can read: a trial
    # with 9 countries and 6 conditions would land in 54 of them.
    group_dim = DIMENSIONS.get(data.get("group_by") or "")
    series_dim = DIMENSIONS.get(data.get("series_by") or "")
    if (
        group_dim is not None
        and series_dim is not None
        and group_dim.semantics == "multi_label"
        and series_dim.semantics == "multi_label"
    ):
        adjustments.append(
            f"Dropped series_by {series_dim.id!r}: combining it with "
            f"{group_dim.id!r} would count one trial in many cells at once."
        )
        data["series_by"] = None

    # --- comparison arms ----------------------------------------------------
    from app.ctgov.compare import COMPARE_FIELD, MAX_ARMS, MIN_ARMS

    values = [str(v).strip() for v in (data.get("compare_values") or []) if str(v).strip()]
    # De-duplicate case-insensitively; two arms with the same filter would draw
    # two identical series.
    deduped: list[str] = []
    for value in values:
        if value.lower() not in {d.lower() for d in deduped}:
            deduped.append(value)
    if len(deduped) != len(values):
        adjustments.append("Dropped duplicate comparison values.")

    if data.get("compare_by") and data["compare_by"] not in COMPARE_FIELD:
        adjustments.append(
            f"Dropped unsupported compare_by {data['compare_by']!r}; "
            f"expected one of {sorted(COMPARE_FIELD)}."
        )
        data["compare_by"] = None

    if deduped and not data.get("compare_by"):
        adjustments.append("Ignored compare_values because compare_by was not set.")
        deduped = []
    if data.get("compare_by") and len(deduped) < MIN_ARMS:
        adjustments.append(
            f"Ignored the comparison: {len(deduped)} value(s) is fewer than the "
            f"{MIN_ARMS} needed to compare."
        )
        data["compare_by"] = None
        deduped = []
    if len(deduped) > MAX_ARMS:
        adjustments.append(
            f"Kept the first {MAX_ARMS} comparison values of {len(deduped)}."
        )
        deduped = deduped[:MAX_ARMS]

    data["compare_values"] = deduped

    # Each arm already carries the term that matters. A leftover full-text
    # phrase would be ANDed into every arm and match almost nothing.
    if deduped and data.get("search_terms"):
        adjustments.append(
            f"Dropped the free-text term {data['search_terms']!r}: each comparison arm "
            "is filtered by its own value."
        )
        data["search_terms"] = None

    # A comparison supplies its own series, so a second one would be unreadable.
    if deduped and data.get("series_by"):
        adjustments.append(
            f"Dropped series_by {data['series_by']!r}: the comparison already "
            "splits the chart into series."
        )
        data["series_by"] = None

    # --- numeric clamping (strict mode does not enforce bounds) -------------
    limit = request.limit or data.get("limit") or settings.top_n
    clamped_limit = max(3, min(int(limit), 25))
    if clamped_limit != limit:
        adjustments.append(f"Clamped limit from {limit} to {clamped_limit}.")
    data["limit"] = clamped_limit

    this_year = datetime.now(timezone.utc).year + 10
    for field in ("start_year", "end_year"):
        year = data.get(field)
        if year is None:
            continue
        clamped = max(_MIN_YEAR, min(int(year), this_year))
        if clamped != year:
            adjustments.append(f"Clamped {field} from {year} to {clamped}.")
        data[field] = clamped

    if (
        data.get("start_year")
        and data.get("end_year")
        and data["start_year"] > data["end_year"]
    ):
        adjustments.append(
            f"Swapped start_year and end_year ({data['start_year']} > {data['end_year']})."
        )
        data["start_year"], data["end_year"] = data["end_year"], data["start_year"]

    # --- enum hygiene on model-supplied filters ----------------------------
    from app.dimensions.registry import PHASE_VALUES, STATUS_VALUES

    for field, allowed in (("trial_phase", PHASE_VALUES), ("overall_status", STATUS_VALUES)):
        values = [str(v).strip().upper() for v in data.get(field) or []]
        kept = [v for v in values if v in allowed]
        if len(kept) != len(values):
            adjustments.append(
                f"Dropped unrecognised {field} values: "
                f"{sorted(set(values) - set(kept))}."
            )
        data[field] = kept

    # A question with no filter at all would describe the entire registry,
    # which is never what was asked. Sending the whole question as a full-text
    # term is worse than useless, though: ClinicalTrials.gov matches it
    # literally, so a sentence like "Compare our pipeline against Merck's"
    # returns zero trials. Keep only the words that could plausibly be a
    # medical or company term.
    if not any(
        data.get(f)
        for f in ("drug_name", "condition", "sponsor", "country", "search_terms")
    ) and not data.get("trial_phase") and not data.get("overall_status") and not deduped:
        terms = _salvage_search_terms(request.query)
        if terms:
            data["search_terms"] = terms
            adjustments.append(
                f"No specific filter was identified; searched for {terms!r} as free text."
            )
        else:
            adjustments.append(
                "No searchable term could be identified in the question."
            )

    # "Our pipeline" cannot be resolved: the service has no notion of who is
    # asking. Say so rather than letting an invented sponsor string look like
    # a real filter.
    if mentions_first_person(request.query) and not request.sponsor:
        invented = (data.get("sponsor") or "").lower()
        if invented and not any(ch.isupper() for ch in (data.get("sponsor") or "")[1:]):
            adjustments.append(
                f"Ignored the inferred sponsor {data['sponsor']!r}: the question refers to "
                "'our'/'my' organisation, which this service cannot identify."
            )
            data["sponsor"] = None
        data.setdefault("assumptions", [])
        data["assumptions"] = list(data["assumptions"]) + [
            "This question refers to your own organisation, which the service cannot "
            "identify. Pass `sponsor` explicitly to scope the answer to it; the result "
            "below covers all sponsors."
        ]

    data["adjustments"] = adjustments
    data["overridden_fields"] = overridden
    return ResolvedPlan(**data)
