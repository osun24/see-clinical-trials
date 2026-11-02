"""Citations must be derived from the payload, never generated.

This is the core claim of the deep-citations feature, so it is tested as a
property over every bucket of every dimension rather than with one example.
"""

from __future__ import annotations

import pytest

from app.aggregate.bucketize import from_sample
from app.aggregate.citations import serialise_value
from app.dimensions.extractors import extract_raw
from app.dimensions.registry import ENABLED_DIMENSIONS, get

CITED_DIMENSIONS = [
    "phase",
    "phase_any",
    "overall_status",
    "start_year",
    "country",
    "lead_sponsor",
    "sponsor_type",
    "study_type",
    "condition",
    "intervention_type",
    "age_group",
    "results_posted",
]


@pytest.mark.parametrize("dimension_id", CITED_DIMENSIONS)
def test_every_citation_matches_the_raw_payload(studies, dimension_id):
    """Each cited value must literally appear at the cited path in that record."""
    by_id = {s.nct_id: s for s in studies}
    dimension = get(dimension_id)
    result = from_sample(studies, dimension, limit=10, citation_limit=5)

    checked = 0
    for bucket in result.buckets:
        if bucket.is_other:
            continue  # a deliberately labelled sample of collapsed values
        for citation in bucket.citations:
            checked += 1
            assert citation.nct_id in by_id, "cited a trial that was never fetched"
            assert citation.field_path == dimension.path

            record = by_id[citation.nct_id].raw
            present = extract_raw(record, citation.field_path)

            if citation.field_value == "null":
                assert not present
                continue

            candidates = {serialise_value(present)}
            candidates |= {serialise_value(value) for value in present}
            assert citation.field_value in candidates, (
                f"{citation.nct_id}: cited {citation.field_value!r} "
                f"but record holds {present!r}"
            )
    assert checked > 0


@pytest.mark.parametrize("dimension_id", CITED_DIMENSIONS)
def test_excerpts_are_verbatim_substrings(studies, dimension_id):
    """An excerpt is a real quote or it is absent. It is never paraphrase."""
    by_id = {s.nct_id: s for s in studies}
    result = from_sample(studies, get(dimension_id), limit=10, citation_limit=5)

    for bucket in result.buckets:
        for citation in bucket.citations:
            if not citation.excerpt:
                continue
            title = by_id[citation.nct_id].brief_title or ""
            assert citation.excerpt.strip("…") in title
            assert citation.evidence_kind == "text_excerpt"


def test_citation_totals_describe_the_bucket(studies):
    result = from_sample(studies, get("phase"), limit=25, citation_limit=3)
    for bucket in result.buckets:
        if bucket.is_other:
            continue
        assert bucket.citation_total == bucket.value
        assert len(bucket.citations) <= 3
        assert bucket.citations_truncated == (bucket.value > len(bucket.citations))


def test_citation_limit_is_respected(studies):
    for limit in (1, 5, 20):
        result = from_sample(studies, get("overall_status"), citation_limit=limit)
        for bucket in result.buckets:
            if not bucket.is_other:
                assert len(bucket.citations) <= limit


def test_citation_urls_point_at_the_cited_trial(studies):
    result = from_sample(studies, get("overall_status"), citation_limit=4)
    for bucket in result.buckets:
        for citation in bucket.citations:
            assert citation.source_url.endswith(citation.nct_id)
            assert citation.source_url.startswith("https://clinicaltrials.gov/study/")


def test_other_bucket_labels_its_citations_as_a_sample(studies):
    result = from_sample(studies, get("lead_sponsor"), limit=4, citation_limit=5)
    other = next(b for b in result.buckets if b.is_other)
    assert other.citation_sample is True
    assert other.citations_truncated is True
    assert other.citation_basis
    assert len(other.citations) < other.value


def test_serialise_value_matches_api_spelling():
    assert serialise_value(["PHASE1", "PHASE2"]) == '["PHASE1","PHASE2"]'
    assert serialise_value(True) == "true"
    assert serialise_value(False) == "false"
    assert serialise_value("United States") == "United States"
    assert serialise_value(483) == "483"


DERIVED_DIMENSIONS = ["duration_bucket"]


@pytest.mark.parametrize("dimension_id", DERIVED_DIMENSIONS)
def test_derived_citations_quote_their_source_fields(studies, dimension_id):
    """A derived value appears nowhere in the record, so quoting it alone would
    be unverifiable. Every contributing field must be quoted verbatim."""
    by_id = {s.nct_id: s for s in studies}
    dimension = get(dimension_id)
    assert dimension.derive is not None and dimension.source_paths

    checked = 0
    for bucket in from_sample(studies, dimension, limit=10, citation_limit=4).buckets:
        for citation in bucket.citations:
            checked += 1
            record = by_id[citation.nct_id].raw

            # Each source path is named, and its real value is present verbatim.
            for path in dimension.source_paths:
                assert path in citation.field_path
                actual = extract_raw(record, path)
                assert actual, f"{citation.nct_id} is missing {path}"
                assert serialise_value(actual[0]) in citation.field_value

            # And the derived result is reproducible from that same record.
            assert str(dimension.derive(record)) in citation.field_value
    assert checked > 0


def test_all_enabled_dimensions_are_citation_tested():
    """A new dimension must not silently skip citation verification."""
    untested = set(ENABLED_DIMENSIONS) - set(CITED_DIMENSIONS) - set(DERIVED_DIMENSIONS)
    assert untested <= {"completion_year", "condition_mesh", "enrollment_bucket", "sex"}, (
        f"add {untested} to CITED_DIMENSIONS or DERIVED_DIMENSIONS"
    )
