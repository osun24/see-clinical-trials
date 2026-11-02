"""Async ClinicalTrials.gov client: retries, deadlines, and a small cache.

This is an unauthenticated public API with no rate-limit headers to guide us,
so the client is deliberately polite: bounded connections, exponential backoff
with full jitter, ``Retry-After`` honoured, and a short-lived response cache so
the demo and test suite do not hammer it.
"""

from __future__ import annotations

import asyncio
import random
import time
from typing import Any

import httpx

from app.config import settings
from app.models.errors import ServiceError


class _TTLCache:
    """Tiny FIFO cache keyed on the normalised request."""

    def __init__(self, ttl_s: float, max_entries: int) -> None:
        self._ttl = ttl_s
        self._max = max_entries
        self._data: dict[str, tuple[float, Any]] = {}

    def get(self, key: str) -> Any | None:
        hit = self._data.get(key)
        if hit is None:
            return None
        stored_at, value = hit
        if time.monotonic() - stored_at > self._ttl:
            self._data.pop(key, None)
            return None
        return value

    def set(self, key: str, value: Any) -> None:
        if len(self._data) >= self._max:
            self._data.pop(next(iter(self._data)), None)
        self._data[key] = (time.monotonic(), value)

    def clear(self) -> None:
        self._data.clear()


_cache = _TTLCache(settings.cache_ttl_s, settings.cache_max_entries)


def clear_cache() -> None:
    _cache.clear()


def _cache_key(path: str, params: dict[str, Any]) -> str:
    return path + "?" + "&".join(f"{k}={params[k]}" for k in sorted(params))


class CtgovClient:
    """One client per request; the underlying connection pool is shared."""

    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self._client = client
        self._owns_client = client is None

    async def __aenter__(self) -> CtgovClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=settings.ctgov_base_url,
                timeout=httpx.Timeout(
                    settings.read_timeout_s, connect=settings.connect_timeout_s
                ),
                limits=httpx.Limits(max_connections=8),
                headers={"User-Agent": settings.user_agent, "Accept": "application/json"},
            )
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    async def get_json(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        """GET with retries. Raises ``ServiceError`` once retries are spent."""
        assert self._client is not None, "use CtgovClient as an async context manager"

        key = _cache_key(path, params)
        cached = _cache.get(key)
        if cached is not None:
            return cached

        last_error: str = "unknown error"
        for attempt in range(settings.max_retries + 1):
            try:
                response = await self._client.get(path, params=params)
            except httpx.HTTPError as exc:
                last_error = f"{type(exc).__name__}: {exc}"
            else:
                if response.status_code == 200:
                    payload = response.json()
                    _cache.set(key, payload)
                    return payload

                # A 400 means the filter expression we built is wrong. Retrying
                # cannot fix our own bug, and the params belong in the message.
                if response.status_code == 400:
                    raise ServiceError(
                        "upstream_rejected_query",
                        "ClinicalTrials.gov rejected the generated query.",
                        detail={"params": params, "body": response.text[:500]},
                    )
                if response.status_code == 404:
                    raise ServiceError(
                        "upstream_unavailable",
                        f"ClinicalTrials.gov returned 404 for {path}.",
                    )
                last_error = f"HTTP {response.status_code}"
                if response.status_code not in (429,) and response.status_code < 500:
                    raise ServiceError(
                        "upstream_unavailable",
                        f"ClinicalTrials.gov returned {response.status_code}.",
                        detail={"body": response.text[:500]},
                    )
                retry_after = response.headers.get("Retry-After")
                if retry_after and retry_after.isdigit():
                    await asyncio.sleep(min(float(retry_after), 10.0))
                    continue

            if attempt < settings.max_retries:
                backoff = 0.5 * (2**attempt)
                await asyncio.sleep(random.uniform(0, backoff))

        raise ServiceError(
            "upstream_unavailable",
            f"ClinicalTrials.gov did not respond successfully after "
            f"{settings.max_retries + 1} attempts ({last_error}).",
        )

    async def data_timestamp(self) -> str | None:
        try:
            payload = await self.get_json("/version", {})
        except ServiceError:
            return None
        return payload.get("dataTimestamp")
