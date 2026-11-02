"""Dimensions computed from more than one field.

Most dimensions read a single JSON path. A few are genuinely derived — trial
duration needs both a start and a completion date — so they supply a function
here instead of a path, plus the source paths the citation must quote.
"""

from __future__ import annotations

from typing import Any

from app.dimensions.extractors import extract_one

START_PATH = "protocolSection.statusModule.startDateStruct.date"
COMPLETION_PATH = "protocolSection.statusModule.completionDateStruct.date"


def _as_year_month(value: Any) -> tuple[int, int] | None:
    """Parse ``2016``, ``2016-11`` or ``2021-07-19`` to (year, month).

    Day precision is discarded: the API mixes precisions within the same field,
    so month is the finest granularity every record supports.
    """
    text = str(value or "")
    if len(text) < 4 or not text[:4].isdigit():
        return None
    year = int(text[:4])
    month = 1
    if len(text) >= 7 and text[5:7].isdigit():
        month = int(text[5:7])
    if not 1 <= month <= 12:
        month = 1
    return year, month


def duration_months(record: dict[str, Any]) -> int | None:
    """Months between trial start and completion.

    Dates are normalised to the first of the month before subtracting, so a
    value carries up to about a month of error in either direction. Returns
    ``None`` when either date is missing or the pair is non-sensical, which the
    dimension's ``exclude`` policy then reports rather than charting.
    """
    start = _as_year_month(extract_one(record, START_PATH))
    end = _as_year_month(extract_one(record, COMPLETION_PATH))
    if start is None or end is None:
        return None
    months = (end[0] - start[0]) * 12 + (end[1] - start[1])
    return months if months >= 0 else None
