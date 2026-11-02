"""Clamp and defensively validate the model's plan.

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

_ENTITY_ASSOCIATION = re.compile(
    r"\b(?:what|which)\s+(drugs?|medications?|interventions?|sponsors?|conditions?|countries?)"
    r"\s+(?:is|are|was|were)\s+(.+?)\s+associated\s+with\b",
    re.IGNORECASE,
)

_ASSOCIATION_TARGETS = {
    "drug": "intervention_name",
    "drugs": "intervention_name",
    "medication": "intervention_name",
    "medications": "intervention_name",
    "intervention": "intervention_name",
    "interventions": "intervention_name",
    "sponsor": "lead_sponsor",
    "sponsors": "lead_sponsor",
    "condition": "condition_mesh",
    "conditions": "condition_mesh",
    "country": "country",
    "countries": "country",
}


def mentions_first_person(text: str) -> bool:
    """Whether the question says "our"/"my" without naming an organisation."""
    lowered = f" {text.lower()} "
    return any(f" {p}" in lowered for p in _POSSESSIVE)


def entity_association(text: str) -> tuple[str, str] | None:
    """Return (target dimension, subject) for named association questions."""
    match = _ENTITY_ASSOCIATION.search(text)
    if not match:
        return None
    target = _ASSOCIATION_TARGETS[match.group(1).lower()]
    subject = match.group(2).strip(" .,?!;:'\"")
    return target, subject


def _looks_like_person(value: str) -> bool:
    words = [word for word in value.replace("-", " ").split() if word]
    return 2 <= len(words) <= 5 and all(word[:1].isupper() for word in words)


def _normalise_graph_plan(
    data: dict, query: str, adjustments: list[str]
) -> str | None:
    """Validate graph semantics and return its first entity dimension."""
    graph = data.get("graph")
    if not isinstance(graph, dict):
        data["graph"] = None
        return None

    root_value = str(graph.get("root_value") or "").strip()
    levels = graph.get("levels") or []
    entities = [level.get("entity") for level in levels if isinstance(level, dict)]
    allowed_targets = {"intervention_name", "lead_sponsor", "condition_mesh", "country"}
    valid_path = (
        bool(root_value)
        and len(entities) in (1, 2)
        and entities[0] in allowed_targets
        and (len(entities) == 1 or entities[1] == "trial")
    )
    if not valid_path:
        adjustments.append("Dropped an unsupported graph path.")
        data["graph"] = None
        return None

    # A graph root is a retrieval subject, not generated display copy. Requiring
    # it to occur in the user's question prevents a plausible-looking but
    # fabricated person or organisation from entering the upstream query.
    if root_value.casefold() not in query.casefold():
        adjustments.append("Dropped a graph whose root was not present in the question.")
        data["graph"] = None
        return None

    requested_limit = int(graph.get("node_limit") or 30)
    graph["node_limit"] = max(5, min(requested_limit, 60))
    graph["root_value"] = root_value
    graph["layout"] = "hierarchical" if len(entities) > 1 else graph.get("layout", "hierarchical")
    data.update(
        intent="relationship",
        group_by=entities[0],
        series_by=None,
        chart_hint="network_graph",
        search_terms=data.get("search_terms") or root_value,
    )
    return str(entities[0])


def _salvage_search_terms(query: str) -> str | None:
    """Reduce a question to the words that could match a trial record."""
    words = [w.strip(".,?!;:'\"()").lower() for w in query.split()]
    kept = [
        w for w in words
        if w and w not in _STOPWORDS and not w.isdigit() and len(w) > 2
    ]
    return " ".join(kept[:6]) or None


def resolve(plan: QueryPlan, request: QueryRequest) -> ResolvedPlan:
    """Turn model output into the validated plan that downstream code executes.

    Unsupported combinations, out-of-range values, and graph/comparison
    inconsistencies are corrected and recorded in the returned plan.
    """
    adjustments: list[str] = []
    if re.search(r"\b(?:which|what)\s+(?:(?:u\.s\.|us|united states|american)\s+)?cities\b|\bby\s+city\b", request.query, re.IGNORECASE):
        from app.models.errors import ServiceError
        raise ServiceError("query_not_interpretable", "City-level grouping is not supported yet. This service cannot substitute country counts for requested cities.")

    data = plan.model_dump()
    if plan.search is not None:
        search = plan.search
        if search.start_date and search.end_date and search.start_date > search.end_date:
            from app.models.errors import ServiceError
            raise ServiceError("query_not_interpretable", "Search date bounds are reversed.")
        # Keep descriptive fields synchronized with the request actually executed.
        for target, source in (("drug_name", "intervention"), ("condition", "condition"),
                               ("sponsor", "sponsor"), ("country", "location"),
                               ("search_terms", "term")):
            data[target] = getattr(search, source)
        data["start_year"] = None
        data["end_year"] = None

    data["trial_phase"] = data.get("trial_phase") or []
    data["overall_status"] = data.get("overall_status") or []

    # A named-subject association is a semantic graph, not a two-axis chart.
    # The regex is a defensive correction when the model mistakes it for a
    # lookup; the model can propose the same constrained GraphPlan directly.
    association = entity_association(request.query)
    if association:
        association_target, association_subject = association
        for field in ("drug_name", "condition", "sponsor", "country"):
            data[field] = None
        data.update(
            graph={
                "root_entity": (
                    "person" if _looks_like_person(association_subject) else "search_subject"
                ),
                "root_value": association_subject,
                "levels": [
                    {"entity": association_target, "relationship": "co_occurs_in_trial"},
                    {"entity": "trial", "relationship": "co_occurs_in_trial"},
                ],
                "layout": "hierarchical",
                "edge_measure": "trial_count",
                "node_limit": 30,
            },
            search_terms=association_subject,
        )
        adjustments.append(
            f"Interpreted association for {association_subject!r} as an evidence-backed "
            f"{association_target} relationship graph."
        )

    graph_target = _normalise_graph_plan(data, request.query, adjustments)

    # --- dimension sanity ---------------------------------------------------
    for field in ("group_by", "series_by"):
        value = data.get(field)
        graph_only_dimension = field == "group_by" and value == graph_target and value in DIMENSIONS
        if value and value not in ENABLED_DIMENSIONS and not graph_only_dimension:
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
    limit = data.get("limit") or settings.top_n
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
    if mentions_first_person(request.query):
        inferred_sponsor = str(data.get("sponsor") or "").strip()
        if inferred_sponsor and inferred_sponsor.casefold() not in request.query.casefold():
            adjustments.append(
                f"Ignored the inferred sponsor {inferred_sponsor!r}: the question refers to "
                "'our'/'my' organisation, which this service cannot identify."
            )
            data["sponsor"] = None
        data.setdefault("assumptions", [])
        data["assumptions"] = list(data["assumptions"]) + [
            "This question refers to your own organisation, which the service cannot "
            "identify. Name the organisation in the question to scope the answer; "
            "the result below covers all sponsors."
        ]

    if data.get("category_selection") and data.get("group_by") not in {"condition", "condition_mesh"}:
        # Keep the selected medical entities on the primary axis so filtering,
        # citations, and top-N all operate on those entities before any split.
        primary = data.get("group_by")
        secondary = data.get("series_by")
        if secondary in {"condition", "condition_mesh"} or (primary and secondary is None):
            data["group_by"] = secondary or "condition_mesh"
            data["series_by"] = primary
            adjustments.append("Made conditions the primary grouping and retained the requested breakdown as the series for category selection.")
    if data.get("category_selection") and (
        data.get("group_by") not in {"condition", "condition_mesh"}
        or data.get("graph") or data.get("compare_values")
        or data.get("intent") in {"count_total", "lookup"}
    ):
        from app.models.errors import ServiceError
        raise ServiceError("query_not_interpretable", "Category selection currently requires a condition distribution without comparisons or graphs.")
    if plan.search is not None:
        # Apply defensive corrections to the executed search as well as its description.
        search = plan.search.model_copy(update={
            "intervention": data.get("drug_name"), "condition": data.get("condition"),
            "sponsor": data.get("sponsor"), "location": data.get("country"),
            "term": data.get("search_terms"),
        })
        data["search"] = search
        adjustments.append("Direct searches use record-based aggregation; exact per-bucket fetching is disabled.")
    data["adjustments"] = adjustments
    return ResolvedPlan(**data)
