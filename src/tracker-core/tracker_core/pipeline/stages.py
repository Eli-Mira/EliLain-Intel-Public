"""Pipeline stages for processing data through the tracker."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Protocol


class PipelineStage(Protocol):
    """Protocol for pipeline stages."""
    
    name: str
    
    async def process(self, data: Any, context: dict[str, Any]) -> tuple[Any, dict[str, Any]]:
        """Process data and return (result, updated_context)."""
        ...


class BaseStage(ABC):
    """Base class for pipeline stages."""
    
    name: str = "base"
    
    def __init__(self, config: dict[str, Any] | None = None):
        self.config = config or {}
    
    @abstractmethod
    async def process(self, data: Any, context: dict[str, Any]) -> tuple[Any, dict[str, Any]]:
        """Process data through this stage."""
        ...
    
    async def __call__(self, data: Any, context: dict[str, Any]) -> tuple[Any, dict[str, Any]]:
        """Allow stage to be called directly."""
        return await self.process(data, context)


class FetchStage(BaseStage):
    """Fetch data from sources."""
    
    name = "fetch"
    
    def __init__(self, fetcher, adapters: list[Any]):
        super().__init__()
        self.fetcher = fetcher
        self.adapters = adapters
    
    async def process(self, data: Any, context: dict[str, Any]) -> tuple[Any, dict[str, Any]]:
        """Fetch from all adapters."""
        results = []
        for adapter in self.adapters:
            try:
                raw = await self.fetcher.fetch(adapter.get_fetch_url())
                parsed = adapter.parse(raw)
                results.extend(parsed)
            except Exception as e:
                context.setdefault("errors", []).append({
                    "adapter": adapter.name,
                    "error": str(e)
                })
        
        context["fetched_items"] = results
        return results, context


class ParseStage(BaseStage):
    """Parse raw data into schema objects."""
    
    name = "parse"
    
    def __init__(self, parser):
        super().__init__()
        self.parser = parser
    
    async def process(self, data: list[Any], context: dict[str, Any]) -> tuple[list[Any], dict[str, Any]]:
        """Parse all items."""
        parsed = []
        for item in data:
            try:
                parsed_item = self.parser.parse(item)
                parsed.append(parsed_item)
            except Exception as e:
                context.setdefault("parse_errors", []).append({
                    "item": str(item)[:100],
                    "error": str(e)
                })
        
        return parsed, context


class DedupeStage(BaseStage):
    """Remove duplicate items."""
    
    name = "dedupe"
    
    def __init__(self, strategy: str = "url"):
        super().__init__()
        self.strategy = strategy
    
    async def process(self, data: list[Any], context: dict[str, Any]) -> tuple[list[Any], dict[str, Any]]:
        """Deduplicate by URL or title."""
        seen = set()
        unique = []
        
        for item in data:
            if self.strategy == "url":
                key = str(item.url) if hasattr(item, "url") else str(item)
            else:
                key = str(item.title) if hasattr(item, "title") else str(item)
            
            if key not in seen:
                seen.add(key)
                unique.append(item)
        
        context["duplicate_count"] = len(data) - len(unique)
        return unique, context


class FilterStage(BaseStage):
    """Filter by relevance."""
    
    name = "filter"
    
    def __init__(self, keywords: list[str] | None = None):
        super().__init__()
        self.keywords = keywords or []
    
    async def process(self, data: list[Any], context: dict[str, Any]) -> tuple[list[Any], dict[str, Any]]:
        """Filter items by relevance."""
        filtered = []
        relevant_count = 0
        
        for item in data:
            text = f"{item.title} {getattr(item, 'summary', '')}".lower()
            is_relevant = any(kw.lower() in text for kw in self.keywords)
            
            item.is_relevant = is_relevant
            if is_relevant:
                relevant_count += 1
                filtered.append(item)
        
        context["relevant_count"] = relevant_count
        return filtered, context


class StoreStage(BaseStage):
    """Store data to persistence."""
    
    name = "store"
    
    def __init__(self, store):
        super().__init__()
        self.store = store
    
    async def process(self, data: list[Any], context: dict[str, Any]) -> tuple[list[Any], dict[str, Any]]:
        """Store items."""
        stored = await self.store.save(data)
        context["stored_count"] = len(stored)
        return stored, context


class ReporterStage(BaseStage):
    """Generate report from data."""
    
    name = "report"
    
    def __init__(self, reporter):
        super().__init__()
        self.reporter = reporter
    
    async def process(self, data: list[Any], context: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        """Generate report text."""
        report = self.reporter.generate(data, context)
        context["report_generated"] = True
        return report, context


class NotifierStage(BaseStage):
    """Send report via notification channel."""
    
    name = "notify"
    
    def __init__(self, notifier):
        super().__init__()
        self.notifier = notifier
    
    async def process(self, report: str, context: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        """Send notification."""
        sent = await self.notifier.send(report)
        context["notification_sent"] = sent
        return report, context
