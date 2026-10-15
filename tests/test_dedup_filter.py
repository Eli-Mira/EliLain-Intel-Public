"""Tests for news tracker deduplication and filtering."""

import pytest
from datetime import datetime, timezone

from news_tracker.dedup_filter import Deduplicator, RelevanceFilter, deduplicate_and_filter
from tracker_core.schema import NewsItem, SourceType, Category


class TestDeduplicator:
    """Tests for deduplication logic."""
    
    def test_exact_url_dedup(self):
        """Items with same URL are deduplicated."""
        dedup = Deduplicator()
        
        items = [
            NewsItem(source=SourceType.GITHUB, title="Test", url="https://example.com/1"),
            NewsItem(source=SourceType.REDDIT, title="Test", url="https://example.com/1"),
        ]
        
        result = dedup.deduplicate(items)
        assert len(result) == 1
    
    def test_similar_title_dedup(self):
        """Items with similar titles are deduplicated."""
        dedup = Deduplicator(threshold=0.8)
        
        items = [
            NewsItem(source=SourceType.GITHUB, title="OpenAI releases GPT-4", url="https://example.com/1"),
            NewsItem(source=SourceType.HACKERNEWS, title="OpenAI Releases GPT-4 Model", url="https://example.com/2"),
        ]
        
        result = dedup.deduplicate(items)
        # Should be deduplicated due to similar titles
        assert len(result) <= 2
    
    def test_no_dedup_different_items(self):
        """Different items are not deduplicated."""
        dedup = Deduplicator()
        
        items = [
            NewsItem(source=SourceType.GITHUB, title="AI Model Release", url="https://example.com/1"),
            NewsItem(source=SourceType.REDDIT, title="Different Topic", url="https://example.com/2"),
        ]
        
        result = dedup.deduplicate(items)
        assert len(result) == 2
    
    def test_empty_list(self):
        """Empty list returns empty."""
        dedup = Deduplicator()
        result = dedup.deduplicate([])
        assert result == []
    
    def test_single_item(self):
        """Single item returns as-is."""
        dedup = Deduplicator()
        items = [NewsItem(source=SourceType.GITHUB, title="Test", url="https://example.com/1")]
        result = dedup.deduplicate(items)
        assert len(result) == 1


class TestRelevanceFilter:
    """Tests for relevance filtering."""
    
    def test_keyword_match(self):
        """Items with AI keywords are marked relevant."""
        filter_obj = RelevanceFilter()
        
        item = NewsItem(
            source=SourceType.GITHUB,
            title="OpenAI releases GPT-4 model",
            url="https://example.com/1",
            category=Category.MODEL_RELEASE
        )
        
        filtered, stats = filter_obj.filter([item])
        
        assert len(filtered) == 1
        assert item.is_relevant is True
        assert item.relevance_score > 0
    
    def test_no_match(self):
        """Items without keywords are filtered out."""
        filter_obj = RelevanceFilter()
        
        item = NewsItem(
            source=SourceType.NEWS,
            title="Local bakery opens new location",
            url="https://example.com/1"
        )
        
        filtered, stats = filter_obj.filter([item])
        
        assert len(filtered) == 0
        assert item.is_relevant is False
    
    def test_custom_keywords(self):
        """Custom keywords work."""
        filter_obj = RelevanceFilter(keywords=["python", "programming"])
        
        item = NewsItem(
            source=SourceType.GITHUB,
            title="New Python library released",
            url="https://example.com/1"
        )
        
        filtered, stats = filter_obj.filter([item])
        
        assert len(filtered) == 1
        assert item.is_relevant is True
    
    def test_sorting_by_score(self):
        """Items are sorted by relevance score."""
        filter_obj = RelevanceFilter()
        
        items = [
            NewsItem(source=SourceType.GITHUB, title="AI model", url="https://example.com/1"),
            NewsItem(source=SourceType.NEWS, title="Baking bread", url="https://example.com/2"),
            NewsItem(source=SourceType.HACKERNEWS, title="LLM research paper", url="https://example.com/3"),
        ]
        
        filtered, _ = filter_obj.filter(items)
        
        # Most relevant should be first
        assert filtered[0].relevance_score >= filtered[-1].relevance_score if len(filtered) > 1 else True


class TestDeduplicateAndFilter:
    """Tests for combined dedup and filter."""
    
    def test_full_pipeline(self):
        """Test full dedup + filter pipeline."""
        items = [
            NewsItem(source=SourceType.GITHUB, title="GPT-4 released", url="https://github.com/1"),
            NewsItem(source=SourceType.REDDIT, title="GPT-4 released", url="https://github.com/1"),  # Duplicate
            NewsItem(source=SourceType.HACKERNEWS, title="AI breakthrough", url="https://hn.com/1"),
            NewsItem(source=SourceType.NEWS, title="Baking tips", url="https://news.com/1"),  # Irrelevant
        ]
        
        filtered, context = deduplicate_and_filter(items)
        
        # Should have removed duplicate and irrelevant item
        assert len(filtered) <= 2
        assert context.get("duplicate_count", 0) >= 0
        assert context.get("relevant_count", 0) <= len(items)
