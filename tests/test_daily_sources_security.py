"""Tests for daily_sources transport security: RED->GREEN cycle."""
import asyncio
import socket
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from src.intel_sources.daily_sources import (
    _safe_get,
    verify_candidates,
    _is_safe_host,
    _is_private_ip,
)


class FakeAsyncStream(httpx.AsyncByteStream):
    """Synthetic stream for testing async consumption."""

    def __init__(self):
        self.closed = False

    async def __aiter__(self):
        yield b"synthetic public response"

    async def aclose(self):
        self.closed = True


@pytest.mark.asyncio
async def test_safe_get_rejects_localhost():
    """localhost must be rejected at host safety check."""
    async with httpx.AsyncClient(follow_redirects=False) as client:
        with pytest.raises(ValueError, match="Private/reserved host rejected"):
            await _safe_get(client, "https://localhost/test", timeout=0.1)


@pytest.mark.asyncio
async def test_safe_get_rejects_private_ip():
    """Private IPs must be rejected."""
    async with httpx.AsyncClient(follow_redirects=False) as client:
        with pytest.raises(ValueError, match="Private/reserved host rejected"):
            await _safe_get(client, "https://127.0.0.1/test", timeout=0.1)


@pytest.mark.asyncio
async def test_safe_get_rejects_url_credentials():
    """URLs with credentials must be rejected."""
    async with httpx.AsyncClient(follow_redirects=False) as client:
        with pytest.raises(ValueError, match="credentials rejected"):
            await _safe_get(client, "https://user:pass@example.com/test", timeout=0.1)


@pytest.mark.asyncio
async def test_safe_get_rejects_unsafe_scheme():
    """Unsafe schemes must be rejected."""
    async with httpx.AsyncClient(follow_redirects=False) as client:
        with pytest.raises(ValueError, match="Unsafe scheme"):
            await _safe_get(client, "ftp://example.com/test", timeout=0.1)


@pytest.mark.asyncio
async def test_safe_get_async_stream_consumption():
    """Body must be consumed asynchronously and stream closed."""
    # This test verifies the async stream consumption pattern
    # by testing with a mock transport that provides a synthetic stream
    from src.intel_sources.public_http import GuardedAsyncTransport

    public_dns = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]
    stream = FakeAsyncStream()

    async def handler(request):
        return httpx.Response(200, stream=stream)

    async def mock_dns(*args, **kwargs):
        return public_dns

    guarded = GuardedAsyncTransport(mock_dns, httpx.MockTransport(handler))
    async with httpx.AsyncClient(transport=guarded, follow_redirects=False) as client:
        resp, _ = await _safe_get(client, "https://example.com/path", timeout=0.1)
        content = resp.content
        assert content == b"synthetic public response"
        assert stream.closed


@pytest.mark.asyncio
async def test_verify_candidates_rejects_private_redirect():
    """HEAD verification must not follow redirects to private addresses."""
    seen = []

    async def inner_handler(request):
        seen.append({"method": request.method, "host": request.url.host, "headers": dict(request.headers)})
        # First HEAD to example.com returns 302 to private
        if len(seen) == 1:
            return httpx.Response(302, headers={"location": "http://127.0.0.1/private"})
        return httpx.Response(200)

    public_dns = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]

    async def mock_resolver(host, port):
        return public_dns

    from src.intel_sources import public_http as ph_module
    from src.intel_sources import daily_sources as ds_module
    # Patch both modules' resolver references
    with patch.object(ds_module, "_default_resolver", mock_resolver):
        with patch.object(ph_module, "_default_resolver", mock_resolver):
            original_init = ph_module.GuardedAsyncTransport.__init__
            def guarded_init(self, resolver, inner=None, *args, **kwargs):
                original_init(self, resolver, inner=httpx.MockTransport(inner_handler), *args, **kwargs)
            with patch.object(ph_module.GuardedAsyncTransport, "__init__", guarded_init):
                result = await verify_candidates(
                    [{"id": "synthetic", "source_url": "https://example.com/head-probe"}],
                    {},
                    now=datetime(2026, 10, 8, 6, tzinfo=timezone.utc),
                )

    # Should only see the original request, not the redirect target
    assert len(seen) == 1
    # Host on wire is pinned IP, Host header is original
    assert seen[0]["host"] == "93.184.216.34"
    assert seen[0]["headers"]["host"] == "example.com"
    assert result[0]["link_verification"]["status"] == "unverified"


@pytest.mark.asyncio
async def test_verify_candidates_skips_http_urls():
    """HTTP URLs should not be verified (only HTTPS)."""
    candidates = [
        {
            "id": "test:1",
            "name": "Test",
            "what": "A test candidate",
            "category": "models",
            "source_id": "test",
            "source_url": "http://example.com/test",
            "published_at": None,
            "date_kind": "observed",
            "observed_at": datetime.now(timezone.utc).isoformat(),
            "free": {"status": "unknown", "detail": "", "evidence_url": ""},
            "api": {"status": "unknown", "detail": "", "evidence_url": ""},
            "topics": [],
        }
    ]
    result = await verify_candidates(candidates, {})
    assert result[0]["link_verification"]["status"] == "unverified"


@pytest.mark.asyncio
async def test_is_safe_host_rejects_localhost_subdomains():
    """localhost and subdomains must be rejected."""
    assert _is_safe_host("localhost") is False
    assert _is_safe_host("sub.localhost") is False
    assert _is_safe_host("test.localhost.localdomain") is False
    assert _is_safe_host("local") is False
    assert _is_safe_host("localdomain") is False


@pytest.mark.asyncio
async def test_is_safe_host_rejects_mapped_ipv6():
    """Mapped IPv6 addresses must be rejected."""
    assert _is_safe_host("::ffff:127.0.0.1") is False
    assert _is_safe_host("::1") is False


@pytest.mark.asyncio
async def test_is_safe_host_accepts_public_hosts():
    """Public hosts must be accepted."""
    assert _is_safe_host("example.com") is True
    assert _is_safe_host("8.8.8.8") is True
    assert _is_safe_host("openrouter.ai") is True
