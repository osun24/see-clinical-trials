"""Display labels for ClinicalTrials.gov enum values.

Raw API enums (``PHASE1``, ``NOT_YET_RECRUITING``) are never shown to a viewer.
Mapping lives here so the registry stays declarative and so a label change is a
one-line edit rather than a grep.
"""

from __future__ import annotations

# Canonical ordering for composite phase keys, so ["PHASE2", "PHASE1"] and
# ["PHASE1", "PHASE2"] collapse to the same bucket rather than two.
PHASE_ORDER: dict[str, int] = {
    "EARLY_PHASE1": 0,
    "PHASE1": 1,
    "PHASE2": 2,
    "PHASE3": 3,
    "PHASE4": 4,
    "NA": 5,
}

PHASE_LABELS: dict[str, str] = {
    "EARLY_PHASE1": "Early Phase 1",
    "PHASE1": "Phase 1",
    "PHASE2": "Phase 2",
    "PHASE3": "Phase 3",
    "PHASE4": "Phase 4",
    # A real editorial answer meaning "not a phased study", NOT missing data.
    # Kept distinct from "Not Specified" (no phases key at all) on purpose.
    "NA": "Not Applicable",
}

STATUS_LABELS: dict[str, str] = {
    "ACTIVE_NOT_RECRUITING": "Active, not recruiting",
    "APPROVED_FOR_MARKETING": "Approved for marketing",
    "AVAILABLE": "Available",
    "COMPLETED": "Completed",
    "ENROLLING_BY_INVITATION": "Enrolling by invitation",
    "NO_LONGER_AVAILABLE": "No longer available",
    "NOT_YET_RECRUITING": "Not yet recruiting",
    "RECRUITING": "Recruiting",
    "SUSPENDED": "Suspended",
    "TEMPORARILY_NOT_AVAILABLE": "Temporarily not available",
    "TERMINATED": "Terminated",
    "UNKNOWN": "Unknown status",
    "WITHDRAWN": "Withdrawn",
    "WITHHELD": "Withheld",
}

SPONSOR_CLASS_LABELS: dict[str, str] = {
    "AMBIG": "Ambiguous",
    "FED": "U.S. Federal",
    "INDIV": "Individual",
    "INDUSTRY": "Industry",
    "NETWORK": "Network",
    "NIH": "NIH",
    "OTHER": "Other (academic / hospital)",
    "OTHER_GOV": "Other government",
    "UNKNOWN": "Unknown",
}

STUDY_TYPE_LABELS: dict[str, str] = {
    "INTERVENTIONAL": "Interventional",
    "OBSERVATIONAL": "Observational",
    "EXPANDED_ACCESS": "Expanded access",
}

INTERVENTION_TYPE_LABELS: dict[str, str] = {
    "BEHAVIORAL": "Behavioral",
    "BIOLOGICAL": "Biological",
    "COMBINATION_PRODUCT": "Combination product",
    "DEVICE": "Device",
    "DIAGNOSTIC_TEST": "Diagnostic test",
    "DIETARY_SUPPLEMENT": "Dietary supplement",
    "DRUG": "Drug",
    "GENETIC": "Genetic",
    "OTHER": "Other",
    "PROCEDURE": "Procedure",
    "RADIATION": "Radiation",
}

SEX_LABELS: dict[str, str] = {"ALL": "All", "FEMALE": "Female", "MALE": "Male"}

AGE_GROUP_LABELS: dict[str, str] = {
    "CHILD": "Child (0-17)",
    "ADULT": "Adult (18-64)",
    "OLDER_ADULT": "Older adult (65+)",
}


def _mapper(table: dict[str, str]):
    def label(raw: object) -> str:
        key = str(raw)
        return table.get(key, key.replace("_", " ").title())

    return label


label_phase = _mapper(PHASE_LABELS)
label_status = _mapper(STATUS_LABELS)
label_sponsor_class = _mapper(SPONSOR_CLASS_LABELS)
label_study_type = _mapper(STUDY_TYPE_LABELS)
label_intervention_type = _mapper(INTERVENTION_TYPE_LABELS)
label_sex = _mapper(SEX_LABELS)
label_age_group = _mapper(AGE_GROUP_LABELS)


def label_identity(raw: object) -> str:
    """Pass through free-text values (sponsor, condition, country) unchanged."""
    return str(raw)


#: Values the API and our own bucket keys use for a true boolean. Needed
#: because bucket keys arrive as strings, and ``bool("false")`` is True — which
#: silently labelled both buckets "Results posted".
_TRUTHY = {"true", "1", "yes", "t"}


def label_bool(raw: object) -> str:
    if isinstance(raw, bool):
        truthy = raw
    else:
        truthy = str(raw).strip().lower() in _TRUTHY
    return "Results posted" if truthy else "No results posted"


def label_phase_composite(raws: tuple[str, ...]) -> str:
    """Join a study's phases into the single label ClinicalTrials.gov itself uses."""
    if not raws:
        return "Not Specified"
    return "/".join(label_phase(r) for r in raws)
