"""The demo renderer must handle every type the API can emit.

`scatter_plot` once shipped with no branch at all and silently rendered as a
different view. A missing branch is now a test failure rather than a silent
downgrade.
"""

from __future__ import annotations

import re
import typing
from pathlib import Path

import pytest

from app.models.viz import VizType

RENDERER = Path(__file__).resolve().parents[1] / "app" / "static" / "index.html"
VIZ_TYPES = sorted(typing.get_args(VizType))


@pytest.fixture(scope="module")
def html() -> str:
    return RENDERER.read_text()


@pytest.fixture(scope="module")
def draw_branches(html: str) -> set[str]:
    """Keys defined on the DRAW object in the renderer."""
    start = html.index("const DRAW = {")
    end = html.index("\n};", start)
    block = html[start:end]
    return set(re.findall(r"^  (\w+)[(:]", block, re.MULTILINE))


@pytest.mark.parametrize("viz_type", VIZ_TYPES)
def test_every_type_has_a_renderer(viz_type: str, draw_branches: set[str]):
    assert viz_type in draw_branches, (
        f"{viz_type} is in VizType but has no DRAW.{viz_type} branch, so it would "
        "silently render as something else"
    )


def test_renderer_has_no_branches_for_unknown_types(draw_branches: set[str]):
    """A stale branch means the enum and the renderer have drifted."""
    assert draw_branches <= set(VIZ_TYPES), (
        f"renderer handles types the API cannot emit: {draw_branches - set(VIZ_TYPES)}"
    )


def test_unknown_type_is_reported(html: str):
    assert "No renderer for" in html


def test_time_series_draws_a_line_not_percentage_height_bars(html: str):
    """The original used percentage heights inside a flex-end container, where
    the percentage had no basis to resolve against and collapsed to a sliver."""
    start = html.index("  time_series(el, v, m) {")
    end = html.index("\n  },\n", start)
    block = html[start:end]
    assert "<svg" in block and "<path" in block
    assert "col-fill" not in block


def test_stacked_bar_actually_stacks(html: str):
    """It previously aliased grouped bars, ignoring stack: 'normalize'."""
    assert "function stackedBars(" in html
    start = html.index("function stackedBars(")
    end = html.index("\n}", start)
    block = html[start:end]
    assert "normalize" in block


def test_network_graph_renders_labeled_hierarchical_levels(html: str):
    start = html.index("  network_graph(el, v, m) {")
    end = html.index("\n  },\n\n  none", start)
    block = html[start:end]
    assert "level_field" in block
    assert "byLevel" in block
    assert "Hierarchical network graph" in block
    assert "nodeEnc.label_field" in block


@pytest.mark.parametrize("viz_type", VIZ_TYPES)
def test_every_type_is_documented(viz_type: str):
    readme = (RENDERER.parents[2] / "README.md").read_text()
    assert f"`{viz_type}`" in readme, f"{viz_type} is not documented in README"
