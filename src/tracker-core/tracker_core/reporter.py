"""Reporter module for generating formatted reports."""

from __future__ import annotations

from datetime import datetime
from typing import Any


class BaseReporter:
    """Base reporter for generating text reports."""
    
    def __init__(self, config: dict[str, Any] | None = None):
        self.config = config or {}
    
    def generate(self, data: list[Any], context: dict[str, Any]) -> str:
        """Generate report from processed data."""
        raise NotImplementedError


class ProviderReport(BaseReporter):
    """Report for free API providers."""
    
    def generate(self, providers: list[Any], context: dict[str, Any]) -> str:
        """Generate markdown table report."""
        lines = [
            "# Free LLM API Tracker Report",
            f"**Generated:** {datetime.utcnow().isoformat()}Z",
            "",
            "## Providers",
            "",
            "| Name | Status | Free Tier | Last Verified |",
            "|------|--------|-----------|---------------|"
        ]
        
        for provider in providers:
            name = getattr(provider, "name", "Unknown")
            status = getattr(provider, "status", "unknown").value
            has_free = "Yes" if getattr(provider, "free_tier_available", False) else "No"
            last_verified = getattr(provider, "last_verified", None)
            if last_verified:
                last_verified = last_verified.strftime("%Y-%m-%d") if hasattr(last_verified, "strftime") else str(last_verified)
            else:
                last_verified = "Never"
            
            lines.append(f"| {name} | {status} | {has_free} | {last_verified} |")
        
        # Add summary
        total = len(providers)
        verified = sum(1 for p in providers if getattr(p, "status", None) == "verified")
        needs_review = sum(1 for p in providers if getattr(p, "needs_review", False))
        
        lines.extend([
            "",
            "## Summary",
            "",
            f"- **Total providers:** {total}",
            f"- **Verified:** {verified}",
            f"- **Needs review:** {needs_review}",
        ])
        
        # Add errors if any
        errors = context.get("errors", [])
        if errors:
            lines.extend(["", "## Errors", ""])
            for err in errors:
                lines.append(f"- **{err['adapter']}**: {err['error']}")
        
        return "\n".join(lines)


class NewsReport(BaseReporter):
    """Report for AI/tech news items."""
    
    def __init__(self, max_items: int = 20):
        super().__init__()
        self.max_items = max_items
    
    def generate(self, items: list[Any], context: dict[str, Any]) -> str:
        """Generate markdown digest report."""
        date = context.get("date", datetime.utcnow().strftime("%Y-%m-%d"))
        
        lines = [
            f"# AI/Tech News Digest — {date}",
            "",
            f"**Total items:** {len(items)} | **Relevant:** {context.get('relevant_count', 0)} | **Duplicates removed:** {context.get('duplicate_count', 0)}",
            "",
        ]
        
        # Group by category
        by_category: dict[str, list[Any]] = {}
        for item in items[:self.max_items]:
            cat = getattr(item, "category", "other")
            cat_str = cat.value if hasattr(cat, "value") else str(cat)
            by_category.setdefault(cat_str, []).append(item)
        
        for cat, cat_items in sorted(by_category.items()):
            lines.extend([
                f"## {cat.replace('_', ' ').title()}",
                ""
            ])
            
            for item in cat_items:
                title = getattr(item, "title", "Untitled")
                url = getattr(item, "url", "#")
                source = getattr(item, "source", "unknown")
                summary = getattr(item, "summary", "")
                
                lines.append(f"### [{title}]({url})")
                lines.append(f"*Source: {source}*")
                if summary:
                    lines.append(f"{summary}")
                lines.append("")
        
        # Add errors
        parse_errors = context.get("parse_errors", [])
        if parse_errors:
            lines.extend(["", "## Parse Errors", ""])
            for err in parse_errors[:10]:  # Limit to 10
                lines.append(f"- {err['error']}: {err['item']}")
        
        return "\n".join(lines)


class ReportGenerator:
    """Factory for creating appropriate reporter."""
    
    @staticmethod
    def get_reporter(tool_type: str, **kwargs) -> BaseReporter:
        """Get reporter for tool type."""
        if tool_type == "providers":
            return ProviderReport(**kwargs)
        elif tool_type == "news":
            return NewsReport(**kwargs)
        else:
            raise ValueError(f"Unknown tool type: {tool_type}")
