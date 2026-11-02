"""A fetched trial record, normalised just enough to aggregate and cite."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from app.dimensions.extractors import extract_one


class NormalizedStudy(BaseModel):
    """One ClinicalTrials.gov study.

    ``raw`` is retained deliberately: citations quote field values straight out
    of the API payload, so the original record has to stay reachable. Nothing
    downstream may invent a value that is not in here.
    """

    model_config = {"extra": "forbid"}

    nct_id: str
    brief_title: str | None = None
    raw: dict[str, Any] = Field(repr=False)

    @classmethod
    def from_api(cls, record: dict[str, Any]) -> NormalizedStudy | None:
        nct_id = extract_one(record, "protocolSection.identificationModule.nctId")
        if not nct_id:
            return None
        return cls(
            nct_id=str(nct_id),
            brief_title=extract_one(
                record, "protocolSection.identificationModule.briefTitle"
            ),
            raw=record,
        )

    @property
    def url(self) -> str:
        return f"https://clinicaltrials.gov/study/{self.nct_id}"
