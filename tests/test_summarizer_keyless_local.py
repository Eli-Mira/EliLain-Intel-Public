"""The AI summarizer must work against a keyless local model.

An explicitly enabled local OpenAI-compatible endpoint may work without an API key.

The old code refused to even try without a key:

    if not summarizer.api_key:
        return summarizer.summarize_manually(items)

which silently downgraded keyless deployments to manual title excerpts.

The fix: inside an EXPLICITLY ENABLED setup, an absent key is only a blocker for a
REMOTE endpoint. A loopback endpoint needs no credential and must be allowed to run.

Opt-in is preserved. An empty config still makes zero requests - a reachable
default endpoint must never switch summarization on by itself. Verified against
`tests/test_release_summarizer_cli_wiring.py`, whose "default CLI makes no
requests" guarantee is load-bearing.
"""
import json
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from news_tracker.summarizer import Summarizer, summarize_news
from tracker_core.schema import NewsItem

LOOPBACK = ("http://localhost:3001/v1/chat/completions",
            "http://127.0.0.1:3001/v1/chat/completions",
            "http://localhost:11434/v1/chat/completions")


def _item(title="Some headline"):
    return NewsItem(title=title, url="https://example.invalid/a", source="news")


def _ok_payload(text):
    return httpx.Response(200, json={"choices": [{"finish_reason": "stop",
                                                  "message": {"content": text}}]})


@pytest.mark.parametrize("url", LOOPBACK)
def test_keyless_loopback_endpoint_is_allowed_to_run(url):
    """The core fix: no key is fine when the endpoint is on this machine."""
    assert Summarizer({"base_url": url, "model": "local"}).api_key is None


def test_remote_endpoint_without_a_key_is_still_refused():
    """Safety unchanged: a REMOTE endpoint with no key must not be called.

    Silently posting a prompt to an arbitrary remote host with no credential
    would be a surprise egress path, so this stays closed.
    """
    summarizer = Summarizer({"base_url": "https://api.openai.com/v1/chat/completions"})
    assert not summarizer.keyless_endpoint


def test_default_endpoint_is_loopback_so_the_default_needs_no_key():
    assert Summarizer({}).keyless_endpoint, (
        "the default endpoint must be recognised as local, otherwise a keyless "
        "local deployment would be refused")


@pytest.mark.anyio
async def test_empty_config_makes_zero_requests_even_with_a_live_default(monkeypatch):
    """The guarantee my first fix broke: not-enabled means NOT called.

    Reaching the default endpoint must not imply permission to call it.
    """
    def explode(*args, **kwargs):
        raise AssertionError("summarization was called without explicit opt-in")

    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", explode)
    out = await summarize_news([_item("Headline")], {})
    assert out[0].summary.startswith("Title excerpt (not AI summary):")


@pytest.mark.anyio
async def test_explicit_local_opt_in_with_no_key_still_calls_the_model(monkeypatch):
    """The free path: an explicit config naming a loopback endpoint, no key."""
    async def handler(request):
        return _ok_payload(json.dumps([{"summary": "Real local summary.",
                                        "dedup_key": "real-local"}]))

    original = httpx.AsyncClient
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(httpx, "AsyncClient",
                        lambda *a, **k: original(*a, **{**k, "transport": transport}))
    out = await summarize_news([_item("Headline")],
                               {"base_url": LOOPBACK[2], "model": "local"})
    assert out[0].summary == "Real local summary."


@pytest.mark.anyio
async def test_real_summaries_are_produced_without_any_api_key(monkeypatch):
    items = [_item("OpenAI ships a new model"), _item("Rust adds a feature")]

    async def handler(request):
        assert "Authorization" not in request.headers, (
            "no credential should be sent to a keyless local endpoint")
        return _ok_payload(json.dumps([
            {"summary": "OpenAI shipped a new multimodal model.", "dedup_key": "openai new model"},
            {"summary": "Rust added a new language feature.", "dedup_key": "rust language feature"},
        ]))

    transport = httpx.MockTransport(handler)
    original = httpx.AsyncClient

    def patched(*args, **kwargs):
        kwargs["transport"] = transport
        return original(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", patched)
    out = await summarize_news(items, {"base_url": LOOPBACK[2], "model": "local"})
    assert [i.summary for i in out] == ["OpenAI shipped a new multimodal model.",
                                         "Rust added a new language feature."]
    assert not any("not AI summary" in i.summary for i in out), \
        "it silently degraded to title excerpts instead of calling the local model"


@pytest.mark.anyio
async def test_a_failing_local_model_still_degrades_instead_of_raising(monkeypatch):
    """Free must not mean fragile: a dead local model falls back, not crashes."""
    async def handler(request):
        return httpx.Response(500, json={"error": "model not loaded"})

    transport = httpx.MockTransport(handler)
    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient",
                        lambda *a, **k: original(*a, **{**k, "transport": transport}))
    out = await summarize_news([_item("Headline")], {"base_url": LOOPBACK[2], "model": "local"})
    assert out[0].summary.startswith("Title excerpt (not AI summary):")


@pytest.mark.anyio
async def test_malformed_model_output_never_reaches_the_report(monkeypatch):
    """Prose instead of JSON must not overwrite an observed summary."""
    items = [_item("Headline")]
    original = httpx.AsyncClient

    async def handler(request):
        return _ok_payload("Sure! Here are your summaries, hope this helps!")

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(httpx, "AsyncClient",
                        lambda *a, **k: original(*a, **{**k, "transport": transport}))
    out = await summarize_news(items, {"base_url": LOOPBACK[2], "model": "local"})
    assert "hope this helps" not in out[0].summary
    assert out[0].summary.startswith("Title excerpt (not AI summary):")

def test_reasoning_local_model_gets_a_larger_completion_budget():
    """Verified live: minicpm5-2b-q8 burns the whole 1000-token budget on
    internal reasoning and returns EMPTY content with finish_reason 'length'
    for a 167-token prompt. That looks like a malformed response but is a
    starved budget."""
    from news_tracker import summarizer as mod
    s = Summarizer({"base_url": LOOPBACK[2], "model": "minicpm5-2b-q8:latest"})
    assert s.model_reasoning, "a reasoning model was not detected"
    assert s.max_tokens == mod.MAX_COMPLETION_TOKENS
    assert mod.MAX_COMPLETION_TOKENS > 1000


def test_ordinary_model_keeps_the_default_budget_and_temperature():
    s = Summarizer({"base_url": LOOPBACK[2], "model": "gpt-4o-mini"})
    assert not s.model_reasoning
    assert s.max_tokens == 1000


@pytest.mark.anyio
async def test_reasoning_model_request_omits_temperature_and_raises_budget(monkeypatch):
    """Some reasoning backends reject an explicit low temperature."""
    seen = {}

    async def handler(request):
        seen.update(json.loads(request.content))
        return _ok_payload(json.dumps([{"summary": "s", "dedup_key": "k"}]))

    original = httpx.AsyncClient
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(httpx, "AsyncClient",
                        lambda *a, **k: original(*a, **{**k, "transport": transport}))
    await summarize_news([_item("Headline")],
                         {"base_url": LOOPBACK[2], "model": "minicpm5-2b-q8:latest"})
    assert seen["max_tokens"] > 1000
    assert "temperature" not in seen

    await summarize_news([_item("Headline")],
                         {"base_url": LOOPBACK[2], "model": "gpt-4o-mini"})
    assert seen["max_tokens"] == 1000
    assert seen["temperature"] == 0.3


@pytest.mark.anyio
async def test_truncated_empty_completion_degrades_rather_than_reporting_nothing(monkeypatch):
    """finish_reason 'length' with empty content must never reach the report."""
    async def handler(request):
        return httpx.Response(200, json={"choices": [{"finish_reason": "length",
                                                      "message": {"content": ""}}]})

    original = httpx.AsyncClient
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(httpx, "AsyncClient",
                        lambda *a, **k: original(*a, **{**k, "transport": transport}))
    out = await summarize_news([_item("Headline")],
                               {"base_url": LOOPBACK[2], "model": "minicpm5-2b-q8:latest"})
    assert out[0].summary.startswith("Title excerpt (not AI summary):")
