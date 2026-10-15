"""Daily source fetching and link verification.

Each source listed in settings is queried asynchronously; results are
validated against security, byte, and time caps.
"""
from __future__ import annotations

import asyncio
import gzip
import io
import ipaddress
import logging
import re
import socket
import zlib
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, localcontext
from typing import Any
from urllib.parse import urlsplit

import httpx
from bs4 import BeautifulSoup

# Import GuardedAsyncTransport
from .public_http import GuardedAsyncTransport, _default_resolver

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Security constants
# ---------------------------------------------------------------------------
_PRIVATE_PREFIXES = [
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("169.254.0.0/16"),
]
_PRIVATE_IPV6_PREFIXES = [
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("fe80::/10"),
]
_MAX_BYTES = 1_000_000
_MAX_SECONDS = 10
_MAX_CONCURRENT = 20
_WAF_HEADERS = {"x-pz-id", "x-amz-cf-pop", "cf-cache-status"}
_WAF_BODY_TERMS = {"just a moment", "access denied", "error code", "cloudflare"}
_MIN_TEXT_CHARS = 100

# Approved OSI license identifiers that establish source-code open-source status
# via bounded public GitHub repository metadata (api.github.com/repos/{owner}/{repo},
# no credentials). A listing alone is never free-tier; downstream hosted/API
# costs remain unverified.
_OPEN_SOURCE_LICENSES = frozenset({
    "MIT", "MIT-0", "Apache-2.0", "BSD-2-Clause", "BSD-3-Clause", "0BSD",
    "ISC", "MPL-2.0", "GPL-2.0", "GPL-3.0", "AGPL-3.0", "LGPL-2.1", "LGPL-3.0",
    "Unlicense",
})

# Plain noncredential User-Agent for ordinary public GitHub metadata requests.
# No token/auth support; this is only a browser-like identity so uncredentialed
# public api.github.com lookups are not rejected as a bot.
_GITHUB_USER_AGENT = (
    "elilain-intel-crawler/1.0 (+https://elilain.ai; public-source research; no credentials)"
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _now_aware() -> datetime:
    return datetime.now(timezone.utc)


def _is_private_ip(ip_str: str) -> bool:
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return False
    for net in _PRIVATE_PREFIXES:
        if ip in net:
            return True
    for net in _PRIVATE_IPV6_PREFIXES:
        if ip in net:
            return True
    return False


def _is_safe_host(hostname: str | None) -> bool:
    if not hostname:
        return False
    # Reject localhost and subdomains
    if hostname == "localhost" or hostname.endswith(".localhost"):
        return False
    # Reject local names and domains ending with localdomain
    if hostname.lower() in ("local", "localdomain", "localhost.localdomain"):
        return False
    if hostname.endswith(".localdomain"):
        return False
    # If hostname is an IP literal, check global
    try:
        ip = ipaddress.ip_address(hostname)
        # Reject private and non-global IPs (including mapped IPv6)
        return ip.is_global
    except ValueError:
        pass
    # Hostname validation: basic sanity, actual DNS check happens later
    return True


def _is_waf_response(status: int, headers: dict[str, str], text: str) -> bool:
    if status != 403:
        return False
    lower_text = text.lower()
    if any(term in lower_text for term in _WAF_BODY_TERMS):
        return True
    lower_headers = {k.lower(): v for k, v in headers.items()}
    if "server" in lower_headers and "cloudflare" in lower_headers["server"].lower():
        return True
    if any(h in lower_headers for h in _WAF_HEADERS):
        return True
    return False


def _text_length(text: str) -> int:
    """Return length of meaningful text (strip HTML tags and whitespace)."""
    stripped = re.sub(r"<[^>]+>", " ", text)
    stripped = re.sub(r"\s+", " ", stripped).strip()
    return len(stripped)


# ---------------------------------------------------------------------------
# HTTP fetch with security
# ---------------------------------------------------------------------------
def _validate_redirect_target(
    location: str,
    request_url: httpx.URL,
    original_scheme: str,
    visited: set[str],
) -> str:
    """Validate a redirect Location header against the SSRF redirect policy.

    Shared by _safe_get and _safe_head so redirect safety is enforced
    consistently regardless of HTTP method.  Returns the resolved new URL,
    or raises ValueError on policy violation.
    """
    if not location:
        raise ValueError("Redirect without location")

    new_url = str(request_url.join(location))
    new_parsed = urlsplit(new_url)

    if new_parsed.scheme not in ("http", "https"):
        raise ValueError("Redirect to unsupported scheme rejected")

    new_port = new_parsed.port
    if new_parsed.scheme == "http" and new_port == 443:
        raise ValueError("Redirect to HTTP on port 443 rejected")
    if new_parsed.scheme == "https" and new_port == 80:
        raise ValueError("Redirect to HTTPS on port 80 rejected")

    if original_scheme == "https" and new_parsed.scheme == "http":
        raise ValueError("HTTPS to HTTP downgrade rejected")

    if new_parsed.username or new_parsed.password:
        raise ValueError("Redirect with credentials rejected")

    new_host = new_parsed.hostname
    if not new_host or not _is_safe_host(new_host):
        raise ValueError("Redirect to private host rejected")

    if new_url in visited:
        raise ValueError("Redirect loop detected")

    return new_url

async def _safe_send_request(
    client: httpx.AsyncClient,
    url: str,
    method: str,
    *,
    timeout: float = _MAX_SECONDS,
    max_bytes: int = _MAX_BYTES,
    extra_headers: dict[str, str] | None = None,
    deadline: float | None = None,
) -> httpx.Response:
    """Send a GET or HEAD request with SSRF-safe redirect following.

    Shared by _safe_get (GET) and _safe_head (HEAD).  DNS resolution and
    IP pinning are delegated to GuardedAsyncTransport.

    ``deadline`` is an absolute monotonic deadline shared across send,
    redirects and (for GET) body reading.  When omitted, a local deadline of
    ``loop.time() + timeout`` is computed here, preserving the original
    self-contained behavior for callers that own no outer budget (HEAD).
    """
    parsed = urlsplit(url)

    if parsed.scheme not in ("http", "https"):
        raise ValueError("Unsafe scheme")

    if parsed.username or parsed.password:
        raise ValueError("URL with credentials rejected")

    hostname = parsed.hostname
    if not hostname:
        raise ValueError("Empty hostname")

    if not _is_safe_host(hostname):
        raise ValueError("Private/reserved host rejected")

    visited = {url}
    original_scheme = parsed.scheme
    max_redirects = 5
    redirect_count = 0

    base_headers = {"Accept-Encoding": ""}
    if extra_headers:
        base_headers.update(extra_headers)
    request = client.build_request(method, url, timeout=timeout, headers=base_headers)
    if deadline is None:
        deadline = asyncio.get_event_loop().time() + timeout

    while True:
        remaining = deadline - asyncio.get_event_loop().time()
        if remaining <= 0:
            raise TimeoutError(f"Request timed out for {url}")
        try:
            response = await asyncio.wait_for(
                client.send(request, stream=True, follow_redirects=False),
                timeout=remaining,
            )
        except asyncio.TimeoutError as exc:
            raise TimeoutError(f"Request timed out for {request.url}") from exc

        if response.is_redirect and redirect_count < max_redirects:
            redirect_count += 1
            location = response.headers.get("location", "")
            new_url = _validate_redirect_target(location, request.url, original_scheme, visited)
            visited.add(new_url)
            await response.aclose()
            request = client.build_request(method, new_url, timeout=timeout, headers=base_headers)
            continue  # deadline is shared across all hops

        break

    return response

async def _safe_get(
    client: httpx.AsyncClient,
    url: str,
    *,
    timeout: float = _MAX_SECONDS,
    max_bytes: int = _MAX_BYTES,
    extra_headers: dict[str, str] | None = None,
) -> tuple[httpx.Response, str | None]:
    """Fetch a URL with SSRF protection and WAF detection.

    Strict behavior:
    - Disable automatic redirects; manually follow with per-hop validation.
    - Reject HTTPS->HTTP downgrade redirects.
    - Reject URLs with credentials.
    - Validate Content-Length before body read (bounded).
    - Stream body consumption with bounded byte cap (no unlimited decode).
    - One total deadline covers DNS, connect, redirects, and body.
    DNS resolution and IP pinning are delegated to GuardedAsyncTransport.
    """
    # Delegate redirect-following to shared _safe_send_request.
    # One absolute deadline is created HERE and shared across send, redirects
    # and body reading; _safe_send_request reuses it instead of granting a
    # second independent timeout after headers arrive.
    deadline = asyncio.get_event_loop().time() + timeout
    response = await _safe_send_request(
        client, url, "GET", timeout=timeout, max_bytes=max_bytes,
        extra_headers=extra_headers, deadline=deadline,
    )

    try:
        # Check WAF
        if _is_waf_response(response.status_code, dict(response.headers), ""):
            raise ValueError("WAF rejection detected")

        # Check Content-Length header before reading body
        cl_header = response.headers.get("content-length")
        if cl_header is not None:
            try:
                cl = int(cl_header)
            except ValueError:
                pass  # Non-integer content-length; fall back to streaming check
            else:
                if cl > max_bytes:
                    raise ValueError(f"Content-Length {cl} exceeds max_bytes {max_bytes}")

        # Handle gzip/deflate decompression with bounded decompressor
        content_encoding = response.headers.get("content-encoding", "")
        # R2-A: Explicitly normalize a SINGLE supported token, case-insensitively.
        # Reject stacked/substring/multiple encodings before any body read.
        encoding_type = None
        if content_encoding:
            tokens = [t.strip().lower() for t in content_encoding.split(",")]
            if len(tokens) != 1:
                raise ValueError(f"Unsupported content-encoding: {content_encoding}")
            token = tokens[0]
            if token == "identity" or token == "":
                encoding_type = None
            elif token == "gzip":
                encoding_type = "gzip"
            elif token == "deflate":
                encoding_type = "deflate"
            else:
                raise ValueError(f"Unsupported content-encoding: {content_encoding}")

        # Use raw iteration for encoded bodies; bytes iteration for identity
        async def iter_chunks():
            if encoding_type is not None:
                async for raw_chunk in response.aiter_raw():
                    yield raw_chunk
            else:
                async for chunk in response.aiter_bytes():
                    yield chunk

        # Stream body with byte cap using async iteration
        content = bytearray()
        decompressor = None

        # R2-C: Construct decompressor BEFORE the loop so it exists even when there are zero chunks.
        if encoding_type is not None:
            wbits = 31 if encoding_type == "gzip" else 15
            decompressor = zlib.decompressobj(wbits)

        # Enforce total deadline on body reading too
        body_start = asyncio.get_event_loop().time()

        async def read_body_chunks():
            """Read body chunks within deadline."""
            nonlocal content, decompressor
            async for raw_chunk in iter_chunks():
                # R2-B: Enforce independent raw/compressed budget BEFORE decoder input.
                # Reject a compressed wire payload larger than max_bytes even if it decodes small.
                if len(raw_chunk) > max_bytes:
                    raise ValueError("Response exceeds byte cap")
                if encoding_type is not None:
                    # Decompress with bounded output: positive max_length = remaining + 1
                    remaining = max_bytes - len(content)
                    try:
                        chunk_content = decompressor.decompress(raw_chunk, max_length=remaining + 1)
                    except zlib.error as exc:
                        raise ValueError(f"Decompression error: {exc}") from exc
                    if chunk_content:
                        content.extend(chunk_content)
                        if len(content) > max_bytes:
                            raise ValueError("Response exceeds byte cap")
                else:
                    # No encoding: use raw bytes directly
                    content.extend(raw_chunk)

                if len(content) > max_bytes:
                    raise ValueError("Response exceeds byte cap")

                # Check deadline
                elapsed = asyncio.get_event_loop().time() - body_start
                if elapsed >= (deadline - body_start):
                    raise TimeoutError(f"Body read timed out after {elapsed:.3f}s")

            # After stream ends, validate EOF and no trailing data (F-3, F-4, F-5)
            # R2-C: decompressor is guaranteed to exist here (constructed above even for empty streams)
            if encoding_type is not None:
                if not decompressor.eof:
                    raise ValueError("Truncated compressed stream")
                if decompressor.unused_data:
                    raise ValueError("Trailing compressed data rejected")

        try:
            # R2-E: No magic time buffer; remaining must be positive to start body read.
            remaining_body = deadline - asyncio.get_event_loop().time()
            if remaining_body <= 0:
                raise TimeoutError("Body read exceeded total deadline")
            await asyncio.wait_for(read_body_chunks(), timeout=remaining_body)
        except asyncio.TimeoutError as exc:
            raise TimeoutError("Body read exceeded total deadline") from exc

        # Set content on response for compatibility
        response._content = bytes(content)

        # Validate final status
        response.raise_for_status()

        return response, None
    finally:
        await response.aclose()

async def _safe_head(
    client: httpx.AsyncClient,
    url: str,
    *,
    timeout: float = _MAX_SECONDS,
) -> httpx.Response:
    """Send a HEAD request with SSRF-safe redirect following.

    Uses the same per-hop redirect validation as _safe_get via the shared
    _safe_send_request helper, so a redirect to a private host raises
    ValueError("Redirect to private host rejected") instead of silently
    following or being missed.  Does not read body content (HEAD responses
    have none).
    """
    response = await _safe_send_request(client, url, "HEAD", timeout=timeout)
    # Check WAF on the final response
    if _is_waf_response(response.status_code, dict(response.headers), ""):
        raise ValueError("WAF rejection detected")
    return response

async def _fetch_json(client: httpx.AsyncClient, url: str, extra_headers: dict[str, str] | None = None) -> dict[str, Any]:
    """Fetch and parse JSON response."""
    response, _ = await _safe_get(client, url, extra_headers=extra_headers)
    response.raise_for_status()
    return response.json()


async def _fetch_html(client: httpx.AsyncClient, url: str, extra_headers: dict[str, str] | None = None) -> str:
    """Fetch HTML response."""
    response, _ = await _safe_get(client, url, extra_headers=extra_headers)
    response.raise_for_status()
    text = response.text
    if _text_length(text) < _MIN_TEXT_CHARS:
        raise ValueError(f"Boilerplate response from {url}: too little text")
    return text


# ---------------------------------------------------------------------------
# Source adapters
# ---------------------------------------------------------------------------
async def _fetch_openrouter(client: httpx.AsyncClient, now_iso: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Fetch OpenRouter models catalog.

    Selection is bounded and honest: models are sorted by the real catalog
    ``created`` timestamp (never altered to fake a new release; max-age stays
    7 days), then the latest free models and the latest other models are taken
    (at most 10 total). Truncation is disclosed on the source status.
    """
    url = "https://openrouter.ai/api/v1/models"
    status_info = {"id": "openrouter-models", "name": "OpenRouter Models", "status": "ok",
                   "checked_at": now_iso, "returned": 0, "accepted": 0, "error_code": None}
    try:
        data = await _fetch_json(client, url)
        models = data.get("data", [])
        total_catalog = len(models)

        def _decimal(value: Any) -> Decimal | None:
            try:
                return Decimal(str(value))
            except (InvalidOperation, TypeError, ValueError):
                return None

        def _is_free_model(model: dict[str, Any]) -> bool | None:
            """True only when prompt AND completion are verified zero AND no
            explicit nonzero additional charge exists. Missing/unparseable
            price => None (unknown), never assumed paid or free."""
            pricing = model.get("pricing") or {}
            prompt_d = _decimal(pricing.get("prompt"))
            completion_d = _decimal(pricing.get("completion"))
            if prompt_d is None or completion_d is None:
                return None
            for key, value in pricing.items():
                if key in ("prompt", "completion"):
                    continue
                extra = _decimal(value)
                if extra is None:
                    return None
                if extra != 0:
                    return False
            return prompt_d == 0 and completion_d == 0

        # Sort by real catalog-created timestamp, descending. Do not rewrite
        # observed dates; the gate's max-age rule applies to the real value.
        with localcontext() as ctx:
            ctx.prec = 28
            dated = [m for m in models
                     if isinstance(m.get("created"), (int, float)) and m["created"] > 0]
            dated.sort(key=lambda m: m["created"], reverse=True)

        free_models = [m for m in dated if _is_free_model(m) is True]
        other_models = [m for m in dated if _is_free_model(m) is not True]
        selected = free_models[:5] + other_models[:5]

        status_info["truncation"] = (
            f"OpenRouter catalog has {total_catalog} models; selected latest "
            f"{len(selected)} (latest free {min(5, len(free_models))} + latest other "
            f"{min(5, len(other_models))}); older entries discarded by bounded cap."
        )

        candidates = []
        for model in selected:
            created = model.get("created", 0)
            published_at = datetime.fromtimestamp(created, tz=timezone.utc).isoformat() if created else None
            model_id = model.get("id", "")
            description = (model.get("description") or "").strip()
            # Missing descriptions must not be padded into eligible cards.
            if not description:
                status_info["returned"] += 1
                continue
            pricing = model.get("pricing") or {}
            prompt_price = pricing.get("prompt")
            completion_price = pricing.get("completion")
            free_flag = _is_free_model(model)
            if free_flag is True:
                free_status = "free-tier"
                free_detail = (f"OpenRouter pricing prompt={prompt_price} completion={completion_price} "
                               f"(both verified zero, no nonzero additional charges)")
            elif free_flag is None:
                free_status = "unknown"
                free_detail = f"OpenRouter pricing prompt={prompt_price!r} completion={completion_price!r}; unverified"
            else:
                free_status = "paid"
                free_detail = f"OpenRouter pricing prompt={prompt_price} completion={completion_price}"
            # Category from actual model identity, not a blanket 'AI tool' label.
            name_lower = model_id.lower()
            if "agent" in name_lower or "mcp" in name_lower:
                category = "agents"
            elif "skill" in name_lower:
                category = "skills"
            else:
                category = "models"
            # Documented capability + honest pricing in what/why, not filler.
            what = description[:200]
            if free_status == "free-tier":
                why = (f"OpenRouter lists {model_id} with prompt={prompt_price} and "
                       f"completion={completion_price} (both zero); free API access.")
            elif free_status == "paid":
                why = (f"OpenRouter lists {model_id} with prompt={prompt_price} and "
                       f"completion={completion_price}; paid API access.")
            else:
                why = (f"OpenRouter lists {model_id} with prompt={prompt_price!r} and "
                       f"completion={completion_price!r}; pricing unverified.")
            candidate = {
                "id": f"openrouter:{model_id}",
                "name": model.get("name", model_id),
                "what": what,
                "category": category,
                "source_id": "openrouter-models",
                "source_url": f"https://openrouter.ai/{model_id}",
                "published_at": published_at,
                "date_kind": "catalog-created",
                "observed_at": now_iso,
                "why": why,
                "free": {"status": free_status,
                         "detail": free_detail,
                         "evidence_url": url},
                "api": {"status": "available", "detail": "REST API", "evidence_url": url},
                "topics": [category] + (["free-api"] if free_status == "free-tier" else []),
            }
            candidates.append(candidate)
            status_info["returned"] += 1
            status_info["accepted"] += 1
        return candidates, status_info
    except Exception:
        logger.warning("OpenRouter fetch failed")
        status_info["status"] = "error"
        status_info["error_code"] = "source_failed"
        return [], status_info


async def _fetch_github_trending(client: httpx.AsyncClient, now_iso: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Fetch GitHub Trending AI repositories with real classification.

    Repositories are classified from their own name/description with word
    boundaries (a substring 'ai' in a URL slug does not make a repo an AI
    tool). Unrelated repos are excluded. License status is established by a
    bounded public repository-metadata lookup (api.github.com/repos/{owner}/{repo},
    no credentials); unknown/NOASSERTION/failed lookups remain unknown and earn
    no free-tier bonus. At most 5 metadata candidates are probed per source.
    """
    url = "https://github.com/trending?since=daily"
    status_info = {"id": "github-trending", "name": "GitHub Trending AI", "status": "ok",
                   "checked_at": now_iso, "returned": 0, "accepted": 0, "error_code": None}
    try:
        text = await _fetch_html(client, url)
        soup = BeautifulSoup(text, "html.parser")
        articles = soup.select("article.Box-row")

        # Word-boundary keyword match: a substring inside a slug is not a
        # capability claim. 'ai' must be a standalone token, not part of
        # 'email', 'said', 'detail', 'main', 'domain', etc.
        _KW_RE = r"(?<![A-Za-z0-9_-])(?:{kw})(?![A-Za-z0-9_-])"
        _AGENT_KW = re.compile(_KW_RE.format(kw="agent|agents|agentic|mcp|autonomous"), re.IGNORECASE)
        _MODEL_KW = re.compile(_KW_RE.format(kw="llm|llmo|rag|generative|diffusion|transformer|inference|prompt|fine-?tun|neural|langchain|crewai|autogen|embedding|vector|multimodal|reasoning"), re.IGNORECASE)
        _SKILL_KW = re.compile(_KW_RE.format(kw="skill|skills"), re.IGNORECASE)
        _AI_KW = re.compile(_KW_RE.format(kw=r"\bai\b|machine learning|\bml\b|genai|gen-?ai"), re.IGNORECASE)

        def _classify(name: str, desc: str) -> str | None:
            blob = f"{name} {desc}"
            if _AGENT_KW.search(blob):
                return "agents"
            if _SKILL_KW.search(blob):
                return "skills"
            if _MODEL_KW.search(blob) or _AI_KW.search(blob):
                return "models"
            return None  # not AI-relevant

        candidates = []
        license_meta: dict[str, tuple[str | None, str | None]] = {}
        for article in articles:
            if len(candidates) >= 5:
                break
            anchor = article.select_one("h2 a[href]")
            if not anchor:
                continue
            name = anchor["href"].lstrip("/")
            description_el = article.select_one("p")
            description = description_el.get_text(" ", strip=True) if description_el else ""
            language_el = article.select_one('[itemprop="programmingLanguage"]')
            language = language_el.get_text(strip=True) if language_el else ""
            category = _classify(name, description)
            if category is None:
                # Not an AI-relevant repository; exclude from the AI report.
                continue
            published_at = now_iso
            repo_url = f"https://github.com/{name}"
            # Bounded public repository metadata lookup for source-code licensing.
            # No credentials; failures stay unknown (no free entitlement).
            spdx_id = license_meta.get(name)
            if spdx_id is None and len(license_meta) < 5:
                try:
                    meta = await _fetch_json(client, f"https://api.github.com/repos/{name}",
                                             extra_headers={"Accept": "application/vnd.github+json",
                                                            "User-Agent": _GITHUB_USER_AGENT})
                    spdx_id = (meta.get("license") or {}).get("spdx_id")
                    full_name = meta.get("full_name") or name
                    if full_name.lower() != name.lower():
                        repo_url = f"https://github.com/{full_name}"
                except Exception:
                    logger.debug("GitHub license metadata lookup failed for %s", name)
                    spdx_id = None
                license_meta[name] = (spdx_id, repo_url)
            else:
                spdx_id, repo_url = spdx_id or (None, repo_url)
            if spdx_id in _OPEN_SOURCE_LICENSES:
                free_status = "open-source"
                free_detail = f"GitHub repository license: {spdx_id} (verified via api.github.com)"
            elif spdx_id:
                free_status = "unknown"
                free_detail = f"GitHub repository license: {spdx_id} (not an approved OSI license)"
            else:
                free_status = "unknown"
                free_detail = "GitHub public repository; license not verified"
            candidate = {
                "id": f"github:{name.lower()}",
                "name": name,
                "what": description or f"{category} repository on GitHub Trending ({language}).",
                "category": category,
                "source_id": "github-trending",
                "source_url": repo_url,
                "published_at": published_at,
                "date_kind": "trending",
                "observed_at": now_iso,
                "why": f"Listed on GitHub's daily trending page: {description or name} ({language}).",
                "free": {"status": free_status, "detail": free_detail,
                         "evidence_url": repo_url},
                "api": {"status": "unknown", "detail": "API availability and service costs not assessed", "evidence_url": ""},
                "topics": [category],
            }
            candidates.append(candidate)
            status_info["returned"] += 1
            status_info["accepted"] += 1
        status_info["truncation"] = (
            f"GitHub Trending returned {len(articles)} articles; {len(candidates)} AI-relevant "
            f"repos accepted after word-boundary classification and bounded license lookup "
            f"({len(license_meta)} metadata probes)."
        )
        return candidates, status_info
    except Exception:
        logger.warning("GitHub Trending fetch failed")
        status_info["status"] = "error"
        status_info["error_code"] = "source_failed"
        return [], status_info


async def _fetch_skills_hot(client: httpx.AsyncClient, now_iso: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Fetch Skills.sh hot/trending skills with real descriptions.

    The live page exposes skills at /owner/repository/skill paths (three
    validated relative segments, no query/fragment/absolute/credential
    paths). Skills are identified by full identity: skills:owner/repository/slug.
    Two skills with the same slug but different owners both survive. Each
    skill's detail page is fetched for its real documented description; a
    generic 'Install the ...' string or missing description rejects the card
    rather than padding a placeholder. License status is established by a
    bounded public GitHub repository metadata lookup (owner/repository, no
    credentials); a listing alone is never free-tier.
    """
    url = "https://skills.sh/trending"
    status_info = {"id": "skills-hot", "name": "Skills Hot", "status": "ok",
                   "checked_at": now_iso, "returned": 0, "accepted": 0, "error_code": None}
    try:
        text = await _fetch_html(client, url)
        soup = BeautifulSoup(text, "html.parser")
        links = soup.select("a[href]")
        skill_anchors = []
        for link in links:
            href = link.get("href", "").strip()
            # Strict relative triple segment: /owner/repository/skill.
            # Reject absolute URLs, query strings, fragments, credentials,
            # and any path that is not exactly three non-empty segments.
            if not href.startswith("/") or any(c in href for c in "?#"):
                continue
            parts = href.strip("/").split("/")
            if len(parts) != 3:
                continue
            owner, repo, slug = parts
            if not owner or not repo or not slug:
                continue
            if "/" in owner or "/" in repo or "/" in slug:
                continue
            if any(c in owner + repo + slug for c in ":@"):
                continue
            skill_anchors.append((link, owner, repo, slug))
        # Dedupe on full identity only (owner/repository/slug).
        seen: set[str] = set()
        unique_anchors = []
        for link, owner, repo, slug in skill_anchors:
            ident = f"{owner}/{repo}/{slug}"
            if ident in seen:
                continue
            seen.add(ident)
            unique_anchors.append((link, owner, repo, slug))

        candidates = []
        license_meta: dict[str, str | None] = {}
        for link, owner, repo, slug in unique_anchors[:5]:
            detail_url = f"https://skills.sh/{owner}/{repo}/{slug}"
            description = ""
            try:
                detail_text = await _fetch_html(client, detail_url)
                detail_soup = BeautifulSoup(detail_text, "html.parser")
                meta = detail_soup.find("meta", attrs={"name": "description"})
                description = meta.get("content", "").strip() if meta else ""
                if description.lower().startswith("install the "):
                    description = ""
            except Exception:
                logger.debug("Skill detail fetch failed for %s", detail_url)
            # Reject boilerplate/missing functional descriptions; do not pad.
            if not description or len(description) < 20:
                status_info["returned"] += 1
                continue
            name_el = link.select_one("h3")
            name = name_el.get_text(strip=True) if name_el else slug
            # Bounded public GitHub repository metadata lookup for the skill's
            # source-code license. A listing alone is never free-tier.
            spdx_id = license_meta.get(f"{owner}/{repo}")
            if spdx_id is None and len(license_meta) < 5:
                try:
                    meta = await _fetch_json(client, f"https://api.github.com/repos/{owner}/{repo}",
                                             extra_headers={"Accept": "application/vnd.github+json", "User-Agent": _GITHUB_USER_AGENT})
                    spdx_id = (meta.get("license") or {}).get("spdx_id")
                except Exception:
                    logger.debug("GitHub license metadata lookup failed for %s/%s", owner, repo)
                    spdx_id = None
                license_meta[f"{owner}/{repo}"] = spdx_id
            if spdx_id in _OPEN_SOURCE_LICENSES:
                free_status = "open-source"
                free_detail = f"Skills.sh skill source repository license: {spdx_id} (verified via api.github.com)"
            elif spdx_id:
                free_status = "unknown"
                free_detail = f"Skills.sh skill source repository license: {spdx_id} (not an approved OSI license)"
            else:
                free_status = "unknown"
                free_detail = "Skills.sh public skill listing; source-code license not verified"
            what = description
            why = (f"Public skill on Skills.sh at {owner}/{repo}/{slug}: {description}.")
            candidate = {
                "id": f"skills:{owner}/{repo}/{slug}",
                "name": name,
                "what": what,
                "category": "skills",
                "source_id": "skills-hot",
                "source_url": detail_url,
                "published_at": now_iso,
                "date_kind": "trending",
                "observed_at": now_iso,
                "why": why,
                "free": {"status": free_status, "detail": free_detail,
                         "evidence_url": detail_url},
                "api": {"status": "unknown", "detail": "API availability and service costs not assessed", "evidence_url": ""},
                "topics": ["skills"],
            }
            candidates.append(candidate)
            status_info["returned"] += 1
            status_info["accepted"] += 1
        status_info["truncation"] = (
            f"Skills.sh trending returned {len(unique_anchors)} unique skill anchors "
            f"(full owner/repo/slug identity); {len(candidates)} accepted after detail-page "
            f"description and bounded license lookup ({len(license_meta)} metadata probes)."
        )
        return candidates, status_info
    except Exception:
        logger.warning("Skills.sh fetch failed")
        status_info["status"] = "error"
        status_info["error_code"] = "source_failed"
        return [], status_info


def _unorouter_free_status(model: dict[str, Any]) -> str:
            """Classify a UnoRouter catalog row's free status as free-tier/paid/unknown.

            free-tier: ``is_free`` is literally ``True`` AND input_price/output_price/
            fixed_price are all explicit finite nonnegative numbers AND all zero.
            paid:      the three prices are explicit finite nonnegative AND at least
                       one is nonzero.
            unknown:   is_free missing or not literally True, or any price missing,
                       non-numeric, non-finite, or negative. A contradictory row (e.g.
                       is_free=False with all-zero prices) is unknown, never inferred
                       free or paid. A single invalid row must not zero the source.
            """
            is_free = model.get("is_free")
            prices: list[Decimal] = []
            for key in ("input_price", "output_price", "fixed_price"):
                value = model.get(key)
                # bool is a subclass of int; reject it explicitly so True/False are
                # never parsed as numeric prices.
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    return "unknown"
                try:
                    d = Decimal(str(value))
                except (InvalidOperation, ValueError, TypeError):
                    return "unknown"
                if not d.is_finite() or d < 0:
                    return "unknown"
                prices.append(d)
            if any(d != 0 for d in prices):
                return "paid"
            if is_free is True:
                return "free-tier"
            return "unknown"



async def _fetch_unorouter(client: httpx.AsyncClient, now_iso: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Fetch UnoRouter pricing catalog."""
    url = "https://api.unorouter.com/api/pricing/catalog"
    status_info = {"id": "unorouter-pricing", "name": "UnoRouter Pricing", "status": "ok",
                   "checked_at": now_iso, "returned": 0, "accepted": 0, "error_code": None}
    try:
        data = await _fetch_json(client, url)
        models = data.get("models", [])
        # UnoRouter's release_ts is a real upstream release timestamp (ms epoch).
        # Sort descending so the latest releases surface first.
        dated = sorted(
            [m for m in models if isinstance(m.get("release_ts"), (int, float)) and m["release_ts"] > 0],
            key=lambda m: m["release_ts"],
            reverse=True,
        )
        free_models, paid_models, unknown_models = [], [], []
        for m in dated:
            st = _unorouter_free_status(m)
            if st == "free-tier":
                free_models.append(m)
            elif st == "paid":
                paid_models.append(m)
            else:
                unknown_models.append(m)
        selected = (free_models + paid_models + unknown_models)[:5]
        candidates = []
        for model in selected:
            free_status = _unorouter_free_status(model)
            raw_ts = model.get("release_ts") or 0
            published_at = datetime.fromtimestamp(raw_ts / 1000, tz=timezone.utc).isoformat() if raw_ts else None
            model_name = model.get("model_name", "")
            vendor = model.get("vendor", "")
            tags_raw = model.get("tags")
            tags_list = tags_raw if isinstance(tags_raw, list) else []
            tags_str = ",".join(str(t) for t in tags_list[:4]) or "none"
            what = (f"UnoRouter pricing catalog entry for {vendor} {model_name}; "
                    f"type={model.get('type', '')}, tags={tags_str}, "
                    f"input={model.get('input_price')!r}, output={model.get('output_price')!r}, "
                    f"fixed={model.get('fixed_price')!r}.")
            if free_status == "free-tier":
                inp = Decimal(str(model.get("input_price")))
                out = Decimal(str(model.get("output_price")))
                fixed = Decimal(str(model.get("fixed_price")))
                free_detail = (f"UnoRouter catalog is_free=True; pricing input={inp} output={out} "
                               f"fixed={fixed} (all verified zero); entitlement unverified")
                why = (f"UnoRouter catalog entry: {vendor} {model_name} (input={inp}, output={out}, "
                       f"fixed={fixed}). Free-tier. Entitlement unverified beyond catalog listing.")
            elif free_status == "paid":
                inp = Decimal(str(model.get("input_price")))
                out = Decimal(str(model.get("output_price")))
                fixed = Decimal(str(model.get("fixed_price")))
                free_detail = (f"UnoRouter catalog is_free={model.get('is_free')!r}; pricing "
                               f"input={inp} output={out} fixed={fixed}; paid")
                why = (f"UnoRouter catalog entry: {vendor} {model_name} (input={inp}, output={out}, "
                       f"fixed={fixed}). Paid. Entitlement unverified beyond catalog listing.")
            else:
                free_detail = (f"UnoRouter catalog is_free={model.get('is_free')!r}; pricing "
                               f"input={model.get('input_price')!r} output={model.get('output_price')!r} "
                               f"fixed={model.get('fixed_price')!r}; unverified")
                why = (f"UnoRouter catalog entry: {vendor} {model_name}; pricing unverified.")
            candidate = {
                "id": f"unorouter:{model_name}",
                "name": model_name,
                "what": what,
                "category": "models",
                "source_id": "unorouter-pricing",
                "source_url": url,
                "published_at": published_at,
                "date_kind": "release",
                "observed_at": now_iso,
                "why": why,
                "free": {"status": free_status,
                         "detail": free_detail,
                         "evidence_url": url},
                "api": {"status": "available", "detail": "REST endpoint", "evidence_url": url},
                "topics": ["models"] + (["free-api"] if free_status == "free-tier" else []),
            }
            candidates.append(candidate)
            status_info["returned"] += 1
            status_info["accepted"] += 1
        free_sel = min(len(free_models), 5)
        _rem = 5 - free_sel
        paid_sel = min(len(paid_models), _rem)
        _rem -= paid_sel
        unknown_sel = min(len(unknown_models), _rem)
        status_info["truncation"] = (
            f"UnoRouter catalog has {len(dated)} dated models; selected latest {len(selected)} "
            f"(latest free-tier {free_sel} + latest paid {paid_sel} + latest unknown {unknown_sel})"
        )
        return candidates, status_info
    except Exception:
        logger.warning("UnoRouter fetch failed")
        status_info["status"] = "error"
        status_info["error_code"] = "source_failed"
        return [], status_info

async def fetch_candidates(settings: dict[str, Any], now: datetime | None = None) -> dict[str, Any]:
    """Fetch real candidates from configured sources with security checks."""
    sources_cfg = settings.get("sources", [])
    statuses: list[dict[str, Any]] = []
    candidates: list[dict[str, Any]] = []
    now = now or _now_aware()
    now_iso = now.isoformat()

    enabled = [s for s in sources_cfg if s.get("enabled")]
    skipped = [s for s in sources_cfg if not s.get("enabled")]

    if not enabled and not skipped:
        return {"candidates": [], "sources": [{"id": "no sources", "name": "no sources",
                                                "status": "skipped", "checked_at": now_iso,
                                                "returned": 0, "accepted": 0, "error_code": None}]}

    # Map source IDs to adapters
    source_adapters = {
        "openrouter-models": _fetch_openrouter,
        "github-trending": _fetch_github_trending,
        "skills-hot": _fetch_skills_hot,
        "unorouter-pricing": _fetch_unorouter,
    }

    # Build guarded client; tests may monkeypatch httpx.AsyncClient to inject mocks
    guarded = GuardedAsyncTransport(_default_resolver, trust_env=False)
    async with httpx.AsyncClient(
        transport=guarded,
        timeout=httpx.Timeout(_MAX_SECONDS),
        follow_redirects=False,
        trust_env=False,
    ) as client:
        semaphore = asyncio.Semaphore(_MAX_CONCURRENT)

        async def fetch_with_semaphore(source):
            async with semaphore:
                source_id = source.get("id", "")
                adapter = source_adapters.get(source_id)
                if not adapter:
                    # F-12/F-13: Normalize unknown source identity
                    return [], {"id": "unknown", "name": "unknown",
                                "status": "skipped", "checked_at": now_iso,
                                "returned": 0, "accepted": 0, "error_code": "unknown_adapter"}
                try:
                    cand_list, status = await adapter(client, now_iso)
                    return cand_list, status
                except Exception:
                    logger.warning(f"Source {source_id} failed")
                    # Keep validated adapter identity for known sources
                    safe_name = source.get("name", source_id) if source_id in source_adapters else "unknown"
                    return [], {"id": source_id, "name": safe_name,
                                "status": "error", "checked_at": now_iso,
                                "returned": 0, "accepted": 0, "error_code": "source_failed"}

        # Run concurrent fetches
        tasks = [fetch_with_semaphore(src) for src in enabled]
        results = await asyncio.gather(*tasks)

        for cand_list, status in results:
            candidates.extend(cand_list)
            statuses.append(status)

    # Handle skipped sources
    for s in skipped:
        sid = s.get("id", "")
        # F-12/F-13: Normalize unknown identity
        safe_id = "unknown" if sid not in source_adapters else sid
        statuses.append({
            "id": safe_id,
            "name": "unknown" if safe_id == "unknown" else s.get("name", safe_id),
            "status": "skipped",
            "checked_at": now_iso,
            "returned": 0,
            "accepted": 0,
            "error_code": None,
        })

    return {"candidates": candidates, "sources": statuses}


# ---------------------------------------------------------------------------
# Link verification
# ---------------------------------------------------------------------------
async def verify_candidates(candidates: list[dict[str, Any]], settings: dict[str, Any],
                            now: datetime | None = None) -> list[dict[str, Any]]:
    """Attach link_verification to each candidate with real HTTP checks.

    Uses _safe_head for HEAD verification so that redirect targets are
    validated against the same SSRF policy as _safe_get — private hosts,
    scheme downgrades, credentials, and loops are all rejected at the
    redirect seam, not merely avoided by client.follow_redirects=False.
    """
    now = now or _now_aware()
    now_iso = now.isoformat()
    verified = []

    # Build guarded client; tests may monkeypatch httpx.AsyncClient to inject mocks
    guarded = GuardedAsyncTransport(_default_resolver, trust_env=False)
    async with httpx.AsyncClient(
        transport=guarded,
        timeout=httpx.Timeout(_MAX_SECONDS),
        follow_redirects=False,
        trust_env=False,
    ) as client:
        semaphore = asyncio.Semaphore(_MAX_CONCURRENT)

        async def verify_single(cand):
            async with semaphore:
                url = cand.get("source_url", "")
                if not url:
                    return {**cand, "link_verification": {"status": "unverified",
                                    "checked_at": now_iso, "url": url, "http_status": None}}
                try:
                    # Only verify https URLs
                    parsed = urlsplit(url)
                    if parsed.scheme != "https":
                        return {**cand, "link_verification": {"status": "unverified",
                                        "checked_at": now_iso, "url": url, "http_status": None}}
                    if not _is_safe_host(parsed.hostname):
                        return {**cand, "link_verification": {"status": "unverified",
                                        "checked_at": now_iso, "url": url, "http_status": None}}

                    # Use _safe_head for redirect-safe HEAD verification.
                    # _safe_head validates every redirect hop through the same
                    # SSRF policy as _safe_get, so a redirect to a private
                    # host raises ValueError("Redirect to private host rejected")
                    # rather than silently following or silently ignoring it.
                    deadline = _MAX_SECONDS
                    response = await asyncio.wait_for(
                        _safe_head(client, url, timeout=deadline),
                        timeout=deadline,
                    )
                    http_status = response.status_code

                    # Check for WAF
                    if _is_waf_response(http_status, dict(response.headers), ""):
                        await response.aclose()
                        return {**cand, "link_verification": {"status": "unverified",
                                        "checked_at": now_iso, "url": url, "http_status": http_status}}

                    await response.aclose()
                    status = "verified" if http_status == 200 else "unverified"
                    return {**cand, "link_verification": {"status": status,
                                    "checked_at": now_iso, "url": url, "http_status": http_status}}
                except Exception:
                    logger.debug("Link verification failed")
                    return {**cand, "link_verification": {"status": "unverified",
                                    "checked_at": now_iso, "url": url, "http_status": None}}

        tasks = [verify_single(cand) for cand in candidates]
        verified = await asyncio.gather(*tasks)

    return list(verified)


# --- Cross-Provider Candidate Fusion ---
def _normalize_url(url: str) -> str:
    """Normalize a URL for dedup by stripping tracking params and normalizing path."""
    try:
        from urllib.parse import urlparse
        parsed = urlparse(url.lower())
        clean = url
        for param in ['utm_source', 'utm_medium', 'utm_campaign', 'ref', 'source', 'medium']:
            clean = clean.replace(f'?{param}=', '?').replace(f'&{param}=', '&').rstrip('?')
        return clean
    except Exception:
        return url


def _fuse_candidates(candidates_by_source: dict[str, list[dict]]) -> list[dict]:
    """Merge candidates from multiple sources, deduplicating by canonical original-story URL.

    Returns one canonical candidate per unique project, with merged evidence.
    Source priority: openrouter-models > github-trending > skills-hot > other.
    """
    source_priority = {'openrouter-models': 3, 'github-trending': 2, 'skills-hot': 1, 'unorouter-pricing': 1}

    entities: dict[str, dict] = {}

    for source_name, candidates in candidates_by_source.items():
        for cand in candidates:
            src_url = cand.get('source_url', '')
            norm_key = _normalize_url(src_url) if src_url else None

            if not norm_key:
                norm_key = f"{cand.get('name', '').lower()}|{cand.get('source_id', '')}"

            if norm_key in entities:
                existing = entities[norm_key]
                existing_priority = source_priority.get(existing.get('source_id', ''), 0)
                new_priority = source_priority.get(cand.get('source_id', ''), 0)

                if new_priority > existing_priority:
                    existing.update(cand)
                    existing['source_ids'] = existing.get('source_ids', []) + [cand.get('source_id', '')]
                    if isinstance(existing['source_ids'], list):
                        existing['source_ids'] = list(set(existing['source_ids']))
            else:
                entities[norm_key] = {
                    **cand,
                    'source_ids': [cand.get('source_id', '')] if cand.get('source_id') else [],
                    'discovered_by': cand.get('source_id', 'unknown'),
                    'normalized_url': norm_key,
                }

    result = list(entities.values())

    def _sort_key(c):
        sp = source_priority.get(c.get('source_id', ''), 0)
        score = c.get('score', 0) or 0
        return (-sp, -score)

    result.sort(key=_sort_key)
    return result


def _dedupe_candidates(candidates: list[dict]) -> list[dict]:
    """Remove duplicate candidates, keeping the most authoritative version."""
    seen: set[str] = set()
    result = []
    for cand in candidates:
        norm_url = cand.get('normalized_url') or cand.get('source_url', '')
        if norm_url and norm_url in seen:
            continue
        if norm_url:
            seen.add(norm_url)
        AliasKey = f"{cand.get('name', '').lower()}|{cand.get('source_id', '')}"
        if AliasKey in seen and norm_url:
            continue
        if AliasKey:
            seen.add(AliasKey)
        result.append(cand)
    return result
