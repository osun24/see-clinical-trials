"""The closed set of groupable dimensions — the single source of truth.

Every other module reads this registry rather than hardcoding field paths or
cardinality rules, so adding a dimension is one entry here instead of edits
spread across the planner, aggregator, and visualization resolver.

Two properties of an entry carry most of the weight:

``semantics``
    ``partition``   a study lands in exactly one bucket, so
                    ``sum(bucket.value) == records_analyzed``.
    ``multi_label`` a study may land in several buckets, so the sum exceeds the
                    number of trials analyzed. Reported to the client rather
                    than papered over.

``exact_filter``
    When set, each bucket is expressible as a server-side filter, so exact
    per-bucket counts can be fetched instead of counted from a sample. Every
    expression here was verified live against the API; see
    ``tests/test_exact_filters_live.py``.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from app.dimensions import bins, derive as derive_fns, labels

DimensionId = Literal[
    "phase",
    "phase_any",
    "overall_status",
    "start_year",
    "completion_year",
    "lead_sponsor",
    "sponsor_type",
    "study_type",
    "country",
    "condition",
    "condition_mesh",
    "intervention_type",
    "intervention_name",
    "enrollment_bucket",
    "sex",
    "age_group",
    "results_posted",
]

Datatype = Literal["nominal_closed", "nominal_open", "temporal", "numeric"]
Semantics = Literal["partition", "multi_label"]


@dataclass(frozen=True)
class DimensionDef:
    """Extraction, counting, labeling, ordering, and exact-filter policy for an axis."""

    id: str
    api_fields: tuple[str, ...]
    path: str
    datatype: Datatype
    semantics: Semantics
    description: str
    label_fn: Callable[[Any], str] = labels.label_identity
    bin_fn: Callable[[Any], str | None] | None = None
    cardinality_rule: Literal["show_all", "top_n"] = "show_all"
    missing_policy: Literal["explicit_bucket", "exclude"] = "explicit_bucket"
    missing_label: str = "Not Reported"
    sort_mode: Literal["value_desc", "label_asc", "natural"] = "value_desc"
    natural_order: tuple[str, ...] = ()
    # A bucket key -> ClinicalTrials.gov query params. None means this
    # dimension can only be counted from a fetched sample.
    #: Computes the value from the whole record, for dimensions that need
    #: more than one field. When set, ``path`` is not read.
    derive: Callable[[dict[str, Any]], Any] | None = None
    #: The fields a derived dimension's citation must quote.
    source_paths: tuple[str, ...] = ()
    exact_filter: Callable[[str], dict[str, str]] | None = None
    # The closed universe of raw values, when one exists. Required to issue a
    # per-bucket exact-count request without first seeing the data.
    enum_values: tuple[str, ...] = ()
    enabled: bool = True
    notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def is_exact_capable(self) -> bool:
        """Whether exact per-bucket counts can be fetched for this dimension.

        Needs both a server-side filter expression and a knowable bucket
        universe: a closed enum, or a temporal range supplied by the caller.
        """
        if self.exact_filter is None:
            return False
        return bool(self.enum_values) or self.datatype == "temporal"


PHASE_VALUES = ("EARLY_PHASE1", "PHASE1", "PHASE2", "PHASE3", "PHASE4", "NA")

STATUS_VALUES = (
    "ACTIVE_NOT_RECRUITING",
    "APPROVED_FOR_MARKETING",
    "AVAILABLE",
    "COMPLETED",
    "ENROLLING_BY_INVITATION",
    "NO_LONGER_AVAILABLE",
    "NOT_YET_RECRUITING",
    "RECRUITING",
    "SUSPENDED",
    "TEMPORARILY_NOT_AVAILABLE",
    "TERMINATED",
    "UNKNOWN",
    "WITHDRAWN",
    "WITHHELD",
)

SPONSOR_CLASS_VALUES = (
    "AMBIG",
    "FED",
    "INDIV",
    "INDUSTRY",
    "NETWORK",
    "NIH",
    "OTHER",
    "OTHER_GOV",
    "UNKNOWN",
)

STUDY_TYPE_VALUES = ("INTERVENTIONAL", "OBSERVATIONAL", "EXPANDED_ACCESS")

INTERVENTION_TYPE_VALUES = (
    "BEHAVIORAL",
    "BIOLOGICAL",
    "COMBINATION_PRODUCT",
    "DEVICE",
    "DIAGNOSTIC_TEST",
    "DIETARY_SUPPLEMENT",
    "DRUG",
    "GENETIC",
    "OTHER",
    "PROCEDURE",
    "RADIATION",
)


def _area(area: str) -> Callable[[str], dict[str, str]]:
    return lambda key: {"filter.advanced": f"AREA[{area}]({key})"}


def _year_range(key: str) -> dict[str, str]:
    return {"filter.advanced": f"AREA[StartDate]RANGE[{key}-01-01,{key}-12-31]"}


def _completion_year_range(key: str) -> dict[str, str]:
    return {"filter.advanced": f"AREA[CompletionDate]RANGE[{key}-01-01,{key}-12-31]"}


DIMENSIONS: dict[str, DimensionDef] = {
    # --- phase ------------------------------------------------------------
    # The default. A ["PHASE1","PHASE2"] study is ONE fact, so it lands in one
    # combined "Phase 1/Phase 2" bucket. Exploding it would silently break
    # sum(value) == records_analyzed, which is what a viewer assumes a bar
    # chart of trial counts satisfies.
    "phase": DimensionDef(
        id="phase",
        api_fields=("Phase",),
        path="protocolSection.designModule.phases",
        datatype="nominal_closed",
        semantics="partition",
        description="Trial phase. Multi-phase trials get one combined bucket (e.g. 'Phase 1/Phase 2').",
        label_fn=labels.label_phase,
        sort_mode="natural",
        missing_label="Not Specified",
        notes=(
            "'Not Applicable' means a non-phased study (the API's NA value); "
            "'Not Specified' means the record has no phase field at all. These are "
            "different facts and are never merged.",
        ),
    ),
    # Same source field, exploded, for "how many Phase 1 trials are there" —
    # a legitimately different question with a different answer.
    "phase_any": DimensionDef(
        id="phase_any",
        api_fields=("Phase",),
        path="protocolSection.designModule.phases",
        datatype="nominal_closed",
        semantics="multi_label",
        description="Trial phase, counted once per phase a trial includes.",
        label_fn=labels.label_phase,
        sort_mode="natural",
        natural_order=PHASE_VALUES,
        missing_label="Not Specified",
        exact_filter=_area("Phase"),
        enum_values=PHASE_VALUES,
    ),
    # --- status / dates -----------------------------------------------------
    "overall_status": DimensionDef(
        id="overall_status",
        api_fields=("OverallStatus",),
        path="protocolSection.statusModule.overallStatus",
        datatype="nominal_closed",
        semantics="partition",
        description="Recruitment status.",
        label_fn=labels.label_status,
        exact_filter=lambda key: {"filter.overallStatus": key},
        enum_values=STATUS_VALUES,
    ),
    "start_year": DimensionDef(
        id="start_year",
        api_fields=("StartDate",),
        path="protocolSection.statusModule.startDateStruct.date",
        datatype="temporal",
        semantics="partition",
        description="Year the trial started.",
        sort_mode="label_asc",
        # There is no honest position on a time axis for "unknown year".
        missing_policy="exclude",
        exact_filter=_year_range,
    ),
    "completion_year": DimensionDef(
        id="completion_year",
        api_fields=("CompletionDate",),
        path="protocolSection.statusModule.completionDateStruct.date",
        datatype="temporal",
        semantics="partition",
        description="Year the trial completed (or is expected to).",
        sort_mode="label_asc",
        missing_policy="exclude",
        exact_filter=_completion_year_range,
    ),
    # --- sponsor ------------------------------------------------------------
    "lead_sponsor": DimensionDef(
        id="lead_sponsor",
        api_fields=("LeadSponsorName",),
        path="protocolSection.sponsorCollaboratorsModule.leadSponsor.name",
        datatype="nominal_open",
        semantics="partition",
        description="Lead sponsor organisation, verbatim.",
        cardinality_rule="top_n",
        notes=(
            "Sponsor names are not entity-resolved: 'Merck Sharp & Dohme LLC' and "
            "'Merck KGaA' are counted as different sponsors.",
        ),
    ),
    "sponsor_type": DimensionDef(
        id="sponsor_type",
        api_fields=("LeadSponsorClass",),
        path="protocolSection.sponsorCollaboratorsModule.leadSponsor.class",
        datatype="nominal_closed",
        semantics="partition",
        description="Sponsor category — industry, NIH, government, academic/other.",
        label_fn=labels.label_sponsor_class,
        exact_filter=_area("LeadSponsorClass"),
        enum_values=SPONSOR_CLASS_VALUES,
    ),
    "study_type": DimensionDef(
        id="study_type",
        api_fields=("StudyType",),
        path="protocolSection.designModule.studyType",
        datatype="nominal_closed",
        semantics="partition",
        description="Interventional, observational, or expanded access.",
        label_fn=labels.label_study_type,
        exact_filter=_area("StudyType"),
        enum_values=STUDY_TYPE_VALUES,
    ),
    # --- geography / topic --------------------------------------------------
    # The API returns one entry per SITE, so the same country repeats many
    # times inside one study. Counting raw entries would answer a completely
    # different question ("how many sites") than the one asked.
    "country": DimensionDef(
        id="country",
        api_fields=("LocationCountry",),
        path="protocolSection.contactsLocationsModule.locations[].country",
        datatype="nominal_open",
        semantics="multi_label",
        description="Countries with at least one trial site.",
        cardinality_rule="top_n",
        notes=(
            "Site entries are de-duplicated per trial, so a trial with 41 US sites "
            "counts once for the United States. A multi-country trial still counts "
            "once for each country it runs in.",
        ),
    ),
    "condition": DimensionDef(
        id="condition",
        api_fields=("Condition",),
        path="protocolSection.conditionsModule.conditions[]",
        datatype="nominal_open",
        semantics="multi_label",
        description="Conditions studied, as written by the sponsor.",
        cardinality_rule="top_n",
        notes=("Free text, so spelling and specificity vary between sponsors.",),
    ),
    "condition_mesh": DimensionDef(
        id="condition_mesh",
        api_fields=("ConditionMeshTerm",),
        path="derivedSection.conditionBrowseModule.meshes[].term",
        datatype="nominal_open",
        semantics="multi_label",
        description="Conditions as normalised MeSH terms — cleaner grouping than free text.",
        cardinality_rule="top_n",
    ),
    "intervention_type": DimensionDef(
        id="intervention_type",
        api_fields=("InterventionType",),
        path="protocolSection.armsInterventionsModule.interventions[].type",
        datatype="nominal_closed",
        semantics="multi_label",
        description="Kind of intervention — drug, device, behavioral, etc.",
        label_fn=labels.label_intervention_type,
        enum_values=INTERVENTION_TYPE_VALUES,
    ),
    "intervention_name": DimensionDef(
        id="intervention_name",
        api_fields=("InterventionName",),
        path="protocolSection.armsInterventionsModule.interventions[].name",
        datatype="nominal_open",
        semantics="multi_label",
        description="Intervention names, verbatim.",
        cardinality_rule="top_n",
        # Arm-specific dosing strings make this extremely noisy
        # ("Pembrolizumab 200mg Q3W" vs "pembrolizumab"); off unless asked for.
        enabled=False,
        notes=("Names include arm-specific dosing, so grouping is unreliable.",),
    ),
    # --- design / eligibility ------------------------------------------------
    "enrollment_bucket": DimensionDef(
        id="enrollment_bucket",
        api_fields=("EnrollmentCount",),
        path="protocolSection.designModule.enrollmentInfo.count",
        datatype="numeric",
        semantics="partition",
        description="Planned or actual enrollment, in size bands.",
        bin_fn=bins.bin_enrollment,
        sort_mode="natural",
        natural_order=tuple(bins.enrollment_bin_order()),
        missing_policy="exclude",
    ),
    "duration_bucket": DimensionDef(
        id="duration_bucket",
        api_fields=("StartDate", "CompletionDate"),
        # Derived from two fields, so there is no single path to read.
        path=derive_fns.COMPLETION_PATH,
        datatype="numeric",
        semantics="partition",
        description="How long a trial runs, from start to completion, in bands.",
        derive=derive_fns.duration_months,
        source_paths=(derive_fns.START_PATH, derive_fns.COMPLETION_PATH),
        bin_fn=bins.bin_duration_months,
        sort_mode="natural",
        natural_order=tuple(bins.duration_bin_order()),
        # No honest band for a trial whose start or end is unknown.
        missing_policy="exclude",
        notes=(
            "Dates are normalised to the first of the month before subtracting, because "
            "ClinicalTrials.gov mixes year, month and day precision in the same field, "
            "so each duration carries up to about a month of error.",
            "Completion dates are often estimates for trials that are still running.",
        ),
    ),
    "sex": DimensionDef(
        id="sex",
        api_fields=("Sex",),
        path="protocolSection.eligibilityModule.sex",
        datatype="nominal_closed",
        semantics="partition",
        description="Eligible sex.",
        label_fn=labels.label_sex,
        # Verified live: AREA[Sex](ALL) returns the entire result set rather
        # than only sex=ALL trials, so this dimension is sampled-only.
        exact_filter=None,
        notes=("Counted from the fetched sample; the API's Sex filter over-matches.",),
    ),
    "age_group": DimensionDef(
        id="age_group",
        api_fields=("StdAge",),
        path="protocolSection.eligibilityModule.stdAges[]",
        datatype="nominal_closed",
        semantics="multi_label",
        description="Standard age groups a trial enrolls.",
        label_fn=labels.label_age_group,
        sort_mode="natural",
        natural_order=("CHILD", "ADULT", "OLDER_ADULT"),
    ),
    "results_posted": DimensionDef(
        id="results_posted",
        api_fields=("HasResults",),
        path="hasResults",
        datatype="nominal_closed",
        semantics="partition",
        description="Whether results have been posted to ClinicalTrials.gov.",
        label_fn=labels.label_bool,
        exact_filter=_area("HasResults"),
        enum_values=("true", "false"),
    ),
}

#: Dimensions the planner is allowed to choose from.
ENABLED_DIMENSIONS: tuple[str, ...] = tuple(
    d.id for d in DIMENSIONS.values() if d.enabled
)

#: Fields used to verify named-person roots in semantic relationship graphs.
GRAPH_API_FIELDS: tuple[str, ...] = (
    "OverallOfficialName",
    "OverallOfficialAffiliation",
    "OverallOfficialRole",
)

#: Every field the client requests. One mask covers dimensions and graph
#: evidence, so the fetch never depends on which view was chosen.
ALL_API_FIELDS: tuple[str, ...] = tuple(
    dict.fromkeys(
        ("NCTId", "BriefTitle")
        + tuple(f for d in DIMENSIONS.values() for f in d.api_fields)
        + GRAPH_API_FIELDS
    )
)


def get(dimension_id: str) -> DimensionDef:
    """Return a dimension definition or raise a descriptive ``KeyError``."""
    try:
        return DIMENSIONS[dimension_id]
    except KeyError:
        raise KeyError(f"Unknown dimension: {dimension_id!r}") from None


def describe_registry() -> list[dict[str, Any]]:
    """Serialisable registry, for the self-documenting GET /dimensions route."""
    return [
        {
            "id": d.id,
            "description": d.description,
            "datatype": d.datatype,
            "semantics": d.semantics,
            "counting_rule": (
                "Each trial counts once."
                if d.semantics == "partition"
                else "A trial can count in several buckets."
            ),
            "api_fields": list(d.api_fields),
            "source_path": d.path,
            "cardinality_rule": d.cardinality_rule,
            "missing_policy": d.missing_policy,
            "counts_can_be_exact": d.is_exact_capable,
            "notes": list(d.notes),
        }
        for d in DIMENSIONS.values()
        if d.enabled
    ]
