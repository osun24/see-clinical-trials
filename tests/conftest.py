from __future__ import annotations

import json
import pathlib

import pytest

from app.models.study import NormalizedStudy

FIXTURES = pathlib.Path(__file__).parent / "fixtures"


@pytest.fixture(scope="session")
def raw_payload() -> dict:
    """A real ClinicalTrials.gov page, captured unmodified."""
    return json.loads((FIXTURES / "studies_pembrolizumab.json").read_text())


@pytest.fixture(scope="session")
def studies(raw_payload) -> list[NormalizedStudy]:
    parsed = (NormalizedStudy.from_api(r) for r in raw_payload["studies"])
    return [s for s in parsed if s is not None]


def make_study(nct_id: str = "NCT00000001", title: str = "A Study", **modules) -> NormalizedStudy:
    """Build a synthetic record for targeting one specific edge case."""
    protocol: dict = {"identificationModule": {"nctId": nct_id, "briefTitle": title}}
    top_level: dict = {}
    for key, value in modules.items():
        if key == "hasResults":
            top_level[key] = value
        elif key == "derivedSection":
            top_level[key] = value
        else:
            protocol[key] = value
    return NormalizedStudy.from_api({"protocolSection": protocol, **top_level})  # type: ignore[return-value]
