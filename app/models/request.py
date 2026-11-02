"""The natural-language request accepted by POST /query and POST /plan."""

from __future__ import annotations

from pydantic import BaseModel, Field, field_validator


class QueryRequest(BaseModel):
    """One natural-language question; the planner derives all execution details."""

    model_config = {"extra": "forbid"}

    query: str = Field(
        min_length=1,
        max_length=500,
        description="Natural-language question about clinical trials.",
        examples=["How many pembrolizumab trials are there by phase?"],
    )

    @field_validator("query")
    @classmethod
    def _query_not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("query must not be blank")
        return v.strip()
