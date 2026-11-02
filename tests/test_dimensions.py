"""The registry is the source of truth; these tests keep it honest."""

from __future__ import annotations

import typing

from app.dimensions.registry import (
    ALL_API_FIELDS,
    DIMENSIONS,
    ENABLED_DIMENSIONS,
    get,
)
from app.models.plan import QueryPlan


def test_plan_literal_matches_registry():
    """The model's dimension enum must match what the registry can aggregate.

    If these drift, the model can return a dimension the aggregator cannot
    handle, which strict mode would happily let through.
    """
    annotation = QueryPlan.model_fields["group_by"].annotation
    literal_values = set(typing.get_args(typing.get_args(annotation)[0]))
    assert literal_values == set(ENABLED_DIMENSIONS)


def test_field_mask_covers_every_dimension():
    for dimension in DIMENSIONS.values():
        for field in dimension.api_fields:
            assert field in ALL_API_FIELDS
    assert "NCTId" in ALL_API_FIELDS and "BriefTitle" in ALL_API_FIELDS


def test_exact_capable_requires_filter_and_universe():
    for dimension in DIMENSIONS.values():
        if dimension.is_exact_capable:
            assert dimension.exact_filter is not None
            assert dimension.enum_values or dimension.datatype == "temporal"


def test_sex_is_not_exact_capable():
    """AREA[Sex](ALL) returns the whole result set rather than only ALL trials.

    Verified live; counting it as exact would silently report the universe size
    as one bucket's count.
    """
    assert get("sex").exact_filter is None
    assert not get("sex").is_exact_capable


def test_multi_label_dimensions_are_declared():
    assert get("country").semantics == "multi_label"
    assert get("condition").semantics == "multi_label"
    assert get("phase_any").semantics == "multi_label"
    assert get("phase").semantics == "partition"
    assert get("lead_sponsor").semantics == "partition"
