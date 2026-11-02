"""Build citations mechanically from fetched records.

Nothing here is generated. ``field_value`` is the raw value at ``field_path``
serialised exactly as the API returned it, and ``excerpt`` — when present — is a
verbatim substring of a fetched text field. A model-written "excerpt" would be a
fabricated quote, which is the worst available failure mode for a traceability
feature, so the model is never given the chance.
"""

from __future__ import annotations

import json
from typing import Any

from app.dimensions.registry import DimensionDef
from app.models.buckets import Citation
from app.models.study import NormalizedStudy

MISSING_KEY = "__missing__"


def serialise_value(raw: Any) -> str:
    """Render a raw field value the way the API expresses it."""
    if isinstance(raw, (list, dict)):
        return json.dumps(raw, separators=(",", ":"))
    if isinstance(raw, bool):
        return "true" if raw else "false"
    return str(raw)


def _verbatim_excerpt(study: NormalizedStudy, needle: str) -> str | None:
    """A real quote from the title when it genuinely contains the value.

    Returns ``None`` rather than paraphrasing when the value does not literally
    appear, so an excerpt is always checkable against the source.
    """
    title = study.brief_title
    if not title or not needle or len(needle) < 3:
        return None
    position = title.lower().find(needle.lower())
    if position < 0:
        return None
    start = max(0, position - 40)
    end = min(len(title), position + len(needle) + 40)
    snippet = title[start:end]
    return ("…" if start > 0 else "") + snippet + ("…" if end < len(title) else "")


def _derived_citation(
    study: NormalizedStudy, dimension: DimensionDef, raw_value: Any
) -> Citation:
    """Cite the source fields, not the computed number.

    A derived value does not appear anywhere in the record, so quoting it alone
    would be unverifiable. The citation quotes each contributing field verbatim
    and shows the computed result alongside.
    """
    from app.dimensions.extractors import extract_one

    parts = []
    for path in dimension.source_paths:
        value = extract_one(study.raw, path)
        parts.append(f"{path.rsplit('.', 2)[-2]}={serialise_value(value)}")

    return Citation(
        nct_id=study.nct_id,
        brief_title=study.brief_title,
        field_path=" + ".join(dimension.source_paths),
        field_value=f"{'; '.join(parts)} -> {serialise_value(raw_value)}",
        evidence_kind="field_value",
        excerpt=None,
        source_url=study.url,
    )


def build_citation(
    study: NormalizedStudy,
    dimension: DimensionDef,
    raw_value: Any,
) -> Citation:
    """Build evidence for one bucket membership from a study's raw payload.

    Derived dimensions cite every source path. Ordinary dimensions cite their
    configured path and include an excerpt only when it is a verbatim substring
    of fetched text.
    """
    if dimension.derive is not None and raw_value is not None:
        return _derived_citation(study, dimension, raw_value)

    is_missing = raw_value is None
    field_value = "null" if is_missing else serialise_value(raw_value)
    excerpt = None if is_missing else _verbatim_excerpt(study, str(raw_value))

    return Citation(
        nct_id=study.nct_id,
        brief_title=study.brief_title,
        field_path=dimension.path,
        field_value=field_value,
        evidence_kind="text_excerpt" if excerpt else "field_value",
        excerpt=excerpt,
        source_url=study.url,
    )
