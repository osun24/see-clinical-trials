"""Bounded deterministic relational operations over trial records."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from app.analysis import domains
from app.analysis.models import AnalysisPlan
from app.analysis.numeric import SOURCES, read, finite, months_between, summarize
from app.aggregate.citations import serialise_value
from app.models.buckets import Bucket, Citation
from app.models.errors import ServiceError

COLLECTIONS = {
    "locations": ("protocolSection.contactsLocationsModule.locations", {"city", "state", "country", "facility", "status"}, "LocationCity,LocationState,LocationCountry,LocationFacility,LocationStatus"),
    "interventions": ("protocolSection.armsInterventionsModule.interventions", {"name", "type", "description"}, "InterventionName,InterventionType,InterventionDescription"),
    "conditions": ("protocolSection.conditionsModule.conditions", {"name"}, "Condition"),
    "officials": ("protocolSection.contactsLocationsModule.overallOfficials", {"name", "role", "affiliation"}, "OverallOfficialName,OverallOfficialRole,OverallOfficialAffiliation"),
}


def fail(message):
    raise ServiceError("query_not_interpretable", message)


def validate(plan: AnalysisPlan) -> dict:
    if not 1 <= len(plan.steps) <= 16:
        fail("Analysis must have 1–16 steps.")
    columns = {"trials": {"trial_id", "title", *SOURCES}}
    types = {name: source[2] for name, source in SOURCES.items()}
    types.update({"trial_id": "string", "title": "string"})
    used_sources = set()
    relationship = False
    canonicalised = []
    group_types = {}
    aggregated = False
    expanded = False
    fields = {"NCTId", "BriefTitle"}
    for index, step in enumerate(plan.steps):
        if not step.id.isidentifier() or step.id in columns:
            fail("Analysis step IDs must be unique identifiers.")
        # A linear chain avoids unused model calls and misleading output branches.
        expected = "trials" if index == 0 else plan.steps[index - 1].id
        if step.input != expected:
            fail(f"Step {step.id} input must be {expected!r}, not {step.input!r}; each step consumes the preceding step.")
        current = set(columns[step.input])
        if step.op == "memberships":
            from app.dimensions.registry import DIMENSIONS
            if aggregated or step.dimension not in DIMENSIONS or not step.output.isidentifier() or step.output in current:
                fail("Membership preset requires a supported dimension and a new output column before aggregation.")
            fields.update(DIMENSIONS[step.dimension].api_fields)
            current.add(step.output)
            types[step.output] = "year" if DIMENSIONS[step.dimension].datatype == "temporal" else "string"
        elif step.op == "expand":
            if expanded or aggregated:
                fail("Version 1 supports one collection expansion before aggregation.")
            expanded = True
            _, names, mask = COLLECTIONS[step.collection]
            current |= names
            fields.update(mask.split(","))
        elif step.op == "relationship":
            if aggregated or expanded or types.get(step.x) != "number" or types.get(step.y) != "number":
                fail("Relationships require two numeric trial columns before aggregation, without collection expansion.")
            if index != len(plan.steps) - 1:
                fail("Relationship must be the final step.")
            used_sources.update({step.x, step.y} & SOURCES.keys())
            relationship = aggregated = True
            current = {"trial_id", "label", "x", "y"}
        elif step.op in {"filter", "derive"}:
            if aggregated or step.field not in current:
                fail(f"Step {step.id}: column {step.field!r} is unavailable before aggregation. Available columns: {', '.join(sorted(current))}.")
            used_sources.update({step.field} & SOURCES.keys())
            if step.op == "filter" and step.predicate in {"equals", "not_equals"}:
                # A closed column compared against a value outside its domain
                # matches nothing. Reject it here so the single planning repair
                # gets the allowed values, rather than returning an empty chart.
                resolved = domains.canonical(step.field, step.value)
                if resolved is None:
                    fail(
                        f"Step {step.id}: {step.value!r} is not a recorded "
                        f"{step.field} value. Allowed values: {domains.describe(step.field)}."
                    )
                if resolved != step.value:
                    canonicalised.append({"step": step.id, "field": step.field,
                                          "given": step.value, "applied": resolved})
            if step.op == "derive" and step.transform == "months_between":
                if types.get(step.field) != "date" or types.get(step.other_field) != "date":
                    fail("months_between requires two date columns.")
                used_sources.update({step.other_field} & SOURCES.keys())
            if step.op == "derive":
                if not step.output.isidentifier() or step.output in current:
                    fail("Derived columns must have a new identifier.")
                current.add(step.output)
                types[step.output] = ("number" if step.op == "derive" and step.transform == "months_between" else "year" if step.op == "derive" and step.transform == "year" else "string")
        elif step.op == "aggregate":
            if aggregated or not 1 <= len(step.group_by) <= 3 or len(set(step.group_by)) != len(step.group_by):
                fail("Aggregate once using one to three distinct grouping columns.")
            if any(types.get(name) == "string_list" for name in step.group_by):
                # Telling the planner to "expand" a column like phase sent it
                # looking for a collection that does not exist. Name the
                # offending columns and the route that actually works.
                from app.dimensions.registry import DIMENSIONS

                listed = [n for n in step.group_by if types.get(n) == "string_list"]
                registered = [n for n in listed if n in DIMENSIONS]
                remedy = (
                    f"Use a memberships step with dimension {registered[0]!r} and group "
                    f"by its output column instead"
                    if registered
                    else "Expand the collection first, then group by an item field"
                )
                fail(
                    f"Column(s) {', '.join(listed)} hold several values per trial and "
                    f"cannot be grouping keys. {remedy}."
                )
            if not set(step.group_by) <= current:
                fail("Unknown grouping column.")
            used_sources.update(set(step.group_by) & SOURCES.keys())
            group_types = {name: types.get(name, "string") for name in step.group_by}
            aggregated = True
            current = {"label", "value"}
        else:
            if not aggregated:
                fail("Sort and limit require an aggregated table.")
            if step.op == "limit" and not 1 <= step.count <= 100:
                fail("Analysis limits must be between 1 and 100.")
        columns[step.id] = current
    if not aggregated:
        # The planner's commonest failure is a plan that simply stops before its
        # aggregate. A bare "must produce a grouped aggregate" left the single
        # repair guessing; naming the step to attach to, the columns in scope,
        # and the exact step shape makes the repair mechanical.
        last = plan.steps[-1]
        available = sorted(c for c in columns[last.id] if not c.startswith("_"))
        fail(
            f"Analysis must end with an aggregate step; the plan stops at {last.id!r} "
            f"({last.op}). Append "
            f'{{"op":"aggregate","id":"counts","input":"{last.id}",'
            f'"group_by":[<one to three of: {", ".join(available)}>],'
            f'"measure":"count_distinct_trials","missing":"exclude"}} '
            "and keep every existing step unchanged."
        )
    fields.update(SOURCES[name][1] for name in used_sources)
    return {"columns": sorted(columns[plan.steps[-1].id]), "filter_canonicalisation": canonicalised, "column_types": ({"x": "number", "y": "number", "label": "string", "trial_id": "string"} if relationship else {"label": "string", "value": "integer"}), "group_types": group_types, "source_columns": sorted(used_sources), "fields_mask": sorted(fields),
            "steps": [s.model_dump() for s in plan.steps]}


@dataclass
class Result:
    buckets: list[Bucket]
    metadata: dict
    unit: str
    points: list[dict] | None = None


async def execute(plan, studies, question, citation_limit=5, mappings=None):
    if mappings:
        fail("Enrichment mappings are not supported.")
    return execute_sync(plan, studies, citation_limit)


def execute_sync(plan, studies, citation_limit=5, max_buckets=10_000):
    schema = validate(plan)
    fingerprint = hashlib.sha256(json.dumps([s.raw for s in studies], sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    rows = [{"trial_id": s.nct_id, "title": s.brief_title or "", "_study": s, "_evidence": {}} for s in studies]
    for row in rows:
        for name in schema["source_columns"]:
            path = SOURCES[name][0]
            value = read(row["_study"].raw, path)
            row[name] = value if SOURCES[name][2] != "number" or (finite(value) and value >= 0) else None
            row["_evidence"][path] = value
    points = None
    numeric_summary = None
    invalid_pairs = 0
    trace = []
    excluded_trials = set()
    removed_rows = missing_rows = 0
    measure = "count_distinct_trials"
    buckets = []
    def evidence(members):
        unique = {}
        for row in members:
            study = row["_study"]
            unique.setdefault(study.nct_id, row)
        citations = []
        for row in list(unique.values())[:citation_limit]:
            study = row["_study"]
            if row.get("_citation"):
                citations.append(row["_citation"])
                continue
            proof = row["_evidence"] or {"protocolSection.identificationModule.nctId": study.nct_id}
            citations.append(Citation(nct_id=study.nct_id, brief_title=study.brief_title,
                field_path=" + ".join(proof), field_value=serialise_value(proof), source_url=study.url))
        return citations, len(unique)
    for step in plan.steps:
        if step.op == "memberships":
            from app.dimensions.registry import DIMENSIONS
            from app.aggregate.keys import bucket_keys, MISSING_KEY
            from app.aggregate.citations import build_citation
            dimension = DIMENSIONS[step.dimension]
            expanded_rows = []
            for row in rows:
                for key, raw in bucket_keys(row["_study"].raw, dimension):
                    if key == MISSING_KEY and dimension.missing_policy == "exclude":
                        excluded_trials.add(row["trial_id"])
                        continue
                    expanded_rows.append({**row, step.output: key,
                        "_citation": row.get("_citation") or build_citation(row["_study"], dimension, raw)})
                    if len(expanded_rows) > 100000:
                        fail("Membership expansion exceeds 100,000 rows.")
            rows = expanded_rows
        elif step.op == "expand":
            path, names, _ = COLLECTIONS[step.collection]
            expanded = []
            for row in rows:
                value = row["_study"].raw
                for part in path.split("."):
                    value = value.get(part, {}) if isinstance(value, dict) else {}
                for item_index, item in enumerate(value if isinstance(value, list) else []):
                    values = item if isinstance(item, dict) else {"name": item}
                    expanded.append({**row, **{n: values.get(n) for n in names},
                        "_evidence": {**row["_evidence"], f"{path}[{item_index}]": item}})
                    if len(expanded) > 100000:
                        fail("Expanded analysis exceeds the 100,000-row budget.")
            rows = expanded
        elif step.op == "filter":
            def keep(row):
                raw = row.get(step.field)
                values = raw if isinstance(raw, list) else [raw]
                texts = [str(value).casefold() for value in values if value is not None]
                target = (domains.canonical(step.field, step.value) or step.value).casefold()
                if step.predicate == "present":
                    return any(texts)
                if step.predicate == "equals":
                    return any(value == target for value in texts)
                if step.predicate == "not_equals":
                    return all(value != target for value in texts)
                return any(target in value for value in texts)
            kept = [r for r in rows if keep(r)]
            removed_rows += len(rows) - len(kept)
            rows = kept
        elif step.op == "derive":
            for row in rows:
                value = row.get(step.field)
                if step.transform == "months_between":
                    row[step.output] = months_between(value, row.get(step.other_field))
                elif value is None:
                    row[step.output] = None
                else:
                    text = str(value)
                    row[step.output] = text.lower() if step.transform == "lower" else text.strip() if step.transform == "strip" else (text[:4] if text[:4].isdigit() else None)
        elif step.op == "relationship":
            points = []
            seen = set()
            for row in rows:
                if row["trial_id"] in seen:
                    continue
                seen.add(row["trial_id"])
                x, y = row.get(step.x), row.get(step.y)
                if not finite(x) or not finite(y):
                    invalid_pairs += 1
                    continue
                citations, _ = evidence([row])
                points.append({"trial_id": row["trial_id"], "label": row["title"], "x": x, "y": y,
                               "citations": [c.model_dump() for c in citations], "citation_total": 1, "citations_truncated": False})
            numeric_summary = summarize(points)
        elif step.op == "aggregate":
            measure = step.measure
            groups = {}
            for row in rows:
                values = [row.get(k) for k in step.group_by]
                if any(v is None or v == "" for v in values) and step.missing == "exclude":
                    missing_rows += 1
                    continue
                key = tuple(str(v) if v is not None and v != "" else "Not Reported" for v in values)
                groups.setdefault(key, []).append(row)
            for key, members in sorted(groups.items()):
                citations, total = evidence(members)
                buckets.append(Bucket(key=json.dumps(key), label=" / ".join(key),
                    value=len({r["trial_id"] for r in members}) if measure == "count_distinct_trials" else len(members),
                    citations=citations, citation_total=total, citations_truncated=total > len(citations)))
        elif step.op == "sort":
            buckets.sort(key=lambda b: b.label)
            buckets.sort(key=lambda b: getattr(b, step.field), reverse=step.descending)
        elif step.op == "limit" and len(buckets) > step.count:
            tail = buckets[step.count:]
            buckets = buckets[:step.count]
            if step.tail == "other":
                # Values are category memberships, not a deduplicated union.
                citations = {c.nct_id: c for b in tail for c in b.citations}
                buckets.append(Bucket(key="__other__", label="Other", value=sum(b.value for b in tail),
                    is_other=True, distinct_values_collapsed=len(tail), citations=list(citations.values())[:citation_limit],
                    citation_total=sum(b.citation_total for b in tail), citations_truncated=True,
                    citation_sample=True, citation_basis="Sample of omitted category memberships; a trial can occur in several categories."))
        trace.append({"step": step.id, "op": step.op, "rows": len(points) if step.op == "relationship" else len(buckets) if step.op in {"aggregate", "sort", "limit"} else len(rows)})
    if len(buckets) > max_buckets:
        # A model-authored plan must bound its own output. A compiled preset is
        # always followed by deterministic top-N, so its cross-product is
        # transient and gets the wider ceiling its caller passes.
        fail(
            f"Result has {len(buckets):,} groups, past the {max_buckets:,} budget; "
            "add an explicit limit step or group by fewer columns."
        )
    return Result(buckets, {"plan": plan.model_dump(), "schema": schema, "input_fingerprint": fingerprint,
        "excluded_trial_ids": sorted(excluded_trials), "trace": trace, "filtered_rows": removed_rows, "missing_group_rows": missing_rows,
        "numeric_summary": numeric_summary, "invalid_pairs": invalid_pairs, "measure": ("relationship" if points is not None else measure), "evidence_basis": "All values are extracted from trial records or computed deterministically from them."},
        "trials" if measure == "count_distinct_trials" else "entries", points)
