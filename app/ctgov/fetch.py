"""Flat, token-chained record fetch with a cap and a wall-clock deadline."""

from __future__ import annotations

import time
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.config import settings
from app.ctgov.client import CtgovClient
from app.ctgov.params import CtgovQuerySpec
from app.models.study import NormalizedStudy


class FetchResult(BaseModel):
    """Normalized studies plus their relationship to the matching universe."""

    model_config = {"extra": "forbid"}

    studies: list[NormalizedStudy] = Field(default_factory=list)
    total_matching: int = 0
    pages_fetched: int = 0
    truncated_reason: Literal["fetch_cap_reached", "time_budget_exceeded"] | None = None
    #: Exact per-bucket counts, when a stratified fetch produced them.
    exact_counts: dict[str, int] | None = None
    sampling_method: Literal[
        "census", "first_n_by_api_relevance_order", "stratified_by_bucket"
    ] = "first_n_by_api_relevance_order"
    #: Arm matches already counted under another arm. A comparison's
    #: ``total_matching`` is a sum of per-arm totals, so a trial matching two
    #: arms is counted twice there but held once in ``studies``.
    duplicate_arm_matches: int = 0

    @property
    def is_sample(self) -> bool:
        """Whether records are missing, as opposed to counted twice.

        A complete two-arm fetch where 39 trials match both arms holds 837
        records against a summed total of 876. Comparing those two numbers
        directly reported a full census as a 95% sample and warned that exact
        counts were approximate.
        """
        return len(self.studies) + self.duplicate_arm_matches < self.total_matching


def _request_params(spec: CtgovQuerySpec, page_size: int) -> dict[str, Any]:
    return {
        **spec.params,
        "fields": ",".join(spec.fields_mask),
        "pageSize": str(page_size),
    }


def parse_studies(payload: dict[str, Any]) -> list[NormalizedStudy]:
    """Normalize valid studies from one API page, skipping records without IDs."""
    parsed = (NormalizedStudy.from_api(r) for r in payload.get("studies", []))
    return [s for s in parsed if s is not None]


async def fetch_flat(
    client: CtgovClient,
    spec: CtgovQuerySpec,
    *,
    cap: int | None = None,
) -> FetchResult:
    """Page through results until the cap, the deadline, or the data runs out.

    Pagination is ``nextPageToken``-chained and therefore strictly sequential —
    there is no way to parallelise this particular fetch. When the deadline
    trips mid-pagination we return what we have, flagged: partial and honest
    beats a 502.
    """
    record_cap = cap or settings.record_cap
    deadline = time.monotonic() + settings.fetch_budget_s

    studies: list[NormalizedStudy] = []
    total_matching = 0
    pages = 0
    token: str | None = None
    truncated: Literal["fetch_cap_reached", "time_budget_exceeded"] | None = None

    while True:
        remaining = record_cap - len(studies)
        if remaining <= 0:
            truncated = "fetch_cap_reached"
            break

        params = _request_params(spec, min(settings.page_size, remaining))
        if pages == 0:
            params["countTotal"] = "true"
        if token:
            params["pageToken"] = token

        payload = await client.get_json("/studies", params)
        pages += 1

        if pages == 1:
            total_matching = int(payload.get("totalCount", 0))

        studies.extend(parse_studies(payload))

        token = payload.get("nextPageToken")
        if not token:
            break
        if time.monotonic() > deadline:
            truncated = "time_budget_exceeded"
            break

    if truncated is None and len(studies) < total_matching:
        truncated = "fetch_cap_reached"

    return FetchResult(
        studies=studies,
        total_matching=total_matching or len(studies),
        pages_fetched=pages,
        truncated_reason=truncated,
        sampling_method=(
            "census" if len(studies) >= total_matching else "first_n_by_api_relevance_order"
        ),
    )


async def fetch_total_only(client: CtgovClient, spec: CtgovQuerySpec) -> int:
    """Fetch only the matching count for adaptive comparison allocation."""
    params = _request_params(spec, 1)
    params["countTotal"] = "true"
    payload = await client.get_json("/studies", params)
    return int(payload.get("totalCount", 0))
