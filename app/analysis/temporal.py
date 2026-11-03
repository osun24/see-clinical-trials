"""Preserve typed year/group columns through the legacy chart adapter."""
import json
from collections import defaultdict
from app.models.buckets import Bucket
from app.viz.resolver import MAX_SERIES_LINES


def adapt(result, aggregate):
    types = result.metadata["schema"]["group_types"]
    years = [name for name, kind in types.items() if kind == "year"]
    if len(years) != 1:
        return None
    year = years[0]
    others = [name for name in aggregate.group_by if name != year]
    year_index = aggregate.group_by.index(year)
    converted = []
    totals = defaultdict(int)
    for bucket in result.buckets:
        if bucket.is_other:
            from app.models.errors import ServiceError
            raise ServiceError("query_not_interpretable", "Temporal results cannot collapse year/category cells. Remove the limit step.")
        values = json.loads(bucket.key)
        # A collapsed tail is no longer a year/category row.
        if not isinstance(values, list):
            from app.models.errors import ServiceError
            raise ServiceError("query_not_interpretable", "Temporal results cannot collapse year/category cells. Remove the limit step.")
        year_value = values[year_index]
        if not str(year_value).isdigit() or not 1 <= int(year_value) <= 9999:
            continue
        series = " / ".join(values[aggregate.group_by.index(name)] for name in others) if others else None
        converted.append(bucket.model_copy(update={"key": year_value, "label": year_value, "series": series}))
        if series is not None:
            totals[series] += bucket.value
    omitted = []
    if len(totals) > MAX_SERIES_LINES:
        kept = set(sorted(totals, key=lambda name: (-totals[name], name))[:MAX_SERIES_LINES])
        omitted = sorted(set(totals) - kept)
        converted = [b for b in converted if b.series in kept]
    # Missing years within each displayed series mean zero observed memberships.
    if converted:
        low = min(int(b.key) for b in converted)
        high = max(int(b.key) for b in converted)
        if high - low > 200:
            from app.models.errors import ServiceError
            raise ServiceError("query_not_interpretable", "Temporal output exceeds a 200-year display span.")
        present = {(b.key, b.series): b for b in converted}
        converted = [present.get((str(y), s), Bucket(key=str(y), label=str(y), series=s, value=0))
                     for s in sorted({b.series for b in converted}, key=lambda s: s or "") for y in range(low, high+1)]
    return year, " / ".join(others) or None, converted, {
        "omitted_series": omitted, "series_totals": dict(totals),
        "selection": f"At most {MAX_SERIES_LINES} series ranked by summed trial-year memberships; remaining series omitted from chart.",
        "basis": "Study start year; zero means no observed qualifying memberships among fetched records.",
        "full_rows": [b.model_dump() for b in result.buckets] if omitted else [],
    }
