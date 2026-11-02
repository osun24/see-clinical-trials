"""Error taxonomy. One envelope for every failure."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel

ErrorCode = Literal[
    "invalid_request",
    "query_not_interpretable",
    "out_of_scope",
    "llm_misconfigured",
    "llm_unavailable",
    "upstream_unavailable",
    "upstream_rejected_query",
    "internal_error",
]

#: code -> (HTTP status, retryable)
ERROR_STATUS: dict[str, tuple[int, bool]] = {
    "invalid_request": (422, False),
    "query_not_interpretable": (400, False),
    "out_of_scope": (400, False),
    "llm_misconfigured": (500, False),
    "llm_unavailable": (503, True),
    "upstream_unavailable": (502, True),
    "upstream_rejected_query": (400, False),
    "internal_error": (500, False),
}


class ErrorBody(BaseModel):
    """Stable JSON body nested under the public ``error`` key."""

    model_config = {"extra": "forbid"}

    code: str
    message: str
    retryable: bool = False
    detail: Any | None = None


class ErrorResponse(BaseModel):
    """Public error envelope shared by all endpoints."""

    model_config = {"extra": "forbid"}

    error: ErrorBody


class ServiceError(Exception):
    """Raised anywhere in the pipeline; rendered by one exception handler."""

    def __init__(self, code: str, message: str, detail: Any | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail

    @property
    def status_code(self) -> int:
        """HTTP status associated with this service error code."""
        return ERROR_STATUS.get(self.code, (500, False))[0]

    @property
    def retryable(self) -> bool:
        """Whether retrying later may succeed without changing the request."""
        return ERROR_STATUS.get(self.code, (500, False))[1]

    def to_response(self) -> ErrorResponse:
        """Convert the exception to the stable public error envelope."""
        return ErrorResponse(
            error=ErrorBody(
                code=self.code,
                message=self.message,
                retryable=self.retryable,
                detail=self.detail,
            )
        )
