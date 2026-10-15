"""Fetcher module for retrieving data from sources."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any, Protocol

import httpx


@dataclass
class FetchResult:
    """Result of a fetch operation."""
    content: str | bytes
    headers: dict[str, str]
    status_code: int
    url: str
    elapsed_ms: float
    success: bool
    error: str | None = None


class Fetcher(Protocol):
    """Protocol for fetchers."""
    
    async def fetch(self, url: str, **kwargs) -> FetchResult:
        ...


class HTTPFetcher:
    """HTTP fetcher with rate limiting and error handling."""
    
    def __init__(
        self,
        timeout: float = 30.0,
        max_retries: int = 3,
        rate_limit_delay: float = 1.0,
        user_agent: str = "TrackerCore/1.0 (+https://github.com/your-org/tracker-core)"
    ):
        self.timeout = timeout
        self.max_retries = max_retries
        self.rate_limit_delay = rate_limit_delay
        self.user_agent = user_agent
        self._last_request_time: dict[str, float] = {}
    
    async def fetch(
        self,
        url: str,
        method: str = "GET",
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
        data: Any = None,
        **kwargs
    ) -> FetchResult:
        """Fetch URL with retries and rate limiting."""
        combined_headers = {
            "User-Agent": self.user_agent,
            "Accept": "application/json, text/html, */*",
            **(headers or {})
        }
        
        last_error = None
        start_time = time.monotonic()
        
        for attempt in range(self.max_retries):
            # Rate limiting per domain
            domain = url.split("/")[2] if "/" in url else url
            await self._maybe_throttle(domain)
            
            try:
                async with httpx.AsyncClient(timeout=self.timeout) as client:
                    response = await client.get(
                        url,
                        headers=combined_headers,
                        params=params,
                        follow_redirects=True
                    )
                    
                    elapsed = (time.monotonic() - start_time) * 1000
                    
                    # Store for next time
                    self._last_request_time[domain] = time.monotonic()
                    
                    if response.status_code == 429:
                        retry_after = int(response.headers.get("Retry-After", 60))
                        await asyncio.sleep(retry_after)
                        continue
                    
                    return FetchResult(
                        content=response.text,
                        headers=dict(response.headers),
                        status_code=response.status_code,
                        url=str(response.url),
                        elapsed_ms=elapsed,
                        success=200 <= response.status_code < 300
                    )
                    
            except httpx.HTTPError as e:
                last_error = str(e)
                if attempt < self.max_retries - 1:
                    await asyncio.sleep(min(2 ** attempt, 30))
        
        return FetchResult(
            content="",
            headers={},
            status_code=0,
            url=url,
            elapsed_ms=(time.monotonic() - start_time) * 1000,
            success=False,
            error=last_error or "Max retries exceeded"
        )
    
    async def _maybe_throttle(self, domain: str) -> None:
        """Enforce rate limiting per domain."""
        last_time = self._last_request_time.get(domain, 0)
        elapsed = time.monotonic() - last_time
        
        if elapsed < self.rate_limit_delay:
            await asyncio.sleep(self.rate_limit_delay - elapsed)
    
    def set_user_agent(self, agent: str) -> None:
        """Update user agent string."""
        self.user_agent = agent
