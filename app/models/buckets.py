"""Aggregation results: buckets, the invariant they satisfy, and what was left out."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class Citation(BaseModel):
    """A single trial record supporting one visualised datum.

    Built mechanically from the fetched JSON. ``field_value`` is the raw value
    at ``field_path`` and ``excerpt`` is a verbatim substring of a fetched
    field — never model-generated prose, which would be a fabricated quote.
    """

    model_config = {"extra": "forbid"}

    nct_id: str
    brief_title: str | None = None
    field_path: str
    field_value: str
    evidence_kind: Literal["field_value", "text_excerpt"] = "field_value"
    excerpt: str | None = None
    source_url: str


class Bucket(BaseModel):
    """One counted category or primary/series cell with supporting evidence."""

    model_config = {"extra": "forbid"}

    key: str
    label: str
    value: int
    series: str | None = None

    #: True when ``value`` came from a server-side count rather than the sample.
    value_is_exact: bool = False
    #: The sampled count, kept alongside an exact value for transparency.
    sample_value: int | None = None

    is_other: bool = False
    is_missing: bool = False
    #: True when the SERIES value is absent from the record. Flagged rather
    #: than inferred from the label, so a graph can refuse to draw an edge to
    #: an unreported value without string-matching "Not Reported".
    series_is_missing: bool = False
    distinct_values_collapsed: int | None = None

    citations: list[Citation] = Field(default_factory=list)
    citation_total: int = 0
    citations_truncated: bool = False
    citation_sample: bool = False
    citation_basis: str | None = None


class Invariant(BaseModel):
    """What a client may safely assume about the sum of the bar values."""

    model_config = {"extra": "forbid"}

    kind: Literal["partition", "multi_label"]
    statement: str
    assignments_total: int | None = None
    records_analyzed: int | None = None


class Exclusions(BaseModel):
    """Records that could not be placed honestly on the selected axis."""

    model_config = {"extra": "forbid"}

    missing_dimension_value: int = 0
    imprecise_for_granularity: int = 0
    no_selected_category: int = 0
    memberships_removed: int = 0
    invalid_numeric_pair: int = 0


class Truncation(BaseModel):
    """Description of high-cardinality values collapsed into ``Other``."""

    model_config = {"extra": "forbid"}

    dimension: str
    rule: Literal["top_n"] = "top_n"
    shown: int
    total_distinct: int
    collapsed_into_other: int


class BucketSet(BaseModel):
    """Chart-independent aggregation result and its counting guarantees."""

    model_config = {"extra": "forbid"}

    dimension: str
    series_dimension: str | None = None
    buckets: list[Bucket] = Field(default_factory=list)
    invariant: Invariant
    exclusions: Exclusions = Field(default_factory=Exclusions)
    truncation: Truncation | None = None
    #: Series values dropped by the co-occurrence node budget. They are removed
    #: rather than pooled: an edge to an "Other" node would assert a
    #: co-occurrence that no trial records.
    series_truncated: int = 0
    records_analyzed: int = 0
    counts_are_exact: bool = False
    extra: dict[str, Any] = Field(default_factory=dict)
