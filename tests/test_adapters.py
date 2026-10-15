"""Tests for news tracker adapters."""

import pytest
import asyncio
from unittest.mock import Mock, AsyncMock, patch
from datetime import datetime, timezone

from news_tracker.adapters import (
    GitHubAdapter,
    RedditAdapter,
    HackerNewsAdapter,
    ArXivAdapter,
    RSSFeedAdapter,
    HuggingFaceAdapter,
    PapersWithCodeAdapter,
    YouTubeAdapter,
    get_adapter,
    get_all_adapters,
)
from tracker_core.schema import NewsItem, SourceType, Category


class TestGitHubAdapter:
    """Tests for GitHub adapter."""
    
    @pytest.mark.asyncio
    async def test_fetch_releases(self):
        """Test fetching GitHub trending repos."""
        adapter = GitHubAdapter()

        # Mock search API response (new structure)
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.content = b'{"items": [{"full_name": "test/repo", "description": "Test repo", "html_url": "https://github.com/test/repo", "updated_at": "2024-01-01T00:00:00Z"}]}'
        mock_response.success = True

        with patch.object(adapter.fetcher, 'fetch', new_callable=AsyncMock, return_value=mock_response):
            items = await adapter.fetch()

            assert len(items) >= 1
            assert items[0]["source"] == "github"
    
    def test_parse_item(self):
        """Test parsing a GitHub release item."""
        adapter = GitHubAdapter()
        
        raw = {
            "title": "GPT-4 Release",
            "url": "https://github.com/openai/gpt-4",
            "published_date": "2023-03-14T00:00:00+00:00",
            "body": "Introducing GPT-4"
        }
        
        item = adapter.parse_item(raw)
        
        assert item.source == SourceType.GITHUB
        assert item.title == "GPT-4 Release"
        assert item.category == Category.MODEL_RELEASE
    
    def test_classify_category(self):
        """Test category classification."""
        adapter = GitHubAdapter()
        
        # Model release
        assert adapter._classify_category("GPT-4 released", "") == Category.MODEL_RELEASE
        
        # Tool launch
        assert adapter._classify_category("New Python library for AI", "") == Category.TOOL_LAUNCH
        
        # Funding
        assert adapter._classify_category("OpenAI raises $10B", "") == Category.FUNDING


class TestRedditAdapter:
    """Tests for Reddit adapter."""
    
    @pytest.mark.asyncio
    async def test_fetch_subreddit(self):
        """Test fetching Reddit posts."""
        adapter = RedditAdapter()
        
        # Mock RSS response
        mock_rss = b'''<?xml version="1.0" encoding="UTF-8"?>
        <rss version="2.0">
            <channel>
                <title>r/MachineLearning</title>
                <item>
                    <title>Test Post</title>
                    <link>https://reddit.com/r/MachineLearning/post/1</link>
                    <pubDate>Mon, 01 Jan 2024 00:00:00 +0000</pubDate>
                    <description>Test description</description>
                    <author>testuser</author>
                </item>
            </channel>
        </rss>'''
        
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.content = mock_rss
        mock_response.success = True
        
        with patch.object(adapter.fetcher, 'fetch', new_callable=AsyncMock, return_value=mock_response):
            items = await adapter.fetch()
            
            assert len(items) >= 1
            assert items[0]["source"] == "reddit"
            assert items[0]["title"] == "Test Post"
    
    def test_parse_item(self):
        """Test parsing a Reddit post."""
        adapter = RedditAdapter()
        
        raw = {
            "title": "New LLM paper",
            "url": "https://reddit.com/r/ml/post/1",
            "published_date": (2024, 1, 1, 0, 0, 0, 0, 0, 0),
            "body": "Discussion about new model",
            "subreddit": "MachineLearning"
        }
        
        item = adapter.parse_item(raw)
        
        assert item.source == SourceType.REDDIT
        assert item.title == "New LLM paper"


class TestHackerNewsAdapter:
    """Tests for Hacker News adapter."""
    
    @pytest.mark.asyncio
    async def test_fetch_stories(self):
        """Test fetching HN stories."""
        adapter = HackerNewsAdapter()
        
        # Mock API response
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.content = b'{"hits": [{"title": "Test Story", "url": "https://example.com", "points": 100, "created_at_i": 1704067200}]}'
        mock_response.success = True
        
        with patch.object(adapter.fetcher, 'fetch', new_callable=AsyncMock, return_value=mock_response):
            items = await adapter.fetch()
            
            assert len(items) >= 1
            assert items[0]["source"] == "hackernews"
    
    def test_parse_item(self):
        """Test parsing HN item."""
        adapter = HackerNewsAdapter()
        
        raw = {
            "title": "AI breakthrough",
            "url": "https://news.ycombinator.com/item?id=123",
            "published_date": 1704067200,
            "points": 150
        }
        
        item = adapter.parse_item(raw)
        
        assert item.source == SourceType.HACKERNEWS
        assert item.title == "AI breakthrough"


class TestArXivAdapter:
    """Tests for arXiv adapter."""
    
    @pytest.mark.asyncio
    async def test_fetch_papers(self):
        """Test fetching arXiv papers."""
        adapter = ArXivAdapter()
        
        # Mock Atom response
        mock_atom = b'''<?xml version="1.0" encoding="utf-8"?>
        <feed xmlns="http://www.w3.org/2005/Atom">
            <entry>
                <title>Test Paper</title>
                <link href="https://arxiv.org/abs/2401.0001"/>
                <published>2024-01-01T00:00:00Z</published>
                <summary>Abstract text here</summary>
                <author><name>Author One</name></author>
            </entry>
        </feed>'''
        
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.content = mock_atom
        mock_response.success = True
        
        with patch.object(adapter.fetcher, 'fetch', new_callable=AsyncMock, return_value=mock_response):
            items = await adapter.fetch()
            
            assert len(items) >= 1
            assert items[0]["source"] == "arxiv"
    
    def test_parse_item(self):
        """Test parsing arXiv item."""
        adapter = ArXivAdapter()
        
        raw = {
            "title": "Attention Is All You Need",
            "url": "https://arxiv.org/abs/1706.03762",
            "published_date": (2017, 6, 12, 0, 0, 0, 0, 0, 0),
            "summary": "We propose a new simple network architecture...",
            "authors": [{"name": "Ashish Vaswani"}]
        }
        
        item = adapter.parse_item(raw)
        
        assert item.source == SourceType.ARXIV
        assert item.category == Category.RESEARCH_PAPER


class TestRSSFeedAdapter:
    """Tests for RSS feed adapter."""
    
    @pytest.mark.asyncio
    async def test_fetch_feeds(self):
        """Test fetching RSS feeds."""
        adapter = RSSFeedAdapter()
        
        # Mock RSS response
        mock_rss = b'''<?xml version="1.0" encoding="UTF-8"?>
        <rss version="2.0">
            <channel>
                <title>TechCrunch AI</title>
                <item>
                    <title>Test Article</title>
                    <link>https://techcrunch.com/test</link>
                    <pubDate>Mon, 01 Jan 2024 00:00:00 +0000</pubDate>
                    <description>Test summary</description>
                </item>
            </channel>
        </rss>'''
        
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.content = mock_rss
        mock_response.success = True
        
        with patch.object(adapter.fetcher, 'fetch', new_callable=AsyncMock, return_value=mock_response):
            items = await adapter.fetch()
            
            assert len(items) >= 1
            assert items[0]["source"] == "rss"


class TestFactoryFunctions:
    """Tests for adapter factory functions."""
    
    def test_get_adapter(self):
        """Test getting adapter by name."""
        github = get_adapter("github")
        assert isinstance(github, GitHubAdapter)
        
        reddit = get_adapter("reddit")
        assert isinstance(reddit, RedditAdapter)
        
        hn = get_adapter("hackernews")
        assert isinstance(hn, HackerNewsAdapter)
        
        arxiv = get_adapter("arxiv")
        assert isinstance(arxiv, ArXivAdapter)
        
        rss = get_adapter("rss")
        assert isinstance(rss, RSSFeedAdapter)
    
    def test_get_all_adapters(self):
        """Test getting all adapters."""
        adapters = get_all_adapters()
        
        assert len(adapters) == 7
        assert isinstance(adapters[0], GitHubAdapter)
        assert isinstance(adapters[1], RedditAdapter)
        assert isinstance(adapters[2], HackerNewsAdapter)
        assert isinstance(adapters[3], ArXivAdapter)
        assert isinstance(adapters[4], RSSFeedAdapter)
        assert isinstance(adapters[5], HuggingFaceAdapter)
        assert isinstance(adapters[6], YouTubeAdapter)
