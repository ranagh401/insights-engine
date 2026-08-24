"""Async Power BI Execute Queries REST API client.

Uses service-principal authentication (ClientSecretCredential) and
respects both a concurrency semaphore **and** a token-bucket rate limiter
so the engine never exceeds the Power BI per-dataset query or rate limit.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from typing import Any

import httpx
from azure.identity.aio import ClientSecretCredential

logger = logging.getLogger(__name__)

PBI_SCOPE = "https://analysis.windows.net/powerbi/api/.default"
PBI_BASE = "https://api.powerbi.com/v1.0/myorg"

_DETERMINISTIC_STATUS_CODES = frozenset({400, 403, 404})


class _TokenBucket:
    """Async token-bucket rate limiter (requests per second)."""

    def __init__(self, rate: float) -> None:
        self._rate = rate
        self._tokens = rate
        self._max = rate
        self._last = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            now = time.monotonic()
            elapsed = now - self._last
            self._tokens = min(self._max, self._tokens + elapsed * self._rate)
            self._last = now
            if self._tokens < 1.0:
                wait = (1.0 - self._tokens) / self._rate
                await asyncio.sleep(wait)
                self._tokens = 0.0
                self._last = time.monotonic()
            else:
                self._tokens -= 1.0


class PBIClient:
    """Thin async wrapper around the Power BI Execute Queries REST API.

    Throttling is two-layered:
    * **Concurrency semaphore** (``max_concurrent``) — caps in-flight requests.
    * **Token-bucket rate limiter** (``max_per_minute``) — caps request *throughput*
      so PBI's per-minute quota is never exceeded, even when many coroutines
      are queued behind the semaphore.
    """

    def __init__(
        self,
        tenant_id: str,
        client_id: str,
        client_secret: str,
        workspace_id: str,
        dataset_id: str,
        *,
        max_concurrent: int = 5,
        max_retries: int = 3,
        max_per_minute: int = 0,
        read_timeout_seconds: float = 0.0,
    ) -> None:
        self._credential = ClientSecretCredential(
            tenant_id, client_id, client_secret
        )
        self._workspace_id = workspace_id
        self._dataset_id = dataset_id
        self._sem = asyncio.Semaphore(max_concurrent)
        self._max_retries = max_retries
        self._http: httpx.AsyncClient | None = None

        if read_timeout_seconds and read_timeout_seconds > 0:
            self._read_timeout = float(read_timeout_seconds)
        else:
            self._read_timeout = float(
                os.environ.get("PBI_DAX_READ_TIMEOUT_SECONDS", "600") or "600"
            )
        self._read_timeout = max(60.0, self._read_timeout)
        self._httpx_timeout = httpx.Timeout(
            connect=60.0,
            read=self._read_timeout,
            write=120.0,
            pool=60.0,
        )

        rpm = max_per_minute or int(os.environ.get("PBI_MAX_REQUESTS_PER_MINUTE", "80") or "80")
        self._rate_limiter = _TokenBucket(rate=max(1.0, rpm / 60.0))

    async def _get_http(self) -> httpx.AsyncClient:
        if self._http is None or self._http.is_closed:
            self._http = httpx.AsyncClient(timeout=self._httpx_timeout)
        return self._http

    @property
    def _execute_url(self) -> str:
        return (
            f"{PBI_BASE}/groups/{self._workspace_id}"
            f"/datasets/{self._dataset_id}/executeQueries"
        )

    async def _get_headers(self) -> dict[str, str]:
        token = await self._credential.get_token(PBI_SCOPE)
        return {
            "Authorization": f"Bearer {token.token}",
            "Content-Type": "application/json",
        }

    @staticmethod
    def _parse_retry_after(resp: httpx.Response) -> float:
        """Extract seconds to wait from a 429 response (header or body)."""
        raw = resp.headers.get("Retry-After", "")
        if raw.strip().isdigit():
            return float(raw)
        try:
            body = resp.json()
            msg = body.get("message", "")
            m = re.search(r"Retry in (\d+) seconds", msg)
            if m:
                return float(m.group(1))
        except Exception:
            pass
        return 60.0

    async def execute_dax(self, query: str) -> list[dict[str, Any]]:
        """Execute a single DAX query and return the result rows.

        * 429 → honour ``Retry-After`` (PBI rate-limit); **does not** count
          against ``max_retries`` so transient throttling never kills the task.
        * 400/403/404 → deterministic; fail immediately, no retry.
        * ``httpx.ReadTimeout`` / other timeouts → retry with back-off (PBI can be
          slow on large ``SUMMARIZECOLUMNS`` bulk WHY queries).
        * Other errors → exponential back-off up to ``max_retries``.
        """
        await self._rate_limiter.acquire()
        async with self._sem:
            headers = await self._get_headers()
            body = {"queries": [{"query": query}]}
            last_exc: Exception | None = None
            client = await self._get_http()
            attempt = 0
            max_429_waits = 6
            while attempt < self._max_retries:
                attempt += 1
                try:
                    resp = await client.post(
                        self._execute_url, headers=headers, json=body
                    )

                    if resp.status_code == 429:
                        wait = self._parse_retry_after(resp)
                        logger.warning(
                            "PBI 429 rate-limited — waiting %.0fs before retry (attempt %d)",
                            wait, attempt,
                        )
                        max_429_waits -= 1
                        if max_429_waits <= 0:
                            raise RuntimeError("PBI 429 — exceeded max back-off cycles")
                        attempt -= 1
                        await asyncio.sleep(wait)
                        headers = await self._get_headers()
                        continue

                    if not resp.is_success:
                        try:
                            err_body = resp.json()
                        except Exception:
                            err_body = resp.text
                        logger.error(
                            "PBI Execute Queries HTTP %s — response body: %s",
                            resp.status_code, err_body,
                        )
                        if resp.status_code in _DETERMINISTIC_STATUS_CODES:
                            raise httpx.HTTPStatusError(
                                f"Deterministic HTTP {resp.status_code}",
                                request=resp.request, response=resp,
                            )
                    resp.raise_for_status()
                    data = resp.json()
                    tables = (
                        data.get("results", [{}])[0]
                        .get("tables", [{}])[0]
                        .get("rows", [])
                    )
                    return tables
                except httpx.HTTPStatusError as exc:
                    last_exc = exc
                    if exc.response.status_code in _DETERMINISTIC_STATUS_CODES:
                        logger.warning(
                            "DAX query deterministic %d — not retrying",
                            exc.response.status_code,
                        )
                        break
                    logger.warning(
                        "DAX query attempt %d/%d failed: %s",
                        attempt, self._max_retries, exc,
                    )
                    if attempt < self._max_retries:
                        await asyncio.sleep(2**attempt)
                except RuntimeError:
                    raise
                except httpx.TimeoutException as exc:
                    last_exc = exc
                    logger.warning(
                        "DAX query timeout (read=%.0fs) attempt %d/%d: %s",
                        self._read_timeout,
                        attempt,
                        self._max_retries,
                        exc,
                    )
                    if attempt < self._max_retries:
                        await asyncio.sleep(min(45.0, 5.0 * attempt))
                except Exception as exc:
                    last_exc = exc
                    logger.warning(
                        "DAX query attempt %d/%d failed: %s",
                        attempt, self._max_retries, exc,
                    )
                    if attempt < self._max_retries:
                        await asyncio.sleep(2**attempt)
            raise RuntimeError(
                f"DAX query failed after {self._max_retries} attempts"
            ) from last_exc

    async def execute_dax_batch(
        self, queries: list[str]
    ) -> list[list[dict[str, Any]]]:
        """Execute multiple DAX queries concurrently, respecting the semaphore."""
        return list(
            await asyncio.gather(*(self.execute_dax(q) for q in queries))
        )

    async def close(self) -> None:
        if self._http and not self._http.is_closed:
            await self._http.aclose()
        await self._credential.close()
