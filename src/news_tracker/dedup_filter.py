"""Deduplication and relevance filtering for news items."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from difflib import SequenceMatcher
from typing import Any

from tracker_core.schema import NewsItem, Category


logger = logging.getLogger(__name__)


class Deduplicator:
    """Remove duplicate news items across sources."""
    
    def __init__(self, threshold: float = 0.85):
        """
        Args:
            threshold: Similarity threshold for considering items duplicates (0.0 to 1.0)
        """
        self.threshold = threshold
    
    def deduplicate(self, items: list[NewsItem]) -> list[NewsItem]:
        """Remove duplicate items, keeping the most recent/higher quality version."""
        if len(items) <= 1:
            return items
        
        unique = []
        seen_urls = {}  # URL -> retained item
        seen_titles = {}  # Normalized title -> retained item

        for item in items:
            norm_title = self._normalize_title(item.title)

            # Check for exact URL match.
            existing = seen_urls.get(item.url)
            if existing is not None:
                if not self._is_better(existing, item):
                    continue
                unique.remove(existing)
                del seen_urls[existing.url]
                del seen_titles[self._normalize_title(existing.title)]

            # Resolve every title collision before removing any title winner.
            # A candidate can bridge two otherwise dissimilar retained titles.
            similar_keys = [
                key for key in seen_titles
                if self._title_similarity(norm_title, key) >= self.threshold
            ]
            if any(
                not self._is_better(seen_titles[key], item)
                for key in similar_keys
            ):
                continue

            for key in similar_keys:
                existing = seen_titles[key]
                unique.remove(existing)
                del seen_urls[existing.url]
                del seen_titles[key]

            unique.append(item)
            seen_urls[item.url] = item
            seen_titles[norm_title] = item
        
        return unique
    
    def _normalize_title(self, title: str) -> str:
        """Normalize title for comparison."""
        # Lowercase, remove special chars, collapse whitespace
        import re
        title = title.lower()
        title = re.sub(r'[^a-z0-9\s]', '', title)
        title = re.sub(r'\s+', ' ', title).strip()
        return title
    
    def _title_similarity(self, title1: str, title2: str) -> float:
        """Calculate similarity between two normalized titles."""
        return SequenceMatcher(None, title1, title2).ratio()
    
    def _is_better(self, existing: NewsItem, new: NewsItem) -> bool:
        """Determine if new item is better than existing."""
        # Prefer items with summaries
        if not existing.summary and new.summary:
            return True
        if existing.summary and not new.summary:
            return False
        
        # Prefer more recent items
        if existing.published_date and new.published_date:
            if new.published_date > existing.published_date:
                return True
        
        # Prefer items with higher relevance scores
        if new.relevance_score > existing.relevance_score:
            return True
        
        return False


class RelevanceFilter:
    """Filter news items by relevance to user interests."""
    
    DEFAULT_KEYWORDS = [
        "ai", "artificial intelligence", "llm", "large language model",
        "gpt", "claude", "gemini", "llama", "mistral",
        "openai", "anthropic", "google ai", "meta ai",
        "machine learning", "deep learning", "neural network",
        "generative ai", "agentic ai", "multimodal",
        "transformer", "reinforcement learning", "rlhf",
    ]
    
    def __init__(self, keywords: list[str] | None = None):
        """
        Args:
            keywords: Custom keywords for relevance filtering
        """
        self.keywords = [k.lower() for k in (keywords or self.DEFAULT_KEYWORDS)]
    
    def filter(self, items: list[NewsItem]) -> tuple[list[NewsItem], dict[str, int]]:
        """
        Filter items by relevance.
        
        Returns:
            Tuple of (filtered_items, stats_dict)
        """
        filtered = []
        stats = {
            "total": len(items),
            "relevant": 0,
            "irrelevant": 0,
            "by_category": {}
        }
        
        for item in items:
            score = self._calculate_relevance(item)
            item.relevance_score = score
            
            if score > 0:
                item.is_relevant = True
                filtered.append(item)
                stats["relevant"] += 1
                
                # Track by category
                cat = item.category.value if item.category else "unknown"
                stats["by_category"][cat] = stats["by_category"].get(cat, 0) + 1
            else:
                item.is_relevant = False
                stats["irrelevant"] += 1
        
        # Sort by relevance score descending
        filtered.sort(key=lambda x: x.relevance_score, reverse=True)
        
        return filtered, stats
    
    def _calculate_relevance(self, item: NewsItem) -> float:
        """Calculate relevance score for an item (0.0 to 1.0)."""
        text = f"{item.title} {item.summary or ''}".lower()
        
        # Count keyword matches
        matches = sum(1 for kw in self.keywords if kw in text)
        
        # Weight by number of matches
        score = min(matches / max(len(self.keywords) * 0.1, 1), 1.0)
        
        # Bonus for certain categories
        if item.category == Category.MODEL_RELEASE:
            score *= 1.2
        elif item.category == Category.RESEARCH_PAPER:
            score *= 1.1
        
        return min(score, 1.0)


def deduplicate_and_filter(
    items: list[NewsItem],
    dedup_threshold: float = 0.85,
    keywords: list[str] | None = None
) -> tuple[list[NewsItem], dict[str, Any]]:
    """
    Run deduplication and filtering on news items.
    
    Returns:
        Tuple of (filtered_items, context_dict)
    """
    # Deduplicate
    deduplicator = Deduplicator(threshold=dedup_threshold)
    unique_items = deduplicator.deduplicate(items)
    
    # Filter by relevance
    relevance_filter = RelevanceFilter(keywords=keywords)
    filtered_items, stats = relevance_filter.filter(unique_items)
    
    context = {
        "duplicate_count": len(items) - len(unique_items),
        **stats
    }
    
    return filtered_items, context
