"""Scoring and categorization for news items.

Implements:
- Cross-source corroboration scoring (item covered by N independent sources)
- Keyword-based weighting for high-signal terms
- Category classification with grouping support
- Urgency/importance scoring (Phase 1: deterministic rule-based)
"""

from __future__ import annotations

import logging
import re
from collections import Counter
from typing import Any

from tracker_core.schema import NewsItem, Category


logger = logging.getLogger(__name__)


# ── Keyword weights for scoring ──────────────────────────────────────────

KEYWORD_WEIGHTS = {
    # High signal — direct actionability
    "launch": 0.3, "open-source": 0.3, "open source": 0.3, "open weight": 0.3,
    "state-of-the-art": 0.25, "state of the art": 0.25, "sota": 0.25,
    "free tier": 0.35, "free api": 0.35, "rate limit": 0.2,
    "locally runnable": 0.3, "self-hosted": 0.3, "self hosted": 0.3,
    "new model": 0.25, "release": 0.15, "announce": 0.15,
    # Python ecosystem / data tooling
    "pandas": 0.2, "polars": 0.2, "sql": 0.15, "etl": 0.2,
    "orchestration": 0.2, "data pipeline": 0.2, "dataframe": 0.15,
    # Research signals
    "paper": 0.1, "arxiv": 0.1, "research": 0.1, "benchmark": 0.15,
    # Funding / industry
    "funding": 0.2, "acquisition": 0.2, "series": 0.1,
}

# Category classification patterns
CATEGORY_PATTERNS: dict[Category, list[str]] = {
    Category.MODEL_RELEASE: [
        r"\bmodel\s+release", r"\brelease[ds]?\b.*\b(model|llm|gpt|claude|gemini|llama)",
        r"\b(new|latest)\s+(model|llm|version)", r"\bopen.?weight\b",
        r"\bcheckpoint\b", r"\bfine.?tun", r"\binstruct\b.*\bmodel",
    ],
    Category.RESEARCH_PAPER: [
        r"\bpaper\b", r"\barxiv\b", r"\bresearch\b", r"\bstudy\b",
        r"\bpublication\b", r"\bproceedings\b", r"\bconference\b",
        r"\bneurips\b", r"\biclr\b", r"\bicml\b", r"\bacl\b",
    ],
    Category.FUNDING: [
        r"\bfunding\b", r"\braised?\b", r"\bseries\s+[a-d]\b",
        r"\bacquisition\b", r"\bvaluation\b", r"\binvestment\b",
        r"\bseed\s+round", r"\bventure\b",
    ],
    Category.TOOL_LAUNCH: [
        r"\bopen.?source\b", r"\bgithub\b", r"\bsdk\b", r"\bapi\b",
        r"\btool\b", r"\blibrary\b", r"\bframework\b", r"\bpackage\b",
        r"\bpip\s+install\b", r"\bnpm\s+install\b", r"\bcli\b",
    ],
    Category.POLICY: [
        r"\bregulation\b", r"\bpolicy\b", r"\bgovernment\b", r"\blaw\b",
        r"\bcompliance\b", r"\blegislation\b", r"\boversight\b",
        r"\bethics?\b", r"\bresponsible\s+ai\b", r"\bai\s+safety\b",
    ],
}


class Scorer:
    """Score news items by cross-source corroboration + keyword weighting."""

    def __init__(self, keyword_weights: dict[str, float] | None = None):
        self.keyword_weights = keyword_weights or KEYWORD_WEIGHTS

    def score_items(self, items: list[NewsItem]) -> list[NewsItem]:
        """
        Score all items. Adds:
        - source_count: how many independent sources cover this story
        - cross_source_boost: bonus for corroborated stories
        - keyword_score: weighted keyword match score
        - final_score: combined score for ranking
        """
        # Count source coverage per normalized title
        title_counts = Counter()
        for item in items:
            norm = self._normalize_title(item.title)
            title_counts[norm] += 1

        for item in items:
            norm = self._normalize_title(item.title)
            item.source_count = title_counts[norm]

            # Cross-source corroboration boost
            cross_source_boost = min((item.source_count - 1) * 0.15, 0.45)

            # Keyword score
            keyword_score = self._keyword_score(item)

            # Combine
            item.relevance_score = min(
                item.relevance_score + cross_source_boost + keyword_score,
                1.0
            )

        return items

    def _keyword_score(self, item: NewsItem) -> float:
        """Calculate keyword-based score for an item."""
        text = f"{item.title} {item.summary or ''}".lower()
        score = 0.0
        for keyword, weight in self.keyword_weights.items():
            if keyword in text:
                score += weight
        return min(score, 0.5)

    def _normalize_title(self, title: str) -> str:
        """Normalize title for cross-source matching."""
        title = title.lower()
        title = re.sub(r'[^a-z0s]', '', title)
        title = re.sub(r'\s+', ' ', title).strip()
        return title


class Categorizer:
    """Classify news items into categories."""

    def __init__(self, patterns: dict[Category, list[str]] | None = None):
        self.patterns = patterns or CATEGORY_PATTERNS

    def categorize(self, item: NewsItem) -> NewsItem:
        """Assign a category to an item."""
        text = f"{item.title} {item.summary or ''}".lower()

        for category, patterns in self.patterns.items():
            for pattern in patterns:
                if re.search(pattern, text, re.IGNORECASE):
                    item.category = category
                    return item

        # Default to discussion if no match
        if item.category is None:
            item.category = Category.DISCUSSION
        return item

    def categorize_all(self, items: list[NewsItem]) -> list[NewsItem]:
        """Categorize all items."""
        return [self.categorize(item) for item in items]


def group_by_category(items: list[NewsItem]) -> dict[str, list[NewsItem]]:
    """Group items by their category."""
    groups: dict[str, list[NewsItem]] = {}
    for item in items:
        cat = item.category.value if item.category else "unknown"
        groups.setdefault(cat, []).append(item)
    return groups


def score_and_categorize(items: list[NewsItem]) -> list[NewsItem]:
    """Convenience: run scoring + categorization in one pass."""
    scorer = Scorer()
    categorizer = Categorizer()

    items = scorer.score_items(items)
    items = categorizer.categorize_all(items)

    # Sort by final score descending
    items.sort(key=lambda x: x.relevance_score, reverse=True)
    return items
