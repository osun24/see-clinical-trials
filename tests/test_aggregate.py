"""Aggregation correctness — the invariants a chart silently depends on."""

from __future__ import annotations

from app.aggregate.bucketize import from_sample
from app.aggregate.keys import MISSING_KEY, bucket_keys
from app.dimensions.registry import get
from tests.conftest import make_study

PARTITION_DIMENSIONS = ["phase", "overall_status", "lead_sponsor", "sponsor_type", "study_type"]


# --- the two cardinality traps -------------------------------------------

def test_phase_composite_keeps_partition_invariant(studies):
    """A multi-phase trial counts once, so the bars sum to the trials analyzed.

    Exploding instead would make the bars sum to more than the number of
    trials, with nothing in the chart to reveal it.
    """
    result = from_sample(studies, get("phase"), limit=25)
    assert result.invariant.kind == "partition"
    assert sum(b.value for b in result.buckets) == result.records_analyzed
    assert any(b.label == "Phase 1/Phase 2" for b in result.buckets)


def test_phase_order_is_canonical_not_input_order():
    """["PHASE2","PHASE1"] and ["PHASE1","PHASE2"] are the same bucket."""
    forward = bucket_keys(
        make_study(designModule={"phases": ["PHASE1", "PHASE2"]}).raw, get("phase")
    )
    reversed_ = bucket_keys(
        make_study(designModule={"phases": ["PHASE2", "PHASE1"]}).raw, get("phase")
    )
    assert forward[0][0] == reversed_[0][0] == "PHASE1/PHASE2"


def test_phase_any_explodes_and_declares_multi_label(studies):
    partition = from_sample(studies, get("phase"), limit=25)
    exploded = from_sample(studies, get("phase_any"), limit=25)

    assert exploded.invariant.kind == "multi_label"
    assert sum(b.value for b in exploded.buckets) > sum(b.value for b in partition.buckets)
    assert exploded.invariant.assignments_total == sum(b.value for b in exploded.buckets)

    # "Phase 1" under phase_any must equal every bucket containing PHASE1.
    combined = sum(
        b.value for b in partition.buckets if "PHASE1" in b.key.split("/")
    )
    phase1 = next(b.value for b in exploded.buckets if b.label == "Phase 1")
    assert phase1 == combined


def test_na_phase_is_distinct_from_missing_phase():
    """'Not Applicable' is an editorial answer; 'Not Specified' is absent data.

    Globally these are 240k and 145k studies — merging them would destroy the
    most common finding in the dataset.
    """
    dimension = get("phase")
    na = from_sample([make_study("NCT1", designModule={"phases": ["NA"]})], dimension)
    absent = from_sample([make_study("NCT2", designModule={})], dimension)

    assert [b.label for b in na.buckets] == ["Not Applicable"]
    assert [b.label for b in absent.buckets] == ["Not Specified"]
    assert na.buckets[0].is_missing is False
    assert absent.buckets[0].is_missing is True


def test_country_dedupes_sites_not_studies():
    """Location entries repeat per site; counting them answers the wrong question."""
    locations = [{"country": "United States"}] * 7 + [{"country": "Canada"}]
    study = make_study(contactsLocationsModule={"locations": locations})

    result = from_sample([study], get("country"), limit=10)
    counts = {b.label: b.value for b in result.buckets}

    assert counts == {"United States": 1, "Canada": 1}
    assert result.invariant.kind == "multi_label"
    assert result.invariant.assignments_total == 2
    assert result.invariant.records_analyzed == 1


def test_country_dedupe_on_real_record_with_many_sites(studies):
    """The fixture contains a trial with hundreds of site entries."""
    worst = max(
        studies,
        key=lambda s: len(
            s.raw["protocolSection"].get("contactsLocationsModule", {}).get("locations", [])
        ),
    )
    site_entries = len(
        worst.raw["protocolSection"]["contactsLocationsModule"]["locations"]
    )
    assert site_entries > 100

    keys = bucket_keys(worst.raw, get("country"))
    assert len(keys) == len({k for k, _ in keys})
    assert len(keys) < site_entries


# --- partitions and exclusions --------------------------------------------

def test_every_partition_dimension_sums_to_records_analyzed(studies):
    for dimension_id in PARTITION_DIMENSIONS:
        result = from_sample(studies, get(dimension_id), limit=25)
        assert result.invariant.kind == "partition", dimension_id
        assert sum(b.value for b in result.buckets) == result.records_analyzed, dimension_id


def test_start_date_precision_and_exclusion():
    """Dates arrive as YYYY, YYYY-MM, or YYYY-MM-DD, and sometimes not at all."""
    dimension = get("start_year")
    assert bucket_keys(
        make_study(statusModule={"startDateStruct": {"date": "2016-11"}}).raw, dimension
    ) == [("2016", "2016-11")]
    assert bucket_keys(
        make_study(statusModule={"startDateStruct": {"date": "2021-07-19"}}).raw, dimension
    ) == [("2021", "2021-07-19")]
    assert bucket_keys(make_study(statusModule={}).raw, dimension) == [(MISSING_KEY, None)]

    result = from_sample(
        [
            make_study("NCT1", statusModule={"startDateStruct": {"date": "2016-11"}}),
            make_study("NCT2", statusModule={"startDateStruct": {"date": "2021-07-19"}}),
            make_study("NCT3", statusModule={}),
        ],
        dimension,
    )
    # No honest position on a time axis for "unknown year", so it is excluded
    # loudly rather than dropped silently.
    assert result.exclusions.missing_dimension_value == 1
    assert result.records_analyzed == 2
    assert {b.label for b in result.buckets} == {"2016", "2021"}
    assert not any(b.is_missing for b in result.buckets)


def test_real_fixture_excludes_missing_start_dates(studies):
    result = from_sample(studies, get("start_year"), limit=50)
    assert result.exclusions.missing_dimension_value > 0
    assert result.records_analyzed == len(studies) - result.exclusions.missing_dimension_value
    assert sum(b.value for b in result.buckets) == result.records_analyzed


# --- top-N ----------------------------------------------------------------

def test_top_n_collapses_tail_into_other(studies):
    result = from_sample(studies, get("lead_sponsor"), limit=5)
    ordinary = [b for b in result.buckets if not b.is_other and not b.is_missing]
    other = [b for b in result.buckets if b.is_other]

    assert len(ordinary) == 5
    assert len(other) == 1
    assert other[0].distinct_values_collapsed > 0
    assert other[0].citation_sample is True
    assert result.truncation is not None
    assert result.truncation.shown == 5
    assert sum(b.value for b in result.buckets) == result.records_analyzed


def test_top_n_is_deterministic_and_other_is_last(studies):
    first = from_sample(studies, get("country"), limit=6)
    second = from_sample(studies, get("country"), limit=6)
    assert [b.label for b in first.buckets] == [b.label for b in second.buckets]

    # Fixed tail order: ordinary buckets, then "Other", then the missing
    # bucket -- regardless of magnitude, so position carries meaning.
    kinds = [
        "other" if b.is_other else "missing" if b.is_missing else "ordinary"
        for b in first.buckets
    ]
    assert kinds.count("other") <= 1 and kinds.count("missing") <= 1
    ordinary_end = len(kinds) - len([k for k in kinds if k != "ordinary"])
    assert kinds[:ordinary_end] == ["ordinary"] * ordinary_end
    if "other" in kinds and "missing" in kinds:
        assert kinds.index("other") < kinds.index("missing")
    if "missing" in kinds:
        assert kinds[-1] == "missing"


def test_ties_break_on_label_ascending():
    studies = [
        make_study("NCT1", sponsorCollaboratorsModule={"leadSponsor": {"name": "Zeta"}}),
        make_study("NCT2", sponsorCollaboratorsModule={"leadSponsor": {"name": "Alpha"}}),
        make_study("NCT3", sponsorCollaboratorsModule={"leadSponsor": {"name": "Mid"}}),
    ]
    result = from_sample(studies, get("lead_sponsor"), limit=25)
    assert [b.label for b in result.buckets] == ["Alpha", "Mid", "Zeta"]
