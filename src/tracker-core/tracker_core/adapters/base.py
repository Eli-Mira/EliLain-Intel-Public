"""Base adapter interface for all data sources.

Every adapter must implement:
- fetch() -> RawData: Fetch raw content from source
- parse(raw) -> list[Item]: Parse into typed items
- to_schema(item) -> dict: Convert to schema dict for validation
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Protocol


class RawData(Protocol):
    """Protocol for raw fetched data."""
    content: str | bytes
    headers: dict[str, str]
    status_code: int
    url: str
    elapsed_ms: float


class BaseAdapter(ABC):
    """Base class for all source adapters."""
    
    name: str = "base"
    source_type: str = "unknown"
    
    def __init__(self, config: dict[str, Any] | None = None):
        self.config = config or {}
        self._cache: dict[str, Any] = {}
    
    @abstractmethod
    def fetch(self) -> RawData:
        """Fetch raw data from the source."""
        ...
    
    @abstractmethod
    def parse(self, raw: RawData) -> list[Any]:
        """Parse raw data into typed items."""
        ...
    
    def to_schema(self, item: Any) -> dict[str, Any]:
        """Convert item to schema dict for validation.
        
        Override in subclasses if custom conversion needed.
        """
        if hasattr(item, 'model_dump'):
            return item.model_dump()
        return dict(item)
    
    def get_cache_key(self) -> str:
        """Get cache key for this adapter's data."""
        return f"{self.__class__.__name__}:{self.name}"
    
    def is_rate_limited(self, raw: RawData) -> bool:
        """Check if response indicates rate limiting."""
        return raw.status_code == 429
    
    def should_backoff(self, raw: RawData) -> bool:
        """Determine if we should back off after this response."""
        return raw.status_code >= 500 or self.is_rate_limited(raw)
    
    def get_retry_after(self, raw: RawData) -> float:
        """Get seconds to wait before retrying."""
        retry_after = raw.headers.get("Retry-After")
        if retry_after:
            try:
                return float(retry_after)
            except ValueError:
                pass
        # Default exponential backoff
        return 60.0


class ProviderAdapter(BaseAdapter):
    """Adapter for LLM API providers."""
    
    source_type = "provider"
    
    @abstractmethod
    def get_providers(self) -> list[Any]:
        """Return list of Provider items."""
        ...


class NewsAdapter(BaseAdapter):
    """Adapter for news sources."""
    
    source_type = "news"
    
    @abstractmethod
    def get_items(self) -> list[Any]:
        """Return list of NewsItem items."""
        ...
