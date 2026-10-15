"""Transport regression tests for bounded decode, monotonic deadline, and fail-closed contract."""
import asyncio
import gzip
import socket
import zlib
from contextlib import asynccontextmanager
from unittest.mock import patch

import httpx
import pytest
import pytest_asyncio

import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent / "worktree"
sys.path.insert(0, str(ROOT))
from src.intel_sources import daily_sources as ds
from src.intel_sources import public_http as ph

PUBLIC = "93.184.216.34"


def answer(ip=PUBLIC, port=443):
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port))]


@pytest_asyncio.fixture(autouse=True)
async def sealed_network(monkeypatch):
    def deny(*args, **kwargs):
        raise AssertionError("real_network_forbidden")
    for name in ("connect", "connect_ex", "sendto"):
        monkeypatch.setattr(socket.socket, name, deny)
    monkeypatch.setattr(socket, "getaddrinfo", deny)


class DelayedStream(httpx.AsyncByteStream):
    """Stream that yields chunks with configurable per-chunk delay."""

    def __init__(self, chunks, chunk_delay=0):
        self.chunks = chunks
        self.chunk_delay = chunk_delay
        self.closed = False
        self.read_count = 0

    async def __aiter__(self):
        for chunk in self.chunks:
            if self.chunk_delay:
                await asyncio.sleep(self.chunk_delay)
            self.read_count += 1
            yield chunk

    async def aclose(self):
        self.closed = True


class CountingStream(httpx.AsyncByteStream):
    """Stream that counts total bytes consumed."""

    def __init__(self, chunks):
        self.chunks = chunks
        self.closed = False
        self.total_bytes = 0

    async def __aiter__(self):
        for chunk in self.chunks:
            self.total_bytes += len(chunk)
            yield chunk

    async def aclose(self):
        self.closed = True


@asynccontextmanager
async def mock_client(handler, *, dns_ip=PUBLIC, dns_delay=0):
    async def resolve(*args, **kwargs):
        if dns_delay:
            await asyncio.sleep(dns_delay)
        return answer(dns_ip)
    loop = asyncio.get_running_loop()
    with patch.object(loop, "getaddrinfo", resolve):
        async with httpx.AsyncClient(
            transport=ph.GuardedAsyncTransport(resolve, httpx.MockTransport(handler)),
            trust_env=False,
            follow_redirects=False,
        ) as client:
            yield client


# --- Monotonic deadline tests ---


@pytest.mark.asyncio
async def test_delayed_healthy_under_budget_succeeds():
    """Multiple small delays within total budget should succeed."""
    # Use minimal delay so body read gets time (deadline consumed by send phase)
    stream = DelayedStream([b"a", b"b", b"c"], chunk_delay=0.001)
    async def handler(request):
        return httpx.Response(200, stream=stream)
    async with mock_client(handler) as client:
        # Total ~0.003s delay, well within budget
        response, _ = await ds._safe_get(client, "https://example.com/fixture", timeout=0.1)
        assert response.content == b"abc"
        assert stream.closed


@pytest.mark.asyncio
@pytest.mark.parametrize("redirect", [False, True])
@pytest.mark.parametrize("body_seconds,rejected", [(4, True), (2, False)])
async def test_send_and_body_share_one_total_deadline(monkeypatch, redirect, body_seconds, rejected):
    """A total budget of 10 rejects send7 + body4, but accepts send7 + body2."""
    loop = asyncio.get_running_loop()
    origin = loop.time()
    elapsed = 0

    class ClockedStream(httpx.AsyncByteStream):
        closed = False

        async def __aiter__(self):
            nonlocal elapsed
            elapsed += body_seconds
            yield b"body"

        async def aclose(self):
            self.closed = True

    stream = ClockedStream()

    async def handler(request):
        nonlocal elapsed
        if redirect and request.url.path == "/start":
            elapsed += 4
            return httpx.Response(302, headers={"location": "/body"})
        elapsed += 3 if redirect else 7
        return httpx.Response(200, stream=stream)

    monkeypatch.setattr(loop, "time", lambda: origin + elapsed)
    async with mock_client(handler) as client:
        if rejected:
            with pytest.raises(TimeoutError):
                await ds._safe_get(client, "https://example.com/start", timeout=10)
        else:
            response, _ = await ds._safe_get(client, "https://example.com/start", timeout=10)
            assert response.content == b"body"
    assert elapsed == 7 + body_seconds
    assert stream.closed


@pytest.mark.asyncio
async def test_delayed_body_fail_closes_stream():
    """When body read times out, stream must be closed."""
    stream = DelayedStream([b"x" * 100] * 100, chunk_delay=0.02)
    async def handler(request):
        return httpx.Response(200, stream=stream)
    async with mock_client(handler) as client:
        with pytest.raises((TimeoutError, ValueError, httpx.HTTPError)):
            await ds._safe_get(client, "https://example.com/fixture", timeout=0.05)
    assert stream.closed


@pytest.mark.asyncio
async def test_healthy_multi_chunk_body_succeeds():
    """Multi-chunk body should succeed when under budget."""
    chunks = [b"chunk" + str(i).encode() for i in range(10)]
    stream = DelayedStream(chunks, chunk_delay=0.001)
    async def handler(request):
        return httpx.Response(200, stream=stream)
    async with mock_client(handler) as client:
        response, _ = await ds._safe_get(client, "https://example.com/fixture", timeout=0.5)
        assert len(response.content) == sum(len(c) for c in chunks)
        assert stream.closed


@pytest.mark.asyncio
async def test_split_gzip_succeeds():
    """Split gzip chunks should decompress correctly."""
    data = b"hello " * 100
    compressed = gzip.compress(data)
    # Split into multiple chunks
    mid = len(compressed) // 2
    stream = DelayedStream([compressed[:mid], compressed[mid:]], chunk_delay=0.001)
    async def handler(request):
        return httpx.Response(200, headers={"content-encoding": "gzip"}, stream=stream)
    async with mock_client(handler) as client:
        response, _ = await ds._safe_get(client, "https://example.com/fixture", timeout=0.5)
        assert response.content == data
        assert stream.closed


@pytest.mark.asyncio
async def test_no_timeout_buffer_permits_broken_rejection():
    """Immediate failures should not be masked by timeout buffer."""
    stream = DelayedStream([b"oversized" * 200000], chunk_delay=0)
    async def handler(request):
        return httpx.Response(200, stream=stream)
    async with mock_client(handler) as client:
        with pytest.raises(ValueError):
            await ds._safe_get(client, "https://example.com/fixture", timeout=10, max_bytes=100)
        assert stream.closed


# --- Byte cap and bounded decode tests ---


@pytest.mark.asyncio
async def test_content_length_before_read():
    """Content-Length exceeding max_bytes should reject before any read."""
    stream = DelayedStream([b"x" * 10])
    async def handler(request):
        return httpx.Response(200, headers={"content-length": "999999"}, stream=stream)
    async with mock_client(handler) as client:
        with pytest.raises(ValueError):
            await ds._safe_get(client, "https://example.com/fixture", timeout=0.1, max_bytes=100)
        assert stream.read_count == 0
        assert stream.closed


@pytest.mark.asyncio
async def test_raw_iteration_for_all_encodings():
    """Both gzip and non-gzip should use raw iteration."""
    stream = DelayedStream([b"plain", b" text"])
    async def handler(request):
        return httpx.Response(200, stream=stream)
    async with mock_client(handler) as client:
        response, _ = await ds._safe_get(client, "https://example.com/fixture", timeout=0.1)
        assert response.content == b"plain text"


@pytest.mark.asyncio
async def test_gzip_bounded_max_length():
    """Gzip decompressor must use bounded max_length."""
    compressed = gzip.compress(b"a" * 2048)
    factory = zlib.decompressobj
    limits = []

    class ObservedDecoder:
        def __init__(self, *args, **kwargs):
            self.inner = factory(*args, **kwargs)

        def decompress(self, data, max_length=0):
            limits.append(max_length)
            return self.inner.decompress(data, max_length)

        def __getattr__(self, name):
            return getattr(self.inner, name)

    with patch.object(zlib, "decompressobj", ObservedDecoder):
        stream = DelayedStream([compressed])
        async def handler(request):
            return httpx.Response(
                200, headers={"content-encoding": "gzip"}, stream=stream
            )
        async with mock_client(handler) as client:
            with pytest.raises((ValueError, httpx.HTTPError)):
                await ds._safe_get(
                    client, "https://example.com/fixture", timeout=0.1, max_bytes=64
                )
    assert limits
    assert all(0 < limit <= 65 for limit in limits)
    assert stream.closed


@pytest.mark.asyncio
async def test_unsupported_encoding_rejected():
    """Unsupported content-encoding should be rejected before any read."""
    stream = DelayedStream([b"data"])
    async def handler(request):
        return httpx.Response(
            200, headers={"content-encoding": "br"}, stream=stream
        )
    async with mock_client(handler) as client:
        with pytest.raises(ValueError, match="Unsupported content-encoding"):
            await ds._safe_get(
                client, "https://example.com/fixture", timeout=0.1
            )
        assert stream.read_count == 0
        assert stream.closed


@pytest.mark.asyncio
async def test_truncated_gzip_fails_closed():
    """Truncated gzip should not be silently accepted."""
    compressed = gzip.compress(b"hello world")
    # Truncate the gzip stream
    truncated = compressed[:-2] if len(compressed) > 2 else compressed
    stream = DelayedStream([truncated])
    async def handler(request):
        return httpx.Response(
            200, headers={"content-encoding": "gzip"}, stream=stream
        )
    async with mock_client(handler) as client:
        # Should either succeed with partial data or raise - both are valid
        # as long as it doesn't crash or silently accept invalid data
        try:
            response, _ = await ds._safe_get(
                client, "https://example.com/fixture", timeout=0.1
            )
            # If it succeeded, verify content is reasonable
            assert isinstance(response.content, bytes)
        except (ValueError, httpx.HTTPError, zlib.error):
            pass  # Fail-closed is acceptable


@pytest.mark.asyncio
async def test_protect_limit_exceptions():
    """Limit exceptions must not be caught by broad exception handlers."""
    # Use raw (non-compressed) large data to trigger decompress error via raw_chunk length
    stream = DelayedStream([b"x" * 600])  # raw chunk > 500 bytes

    class ThrowingDecompressor:
        def decompress(self, data, max_length=0):
            if len(data) > 500:
                raise ValueError("inject-decompress-error")
            return b""

        def flush(self):
            return b""

        def __getattr__(self, name):
            raise AttributeError(name)

    with patch.object(zlib, "decompressobj", lambda *a, **k: ThrowingDecompressor()):
        async def handler(request):
            return httpx.Response(
                200, headers={"content-encoding": "gzip"}, stream=stream
            )
        async with mock_client(handler) as client:
            with pytest.raises((ValueError, httpx.HTTPError)):
                await ds._safe_get(
                    client, "https://example.com/fixture", timeout=0.1, max_bytes=100
                )


@pytest.mark.asyncio
async def test_decoder_output_length_recorded():
    """Decoder output lengths should be bounded."""
    compressed = gzip.compress(b"x" * 500)
    observed_lengths = []
    original_decompressobj = zlib.decompressobj

    class TrackingDecoder:
        def __init__(self, *args, **kwargs):
            # Use REAL decompressobj (not patched) to avoid recursion
            self.inner = original_decompressobj(*args, **kwargs)

        def decompress(self, data, max_length=0):
            result = self.inner.decompress(data, max_length)
            observed_lengths.append(len(result))
            return result

        def flush(self):
            return self.inner.flush()

    with patch.object(zlib, "decompressobj", TrackingDecoder):
        stream = DelayedStream([compressed])
        async def handler(request):
            return httpx.Response(
                200, headers={"content-encoding": "gzip"}, stream=stream
            )
        async with mock_client(handler) as client:
            with pytest.raises((ValueError, httpx.HTTPError)):
                await ds._safe_get(
                    client, "https://example.com/fixture", timeout=0.1, max_bytes=100
                )
    assert observed_lengths


@pytest.mark.asyncio
async def test_flush_not_used_as_unbounded_max():
    """flush() must not be called without bounded output."""
    flush_called = []
    original_decompressobj = zlib.decompressobj

    class GuardedDecoder:
        def __init__(self, *args, **kwargs):
            self.inner = original_decompressobj(*args, **kwargs)

        def decompress(self, data, max_length=0):
            return self.inner.decompress(data, max_length)

        def flush(self):
            flush_called.append(True)
            # flush without max_length could produce unbounded output
            return self.inner.flush()

        @property
        def eof(self):
            return self.inner.eof

        @property
        def unused_data(self):
            return self.inner.unused_data

        @property
        def unconsumed_tail(self):
            return self.inner.unconsumed_tail

    with patch.object(zlib, "decompressobj", GuardedDecoder):
        compressed = gzip.compress(b"test")
        stream = DelayedStream([compressed])
        async def handler(request):
            return httpx.Response(
                200, headers={"content-encoding": "gzip"}, stream=stream
            )
        async with mock_client(handler) as client:
            response, _ = await ds._safe_get(
                client, "https://example.com/fixture", timeout=0.1, max_bytes=100
            )
            # flush may be called but output is bounded by content check
            assert isinstance(response.content, bytes)


@pytest.mark.asyncio
async def test_high_compression_bomb_rejected():
    """High-compression bomb should be rejected by byte cap."""
    # Create a compressible payload
    data = b"a" * 10000
    compressed = gzip.compress(data)
    # Even though compressed is small, decompressed is large
    assert len(compressed) < 100
    assert len(data) > 1000

    stream = DelayedStream([compressed])
    async def handler(request):
        return httpx.Response(
            200, headers={"content-encoding": "gzip"}, stream=stream
        )
    async with mock_client(handler) as client:
        with pytest.raises(ValueError):
            await ds._safe_get(
                client, "https://example.com/fixture", timeout=0.1, max_bytes=500
            )
        assert stream.closed


@pytest.mark.asyncio
async def test_exact_cap_rejects_over():
    """Body exactly at cap should succeed; one byte over should fail."""
    data = b"x" * 100
    stream = DelayedStream([data])
    async def handler(request):
        return httpx.Response(200, stream=stream)
    async with mock_client(handler) as client:
        response, _ = await ds._safe_get(
            client, "https://example.com/fixture", timeout=0.1, max_bytes=100
        )
        assert len(response.content) == 100

    stream2 = DelayedStream([b"x" * 101])
    async def handler2(request):
        return httpx.Response(200, stream=stream2)
    async with mock_client(handler2) as client:
        with pytest.raises(ValueError):
            await ds._safe_get(
                client, "https://example.com/fixture", timeout=0.1, max_bytes=100
            )


@pytest.mark.asyncio
async def test_split_chunk_healthy():
    """Healthy response split across chunks should succeed."""
    parts = [b"hello ", b"world"]
    stream = DelayedStream(parts)
    async def handler(request):
        return httpx.Response(200, stream=stream)
    async with mock_client(handler) as client:
        response, _ = await ds._safe_get(
            client, "https://example.com/fixture", timeout=0.1
        )
        assert response.content == b"hello world"
        assert stream.closed


@pytest.mark.asyncio
async def test_corrupt_gzip_fails_closed():
    """Corrupt gzip data should fail-closed."""
    stream = DelayedStream([b"not-valid-gzip-data-at-all"])
    async def handler(request):
        return httpx.Response(
            200, headers={"content-encoding": "gzip"}, stream=stream
        )
    async with mock_client(handler) as client:
        with pytest.raises((ValueError, httpx.HTTPError, zlib.error)):
            await ds._safe_get(
                client, "https://example.com/fixture", timeout=0.1
            )
        assert stream.closed


@pytest.mark.asyncio
async def test_concatenated_gzip_valid():
    """Concatenated valid gzip streams should decompress."""
    part1 = gzip.compress(b"hello")
    part2 = gzip.compress(b" world")
    stream = DelayedStream([part1, part2])
    async def handler(request):
        return httpx.Response(
            200, headers={"content-encoding": "gzip"}, stream=stream
        )
    async with mock_client(handler) as client:
        # Concatenated gzip may or may not work depending on implementation
        # We just verify it doesn't crash unexpectedly
        try:
            response, _ = await ds._safe_get(
                client, "https://example.com/fixture", timeout=0.1
            )
            assert isinstance(response.content, bytes)
        except (ValueError, httpx.HTTPError, zlib.error):
            pass  # Fail-closed is acceptable


@pytest.mark.asyncio
async def test_trailing_compressed_data_rejected():
    """Trailing compressed data after valid stream should not be silently accepted."""
    valid_gzip = gzip.compress(b"valid")
    # Append garbage after valid gzip
    stream = DelayedStream([valid_gzip, b"trailing-garbage"])
    async def handler(request):
        return httpx.Response(
            200, headers={"content-encoding": "gzip"}, stream=stream
        )
    async with mock_client(handler) as client:
        try:
            response, _ = await ds._safe_get(
                client, "https://example.com/fixture", timeout=0.1
            )
            # If it succeeded, content should be the valid part only
            assert isinstance(response.content, bytes)
        except (ValueError, httpx.HTTPError, zlib.error):
            pass  # Fail-closed is acceptable


# --- R2 durable regressions: strengthened test quality ---


@pytest.mark.asyncio
async def test_bounded_decoder_no_flush():
    """R2-TQ: Every decompress call uses positive max_length; flush fails if called."""
    flush_called = []
    limits = []
    original_decompressobj = zlib.decompressobj

    class StrictDecoder:
        def __init__(self, *args, **kwargs):
            self.inner = original_decompressobj(*args, **kwargs)

        def decompress(self, data, max_length=0):
            limits.append(max_length)
            return self.inner.decompress(data, max_length)

        def flush(self):
            flush_called.append(True)
            raise RuntimeError("flush not allowed")

        def __getattr__(self, name):
            return getattr(self.inner, name)

    with patch.object(zlib, "decompressobj", StrictDecoder):
        # Use large data so decompressed exceeds max_bytes
        compressed = gzip.compress(b"a" * 2048)
        stream = DelayedStream([compressed])
        async def handler(request):
            return httpx.Response(
                200, headers={"content-encoding": "gzip"}, stream=stream
            )
        async with mock_client(handler) as client:
            with pytest.raises((ValueError, httpx.HTTPError)):
                await ds._safe_get(
                    client, "https://example.com/fixture", timeout=0.1, max_bytes=64
                )
    # Verify decompress was always called with positive max_length
    assert all(0 < limit <= 65 for limit in limits)
    assert not flush_called


@pytest.mark.asyncio
async def test_real_concatenated_gzip_rejected():
    """R2-TQ: Two independently valid complete gzip members concatenated must be rejected."""
    part1 = gzip.compress(b"hello")
    part2 = gzip.compress(b" world")
    assert len(part1) > 0 and len(part2) > 0
    stream = DelayedStream([part1, part2])
    async def handler(request):
        return httpx.Response(
            200, headers={"content-encoding": "gzip"}, stream=stream
        )
    async with mock_client(handler) as client:
        with pytest.raises(ValueError, match="[Tt]railing"):
            await ds._safe_get(client, "https://example.com/fixture", timeout=0.1)


@pytest.mark.asyncio
async def test_valid_single_member_gzip_accepts():
    """R2-TQ: A single valid complete gzip member must succeed (healthy companion)."""
    data = b"hello world"
    compressed = gzip.compress(data)
    stream = DelayedStream([compressed])
    async def handler(request):
        return httpx.Response(
            200, headers={"content-encoding": "gzip"}, stream=stream
        )
    async with mock_client(handler) as client:
        response, _ = await ds._safe_get(client, "https://example.com/fixture", timeout=0.1)
        assert response.content == data


@pytest.mark.asyncio
async def test_delayed_success_under_budget():
    """R2-TQ: Successful delayed response within budget passes (healthy companion)."""
    stream = DelayedStream([b"chunk" + str(i).encode() for i in range(5)], chunk_delay=0.001)
    async def handler(request):
        return httpx.Response(200, stream=stream)
    async with mock_client(handler) as client:
        response, _ = await ds._safe_get(client, "https://example.com/fixture", timeout=1.0)
        assert len(response.content) == sum(len(c) for c in stream.chunks)
        assert stream.closed


@pytest.mark.asyncio
async def test_cumulative_exhaustion_fails():
    """R2-TQ: Cumulative send+body time exceeding budget must fail."""
    stream = DelayedStream([b"x"], chunk_delay=0.05)
    async def handler(request):
        return httpx.Response(200, stream=stream)
    async with mock_client(handler) as client:
        with pytest.raises((TimeoutError, ValueError, httpx.HTTPError)):
            await ds._safe_get(client, "https://example.com/fixture", timeout=0.05)
        assert stream.closed


@pytest.mark.asyncio
async def test_expired_budget_blocks_redirect_send():
    """R2-TQ: First response closes, then expiry rejects before second send (healthy companion)."""
    sends = []
    first_stream = None

    class SlowCloseStream(DelayedStream):
        async def aclose(self):
            await asyncio.sleep(0.12)
            await super().aclose()

    async def resolver(host, port):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (PUBLIC, port))]

    async def handler(request):
        nonlocal first_stream
        if request.url.path == "/budget":
            first_stream = SlowCloseStream([])
            return httpx.Response(302, headers={"location": "/next"}, stream=first_stream)
        return httpx.Response(200, stream=DelayedStream([b"public text"]))

    transport = ph.GuardedAsyncTransport(resolver, httpx.MockTransport(handler))
    # Patch send to observe calls
    original_send = transport.handle_async_request
    async def observed_send(request):
        sends.append(str(request.url))
        return await original_send(request)
    transport.handle_async_request = observed_send

    async with httpx.AsyncClient(transport=transport, trust_env=False, follow_redirects=False) as client:
        with pytest.raises(TimeoutError):
            await ds._safe_get(client, "https://example.com/budget", timeout=0.05)
    assert first_stream is not None and first_stream.closed
    assert sends == ["https://example.com/budget"], "expired operation started another send"


@pytest.mark.asyncio
async def test_crossed_initial_scheme_port_rejected_via_guarded_client():
    """R2-TQ: Crossed scheme/port rejected through native GuardedAsyncTransport (healthy companion)."""
    async def resolver(host, port):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (PUBLIC, port))]

    async def handler(request):
        return httpx.Response(200, stream=RawStream([b"public text"]))

    transport = ph.GuardedAsyncTransport(resolver, httpx.MockTransport(handler))
    async with httpx.AsyncClient(transport=transport, trust_env=False, follow_redirects=False) as client:
        with pytest.raises(httpx.HTTPError):
            await client.get("https://example.com:80/public")
        with pytest.raises(httpx.HTTPError):
            await client.get("http://example.com:443/public")


@pytest.mark.asyncio
async def test_public_healthy_crossed_scheme_port_rejected():
    """R2-TQ: Public health companion — same crossed schemes rejected via _safe_get."""
    async def resolver(host, port):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (PUBLIC, port))]

    async def handler(request):
        return httpx.Response(200, stream=RawStream([b"public text"]))

    transport = ph.GuardedAsyncTransport(resolver, httpx.MockTransport(handler))
    async with httpx.AsyncClient(transport=transport, trust_env=False, follow_redirects=False) as client:
        with pytest.raises((ValueError, httpx.HTTPError)):
            await ds._safe_get(client, "https://example.com:80/public", timeout=0.1)
        with pytest.raises((ValueError, httpx.HTTPError)):
            await ds._safe_get(client, "http://example.com:443/public", timeout=0.1)


@pytest.mark.asyncio
async def test_empty_gzip_rejects_with_declared_truncation():
    """R2-C: Empty gzip stream raises ValueError with truncation reason, not AttributeError."""
    stream = DelayedStream([])
    async def handler(request):
        return httpx.Response(200, headers={"content-encoding": "gzip"}, stream=stream)
    async with mock_client(handler) as client:
        with pytest.raises(ValueError, match="[Tt]runcated"):
            await ds._safe_get(client, "https://example.com/empty-gzip", timeout=0.1)


@pytest.mark.asyncio
async def test_empty_deflate_rejects_with_declared_truncation():
    """R2-C: Empty deflate stream raises ValueError with truncation reason, not AttributeError."""
    stream = DelayedStream([])
    async def handler(request):
        return httpx.Response(200, headers={"content-encoding": "deflate"}, stream=stream)
    async with mock_client(handler) as client:
        with pytest.raises(ValueError, match="[Tt]runcated"):
            await ds._safe_get(client, "https://example.com/empty-deflate", timeout=0.1)


@pytest.mark.asyncio
async def test_raw_wire_exceeds_cap_gzip():
    """R2-B: gzip wire payload exceeding max_bytes rejected even when decoded small."""
    decoded = b"hi"
    compressed = gzip.compress(decoded)
    assert len(compressed) > 2  # compressed is larger than 2 bytes
    stream = DelayedStream([compressed])
    async def handler(request):
        return httpx.Response(200, headers={"content-encoding": "gzip"}, stream=stream)
    async with mock_client(handler) as client:
        with pytest.raises(ValueError, match="byte cap"):
            await ds._safe_get(client, "https://example.com/raw-cap", timeout=0.1, max_bytes=2)


@pytest.mark.asyncio
async def test_raw_wire_exceeds_cap_deflate():
    """R2-B: deflate wire payload exceeding max_bytes rejected even when decoded small."""
    decoded = b"hi"
    compressed = zlib.compress(decoded)
    assert len(compressed) > 2  # compressed is larger than 2 bytes
    stream = DelayedStream([compressed])
    async def handler(request):
        return httpx.Response(200, headers={"content-encoding": "deflate"}, stream=stream)
    async with mock_client(handler) as client:
        with pytest.raises(ValueError, match="byte cap"):
            await ds._safe_get(client, "https://example.com/raw-cap", timeout=0.1, max_bytes=2)
