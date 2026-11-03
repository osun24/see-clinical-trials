"""90 full live runs + five unsupported cases. No secrets or trial payloads saved.
Run: uv run python -m evaluations.run --output evaluations/results.json
"""
import argparse
import asyncio
import hashlib
import json
import time
from collections import defaultdict
from pathlib import Path
from app.pipeline import answer
from app.ctgov.client import CtgovClient
from app.models.request import QueryRequest
from app.models.errors import ServiceError
from app.config import settings
from app.interpret.prompt import SYSTEM_PROMPT
from app.interpret import usage as usage_meter
from evaluations.cases import CASES, NEGATIVE


def contract(body):
    viz = body["visualization"]
    data = viz["data"]
    failures = []
    if viz["type"] == "network_graph":
        ids = {n["id"] for n in data["nodes"]}
        if any(e["source"] not in ids or e["target"] not in ids for e in data["edges"]):
            failures.append("dangling graph edge")
        rows = data["edges"]
        value_field = "weight"
    else:
        rows = data
        value_field = "value"
        for channel in viz["encoding"].values():
            if isinstance(channel, dict) and "field" in channel:
                if any(channel["field"] not in r for r in rows):
                    failures.append("missing encoded field")
    if any((r.get(value_field, 1) or 0) > 0 and not r.get("citations") for r in rows):
        failures.append("nonzero datum lacks evidence")
    return failures


def semantic(body, expected, terms):
    meta = body["meta"]
    viz = body["visualization"]
    filters = json.dumps(meta["query_interpretation"]["filters_applied"]).lower()
    failures = ["missing search constraint: " + t for t in terms.split("|") if t not in filters]
    group = meta.get("group_by") or ""
    analysis = meta.get("analysis") or {}
    steps = analysis.get("plan", {}).get("steps", [])
    text = json.dumps(steps).lower()
    source_for_output = {s["output"]: s["dimension"] for s in steps if s["op"] == "memberships"}
    if group in source_for_output:
        group = source_for_output[group]
    if expected.startswith("compare_"):
        if not (meta.get("series_by") or "").startswith("compare:"):
            failures.append("comparison arms missing")
        wanted = {"compare_phase":"phase", "compare_status":"overall_status", "compare_type":"study_type"}[expected]
        if group != wanted:
            failures.append("wrong comparison grouping")
    elif expected == "person_graph":
        if viz["type"] != "network_graph" or not any(n.get("level") == 2 for n in viz["data"].get("nodes", [])):
            failures.append("person/trial graph missing")
    elif "relationship" in expected:
        if viz["type"] != "scatter_plot" or not any(s["op"] == "relationship" for s in steps):
            failures.append("numeric relationship missing")
        if expected.startswith("planned") and not any(s["op"] == "filter" and s["field"] == "enrollment_type" and s["value"] == "ESTIMATED" for s in steps):
            failures.append("planned enrollment filter missing")
    elif expected == "total_recruiting":
        if viz["type"] != "stat" or "recruiting" not in filters:
            failures.append("recruiting total missing")
    elif expected in {"city_phase4", "state", "facility", "name_phase4", "site_count", "temporal_names_phase4"}:
        wanted = {"city_phase4":"city", "state":"state", "facility":"facility", "name_phase4":"name", "site_count":"city", "temporal_names_phase4":"name"}[expected]
        if wanted not in group:
            failures.append("requested nested grouping missing")
        if expected in {"city_phase4", "state", "facility", "site_count"} and not any(s["op"] == "filter" and s["field"] == "country" and s["value"].lower() == "united states" for s in steps):
            failures.append("individual US location filter missing")
        if "phase4" in expected and "phase4" not in filters:
            failures.append("phase4 search filter missing")
        if expected == "site_count" and meta["measure"] != "count_rows":
            failures.append("site-entry measure missing")
        if expected == "temporal_names_phase4" and viz["type"] != "time_series":
            failures.append("temporal result missing")
    elif group != expected:
        failures.append("wrong grouping")
    if expected in {"start_year", "completion_year"}:
        if viz["type"] not in {"time_series", "grouped_bar"}:
            failures.append("temporal chart missing")
        if expected == "completion_year" and "completiondate" not in filters:
            failures.append("completion-date interval missing")
    return failures


async def main(output, smoke=False):
    sem = asyncio.Semaphore(3)
    results = []
    async def one(index, run, case):
        family, question, expected, terms = case
        async with sem:
            started = time.monotonic()
            entry = {"case": index, "run": run, "family": family, "question": question}
            try:
                async with CtgovClient() as client:
                    response = await answer(QueryRequest(query=question), client)
                body = response.model_dump(mode="json", exclude_none=True)
                entry.update({"contract_failures":contract(body), "semantic_failures":semantic(body,expected,terms),
                    "visualization_type":body["visualization"]["type"],
                    "interpretation":body["meta"]["query_interpretation"], "group_by":body["meta"].get("group_by"),
                    "series_by":body["meta"].get("series_by"), "measure":body["meta"]["measure"],
                    "coverage":body["meta"]["coverage"], "plan":(body["meta"].get("analysis") or {}).get("plan"),
                    "repair": any("revised its plan once" in a for a in body["meta"]["query_interpretation"]["plan_adjustments"])})
            except ServiceError as exc:
                entry.update({"error": exc.code, "message":exc.message, "detail":exc.detail})
            except Exception as exc:
                entry.update({"error":type(exc).__name__, "message":str(exc)[:300]})
            entry["usage"] = usage_meter.summary(settings.openai_model)
            entry["elapsed_s"] = round(time.monotonic()-started,2)
            entry["passed_checks"] = not entry.get("error") and not entry.get("contract_failures") and not entry.get("semantic_failures")
            results.append(entry)
            Path(output).write_text(json.dumps({"status":"running", "results":results},indent=2))
            print(json.dumps({k:entry.get(k) for k in ["case","run","family","passed_checks","error","semantic_failures"]}),flush=True)
    chosen = [(i,c) for i,c in enumerate(CASES) if not smoke or i in {0,5,10,15,20,25}]
    await asyncio.gather(*(one(i,r,c) for i,c in chosen for r in range(1 if smoke else 3)))
    negatives = []
    for question in NEGATIVE:
        try:
            async with CtgovClient() as client:
                await answer(QueryRequest(query=question),client)
            negatives.append({"question":question,"passed":False,"error":None})
        except ServiceError as exc:
            negatives.append({"question":question,"passed":exc.code=="unsupported_query","error":exc.code})
    families = {}
    for family in sorted({r["family"] for r in results}):
        group = [r for r in results if r["family"]==family]
        families[family] = sum(r["passed_checks"] for r in group)/len(group)
    overall = sum(r["passed_checks"] for r in results)/len(results)
    priced = [r["usage"] for r in results if r.get("usage")]
    latencies = sorted(r["elapsed_s"] for r in results if r.get("elapsed_s") is not None)
    costs = [u["usd"] for u in priced if u.get("usd") is not None]
    cost_report = {
        "runs_with_usage": len(priced),
        "planner_calls": sum(u["planner_calls"] for u in priced),
        "prompt_tokens": sum(u["prompt_tokens"] for u in priced),
        "completion_tokens": sum(u["completion_tokens"] for u in priced),
        "cached_prompt_tokens": sum(u["cached_prompt_tokens"] for u in priced),
        "total_usd": round(sum(costs), 4) if costs else None,
        "usd_per_run": round(sum(costs)/len(costs), 6) if costs else None,
        "pricing_basis": priced[0]["pricing_basis"] if priced else None,
    }
    latency_report = {
        "median_s": latencies[len(latencies)//2] if latencies else None,
        "p90_s": latencies[int(.9*len(latencies))] if latencies else None,
        "max_s": latencies[-1] if latencies else None,
    }
    report = {"status":"complete","model":settings.openai_model,"prompt_sha256":hashlib.sha256(SYSTEM_PROMPT.encode()).hexdigest(),
        "runs":len(results),"overall_checks_rate":overall,"family_checks_rates":families,"negative_cases":negatives,
        "repairs":sum(1 for r in results if r.get("repair")),"failure_categories":dict(sorted(
            ((k, sum(1 for r in results if (r.get("error") or ("semantic" if r.get("semantic_failures") else None) or ("contract" if r.get("contract_failures") else None)) == k))
             for k in {(r.get("error") or ("semantic" if r.get("semantic_failures") else None) or ("contract" if r.get("contract_failures") else None)) for r in results} - {None}),
            key=lambda kv: -kv[1])),
        "latency":latency_report,"cost":cost_report,
        "automated_gate_passed":overall>=.9 and min(families.values())>=.8 and all(n["passed"] for n in negatives),
        "limitation":"Constraint assertions and contract checks are automated; medical truth and complete semantic correctness still require audit. Cost covers planner tokens only: ClinicalTrials.gov is free and all other work is local. Scoring verifies that requested constraints are present, not that no extra constraint was added.",
        "results":sorted(results,key=lambda r:(r["case"],r["run"]))}
    Path(output).write_text(json.dumps(report,indent=2))
    print(json.dumps({k:v for k,v in report.items() if k!="results"}),flush=True)

if __name__ == "__main__":
    parser=argparse.ArgumentParser();parser.add_argument("--output",default="evaluations/results.json");parser.add_argument("--smoke",action="store_true")
    args=parser.parse_args()
    asyncio.run(main(args.output,args.smoke))
