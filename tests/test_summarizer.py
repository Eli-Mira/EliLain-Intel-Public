"""Tests for news tracker summarizer."""

import pytest
import httpx

from news_tracker.summarizer import Summarizer
from tracker_core.schema import NewsItem, SourceType


class TestSummarizer:
    """Tests for summarizer."""
    
    def test_build_prompt(self):
        """Test prompt building."""
        summarizer = Summarizer()
        
        items = [
            NewsItem(source=SourceType.GITHUB, title="GPT-4 released", url="https://example.com/1"),
            NewsItem(source=SourceType.REDDIT, title="AI discussion", url="https://example.com/2"),
        ]
        
        prompt = summarizer._build_prompt(items)
        
        assert "GPT-4 released" in prompt
        assert "AI discussion" in prompt
        assert "JSON" in prompt.upper()
    
    @pytest.mark.asyncio
    async def test_summarize_manually(self):
        """Test manual summarization fallback."""
        summarizer = Summarizer()
        
        items = [
            NewsItem(source=SourceType.GITHUB, title="New AI Model Released Today", url="https://example.com/1"),
        ]
        
        result = summarizer.summarize_manually(items)
        
        assert result[0].summary is not None
        assert len(result[0].summary) > 0
    
    @pytest.mark.asyncio
    async def test_call_llm_success(self, monkeypatch):
        """Exercise production HTTP parsing, not an invented AsyncClient shape."""
        summarizer = Summarizer(config={"api_key": "test-key", "base_url": "https://example.org/v1"})
        original = httpx.AsyncClient
        transport = httpx.MockTransport(lambda request: httpx.Response(200, json={
            "choices": [{"message": {"content": '[{"summary": "Test summary", "dedup_key": "test"}]'}}]}))
        monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: original(**kw, transport=transport))
        result = await summarizer._call_llm("Test prompt")
        assert "Test summary" in result
    
    @pytest.mark.asyncio
    async def test_call_llm_failure(self, monkeypatch):
        """A real transport error is not a successful AI response."""
        summarizer = Summarizer()
        def failed(request):
            raise httpx.ConnectError("private-canary", request=request)
        original = httpx.AsyncClient
        transport = httpx.MockTransport(failed)
        monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: original(**kw, transport=transport))
        with pytest.raises(RuntimeError, match="AI summarization unavailable"):
            await summarizer._call_llm("Test prompt")


@pytest.mark.asyncio
async def test_summarize_news_function():
    """Test the main summarize_news function."""
    from news_tracker.summarizer import summarize_news
    
    items = [
        NewsItem(source=SourceType.GITHUB, title="Test AI Model", url="https://example.com/1"),
    ]
    
    result = await summarize_news(items)
    
    assert len(result) == 1
    assert result[0].summary is not None
