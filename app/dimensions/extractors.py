"""Pull raw values out of a ClinicalTrials.gov study record by dotted path.

Paths use a ``[]`` suffix on a segment to mark an array hop, so
``protocolSection.contactsLocationsModule.locations[].country`` walks into the
locations list and collects ``country`` from each element.

Every extractor returns a *list* of raw values, because the API represents
genuinely multi-valued facts (phases, countries, conditions) as arrays. Callers
decide what the multiplicity means; see ``DimensionDef.semantics``.
"""

from __future__ import annotations

from typing import Any


def extract_raw(record: dict[str, Any], path: str) -> list[Any]:
    """Walk ``path`` through ``record`` and return every value found.

    Missing keys yield ``[]`` rather than raising: absence is a normal, expected
    state in this dataset and is handled by each dimension's missing policy.
    """
    current: list[Any] = [record]

    for segment in path.split("."):
        is_array_hop = segment.endswith("[]")
        key = segment[:-2] if is_array_hop else segment

        nxt: list[Any] = []
        for node in current:
            if not isinstance(node, dict):
                continue
            value = node.get(key)
            if value is None:
                continue
            if is_array_hop:
                if isinstance(value, list):
                    nxt.extend(v for v in value if v is not None)
            else:
                nxt.append(value)
        current = nxt
        if not current:
            return []

    # A non-array-hop final segment can still land on a list (e.g. `phases`),
    # so flatten one level to keep the return type uniform.
    flattened: list[Any] = []
    for value in current:
        if isinstance(value, list):
            flattened.extend(v for v in value if v is not None)
        else:
            flattened.append(value)
    return flattened


def extract_one(record: dict[str, Any], path: str) -> Any | None:
    """Return the single value at ``path``, or ``None`` when absent."""
    values = extract_raw(record, path)
    return values[0] if values else None
