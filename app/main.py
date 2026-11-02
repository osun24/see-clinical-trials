"""FastAPI application: routes, error handling, and the demo page."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.encoders import jsonable_encoder
from fastapi.responses import FileResponse, JSONResponse

from app.config import settings
from app.ctgov.client import CtgovClient
from app.dimensions.registry import describe_registry
from app.models.errors import ErrorBody, ErrorResponse, ServiceError
from app.models.request import QueryRequest
from app.models.response import AnswerResponse
from app.pipeline import answer, plan_only

logger = logging.getLogger("clinical-trials-viz")

STATIC_DIR = Path(__file__).parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    """One shared connection pool for the process."""
    app.state.http = httpx.AsyncClient(
        base_url=settings.ctgov_base_url,
        timeout=httpx.Timeout(settings.read_timeout_s, connect=settings.connect_timeout_s),
        limits=httpx.Limits(max_connections=8),
        headers={"User-Agent": settings.user_agent, "Accept": "application/json"},
    )
    try:
        yield
    finally:
        await app.state.http.aclose()


app = FastAPI(
    title="Clinical Trials Visualization API",
    version="0.1.0",
    summary="Answers natural-language questions about clinical trials with a visualization specification.",
    description=(
        "Interprets a question with OpenAI Structured Outputs, retrieves matching "
        "trials from the ClinicalTrials.gov v2 API, decides whether and how to "
        "visualize the answer, and returns a renderable specification in which "
        "every datum carries citations back to the trial records behind it."
    ),
    lifespan=lifespan,
)


def _error_response(error: ServiceError) -> JSONResponse:
    return JSONResponse(
        status_code=error.status_code,
        content=error.to_response().model_dump(exclude_none=True),
    )


@app.exception_handler(ServiceError)
async def _service_error_handler(request: Request, exc: ServiceError) -> JSONResponse:
    if exc.status_code >= 500:
        logger.error("%s: %s", exc.code, exc.message)
    return _error_response(exc)


@app.exception_handler(RequestValidationError)
async def _validation_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    return JSONResponse(
        status_code=422,
        content=ErrorResponse(
            error=ErrorBody(
                code="invalid_request",
                message="The request did not match the expected schema.",
                # Pydantic puts the original exception object in `ctx`, which is
            # not JSON serialisable; encoding it would otherwise 500 the 422.
            detail=jsonable_encoder(
                exc.errors(), custom_encoder={Exception: str}
            ),
            )
        ).model_dump(exclude_none=True),
    )


@app.exception_handler(Exception)
async def _unhandled_handler(request: Request, exc: Exception) -> JSONResponse:
    logger.exception("unhandled error")
    return JSONResponse(
        status_code=500,
        content=ErrorResponse(
            error=ErrorBody(code="internal_error", message="An unexpected error occurred.")
        ).model_dump(exclude_none=True),
    )


@app.post(
    "/query",
    response_model=AnswerResponse,
    response_model_exclude_none=True,
    summary="Answer a question with a visualization specification",
)
async def query(request: QueryRequest, http_request: Request) -> AnswerResponse:
    async with CtgovClient(http_request.app.state.http) as client:
        return await answer(request, client)


@app.post(
    "/plan",
    summary="Interpret a question without fetching data",
    description=(
        "Returns the query plan and the exact ClinicalTrials.gov parameters that "
        "would be sent. Useful for inspecting interpretation without spending a fetch."
    ),
)
async def plan(request: QueryRequest) -> dict[str, Any]:
    return await plan_only(request)


@app.get(
    "/dimensions",
    summary="List the groupable dimensions",
    description=(
        "The registry the planner chooses from, including each dimension's counting "
        "rule and whether exact counts are available for it."
    ),
)
async def dimensions() -> dict[str, Any]:
    return {
        "dimensions": describe_registry(),
        "counting_rules": {
            "partition": "Each trial counts once; the values sum to the number of trials analyzed.",
            "multi_label": "A trial can count in several buckets; the values sum to more than the number of trials analyzed.",
        },
    }


@app.get("/healthz", summary="Liveness and configuration check")
async def healthz() -> dict[str, Any]:
    return {
        "status": "ok",
        "openai_configured": bool(settings.openai_api_key),
        "model": settings.openai_model,
        "record_cap": settings.record_cap,
        "exact_counts_enabled": settings.exact_counts,
    }


@app.get("/", include_in_schema=False)
async def demo() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")
