"""Typed source catalog and deterministic numeric relationship summaries."""
import math
import re
from datetime import date
from statistics import correlation, StatisticsError

# column -> source path, API field, type, unit
SOURCES = {
    "phase": ("protocolSection.designModule.phases", "Phase", "string_list", None),
    "condition": ("protocolSection.conditionsModule.conditions", "Condition", "string_list", None),
    "enrollment": ("protocolSection.designModule.enrollmentInfo.count", "EnrollmentCount", "number", "participants"),
    "enrollment_type": ("protocolSection.designModule.enrollmentInfo.type", "EnrollmentType", "string", None),
    "start_date": ("protocolSection.statusModule.startDateStruct.date", "StartDate", "date", None),
    "completion_date": ("protocolSection.statusModule.completionDateStruct.date", "CompletionDate", "date", None),
    "start_date_type": ("protocolSection.statusModule.startDateStruct.type", "StartDateType", "string", None),
    "completion_date_type": ("protocolSection.statusModule.completionDateStruct.type", "CompletionDateType", "string", None),
}


# Expose ordinary source fields from the same registry, rather than adding a
# new root-column special case whenever the planner needs a recorded scalar.
from app.dimensions.registry import DIMENSIONS
for _name, _dimension in DIMENSIONS.items():
    if _name in SOURCES or _dimension.derive or _dimension.bin_fn or _dimension.datatype == "temporal":
        continue
    if len(_dimension.api_fields) != 1:
        continue
    SOURCES[_name] = (_dimension.path, _dimension.api_fields[0],
                      "string_list" if "[]" in _dimension.path else "string", None)


def read(record, path):
    if "[]" in path:
        from app.dimensions.extractors import extract_raw
        return extract_raw(record, path)
    value = record
    for part in path.split("."):
        value = value.get(part) if isinstance(value, dict) else None
    return value


def finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def months_between(start, end):
    def parse(value):
        text = str(value or "")
        if not re.fullmatch(r"\d{4}-\d{2}(?:-\d{2})?", text):
            return None
        try:
            return date.fromisoformat(text if len(text) == 10 else text + "-01")
        except ValueError:
            return None
    low, high = parse(start), parse(end)
    if low is None or high is None or high < low:
        return None
    return (high.year - low.year) * 12 + high.month - low.month


def summarize(points):
    value = None
    reason = None
    if len(points) < 3:
        reason = "At least three complete trial pairs are required."
    else:
        try:
            value = correlation([p["x"] for p in points], [p["y"] for p in points])
        except StatisticsError:
            reason = "Correlation is undefined when either variable is constant."
    return {"method": "pearson", "n": len(points), "coefficient": value, "undefined_reason": reason,
            "interpretation": "Descriptive linear association among included records; not a causal effect or a significance test."}
