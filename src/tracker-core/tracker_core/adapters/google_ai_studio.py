"""Google AI Studio adapter.

Fetches provider info from Google AI Studio pricing/docs page.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from typing import Any

import httpx

from .base import ProviderAdapter, RawData
from tracker_core.schema import Provider, VerificationStatus


logger = logging.getLogger(__name__)


@dataclass
class GoogleRawData:
    """Raw data from Google AI Studio."""
    content: str
    status_code: int
    url: str
    headers: dict[str, str]
    elapsed_ms: float


class GoogleAIStudioAdapter(ProviderAdapter):
    """Adapter for Google AI Studio (Gemini)."""
    
    name = "google-ai-studio"
    _BASE_URL = "https://ai.google.dev/pricing"
    _DOCS_URL = "https://ai.google.dev/docs"
    
    def __init__(self, config: dict[str, Any] | None = None):
        super().__init__(config)
        self._session = httpx.Client(timeout=30.0)
        self._session.headers.update({
            "User-Agent": "TrackerCore/1.0 (+https://github.com/tracker-core)"
        })
    
    def fetch(self) -> RawData:
        """Fetch Google AI Studio pricing page."""
        try:
            response = self._session.get(self._BASE_URL)
            return GoogleRawData(
                content=response.text,
                status_code=response.status_code,
                url=self._BASE_URL,
                headers=dict(response.headers),
                elapsed_ms=0  # httpx.Client doesn't track this
            )
        except httpx.HTTPError as e:
            logger.error(f"Failed to fetch Google AI Studio: {e}")
            raise
    
    def parse(self, raw: RawData) -> list[Provider]:
        """Parse raw data into Provider models."""
        if not isinstance(raw, GoogleRawData):
            raise ValueError(f"Expected GoogleRawData, got {type(raw)}")
        
        if raw.status_code != 200:
            logger.warning(f"Google AI Studio returned status {raw.status_code}")
            return []
        
        # Extract key information from the page
        provider = self._parse_content(raw.content, raw)
        return [provider]
    
    def _parse_content(self, content: str, raw: GoogleRawData) -> Provider:
        """Parse HTML content to extract provider info."""
        # Content hash for change detection
        content_hash = hashlib.sha256(content.encode()).hexdigest()[:16]
        
        # Extract free tier info from content
        free_tier_details = self._extract_free_tier(content)
        
        return Provider(
            name="Google AI Studio",
            base_url="https://ai.google.dev/",
            docs_url="https://ai.google.dev/docs",
            pricing_url="https://ai.google.dev/pricing",
            free_tier_available=True,
            free_tier_details=free_tier_details,
            status=VerificationStatus.UNVERIFIED,
            region="western",
            tags=["google", "gemini", "multimodal"],
            content_hash=content_hash,
            last_checked=raw.url  # Store URL for reference
        )
    
    def _extract_free_tier(self, content: str) -> dict[str, Any]:
        """Extract free tier details from page content."""
        details = {
            "models": [],
            "rate_limit": "Unknown",
            "source_url": self._BASE_URL
        }
        
        # Look for Gemini model mentions
        gemini_models = [
            "gemini-pro", "gemini-pro-vision", "gemini-1.5-pro",
            "gemini-1.5-flash", "gemini-1.0-pro"
        ]
        
        for model in gemini_models:
            if model in content.lower():
                details["models"].append(model)
        
        # Look for rate limit info
        import re
        rate_match = re.search(r'(\d+)\s*requests?\s*per\s*(minute|hour)', content, re.IGNORECASE)
        if rate_match:
            details["rate_limit"] = f"{rate_match.group(1)} per {rate_match.group(2)}"
        
        # Look for free tier mention
        if "free" in content.lower():
            details["has_free_tier"] = True
        
        return details
    
    def get_providers(self) -> list[Provider]:
        """Return seed provider data for Google AI Studio."""
        return [
            Provider(
                name="Google AI Studio",
                base_url="https://ai.google.dev/",
                docs_url="https://ai.google.dev/docs",
                pricing_url="https://ai.google.dev/pricing",
                free_tier_available=True,
                free_tier_details={
                    "rate_limit": "60 requests per minute",
                    "models": ["gemini-pro", "gemini-pro-vision", "gemini-1.5-pro"]
                },
                status=VerificationStatus.UNVERIFIED,
                region="western",
                tags=["google", "gemini", "multimodal"]
            )
        ]
    
    def get_fetch_url(self) -> str:
        """Return the URL to fetch."""
        return self._BASE_URL
    
    def close(self):
        """Close the HTTP client."""
        self._session.close()
    
    def __del__(self):
        """Ensure session is closed."""
        try:
            self.close()
        except Exception:
            pass
