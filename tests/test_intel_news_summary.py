"""News summaries must degrade transparently, not fabricate successful output."""
import httpx
import pytest
from tracker_core.schema import NewsItem, SourceType
from news_tracker import summarizer


def item(title):
    return NewsItem(title=title,url='https://example.org/'+title.replace(' ','-'),source=SourceType.GITHUB)


@pytest.mark.asyncio
async def test_unconfigured_summarizer_never_contacts_router(monkeypatch):
    async def forbidden(*args, **kwargs):
        pytest.fail('Unconfigured AI summarization must not contact a router')
    monkeypatch.setattr(summarizer.Summarizer,'_call_llm',forbidden)
    rows = await summarizer.summarize_news([item('Actual observed news title')])
    assert rows[0].summary == 'Title excerpt (not AI summary): Actual observed news title'


@pytest.mark.asyncio
async def test_failed_ai_summary_keeps_news_and_stops_repeated_unauthorized_requests(monkeypatch):
    requests = []
    def handler(request):
        requests.append(request)
        return httpx.Response(401,json={'error':'Unauthorized'})
    original = httpx.AsyncClient
    monkeypatch.setattr(summarizer.httpx,'AsyncClient',lambda **kw:original(transport=httpx.MockTransport(handler),**kw))
    rows = [item('Observed news '+str(i)) for i in range(12)]
    result = await summarizer.summarize_news(rows,config={'api_key':'fixture','model':'fixture','base_url':'https://example.org/v1'})
    assert len(requests) == 1
    assert len(result) == 12
    assert all(r.summary.startswith('Title excerpt (not AI summary): ') for r in result)
    assert all(r.dedup_key != 'error' for r in result)
