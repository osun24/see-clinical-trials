"""Map a study onto the bucket key(s) it belongs in, per dimension semantics.

This module is where the two cardinality traps in this dataset are resolved.
"""

from __future__ import annotations

from typing import Any

from app.dimensions.extractors import extract_raw
from app.dimensions.labels import PHASE_ORDER, label_phase_composite
from app.dimensions.registry import DimensionDef

MISSING_KEY = "__missing__"


def _year_of(raw: Any) -> str | None:
    """Year from a date that may be ``2016-11`` or ``2021-07-19``."""
    text = str(raw)
    return text[:4] if len(text) >= 4 and text[:4].isdigit() else None


def bucket_keys(study_raw: dict[str, Any], dimension: DimensionDef) -> list[tuple[str, Any]]:
    """Return ``(key, raw_value)`` pairs this study contributes to.

    A ``partition`` dimension always returns exactly one pair, which is what
    guarantees ``sum(bucket.value) == records_analyzed``. A ``multi_label``
    dimension may return several, de-duplicated within the study.
    """
    # --- derived dimensions ------------------------------------------------
    # Computed from several fields, so there is no path to read.
    if dimension.derive is not None:
        value = dimension.derive(study_raw)
        if value is None:
            return [(MISSING_KEY, None)]
        label = dimension.bin_fn(value) if dimension.bin_fn else str(value)
        return [(label, value)] if label else [(MISSING_KEY, None)]

    raws = extract_raw(study_raw, dimension.path)

    # --- phase, as a partition -------------------------------------------
    # A ["PHASE1","PHASE2"] study is one fact, not two, so it gets one
    # combined bucket. Canonical sort order means ["PHASE2","PHASE1"] and
    # ["PHASE1","PHASE2"] collapse together rather than forming two buckets.
    if dimension.id == "phase":
        if not raws:
            return [(MISSING_KEY, None)]
        ordered = tuple(sorted((str(r) for r in raws), key=lambda p: PHASE_ORDER.get(p, 99)))
        return [("/".join(ordered), list(raws))]

    if not raws:
        return [(MISSING_KEY, None)]

    # --- temporal ----------------------------------------------------------
    if dimension.datatype == "temporal":
        year = _year_of(raws[0])
        return [(year, raws[0])] if year else [(MISSING_KEY, None)]

    # --- numeric, binned ----------------------------------------------------
    if dimension.bin_fn is not None:
        label = dimension.bin_fn(raws[0])
        return [(label, raws[0])] if label else [(MISSING_KEY, None)]

    # --- multi-valued -------------------------------------------------------
    # Location entries repeat once per SITE, so a trial with 963 site entries
    # would otherwise swamp every bucket. De-duplicating first keeps the answer
    # about trials rather than silently becoming about sites.
    if dimension.semantics == "multi_label":
        seen: dict[str, Any] = {}
        for raw in raws:
            seen.setdefault(str(raw), raw)
        return list(seen.items())

    # --- single-valued partition --------------------------------------------
    return [(str(raws[0]), raws[0])]


def display_label(key: str, dimension: DimensionDef) -> str:
    if key == MISSING_KEY:
        return dimension.missing_label
    if dimension.id == "phase":
        return label_phase_composite(tuple(key.split("/")))
    if dimension.datatype == "temporal" or dimension.bin_fn is not None:
        return key
    return dimension.label_fn(key)
