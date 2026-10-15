from __future__ import annotations

import asyncio
import ipaddress
import logging
import socket
from enum import Enum, auto
from typing import Callable

import httpx

logger = logging.getLogger(__name__)

Resolver = Callable[[str, int], list[tuple[int, int, int, str, tuple[str, int]]]]


# Default max pinned origins; callers may override via max_pinned kwarg.
_DEFAULT_MAX_PINNED = 20


def _prefer_ipv4(addresses: list[str]) -> list[str]:
    """Prefer IPv4 answers while keeping IPv6 available."""
    return sorted(addresses, key=lambda ip: ipaddress.ip_address(ip).version != 4)


def _is_global_ip(ip_str: str) -> bool:
    """Return True if the address is a global (non-private/reserved) IP.

    Explicitly rejects multicast and other special-use addresses that
    Python's ipaddress considers is_global but are not connectable.
    """
    try:
        ip = ipaddress.ip_address(ip_str)
        # Reject multicast, link-local, loopback, private, and reserved
        if ip.is_multicast:
            return False
        if ip.is_link_local:
            return False
        if ip.is_loopback:
            return False
        if ip.is_private:
            return False
        if ip.is_reserved:
            return False
        return ip.is_global
    except ValueError:
        return False


def _default_resolver(host: str, port: int) -> list[tuple[int, int, int, str, tuple[str, int]]]:
    """Default DNS resolver using stdlib getaddrinfo."""
    return asyncio.get_event_loop().getaddrinfo(
        host, port, type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP
    )


class _EntryState(Enum):
    """Internal states for owned wire entries in the private ledger."""
    ACTIVE = auto()
    IDLE = auto()
    RETIRING = auto()
    FAILED_CLOSE = auto()
    FAILED_SHUTDOWN = auto()


class _LedgerEntry:
    """A single owned wire entry in the private ledger."""
    __slots__ = ('origin', 'wire', 'state', 'lease_count')

    def __init__(self, origin: str, wire: httpx.AsyncBaseTransport) -> None:
        self.origin = origin
        self.wire = wire
        self.state = _EntryState.IDLE
        self.lease_count = 0


class _AdmissionToken:
    """Request-local lease token carrying the admitted entry by identity.

    Ensures once-only cleanup at the AsyncByteStream seam and preserves the
    original entry reference through every post-admission await, dispatch, and
    stream handoff — even if the ledger entry is later removed by shutdown.
    """
    __slots__ = ('entry', 'released')

    def __init__(self, entry: _LedgerEntry) -> None:
        self.entry = entry
        self.released = False


class GuardedAsyncTransport(httpx.AsyncBaseTransport):
    """Async transport that pins connections to validated global IPs.

    Wraps an inner transport (usually httpx.MockTransport in tests or
    httpx.AsyncHTTPTransport in production). Before dispatching, it:
    1. Resolves the request hostname to IPs
    2. Validates every resolved IP is global
    3. Rewrites the URL to use the preferred global IP
    4. Preserves the original hostname in Host header and SNI extension
    5. Creates inner transport with trust_env=False to prevent proxy/CA leakage
    6. Reuses generated wires per-origin across sequential requests
    7. Retains at most ``max_pinned`` origins, evicting idle LRU when full
       (active body streams are NOT evicted)
    """

    def __init__(
        self,
        resolver: Resolver,
        inner: httpx.AsyncBaseTransport | None = None,
        *,
        trust_env: bool = False,
        max_pinned: int | None = None,
    ) -> None:
        self.resolver = resolver
        self._inner = inner
        self._trust_env = trust_env
        self._max_pinned = max_pinned if max_pinned is not None else _DEFAULT_MAX_PINNED

        # Private bounded serialized entry ledger
        self._ledger: list[_LedgerEntry] = []
        # Origin -> index in ledger for O(1) lookup
        self._origin_to_idx: dict[str, int] = {}
        # LRU order (most-recently-used appended; evict from front)
        self._lru_order: list[str] = []
        # Narrow async lock for serialized admission/idle retirement/shutdown
        self._lock = asyncio.Lock()
        # Closed fence: set BEFORE awaiting lock
        self._closed = False
        # Track terminal inner failure to prevent retry (F-06)
        self._inner_terminal_failed = False

    # ------------------------------------------------------------------
    # Backward-compatible properties for test access
    # ------------------------------------------------------------------

    @property
    def _wires(self) -> dict[str, httpx.AsyncBaseTransport]:
        """Expose active wires as a dict for backward-compatible test access."""
        return {e.origin: e.wire for e in self._ledger if e.state == _EntryState.IDLE}

    @property
    def _wire_order(self) -> list[str]:
        """Expose LRU order for backward-compatible test access."""
        return list(self._lru_order)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _origin_key(self, request: httpx.Request) -> str:
        """Return scheme://host:port for the logical request origin."""
        host = request.url.host
        port = request.url.port or (443 if request.url.scheme == "https" else 80)
        return f"{request.url.scheme}://{host}:{port}"

    def _find_entry_by_index(self, idx: int) -> _LedgerEntry | None:
        """Find ledger entry by index (caller must hold lock)."""
        if 0 <= idx < len(self._ledger):
            return self._ledger[idx]
        return None

    def _remove_from_lru(self, origin: str) -> None:
        """Remove origin from LRU order (caller must hold lock)."""
        try:
            self._lru_order.remove(origin)
        except ValueError:
            pass

    def _append_to_lru(self, origin: str) -> None:
        """Append origin to LRU order (caller must hold lock)."""
        self._lru_order.append(origin)

    def _rebuild_origin_index(self) -> None:
        """Rebuild origin-to-index map after ledger mutations (caller must hold lock)."""
        self._origin_to_idx.clear()
        for i, entry in enumerate(self._ledger):
            self._origin_to_idx[entry.origin] = i

    def _synchronous_release_entry(self, entry: _LedgerEntry) -> None:
        """Synchronously release one lease for an entry and update state.

        Must be called outside of any lock to avoid deadlocks.
        """
        entry.lease_count -= 1
        if entry.lease_count < 0:
            entry.lease_count = 0
        # Late-release guard: if this entry was removed from the ledger
        # (e.g., by explicit shutdown), do not restore it to IDLE/LRU.
        # The physical wire is already closed/detached; only the logical
        # request lease remains outstanding.
        if entry.lease_count == 0 and entry.state == _EntryState.ACTIVE:
            # Verify the entry is still in the ledger before transitioning.
            still_present = any(e is entry for e in self._ledger)
            if still_present:
                entry.state = _EntryState.IDLE
                try:
                    self._lru_order.remove(entry.origin)
                except ValueError:
                    pass
                self._lru_order.append(entry.origin)

    async def _acquire_wire(self, origin: str) -> tuple[httpx.AsyncBaseTransport, bool]:
        """Return (wire, is_new) for *origin*, creating one if absent.

        Serialized admission with narrow lock. Evicts idle LRU origin when
        the pool would exceed ``max_pinned``. Active leases prevent eviction;
        exhausted capacity fails closed.
        """
        # Set closed fence BEFORE acquiring lock
        if self._closed:
            raise httpx.HTTPError("GuardedAsyncTransport is closed")

        async with self._lock:
            # Recheck after acquiring lock
            if self._closed:
                raise httpx.HTTPError("GuardedAsyncTransport is closed")

            # Try to find existing non-quarantined entry
            idx = self._origin_to_idx.get(origin)
            entry = None
            if idx is not None:
                entry = self._ledger[idx]

            if entry is not None and entry.state not in (_EntryState.RETIRING, _EntryState.FAILED_CLOSE):
                # Move to end (most-recently-used)
                self._remove_from_lru(origin)
                self._append_to_lru(origin)
                # Increment lease while holding lock — ensures no idle window
                # between acquisition and dispatch (F-02).
                entry.lease_count += 1
                entry.state = _EntryState.ACTIVE
                return entry.wire, False, entry

            # F07/F09: Reject reuse AND replacement generation while a
            # physically unconfirmed RETIRING or FAILED_CLOSE owner exists for
            # this origin. Count every unconfirmed wire until confirmed closure.
            if idx is not None:
                existing = self._ledger[idx]
                if existing.state in (_EntryState.RETIRING, _EntryState.FAILED_CLOSE):
                    raise httpx.HTTPError(
                        f"GuardedAsyncTransport: origin {origin} has unconfirmed owner"
                    )

            # Count ALL unconfirmed-close entries against capacity.
            # RETIRING and FAILED_CLOSE entries still consume a slot until
            # their close is confirmed (F-01).
            live_count = len(self._ledger)

            # Evict idle LRU wires until we have space or no evictable wires remain
            while live_count >= self._max_pinned:
                # Find first idle LRU origin
                lru_key = None
                for candidate in list(self._lru_order):
                    cand_idx = self._origin_to_idx.get(candidate)
                    if cand_idx is not None and cand_idx < len(self._ledger):
                        cand_entry = self._ledger[cand_idx]
                        if cand_entry.state == _EntryState.IDLE:
                            lru_key = candidate
                            break
                if lru_key is None:
                    # All wires are active or quarantined — fail closed
                    raise httpx.HTTPError(
                        f"GuardedAsyncTransport: exhausted active capacity for origin {origin}"
                    )
                # Retire idle LRU entry — mark RETIRING but keep in ledger so
                # other tasks see it consumes capacity. Per brief:
                # "ACTIVE/IDLE/RETIRING/FAILED_CLOSE all consume one finite max_pinned slot."
                retired_idx = self._origin_to_idx[lru_key]
                retired_entry = self._ledger[retired_idx]
                retired_entry.state = _EntryState.RETIRING
                live_count -= 1  # RETIRING doesn't count as live; it will be removed on success

                # Await close while holding lock (tradeoff: admission latency
                # during rare idle eviction for fewer states/tasks)
                try:
                    await retired_entry.wire.aclose()
                    # Success: remove entry from ledger using stable index
                    self._ledger.pop(retired_idx)
                    self._rebuild_origin_index()
                    self._lru_order.remove(lru_key)
                except BaseException as exc:
                    # Failure or cancellation: keep entry in ledger for explicit shutdown
                    if isinstance(exc, asyncio.CancelledError):
                        retired_entry.state = _EntryState.RETIRING  # preserve for next shutdown
                    else:
                        retired_entry.state = _EntryState.FAILED_CLOSE
                    logger.debug(
                        "GuardedAsyncTransport: RETIREMENT_FAILURE"
                    )
                    live_count += 1
                    # Re-raise cancellation so the task gets its CancelledError
                    if isinstance(exc, asyncio.CancelledError):
                        raise
                    # F01: Failed retirement aborts admission — do NOT create
                    # a replacement wire. The failed entry retains its slot.
                    raise httpx.HTTPError(
                        f"GuardedAsyncTransport: retirement failed for origin {lru_key}"
                    )

            # Recheck closed fence after eviction loop before creating new wire
            if self._closed:
                raise httpx.HTTPError("GuardedAsyncTransport is closed")

            # F07/F09: If the queried origin itself is quarantined (FAILED_CLOSE),
            # do not create a replacement — fail closed.
            idx = self._origin_to_idx.get(origin)
            if idx is not None and self._ledger[idx].state == _EntryState.FAILED_CLOSE:
                raise httpx.HTTPError(
                    f"GuardedAsyncTransport: origin {origin} is quarantined"
                )

            # Create new wire and immediately mark it ACTIVE so no other
            # task can retire it before the current dispatch begins.
            wire = httpx.AsyncHTTPTransport(trust_env=self._trust_env)
            entry = _LedgerEntry(origin, wire)
            entry.lease_count = 1
            entry.state = _EntryState.ACTIVE
            self._ledger.append(entry)
            self._origin_to_idx[origin] = len(self._ledger) - 1
            self._append_to_lru(origin)
            return wire, True, entry

    # ------------------------------------------------------------------
    # Stream lease wrapper
    # ------------------------------------------------------------------

    class _LeasedStream(httpx.AsyncByteStream):
        """Wraps an underlying stream with idempotent per-request lease tracking.

        Lease release is synchronous/await-free and once-only per REQUEST.
        Repeated/concurrent stream close must not release a sibling, double
        close or strand cleanup.
        """

        def __init__(
            self,
            stream: httpx.AsyncByteStream,
            transport: "GuardedAsyncTransport",
            token: _AdmissionToken,
        ) -> None:
            self._stream = stream
            self._transport = transport
            self._token = token
            self._closed = False
            self._released = False

        async def __aiter__(self) -> httpx.AsyncByteStream:
            if self._closed:
                return
            async for chunk in self._stream:
                yield chunk

        async def aclose(self) -> None:
            if self._closed:
                return
            self._closed = True
            exc = None
            try:
                await self._stream.aclose()
            except BaseException as e:
                exc = e
            finally:
                if not self._released:
                    self._released = True
                    # Release via the original admission token — this preserves
                    # the exact entry reference even if the ledger was modified
                    # (e.g., by concurrent shutdown that removed the entry).
                    entry = self._token.entry
                    if entry is not None and not self._token.released:
                        self._token.released = True
                        self._transport._synchronous_release_entry(entry)
                    # Quarantine wire on stream-close failure/cancellation (F-07)
                    if exc is not None:
                        self._transport._quarantine_wire(self._token.entry)
                if exc is not None:
                    raise exc

    def _quarantine_wire(self, entry: _LedgerEntry | None) -> None:
        """Mark wire as FAILED_CLOSE to quarantine it after stream close failure.
        Must not hold lock; called from _LeasedStream with no concurrency risk
        because stream closes for the same origin are serialized.
        Routes by original admitted entry identity (F-R3-003), not mutable origin."""
        if entry is None:
            return
        # Verify the entry is still in the ledger before mutating it.
        if any(e is entry for e in self._ledger):
            if entry.state in (_EntryState.IDLE, _EntryState.ACTIVE):
                entry.state = _EntryState.FAILED_CLOSE

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            if self._closed:
                raise httpx.HTTPError("GuardedAsyncTransport is closed")

            host = request.url.host
            port = request.url.port or (443 if request.url.scheme == "https" else 80)

            # R2-D: Apply same http→80/https→443 admission policy for initial requests.
            # Reject crossed scheme/port combinations before DNS/wire admission.
            if request.url.scheme == "http" and port == 443:
                raise httpx.HTTPError("HTTP on port 443 rejected")
            if request.url.scheme == "https" and port == 80:
                raise httpx.HTTPError("HTTPS on port 80 rejected")

            # Reject non-standard ports
            if port not in (80, 443):
                raise httpx.HTTPError(f"Non-standard port rejected: {port}")

            # Reject URL credentials
            if request.url.username or request.url.password:
                raise httpx.HTTPError("URL credentials rejected")

            # F-14: Reject foreign caller-supplied SNI before DNS/wire admission
            caller_sni = request.extensions.get("sni_hostname")
            if caller_sni is not None and caller_sni != host:
                raise httpx.HTTPError("Foreign SNI rejected")

            # Preserve original hostname for SNI if already set (e.g., from parent code)
            original_sni = host

            # Resolve DNS before connecting (fresh validation on every dispatch)
            infos = await self.resolver(host, port)

            if self._closed:
                raise httpx.HTTPError("GuardedAsyncTransport is closed")

            # Collect all resolved IPs
            raw_ips = [info[4][0] for info in infos]
            if not raw_ips:
                raise httpx.ConnectError(f"DNS resolution failed for {original_sni}")

            # Validate every resolved IP is global (reject multicast, private, etc.)
            for ip_str in raw_ips:
                if not _is_global_ip(ip_str):
                    raise httpx.ConnectError(
                        f"Resolved non-global IP rejected: {ip_str}"
                    )

            # Pick preferred address (IPv4 first)
            ordered = _prefer_ipv4(raw_ips)
            pinned_ip = ordered[0]

            # Rewrite URL to use the validated IP
            new_url = request.url.copy_with(host=pinned_ip)

            # Build a new request with original Host header and SNI
            # Remove existing Host header to avoid duplicates
            headers = {k: v for k, v in dict(request.headers).items() if k.lower() != "host"}
            headers["Host"] = original_sni
            # Use original hostname for SNI (foreign SNI already rejected above)
            extensions = dict(request.extensions)
            if "sni_hostname" not in extensions:
                extensions["sni_hostname"] = original_sni

            pinned = httpx.Request(
                request.method,
                new_url,
                headers=httpx.Headers(headers),
                stream=request.stream,
                extensions=extensions,
            )

            # Use inner transport if provided (e.g., MockTransport in tests);
            # otherwise route through the per-origin wire pool.
            if self._inner is not None:
                response = await self._inner.handle_async_request(pinned)
                # Preserve original logical URL in response for caller validation
                response._request = request
                return response

            origin = self._origin_key(request)

            if self._closed:
                raise httpx.HTTPError("GuardedAsyncTransport is closed")

            wire, is_new, admitted_entry = await self._acquire_wire(origin)

            # Create a once-only lease token for this request — captured at
            # admission time, not looked up after await (F-R3-002).
            token = _AdmissionToken(admitted_entry)

            # Dispatch fence: recheck closed after admission before actual send (F-03).
            try:
                async with self._lock:
                    if self._closed:
                        # Released before dispatch — prevent lease leak
                        if not token.released:
                            token.released = True
                            self._synchronous_release_entry(admitted_entry)
                        raise httpx.HTTPError("GuardedAsyncTransport is closed")
            except BaseException:
                # If we get cancelled while waiting for the lock, release the lease.
                if not token.released:
                    token.released = True
                    self._synchronous_release_entry(admitted_entry)
                raise

            try:
                response = await wire.handle_async_request(pinned)
            except BaseException:
                # Dispatch catches BaseException for cleanup then re-raises
                # Synchronous, once-only lease release (F-02/F-07)
                if not token.released:
                    token.released = True
                    self._synchronous_release_entry(admitted_entry)
                logger.debug("GuardedAsyncTransport: DISPATCH_EXCEPTION")
                raise

            # Preserve original logical URL in response for caller validation
            # Response URL comes from its request object, so replace it
            response._request = request

            # Wrap response.stream with idempotent lease tracking
            # HTTPX wraps returned response.stream in its own BoundAsyncStream;
            # it gates Response.aclose on is_closed. Therefore changing the
            # response instance method is not the correct lease seam.
            # We wrap the stream at the AsyncByteStream seam.
            if response.stream is not None:
                # If response is already closed (e.g., content-based response),
                # release lease immediately since stream.aclose() won't be called.
                if response.is_closed:
                    if not token.released:
                        token.released = True
                        self._synchronous_release_entry(admitted_entry)
                else:
                    response.stream = self._LeasedStream(response.stream, self, token)
            else:
                # Non-streaming response: release lease immediately
                if not token.released:
                    token.released = True
                    self._synchronous_release_entry(admitted_entry)

            return response

    async def aclose(self) -> None:
        """Close all generated wires and the injected inner transport.

        Sets admission fence _closed=True BEFORE waiting for the lock, so
        DNS-pending/retirement-pending/new requests cannot resume admission
        after shutdown begins.
        """
        # Set closed fence BEFORE waiting for lock
        self._closed = True

        # F05: Serialize the COMPLETE shutdown operation (inner + ledger) with the lock.
        # Hold lock during entire shutdown to prevent concurrent shutdowns from
        # racing on the same entries.
        async with self._lock:
            # Close injected inner while holding lock — serializes concurrent shutdowns (F-05)
            inner = self._inner
            if inner is not None and not self._inner_terminal_failed:
                try:
                    await inner.aclose()
                    self._inner = None
                except asyncio.CancelledError:
                    # Preserve inner for retry on next explicit aclose
                    raise
                except BaseException:
                    # F06: Terminal failure — retain reference so repeated aclose does not retry.
                    self._inner_terminal_failed = True
                    logger.debug("GuardedAsyncTransport: INNER_CLOSE_FAILURE")

            # Close all entries while holding lock — prevents concurrent shutdown races
            entries = list(self._ledger)

            for entry in entries:
                if entry.state in (_EntryState.IDLE, _EntryState.ACTIVE):
                    # Close idle AND active wires on explicit shutdown (F-04)
                    try:
                        await entry.wire.aclose()
                        # Success: remove from ledger using stable index
                        idx = self._origin_to_idx.get(entry.origin)
                        if idx is not None and idx < len(self._ledger) and self._ledger[idx] is entry:
                            self._ledger.pop(idx)
                            self._rebuild_origin_index()
                            try:
                                self._lru_order.remove(entry.origin)
                            except ValueError:
                                pass
                    except asyncio.CancelledError:
                        # Preserve remaining entries for retry at next aclose
                        raise
                    except BaseException:
                        entry.state = _EntryState.FAILED_SHUTDOWN
                        logger.debug("GuardedAsyncTransport: OWNED_CLOSE_FAILURE")
                elif entry.state in (_EntryState.RETIRING, _EntryState.FAILED_CLOSE):
                    # Retry prior retirement/stream failure once at explicit shutdown (F-05)
                    try:
                        await entry.wire.aclose()
                        # Success: remove from ledger
                        idx = self._origin_to_idx.get(entry.origin)
                        if idx is not None and idx < len(self._ledger) and self._ledger[idx] is entry:
                            self._ledger.pop(idx)
                            self._rebuild_origin_index()
                            try:
                                self._lru_order.remove(entry.origin)
                            except ValueError:
                                pass
                    except asyncio.CancelledError:
                        raise
                    except BaseException:
                        entry.state = _EntryState.FAILED_SHUTDOWN
                        logger.debug("GuardedAsyncTransport: OWNED_CLOSE_FAILURE")
                elif entry.state == _EntryState.FAILED_SHUTDOWN:
                    # Terminal shutdown failure; do not retry (F-05)
                    pass
