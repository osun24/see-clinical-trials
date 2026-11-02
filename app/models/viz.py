"""The visualization specification — the contract a frontend renders against.

Two invariants make a renderer a pure ``switch (type)``:

1. ``data`` is always a flat array of uniform row objects. The single
   documented exception is ``network_graph``, which uses ``{nodes, edges}``.
2. Every ``encoding`` channel references a row key by name, so
   ``encoding.x.field`` is guaranteed to be a key present on every row.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from app.models.buckets import Citation

VizType = Literal[
    "stat",
    "bar_chart",
    "grouped_bar",
    "stacked_bar",
    "time_series",
    "pie_chart",
    "scatter_plot",
    "network_graph",
    "none",
]


class Channel(BaseModel):
    """One visual channel bound to a field on every data row."""

    model_config = {"extra": "forbid"}

    field: str
    type: Literal["nominal", "quantitative", "temporal"] = "nominal"
    axis_title: str | None = None
    legend_title: str | None = None
    unit: str | None = None
    format: str | None = None
    granularity: Literal["year", "month"] | None = None
    scale: Literal["linear", "log"] | None = None
    sort: str | None = None


class Encoding(BaseModel):
    """Channels for every supported type. Only the relevant keys are populated."""

    model_config = {"extra": "forbid"}

    x: Channel | None = None
    y: Channel | None = None
    series: Channel | None = None
    theta: Channel | None = None
    color: Channel | None = None
    size: Channel | None = None
    value: Channel | None = None
    label: Channel | None = None
    orientation: Literal["vertical", "horizontal"] | None = None
    stack: bool | Literal["normalize"] | None = None
    point_markers: bool | None = None
    total: int | None = None
    tooltip: list[str] = Field(default_factory=list)

    node: dict[str, str] | None = None
    edge: dict[str, str] | None = None
    layout_hint: str | None = None


class Datum(BaseModel):
    """One row of ``data``.

    Declared with ``extra="allow"`` because each visualization type adds its own
    keys (for example, ``share`` for pie charts). The fields below are the ones
    a renderer can rely on for any aggregated type.
    """

    model_config = {"extra": "allow"}

    label: str | None = None
    # int first so counts stay integers in JSON; float allows a future
    # non-count measure (a mean enrollment, say) without a schema change.
    value: int | float | None = None
    series: str | None = None
    #: True when ``value`` is a server-side count rather than a sample count.
    value_is_exact: bool | None = None
    sample_value: int | None = None
    is_other: bool | None = None
    is_missing: bool | None = None
    distinct_values_collapsed: int | None = None
    #: The trial records this datum was computed from.
    citations: list[Citation] = Field(default_factory=list)
    citation_total: int | None = None
    citations_truncated: bool | None = None
    citation_sample: bool | None = None
    citation_basis: str | None = None


class OmittedFromGraph(BaseModel):
    """Trials that could not be placed on the graph, stated rather than dropped.

    A collapsed "Other" bucket has no second-dimension value, so it has no
    edge to sit on. Silently leaving it out would make the edge weights fail to
    account for every analyzed trial with nothing in the response to say so.
    """

    model_config = {"extra": "forbid"}

    trials: int
    distinct_values_collapsed: int | None = None
    reason: str


class NetworkData(BaseModel):
    """``data`` for ``network_graph`` — the one non-flat shape."""

    model_config = {"extra": "forbid"}

    nodes: list[dict[str, Any]] = Field(default_factory=list)
    edges: list[dict[str, Any]] = Field(default_factory=list)
    omitted: OmittedFromGraph | None = None


class VisualizationSpec(BaseModel):
    model_config = {"extra": "forbid"}

    type: VizType
    title: str
    #: Always present and never null. States the basis of the numbers, so a
    #: viewer cannot miss that a count came from a sample. The renderer
    #: contract requires displaying it beneath the title.
    subtitle: str
    encoding: Encoding = Field(default_factory=Encoding)
    data: list[Datum] | NetworkData = Field(default_factory=list)
