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

#: The entity nouns a question can ask a named subject's associations for. The
#: trial level is handled separately: it is a level of the same graph rather
#: than a target of its own, so "drugs and trials" is one request, not two.
_TARGET_NOUNS = (
    r"drugs?|medications?|interventions?|sponsors?|organi[sz]ations?|"
    r"compan(?:y|ies)|institutions?|conditions?|diseases?|countr(?:y|ies)"
)
_TRIAL_NOUNS = r"trials?|studies|study"

_ENTITY_ASSOCIATION = re.compile(
    rf"\b(?:what|which)\s+({_TARGET_NOUNS})"
    # "... and trials", "... or studies", "..., trials" — an optional request to
    # keep the trial level visible, which the graph always builds anyway.
    rf"(?:\s*,?\s*(?:and|or)?\s*(?:{_TRIAL_NOUNS}))?"
    r"\s+(?:is|are|was|were)\s+(.+?)\s+(?:associated|affiliated|connected|linked)\s+(?:with|to)\b",
    re.IGNORECASE,
)

#: A question that reads as an association request, whatever its entity noun.
#: Used to refuse rather than substitute when no supported graph path exists.
#: The leading interrogative is required: it is what separates "which drugs is X
#: associated with" (a graph) from "is enrollment associated with duration"
#: (a numeric relationship), which must keep its own execution path.
_ASSOCIATION_INTENT = re.compile(
    r"\b(?:what|which)\s+[\w-]+(?:\s*,?\s*(?:and|or)\s+[\w-]+)?"
    r"\s+(?:is|are|was|were)\s+.+?"
    r"\s+(?:associated|affiliated|connected|linked)\s+(?:with|to)\b",
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
    # Recorded lead-sponsor names, not a verified employment or ownership claim.
    "organization": "lead_sponsor",
    "organizations": "lead_sponsor",
    "organisation": "lead_sponsor",
    "organisations": "lead_sponsor",
    "company": "lead_sponsor",
    "companies": "lead_sponsor",
    "institution": "lead_sponsor",
    "institutions": "lead_sponsor",
    "condition": "condition_mesh",
    "conditions": "condition_mesh",
    "disease": "condition_mesh",
    "diseases": "condition_mesh",
    "country": "country",
    "countries": "country",
}


#: Condition abbreviations expanded to an explicit OR of abbreviation and full
#: name before the term reaches ``query.cond``.
#:
#: This is a retrieval alias, not a medical claim: it changes which records are
#: searched, never what any record is said to contain, and no value is derived
#: from it. It exists because the planner spells the same question two ways run
#: to run — "NSCLC" once, "non-small cell lung cancer" the next — which silently
#: changes both recall and the constraint the response reports. Both spellings
#: together retrieve more than either alone (verified live: 8,845 / 8,646 /
#: 9,097 studies), so the expansion is a strict improvement that is also stable.
#: Every entry is a name ClinicalTrials.gov itself records; the mapping is
#: deterministic, auditable here, and disclosed in the response's assumptions.
_CONDITION_ALIASES: dict[str, str] = {
    "nsclc": "non-small cell lung cancer OR NSCLC",
    "sclc": "small cell lung cancer OR SCLC",
    "gbm": "glioblastoma OR GBM",
    "tnbc": "triple negative breast cancer OR TNBC",
    "aml": "acute myeloid leukemia OR AML",
    # "ALL" (acute lymphoblastic leukemia) is deliberately absent: it is also an
    # ordinary English word, and expanding a planner's "all" into a leukemia
    # search would silently narrow a question to the wrong population.
    "cll": "chronic lymphocytic leukemia OR CLL",
    "cml": "chronic myeloid leukemia OR CML",
    "copd": "chronic obstructive pulmonary disease OR COPD",
    "t2d": "type 2 diabetes OR T2D",
    "t1d": "type 1 diabetes OR T1D",
    "ra": "rheumatoid arthritis OR RA",
    "ms": "multiple sclerosis OR MS",
    "ad": "alzheimer disease OR AD",
    "ckd": "chronic kidney disease OR CKD",
    "hcc": "hepatocellular carcinoma OR HCC",
    "crc": "colorectal cancer OR CRC",
    "ibd": "inflammatory bowel disease OR IBD",
}


def expand_condition(value: str | None) -> tuple[str | None, str | None] :
    """Expand a bare condition abbreviation, returning (term, disclosure)."""
    if not value:
        return value, None
    expanded = _CONDITION_ALIASES.get(value.strip().casefold())
    if expanded is None:
        return value, None
    return expanded, (
        f"Searched the condition {value.strip()!r} as {expanded!r} so records using "
        "either spelling are included; this affects retrieval only."
    )


def mentions_first_person(text: str) -> bool:
    """Whether the question says "our"/"my" without naming an organisation."""
    text = re.sub(r"\bU\.?S\.?(?=\s|$)", "United States", text)
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
    def unsupported(reason: str) -> None:
        """Refuse an association question instead of answering a different one.

        Dropping the graph used to leave the rest of the plan intact, so
        "what drugs is <person> associated with" fell through to the default
        sponsor count — a confident answer to a question nobody asked. A
        request this service cannot model is reported as a limitation.
        """
        from app.models.errors import ServiceError

        raise ServiceError(
            "unsupported_query",
            "This association cannot be built from trial-record fields.",
            detail={
                "requirements": [reason],
                "alternative": (
                    "Ask for the recorded sponsors, interventions, conditions, or "
                    "countries a named subject co-occurs with in trial records."
                ),
            },
        )

    graph = data.get("graph")
    if not isinstance(graph, dict):
        data["graph"] = None
        if _ASSOCIATION_INTENT.search(query):
            unsupported(
                "The question asks which entities a subject is associated with, but "
                "the requested entity is not a recorded trial-record field."
            )
        return None

    root_value = str(graph.get("root_value") or "").strip()
    levels = graph.get("levels") or []
    entities = [level.get("entity") for level in levels if isinstance(level, dict)]
    allowed_targets = {"intervention_name", "lead_sponsor", "condition_mesh", "country"}

    # A population graph has no root node: it relates two recorded entity types
    # to each other through the trials that record both. Lower it to the
    # ordinary two-dimension co-occurrence form so it reuses the shared
    # bucketing, citations, and network encoder rather than the subject-rooted
    # adapter, which has a root to hang levels from and this does not.
    if graph.get("root_entity") == "population":
        pair = [e for e in entities if e != "trial"]
        if len(pair) != 2 or len(set(pair)) != 2 or not set(pair) <= allowed_targets:
            data["graph"] = None
            unsupported(
                "A co-occurrence graph relates exactly two distinct recorded entity "
                "types — sponsors, interventions, conditions, or countries — across "
                f"the matching trials; received {entities!r}."
            )
        data.update(
            graph=None,
            intent="relationship",
            group_by=pair[0],
            series_by=pair[1],
            chart_hint="network_graph",
        )
        adjustments.append(
            f"Built a {pair[0]} / {pair[1]} co-occurrence graph across the matching "
            "trials. Edges mean both values are recorded on the same trial."
        )
        return pair
    valid_path = (
        bool(root_value)
        and len(entities) in (1, 2)
        and entities[0] in allowed_targets
        and (len(entities) == 1 or entities[1] == "trial")
    )
    if not valid_path:
        data["graph"] = None
        if _ASSOCIATION_INTENT.search(query):
            unsupported(
                "The proposed association path is not supported: a graph relates a named "
                "subject to recorded sponsors, interventions, conditions, or countries, "
                "optionally through the trials that record them."
            )
        adjustments.append("Dropped an unsupported graph path.")
        return None

    # A graph root is a retrieval subject, not generated display copy. Requiring
    # it to occur in the user's question prevents a plausible-looking but
    # fabricated person or organisation from entering the upstream query.
    if root_value.casefold() not in query.casefold():
        data["graph"] = None
        if _ASSOCIATION_INTENT.search(query):
            unsupported(
                f"The graph subject {root_value!r} does not occur in the question, so it "
                "cannot be verified as the subject that was asked about."
            )
        adjustments.append("Dropped a graph whose root was not present in the question.")
        return None

    requested_limit = int(graph.get("node_limit") or 30)
    graph["node_limit"] = max(5, min(requested_limit, 60))
    graph["root_value"] = root_value
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

    data = plan.model_dump()
    if plan.search is not None:
        search = plan.search
        # The advanced expression is the one planner output that used to reach
        # the API unchecked; an invalid enum value is a planning error, not an
        # upstream failure, so it is caught here before any request is spent.
        from app.interpret.advanced import validate_advanced, validate_area_names

        renamed, area_fixes = validate_area_names(search.advanced)
        checked, rewrites = validate_advanced(renamed)
        rewrites = area_fixes + rewrites
        if rewrites:
            search = search.model_copy(update={"advanced": checked})
            plan = plan.model_copy(update={"search": search})
            data["search"] = search.model_dump()
            adjustments.extend(rewrites)
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

    # The planner spells an abbreviated condition inconsistently run to run,
    # which moves both recall and the reported constraint. Pin it here.
    condition, disclosure = expand_condition(data.get("condition"))
    if disclosure:
        data["condition"] = condition
        data["assumptions"] = list(data.get("assumptions") or []) + [disclosure]
        adjustments.append(disclosure)

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
    # A subject graph owns one dimension; a lowered co-occurrence graph owns
    # both of its axes. Either way these are graph-only dimensions, allowed
    # here although they are not offered for ordinary grouping.
    graph_targets = (
        set(graph_target) if isinstance(graph_target, list)
        else {graph_target} if graph_target else set()
    )
    is_cooccurrence_graph = isinstance(graph_target, list)

    # --- dimension sanity ---------------------------------------------------
    for field in ("group_by", "series_by"):
        value = data.get(field)
        graph_only_dimension = value in graph_targets and value in DIMENSIONS
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
        # Co-occurrence is the question here, not an accidental cross-product:
        # an edge between two multi-label values is exactly what was asked for,
        # and the node budget bounds the result instead.
        and not is_cooccurrence_graph
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

    # Each arm replaces its own slot, but `filter.advanced` is merged with AND.
    # A base expression naming the compared values therefore intersects every
    # arm with the union of all arms, shrinking each one by a different amount
    # and making the same question return a different total run to run.
    if deduped and isinstance(data.get("search"), dict) and data["search"].get("advanced"):
        from app.interpret.advanced import strip_compared_area

        stripped, removed = strip_compared_area(
            data["search"]["advanced"], data.get("compare_by")
        )
        if removed:
            data["search"]["advanced"] = stripped
            adjustments.append(
                "Dropped a base filter on the compared field: each arm supplies its "
                "own value, and keeping it would narrow every arm to the others."
            )

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
    # `advanced` and `status` live only on the typed search object: they are
    # never synced onto the legacy fields above. Ignoring them treated a query
    # constrained solely by an advanced expression as having no filter at all,
    # and ANDed a salvaged phrase like "phases involving semaglutide
    # tirzepatide" onto it, which matches nothing. A question that had real
    # filters then returned zero trials.
    search_data = data.get("search") if isinstance(data.get("search"), dict) else {}
    already_constrained = bool(search_data.get("advanced") or search_data.get("status"))
    if not already_constrained and not any(
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

    if plan.category_selection or plan.unsupported_requirements:
        from app.models.errors import ServiceError
        raise ServiceError("unsupported_query", "This request requires information or classification outside the trial records.",
                           detail={"requirements": plan.unsupported_requirements or [plan.category_selection],
                                   "alternative": "Group by recorded condition or intervention names instead."})
    if plan.search is not None:
        # Apply defensive corrections to the executed search as well as its description.
        search = plan.search.model_copy(update={
            "intervention": data.get("drug_name"), "condition": data.get("condition"),
            "sponsor": data.get("sponsor"), "location": data.get("country"),
            "term": data.get("search_terms"),
            # Rebuilt from the original plan, so corrections already applied to
            # the advanced expression have to be carried over explicitly.
            "advanced": (
                data["search"].get("advanced")
                if isinstance(data.get("search"), dict)
                else plan.search.advanced
            ),
        })
        data["search"] = search
    if plan.analysis is not None:
        from app.analysis.engine import validate
        from app.models.errors import ServiceError
        validate(plan.analysis)
        if plan.graph or plan.compare_by or plan.category_selection:
            raise ServiceError("query_not_interpretable", "Analysis plans cannot be combined with legacy graph, comparison, or category-selection execution.")
    data["adjustments"] = adjustments
    return ResolvedPlan(**data)
