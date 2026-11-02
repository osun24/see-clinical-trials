"""Numeric binning for dimensions that have no natural categories."""

from __future__ import annotations

ENROLLMENT_BINS: list[tuple[int, int | None, str]] = [
    (0, 10, "1-10"),
    (11, 50, "11-50"),
    (51, 200, "51-200"),
    (201, 1000, "201-1,000"),
    (1001, 5000, "1,001-5,000"),
    (5001, None, "5,000+"),
]

DURATION_BINS: list[tuple[int, int | None, str]] = [
    (0, 6, "Under 6 months"),
    (6, 12, "6-12 months"),
    (12, 24, "1-2 years"),
    (24, 60, "2-5 years"),
    (60, None, "Over 5 years"),
]


def _bin(value: object, bins: list[tuple[int, int | None, str]]) -> str | None:
    try:
        n = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if n < 0:
        return None
    for low, high, label in bins:
        if n >= low and (high is None or n <= high):
            return label
    return None


def bin_enrollment(value: object) -> str | None:
    return _bin(value, ENROLLMENT_BINS)


def bin_duration_months(value: object) -> str | None:
    return _bin(value, DURATION_BINS)


def enrollment_bin_order() -> list[str]:
    return [label for _, _, label in ENROLLMENT_BINS]


def duration_bin_order() -> list[str]:
    return [label for _, _, label in DURATION_BINS]
