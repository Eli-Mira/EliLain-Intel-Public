"""Optional private providers exercised through the real CLI/news/store path.

All HTTP is MockTransport; no paid inference, feeds, router probes or notifications.
"""
import json
from datetime import datetime, timezone

import httpx
import pytest
import main
from tracker_core.schema import NewsItem, SourceType


KEY = "SYNTHETIC_PRIVATE_SUMMARY_KEY"


def prepare(tmp_path, monkeypatch, provider="anthropic", endpoint="https://api.anthropic.com/v1/messages", key=KEY):
    monkeypatch.setenv("NOTIFICATION_CHANNEL", "none")
    monkeypatch.setenv("SUMMARIZER_PROVIDER", provider)
    monkeypatch.setenv("SUMMARIZER_MODEL", "fixture-model")
    monkeypatch.setenv("SUMMARIZER_API_URL", endpoint)
    if key is None:
        monkeypatch.delenv("SUMMARIZER_API_KEY", raising=False)
    else:
        monkeypatch.setenv("SUMMARIZER_API_KEY", key)
    calls, trackers = [], []
    original_tracker, original_client = main.NewsTracker, httpx.AsyncClient

    class FixtureTracker(original_tracker):
        def __init__(self, **kwargs):
            super().__init__(**kwargs, cache_dir=tmp_path / "cache")
            trackers.append(self)

        async def fetch_all(self):
            return [NewsItem(source=SourceType.GITHUB, title="Open source Python AI agents release",
                             url="https://example.org/fixture", fetched_date=datetime.now(timezone.utc))]

    def response(request):
        calls.append(request)
        body = json.loads(request.content)
        assert body["model"] == "fixture-model"
        assert body["messages"][0]["role"] == "user"
        text = json.dumps([{"summary": "Validated private provider summary", "dedup_key": "fixture"}])
        if provider == "anthropic":
            assert request.headers["x-api-key"] == KEY
            assert request.headers["anthropic-version"] == "2023-06-01"
            assert "authorization" not in request.headers
            assert body["max_tokens"] == 1000
            return httpx.Response(200, json={"type": "message", "role": "assistant", "stop_reason": "end_turn",
                                            "content": [{"type": "text", "text": text}]})
        if key is None:
            assert "authorization" not in request.headers
        else:
            assert request.headers["authorization"] == "Bearer " + key
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content": text}}]})

    monkeypatch.setattr(main, "NewsTracker", FixtureTracker)
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: original_client(**kw, transport=httpx.MockTransport(response)))
    args = ["run", "--data-dir", str(tmp_path / "data"), "--reports-dir", str(tmp_path / "reports")]
    return args, calls, trackers


def test_native_anthropic_cli_persists_summary(tmp_path, monkeypatch):
    args, calls, trackers = prepare(tmp_path, monkeypatch)
    main.main([*args, "--enable-ai-summary"])
    assert len(calls) == 1
    assert str(calls[0].url) == "https://api.anthropic.com/v1/messages"
    assert trackers[0].sqlite.get_items()[0]["summary"] == "Validated private provider summary"
    assert KEY not in str(trackers[0].sqlite.get_items())


def test_keyless_local_cli_uses_existing_compatible_path(tmp_path, monkeypatch):
    args, calls, trackers = prepare(tmp_path, monkeypatch, "openai-compatible",
                                   "http://127.0.0.1:11434/v1/chat/completions", None)
    main.main([*args, "--enable-ai-summary"])
    assert len(calls) == 1
    assert trackers[0].sqlite.get_items()[0]["summary"] == "Validated private provider summary"


@pytest.mark.parametrize("setting,value", [
    ("SUMMARIZER_PROVIDER", "claude"),
    ("SUMMARIZER_PROVIDER", "Anthropic"),
    ("SUMMARIZER_PROVIDER", " anthropic"),
    ("SUMMARIZER_API_KEY", " key "),
    ("SUMMARIZER_API_KEY", "key\nInjected: value"),
    ("SUMMARIZER_API_KEY", "key\x7f"),
    ("SUMMARIZER_MODEL", " fixture-model"),
    ("SUMMARIZER_API_URL", "http://0.0.0.0:3001/v1/messages"),
    ("SUMMARIZER_API_URL", "https://127.1/v1/messages"),
    ("SUMMARIZER_API_URL", "https://2130706433/v1/messages"),
    ("SUMMARIZER_API_URL", "https://api.anthropic.com:/v1/messages"),
    ("SUMMARIZER_API_URL", "https://api.anthropic.com/v1/messages?"),
    ("SUMMARIZER_API_URL", "https://api.anthropic.com/v1/messages#"),
    ("SUMMARIZER_API_URL", "https://api.anthropic.com\\evil/v1/messages"),
    ("SUMMARIZER_API_URL", "https://api.anthropic.com/%0a"),
])
def test_cli_rejects_invalid_literals_before_state(tmp_path, monkeypatch, capsys, setting, value):
    args, calls, trackers = prepare(tmp_path, monkeypatch)
    monkeypatch.setenv(setting, value)
    with pytest.raises(SystemExit) as error:
        main.main([*args, "--enable-ai-summary"])
    assert error.value.code == 2
    assert not calls and not trackers
    assert not (tmp_path / "data").exists()
    assert KEY not in capsys.readouterr().err


def test_native_anthropic_always_requires_key_even_loopback(tmp_path, monkeypatch):
    args, calls, trackers = prepare(tmp_path, monkeypatch, endpoint="http://localhost:3001/v1/messages", key=None)
    with pytest.raises(SystemExit):
        main.main([*args, "--enable-ai-summary"])
    assert not calls and not trackers


def test_default_cli_ignores_invalid_provider_and_makes_zero_requests(tmp_path, monkeypatch):
    args, calls, trackers = prepare(tmp_path, monkeypatch, provider="not-a-provider")
    main.main(args)
    assert calls == []
    assert trackers[0].sqlite.get_items()[0]["summary"].startswith("Title excerpt (not AI summary):")


def test_native_openai_uses_completion_budget_without_legacy_parameters(tmp_path, monkeypatch):
    args, calls, trackers = prepare(tmp_path, monkeypatch, "openai", "https://api.openai.com/v1/chat/completions")
    main.main([*args, "--enable-ai-summary"])
    assert len(calls) == 1
    payload = json.loads(calls[0].content)
    assert payload["max_completion_tokens"] == 1000
    assert "max_tokens" not in payload and "temperature" not in payload
    assert trackers[0].sqlite.get_items()[0]["summary"] == "Validated private provider summary"


@pytest.mark.asyncio
@pytest.mark.parametrize("config", [
    {"provider": "anthropic", "base_url": "http://127.0.0.1/v1/messages"},
    {"provider": "openai-compatible", "base_url": "http://0.0.0.0/v1/chat/completions"},
    {"provider": "anthropic", "api_key": "key\nInjected: value"},
    {"provider": "unknown", "api_key": KEY},
    {"base_url": "https://example.org/v1?", "api_key": KEY},
])
async def test_programmatic_invalid_config_makes_zero_http_requests(monkeypatch, config):
    from news_tracker.summarizer import summarize_news
    def forbidden(**kwargs):
        pytest.fail("invalid configuration created an HTTP client")
    monkeypatch.setattr(httpx, "AsyncClient", forbidden)
    row = NewsItem(source=SourceType.GITHUB, title="Observed headline", url="https://example.org/story")
    result = await summarize_news([row], config)
    assert result[0].summary == "Title excerpt (not AI summary): Observed headline"


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["anthropic", "openai-compatible"])
@pytest.mark.parametrize("failure", ["oversized_body", "oversized_summary", "oversized_key", "deep_json", "duplicate_field", "credential_echo", "escaped_credential_echo", "endpoint_echo", "redirect", "truncated", "nontext", "http_401", "http_429", "timeout"])
async def test_untrusted_provider_response_has_bounded_honest_fallback(monkeypatch, caplog, provider, failure):
    from news_tracker.summarizer import summarize_news
    endpoint = "https://private-fixture.example/v1/messages"
    text = json.dumps([{"summary": "safe", "dedup_key": "fixture"}])
    if failure == "oversized_summary":
        text = json.dumps([{"summary": "x" * 2001, "dedup_key": "fixture"}])
    elif failure == "oversized_key":
        text = json.dumps([{"summary": "safe", "dedup_key": "x" * 129}])
    elif failure == "deep_json":
        text = "[" * 1100 + "0" + "]" * 1100
    elif failure == "duplicate_field":
        text = '[{"summary":"safe","summary":"untrusted","dedup_key":"fixture"}]'
    elif failure == "credential_echo":
        text = json.dumps([{"summary": KEY, "dedup_key": "fixture"}])
    elif failure == "escaped_credential_echo":
        text = json.dumps([{"summary": KEY, "dedup_key": "fixture"}]).replace("S", "\\u0053")
    elif failure == "endpoint_echo":
        text = json.dumps([{"summary": endpoint, "dedup_key": "fixture"}])
    calls = []
    def handler(request):
        calls.append(request)
        if failure == "timeout":
            raise httpx.ReadTimeout(KEY + endpoint, request=request)
        if failure in {"http_401", "http_429"}:
            return httpx.Response(int(failure[5:]), json={"error": KEY + endpoint})
        if failure == "redirect":
            return httpx.Response(307, headers={"location": "https://other.example/inference"})
        if failure == "oversized_body":
            return httpx.Response(200, content=b" " * 65537)
        if provider == "anthropic":
            return httpx.Response(200, json={"stop_reason": "max_tokens" if failure == "truncated" else "end_turn",
                "content": [{"type": "tool_use" if failure == "nontext" else "text", "text": text}]})
        return httpx.Response(200, json={"choices": [{"finish_reason": "length" if failure == "truncated" else "stop",
            "message": {"content": [] if failure == "nontext" else text}}]})
    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: original(**kw, transport=httpx.MockTransport(handler)))
    rows = [NewsItem(source=SourceType.GITHUB, title="Observed headline", url="https://example.org/story")]
    await summarize_news(rows, {"provider": provider, "api_key": KEY, "model": "fixture", "base_url": endpoint})
    assert len(calls) == 1
    assert rows[0].summary == "Title excerpt (not AI summary): Observed headline"
    assert rows[0].dedup_key is None
    assert KEY not in caplog.text and endpoint not in caplog.text


@pytest.mark.asyncio
async def test_response_stream_stops_reading_over_limit(monkeypatch):
    from news_tracker.summarizer import summarize_news
    class BoundedStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b" " * 65537
            pytest.fail("read continued after bounded response limit")
    original = httpx.AsyncClient
    transport = httpx.MockTransport(lambda request: httpx.Response(200, stream=BoundedStream()))
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: original(**kw, transport=transport))
    rows = [NewsItem(source=SourceType.GITHUB, title="Observed headline", url="https://example.org/story")]
    await summarize_news(rows, {"model": "fixture"})
    assert rows[0].summary.startswith("Title excerpt (not AI summary):")


def test_provider_request_logs_do_not_expose_private_endpoint(tmp_path, monkeypatch, caplog):
    import logging
    args, calls, trackers = prepare(tmp_path, monkeypatch)
    caplog.set_level(logging.DEBUG)
    main.main([*args, "--enable-ai-summary"])
    assert len(calls) == 1
    assert "api.anthropic.com" not in caplog.text and KEY not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("budget", [0, -1, 4097, True, "1000"])
async def test_invalid_token_budget_makes_zero_requests(monkeypatch, budget):
    from news_tracker.summarizer import summarize_news
    def forbidden(**kwargs):
        pytest.fail("invalid budget created an HTTP client")
    monkeypatch.setattr(httpx, "AsyncClient", forbidden)
    rows = [NewsItem(source=SourceType.GITHUB, title="Observed headline", url="https://example.org/story")]
    await summarize_news(rows, {"max_tokens": budget})
    assert rows[0].summary.startswith("Title excerpt (not AI summary):")


def test_prompt_bounds_untrusted_headline():
    from news_tracker.summarizer import Summarizer
    row = NewsItem(source=SourceType.GITHUB, title="x" * 50000, url="https://example.org/story")
    assert len(Summarizer()._build_prompt([row])) < 4096


@pytest.mark.asyncio
async def test_oversized_batch_rejected_before_request(monkeypatch):
    from news_tracker.summarizer import Summarizer
    async def forbidden(self, prompt):
        pytest.fail("oversized batch called inference")
    monkeypatch.setattr(Summarizer, "_call_llm", forbidden)
    rows = [NewsItem(source=SourceType.GITHUB, title="Observed headline", url="https://example.org/story")]
    with pytest.raises(ValueError):
        await Summarizer().summarize_items(rows, batch_size=100)


@pytest.mark.parametrize("url", ["HTTPS://api.example.org/v1", "https://EXAMPLE.org/v1", "https://example.org:0443/v1", "https://example.org/a{b}", "https://[::1]junk/v1"])
def test_endpoint_literals_are_not_repaired(tmp_path, monkeypatch, url):
    args, calls, trackers = prepare(tmp_path, monkeypatch, endpoint=url)
    with pytest.raises(SystemExit):
        main.main([*args, "--enable-ai-summary"])
    assert not calls and not trackers
