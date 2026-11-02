"""The public request schema for POST /query."""

from __future__ import annotations

from datetime import datetime, timezone

from pydantic import BaseModel, Field, field_validator, model_validator

_MAX_YEAR = datetime.now(timezone.utc).year + 10


class QueryRequest(BaseModel):
    """A natural-language question plus optional structured narrowing.

    Structured fields are never required — the free-text ``query`` alone is a
    complete request. When supplied they are treated as user-asserted facts and
    take precedence over anything the model inferred from the prose.
    """

    model_config = {"extra": "forbid"}

    query: str = Field(
        min_length=1,
        max_length=500,
        description="Natural-language question about clinical trials.",
        examples=["How many pembrolizumab trials are there by phase?"],
    )

    drug_name: str | None = Field(
        default=None, max_length=200, description="Intervention / drug name."
    )
    condition: str | None = Field(
        default=None, max_length=200, description="Condition or disease."
    )
    sponsor: str | None = Field(
        default=None,
        max_length=200,
        description="Lead sponsor. Matched as fuzzy full text, not an exact name.",
    )
    country: str | None = Field(
        default=None, max_length=100, description="Country with a trial site."
    )
    trial_phase: list[str] | None = Field(
        default=None, description="Restrict to these phases, e.g. ['PHASE2','PHASE3']."
    )
    overall_status: list[str] | None = Field(
        default=None, description="Restrict to these statuses, e.g. ['RECRUITING']."
    )
    start_year: int | None = Field(default=None, ge=1990, le=_MAX_YEAR)
    end_year: int | None = Field(default=None, ge=1990, le=_MAX_YEAR)

    group_by: str | None = Field(
        default=None,
        description="Force a grouping dimension instead of letting the model choose. See GET /dimensions.",
    )
    limit: int | None = Field(
        default=None, ge=1, le=50, description="Max categories before 'Other' collapsing."
    )
    max_records: int | None = Field(
        default=None,
        ge=1,
        le=10000,
        description=(
            "Override the record fetch cap. Raise it to turn a sampled answer into "
            "a census when the matching universe is larger than the default cap; "
            "costs roughly 0.85 s per 1,000 records."
        ),
    )

    @field_validator("query")
    @classmethod
    def _query_not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("query must not be blank")
        return v.strip()

    @field_validator("trial_phase", "overall_status")
    @classmethod
    def _upper(cls, v: list[str] | None) -> list[str] | None:
        return [item.strip().upper() for item in v if item.strip()] if v else v

    @field_validator("trial_phase")
    @classmethod
    def _known_phase(cls, v: list[str] | None) -> list[str] | None:
        from app.dimensions.registry import PHASE_VALUES

        for item in v or []:
            if item not in PHASE_VALUES:
                raise ValueError(f"unknown phase {item!r}; expected one of {list(PHASE_VALUES)}")
        return v

    @field_validator("overall_status")
    @classmethod
    def _known_status(cls, v: list[str] | None) -> list[str] | None:
        from app.dimensions.registry import STATUS_VALUES

        for item in v or []:
            if item not in STATUS_VALUES:
                raise ValueError(f"unknown status {item!r}; expected one of {list(STATUS_VALUES)}")
        return v

    @field_validator("group_by")
    @classmethod
    def _known_dimension(cls, v: str | None) -> str | None:
        if v is None:
            return None
        from app.dimensions.registry import ENABLED_DIMENSIONS

        if v not in ENABLED_DIMENSIONS:
            raise ValueError(
                f"unknown dimension {v!r}; expected one of {list(ENABLED_DIMENSIONS)}"
            )
        return v

    @model_validator(mode="after")
    def _year_order(self) -> QueryRequest:
        if self.start_year and self.end_year and self.start_year > self.end_year:
            raise ValueError("start_year must not be after end_year")
        return self

    def supplied_filter_fields(self) -> dict[str, object]:
        """Structured fields the caller actually set, for precedence handling."""
        keys = (
            "drug_name",
            "condition",
            "sponsor",
            "country",
            "trial_phase",
            "overall_status",
            "start_year",
            "end_year",
        )
        return {k: getattr(self, k) for k in keys if getattr(self, k) is not None}
