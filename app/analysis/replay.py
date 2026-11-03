"""Replay a saved plan against explicitly supplied records, without model calls.

uv run python -m app.analysis.replay --plan plan.json --records records.json
Records may be an API response or an array. No model calls or enrichment.
"""
import argparse
import asyncio
import json
from pathlib import Path
from app.analysis.models import AnalysisPlan
from app.analysis.engine import execute
from app.models.study import NormalizedStudy


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", required=True)
    parser.add_argument("--records", required=True)
    args = parser.parse_args()
    plan = AnalysisPlan.model_validate_json(Path(args.plan).read_text())
    raw = json.loads(Path(args.records).read_text())
    if isinstance(raw, dict):
        raw = raw["studies"]
    studies = [study for record in raw if (study := NormalizedStudy.from_api(record)) is not None]
    result = asyncio.run(execute(plan, studies, "Replay"))
    print(json.dumps({"data": result.points if result.points is not None else [b.model_dump() for b in result.buckets], "analysis": result.metadata, "unit": result.unit}, indent=2))


if __name__ == "__main__":
    main()
