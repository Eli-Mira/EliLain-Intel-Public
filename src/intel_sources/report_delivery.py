"""Publish an already-sanitized, single-file report; verify before notifying.

No repository visibility changes, site creation, raw-input export, dotenv loading,
or retry of side-effecting requests. Errors and result objects contain safe codes
only, never server responses, request URLs containing tokens, or credentials.
"""
from __future__ import annotations

from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
import hashlib
import logging
import os
import ipaddress
import json
import logging
import os
from pathlib import Path
import re
import socket
import time
from typing import Callable, Mapping, Sequence
from urllib.parse import urlsplit

from bs4 import BeautifulSoup
import httpx

from .public_report import PUBLIC_SOURCES, _standalone_html

MAX_HTML_BYTES = 4 * 1024 * 1024
MAX_API_BYTES = 128 * 1024
ATTACHMENT_NAME = "index.html"
MAX_RECEIPT_ARTIFACTS = 64
RECEIPT_FIELDS = {"url"} | {key + suffix for key in
    ("telegram", "discord", "telegram_link", "discord_link", "telegram_text")
    for suffix in ("", "_destination")}
log = logging.getLogger(__name__)


def _debug() -> bool:
    """True when DELIVERY_DEBUG is set, with the logger level made explicit.

    `log.debug()` on a NOTSET logger inherits the root level (WARNING by
    default) and is silently dropped - which is exactly how the first
    host/status logging attempt vanished from CI. The level is set at call time
    rather than import time so it does not depend on when the variable appears.
    """
    if not os.getenv("DELIVERY_DEBUG"):
        return False
    log.setLevel(logging.DEBUG)
    return True

API_HOSTS = {"api.netlify.com", "api.telegram.org", "discord.com"}
NETLIFY_API = "https://api.netlify.com/api/v1"
PAGES_PUBLIC_URL = "https://example.pages.dev/"
ISO_TIME = r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})"
Resolver = Callable[[str], Sequence[str]]


class DeliveryError(Exception):
    """Public exception: a fixed safe code plus optional channel receipts."""
    def __init__(self, code: str, *, result: DeliveryResult | None = None):
        super().__init__(code)
        self.code = code
        self.result = result


def _pages_url(value: str | None = None) -> str:
    value = value or os.getenv("PUBLIC_REPORT_URL") or PAGES_PUBLIC_URL
    if not isinstance(value, str) or not re.fullmatch(
        r"https://[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.pages\.dev/", value
    ):
        raise DeliveryError("unsafe_public_url")
    return value


@dataclass(frozen=True, repr=False)
class DeliveryConfig:
    netlify_token: str
    site_id: str
    telegram_token: str
    telegram_chat_id: str
    discord_token: str
    discord_channel_id: str
    public_report_url: str = ""
    mirror_enabled: bool = False
    requested_mirror: bool = False
    include_html_attachment: bool = False

    def __repr__(self) -> str:
        return "DeliveryConfig(<redacted>)"

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> DeliveryConfig:
        env = os.environ if environ is None else environ
        names = ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID",
                 "DISCORD_BOT_TOKEN", "DISCORD_CHANNEL_ID")
        if any(not env.get(name) for name in names):
            raise DeliveryError("missing_config")
        if env.get("PUBLIC_REPORT_ATTACHMENT", "false") not in {"true", "false"}:
            raise DeliveryError("invalid_attachment_setting")
        public_url = _pages_url(env.get("PUBLIC_REPORT_URL") or PAGES_PUBLIC_URL)
        # Cloudflare Pages is the host. Netlify is opt-in and only ever a
        # best-effort mirror, so its credentials are required solely when it is
        # explicitly enabled - never as a precondition for publishing.
        mirror_enabled = env.get("NETLIFY_MIRROR_ENABLED", "") == "true"
        netlify_token = env.get("NETLIFY_AUTH_TOKEN", "")
        site_id = env.get("NETLIFY_SITE_ID", "")
        # A mirror enabled without its credentials is a degraded mirror, not a
        # failed publish: it must never reproduce the incident this replaced.
        mirror_configured = mirror_enabled and bool(netlify_token) and bool(site_id)
        if not mirror_configured:
            netlify_token = site_id = ""
        return cls(netlify_token, site_id, *(env[name] for name in names),
                   public_report_url=public_url, mirror_enabled=mirror_configured,
                   requested_mirror=mirror_enabled,
                   include_html_attachment=env.get("PUBLIC_REPORT_ATTACHMENT", "false") == "true")


@dataclass(frozen=True)
class ReportArtifact:
    data: bytes = field(repr=False)
    sha1: str
    sha256: str
    rendered_at: str
    observations: tuple[str, ...]
    source_counts: tuple[tuple[str, int], ...]


@dataclass(frozen=True)
class HostedReport:
    url: str
    sha256: str
    deploy_id: str


@dataclass(frozen=True)
class ChannelReceipt:
    channel: str
    message_id: str | None
    confirmation: str
    error_code: str | None = None

    @property
    def confirmed(self) -> bool:
        return self.confirmation in {"receipt-confirmed", "independently-read-back",
                                     "previously-confirmed"}


@dataclass(frozen=True)
class MirrorReceipt:
    """Best-effort secondary host. Never gates the required primary host.

    A deployed-but-uncredited mirror provider, an expired grant or a provider
    outage must not be able to withhold a report the primary host already
    published byte-verified.
    """
    url: str | None
    confirmation: str
    error_code: str | None = None

    @property
    def confirmed(self) -> bool:
        return self.confirmation == "mirror-verified"


@dataclass(frozen=True)
class DeliveryResult:
    hosted: HostedReport
    brief: str
    telegram: ChannelReceipt
    discord: ChannelReceipt
    mirror: MirrorReceipt | None = None

    @property
    def success(self) -> bool:
        return self.telegram.confirmed and self.discord.confirmed


def _is_valid_daily_source(source_id: str) -> bool:
    """Check if source_id belongs to approved daily source families."""
    approved = {"openrouter-models", "github-trending", "skills-hot", "unorouter-pricing"}
    return source_id in approved


def _load_daily_format(soup, data, text_html):
    """Strict daily-digest-v1 format validation branch."""
    # Must have meta kind
    kind_meta = soup.select_one('meta[name="elilain-report-kind" i]')
    if kind_meta is None or kind_meta.get('content') != 'daily-digest-v1':
        raise DeliveryError("invalid_artifact")

    # Must have generated-at meta
    gen_meta = soup.select_one('meta[name="elilain-generated-at" i]')
    if gen_meta is None:
        raise DeliveryError("invalid_artifact")
    generated_at = gen_meta.get('content', '')
    if not re.fullmatch(ISO_TIME, generated_at):
        raise DeliveryError("invalid_artifact")

    # Must have daily footer
    footer = soup.select_one("footer")
    footer_text = footer.get_text(" ", strip=True) if footer else ""
    if "Fresh public-source snapshot" not in footer_text:
        raise DeliveryError("invalid_artifact")

    # Extract generated_at from footer
    rendered = re.findall(ISO_TIME, footer_text)
    if not rendered:
        rendered = [generated_at]

    # Validate source families in cards
    source_counts = Counter()
    for article in soup.select("article[data-source]"):
        src = article.get("data-source", "")
        if src:
            source_counts[src] += 1

    for source in source_counts:
        if not _is_valid_daily_source(source):
            raise DeliveryError("invalid_artifact")

    # Count items (articles in main)
    item_count = len(soup.select("main article"))
    if item_count != sum(source_counts.values()):
        raise DeliveryError("invalid_artifact")

    observations = set()
    for node in soup.select("[data-observed-at]"):
        val = node.get("data-observed-at", "")
        if re.fullmatch(ISO_TIME, val):
            observations.add(val)

    # Discoveries repeats the selected cards; count their canonical copies once.
    source_counts = Counter(article["data-source"] for article in soup.select("#daily-digest article[data-source]"))
    return ReportArtifact(data, hashlib.sha1(data).hexdigest(), hashlib.sha256(data).hexdigest(),
                          rendered[0], tuple(sorted(observations)), tuple(sorted(source_counts.items())))


def load_artifact(export_dir: str | Path) -> ReportArtifact:
    directory = Path(export_dir)
    # Only the sanitized single-file export may be published. Deploying a
    # directory tree would expose state, databases or probe diagnostics.
    if (not directory.is_dir()
            or getattr(directory.lstat(), "st_file_attributes", 0) & 0x400
            or directory.is_symlink()
            or any(entry.name != "index.html" for entry in directory.iterdir())):
        raise DeliveryError("unexpected_files")
    index = directory / "index.html"
    if not index.is_file() or index.is_symlink() or getattr(index.lstat(), "st_file_attributes", 0) & 0x400:
        raise DeliveryError("unexpected_files")
    if index.stat().st_size > MAX_HTML_BYTES:
        raise DeliveryError("artifact_too_large")
    # Bound the read too, in case the file grows between stat() and open().
    with index.open("rb") as handle:
        data = handle.read(MAX_HTML_BYTES + 1)
    if len(data) > MAX_HTML_BYTES:
        raise DeliveryError("artifact_too_large")
    try:
        text_html = data.decode("utf-8")
        # Validate, but never rewrite, the bytes we are about to publish. Reuse
        # the exporter's boundary so relative/active dependencies fail too.
        # HTML parsing normalizes CRLF/CR before script CSP hashing. Preserve
        # the original artifact bytes; normalize only this validation copy.
        checked_html = _standalone_html(text_html.replace("\r\n", "\n").replace("\r", "\n"))
    except (UnicodeError, ValueError):
        raise DeliveryError("invalid_artifact") from None
    soup = BeautifulSoup(text_html, "html.parser")
    expected_policy = re.search(r'<meta http-equiv="Content-Security-Policy" content="([^"]+)">', checked_html)
    policies = soup.select('meta[http-equiv="Content-Security-Policy" i]')
    referrers = soup.select('meta[name="referrer" i]')
    # Both formats must pass the same policy and decoded privacy boundary.
    if (not expected_policy or len(policies) != 1
            or policies[0].get('content') != expected_policy[1]
            or len(referrers) != 1 or referrers[0].get('content') != 'no-referrer'):
        raise DeliveryError("invalid_artifact")
    # Detect sensitive content even when HTML entities conceal punctuation.
    # Credential SHAPES only: innocent catalog names containing token/password
    # or sk-matt must not be discarded as if they were credentials.
    from html import unescape
    private = re.compile(
        r"(?i)(?<![\w-])(?:sk-[A-Za-z0-9_-]{6,}|gh[pousr]_[A-Za-z0-9]{16,}"
        r"|github_pat_[A-Za-z0-9_]{20,}|cfat_[A-Za-z0-9_-]{20,}"
        r"|bearer\s+[A-Za-z0-9._~+/=-]{12,})"
        r"|\b(?:api[_-]?key|access[_-]?token|password|secret|token)\s*[=:]\s*['\"]?[^\s&<>'\"]{3,}"
        r"|\b[a-z]:[\\/]|file://|(?<![A-Za-z0-9/:])/(?:users|home|tmp|etc)/"
        r"|https?://(?:localhost(?:[/:.]|$)|127\.|0\.0\.0\.0|10\.|192\.168\.|172\.(?:1[6-9]|2\d|3[01])\.)"

    )
    if private.search(unescape(text_html)) or private.search(soup.get_text(" ")):
        raise DeliveryError("private_artifact")
    from .public_report import private_repository_ids
    if any(value in unescape(text_html).casefold() for value in private_repository_ids()):
        raise DeliveryError("private_artifact")
    kind_meta = soup.select_one('meta[name="elilain-report-kind" i]')
    if kind_meta is not None and kind_meta.get('content') == 'daily-digest-v1':
        return _load_daily_format(soup, data, text_html)

    # Legacy format validation; accepted bytes remain untouched.
    footer = soup.select_one("footer")
    text = footer.get_text(" ", strip=True) if footer else ""
    if "Public sanitized export of saved observations; not a fresh fetch." not in text:
        raise DeliveryError("invalid_artifact")
    rendered = re.findall(ISO_TIME, text)
    if len(rendered) != 1:
        raise DeliveryError("invalid_artifact")
    counts = Counter(row.get("data-source") for row in soup.select("#discoveries .discovery-item"))
    if any(source not in PUBLIC_SOURCES for source in counts):
        raise DeliveryError("invalid_artifact")
    observations = set()
    for node in soup.select("#discoveries .evidence li div"):
        match = re.fullmatch("observed_at: ?(" + ISO_TIME + ")", node.get_text(" ", strip=True))
        if match:
            observations.add(match[1])
    return ReportArtifact(data, hashlib.sha1(data).hexdigest(), hashlib.sha256(data).hexdigest(),
                          rendered[0], tuple(sorted(observations)), tuple(sorted(counts.items())))


def _attachment_verified(message: dict, artifact: ReportArtifact) -> bool:
    """A text confirmation is not file delivery: check the stored attachment."""
    attachments = message.get("attachments")
    if not isinstance(attachments, list):
        return False
    return any(isinstance(entry, dict) and entry.get("filename") == ATTACHMENT_NAME
               and entry.get("size") == len(artifact.data) for entry in attachments)


def load_receipts(state_path: str | Path | None) -> dict[str, dict[str, str]]:
    """Missing state is a baseline; corrupt state must not trigger repeat sends."""
    if state_path is None:
        return {}
    path = Path(state_path)
    try:
        with path.open("rb") as handle:
            raw = handle.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024:
            raise ValueError
        value = json.loads(raw)
        if not isinstance(value, dict) or not isinstance(value.get("artifacts"), dict):
            raise ValueError
        artifacts = value["artifacts"]
        for digest, record in artifacts.items():
            if not isinstance(digest, str) or not re.fullmatch(r"[a-f0-9]{64}", digest):
                raise ValueError
            if (not isinstance(record, dict) or not set(record) <= RECEIPT_FIELDS
                    or any(not isinstance(item, str) or len(item) > 2048 for item in record.values())):
                raise ValueError
            for channel, pattern in (("telegram", r"[1-9]\d{0,19}/[1-9]\d{0,19}"),
                                     ("telegram_text", r"[1-9]\d{0,19}"),
                                     ("telegram_link", r"[1-9]\d{0,19}"),
                                     ("discord_link", r"[1-9]\d{16,19}"),
                                     ("discord", r"[1-9]\d{16,19}")):
                identifier = record.get(channel, "")
                binding = record.get(channel + "_destination", "")
                if identifier and not re.fullmatch(pattern, identifier):
                    raise ValueError
                if binding and not re.fullmatch(r"[a-f0-9]{64}", binding):
                    raise ValueError
        return artifacts
    except FileNotFoundError:
        return {}
    except (ValueError, UnicodeError):
        raise DeliveryError("invalid_receipt_state") from None
    except OSError:
        raise DeliveryError("receipt_state_unreadable") from None


def _bounded_receipts(artifacts: dict[str, dict[str, str]], current: str) -> dict[str, dict[str, str]]:
    """Keep a finite replay window, pin incomplete fan-out and latest siblings.

    Never trade a partial confirmation for more history. If pinned records fill
    the budget, fail closed and require an operator decision, not duplicate sends.
    JSON insertion order is the recency clock (legacy sorted files are unordered
    history); subsequent writes promote the touched artifact to the newest slot.
    """
    protected = {current}
    for suffix in ("", "_link"):
        keys = ("telegram" + suffix, "discord" + suffix)
        for key in keys:
            latest = next((digest for digest in reversed(artifacts) if artifacts[digest].get(key)), None)
            if latest:
                protected.add(latest)
        for digest, record in artifacts.items():
            if bool(record.get(keys[0])) != bool(record.get(keys[1])):
                protected.add(digest)
            if not suffix and record.get("telegram_text") and not record.get("telegram"):
                protected.add(digest)
    if len(protected) > MAX_RECEIPT_ARTIFACTS:
        raise DeliveryError("receipt_capacity")
    keep = set(protected)
    for digest in reversed(artifacts):
        if len(keep) >= MAX_RECEIPT_ARTIFACTS:
            break
        keep.add(digest)
    return {digest: record for digest, record in artifacts.items() if digest in keep}


def _destination_bindings(config: DeliveryConfig) -> dict[str, str]:
    """Bind a receipt to where it was sent and which public link was verified.

    Deliberately independent of the Netlify site id: toggling the optional
    mirror must not invalidate a receipt for a byte-identical artifact and
    trigger a duplicate send.
    """
    destinations = {"telegram": (config.telegram_token, config.telegram_chat_id),
                    "discord": (config.discord_token, config.discord_channel_id)}
    if config.public_report_url:
        destinations = {channel: (*values, config.public_report_url)
                        for channel, values in destinations.items()}
    if not config.include_html_attachment:
        destinations = {channel: (*values, "link-only-v1") for channel, values in destinations.items()}
    return {channel: hashlib.sha256(json.dumps(values).encode("utf-8")).hexdigest()
            for channel, values in destinations.items()}


@contextmanager
def _receipt_lock(path: Path, timeout: float = 10.0):
    """Serialize concurrent receipt writers with an O_EXCL lock file.

    Without this the read-modify-write races: two runs each write a snapshot
    taken before the other's record landed, and the lost record means an already
    delivered artifact is announced a second time.
    """
    lock = path.with_name(path.name + ".lock")
    deadline = time.monotonic() + timeout
    while True:
        try:
            handle = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            if time.monotonic() >= deadline:
                # Never block a publish forever on a stale lock: fail closed
                # with a safe code rather than risk a duplicate announcement.
                raise DeliveryError("receipt_state_locked") from None
            time.sleep(0.05)
            continue
        else:
            os.close(handle)
            break
    try:
        yield
    finally:
        try:
            lock.unlink()
        except OSError:
            pass


def save_receipts(state_path: str | Path | None, artifact: ReportArtifact,
                  receipts: DeliveryResult, config: DeliveryConfig) -> None:
    """Record the delivery so an identical artifact is not announced twice."""
    if state_path is None:
        return
    path = Path(state_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    bindings = _destination_bindings(config)
    suffix = "" if config.include_html_attachment else "_link"
    record = {"url": receipts.hosted.url}
    for receipt in (receipts.telegram, receipts.discord):
        key = receipt.channel + suffix
        if receipt.confirmed:
            record[key] = receipt.message_id or ""
            record[key + "_destination"] = bindings[receipt.channel]
        elif (config.include_html_attachment and receipt.channel == "telegram"
              and receipt.message_id and re.fullmatch(r"[1-9]\d*", receipt.message_id)):
            record["telegram_text"] = receipt.message_id
            record["telegram_text_destination"] = bindings["telegram"]
    with _receipt_lock(path):
        # Read INSIDE the lock, so a concurrent writer's record is merged
        # rather than clobbered by a pre-lock snapshot.
        artifacts = load_receipts(state_path)
        previous = artifacts.pop(artifact.sha256, {})
        artifacts[artifact.sha256] = {**previous, **record}
        artifacts = _bounded_receipts(artifacts, artifact.sha256)
        temporary = path.with_name(path.name + ".tmp")
        try:
            with open(temporary, "w", encoding="utf-8") as handle:
                json.dump({"artifacts": artifacts}, handle)
                handle.flush()
                # Durability: os.replace is atomic, but without fsync of the
                # temp file a crash can leave the renamed file empty, which
                # fails to load and would re-announce a delivered report.
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        except OSError:
            try:
                temporary.unlink()
            except OSError:
                pass
            raise


def resolve_public_dns(host: str) -> Sequence[str]:
    return list({row[4][0] for row in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)})


def prefer_reachable(addresses: Sequence[str]) -> Sequence[str]:
    """Order validated addresses so the most likely-reachable one is pinned first.

    getaddrinfo ordering is platform-dependent: this Windows host returns IPv4
    only for these hosts, while a dual-stack CI runner can order an IPv6
    literal first and then fail to connect to it, surfacing as an opaque
    ConnectError. IPv4 is preferred because hosted/CDN edges reachable over it
    are the common case; IPv6 answers remain available as fallbacks.

    This is ordering only. Every address is still checked for being global by
    the caller, so the DNS safety rule is unchanged.
    """
    return sorted(addresses, key=lambda ip: ipaddress.ip_address(ip).version != 4)


class _PinnedTransport(httpx.BaseTransport):
    """Connect only to validated global IPs; preserve the hostname for TLS SNI.

    Pools are separated by hostname to avoid sharing IP-origin TLS connections
    between differently authenticated names. Test transports never use sockets.
    """
    def __init__(self, resolver: Resolver, transport: httpx.BaseTransport | None):
        self.resolver = resolver
        self.test_transport = transport
        self.transports: dict[str, httpx.HTTPTransport] = {}
        self.allowed_hosts = set(API_HOSTS)

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        host = request.url.host
        if host not in self.allowed_hosts or request.url.scheme != "https" or request.url.port not in (None, 443):
            raise DeliveryError("unsafe_url")
        addresses = self.resolver(host)
        if not addresses or any(not ipaddress.ip_address(ip).is_global for ip in addresses):
            raise DeliveryError("unsafe_dns")
        ordered = prefer_reachable(addresses)
        if _debug():
            log.debug("delivery pinned %s -> %s", host, ordered[0])
        pinned = httpx.Request(request.method, request.url.copy_with(host=ordered[0]),
                              headers=request.headers, stream=request.stream,
                              extensions={**request.extensions, "sni_hostname": host})
        inner = self.test_transport
        if inner is None:
            inner = self.transports.setdefault(host, httpx.HTTPTransport(verify=True, retries=0))
        return inner.handle_request(pinned)

    def close(self) -> None:
        for transport in self.transports.values():
            transport.close()
        if self.test_transport is not None:
            self.test_transport.close()


@contextmanager
def _quiet_http_logs():
    # httpx INFO logs contain the Telegram token in the request path. Never
    # enable wire logging inside this secret-bearing operation.
    # Disabling only the parent is NOT enough: `Logger.disabled` is consulted
    # per logger, and child loggers (httpcore.http11, h11._connection) do not
    # inherit it, so a descendant can still emit a token-bearing line. Cover the
    # whole tree by name prefix and force the level above every wire message.
    prefixes = ("httpx", "httpcore", "h11")
    names = {name for name in list(logging.root.manager.loggerDict)
             if any(name == prefix or name.startswith(prefix + ".") for prefix in prefixes)}
    names |= set(prefixes)
    previous = {name: (logging.getLogger(name).disabled, logging.getLogger(name).level)
                for name in names}
    for name in names:
        logger = logging.getLogger(name)
        logger.disabled = True
        logger.setLevel(logging.CRITICAL + 1)
    try:
        yield
    finally:
        for name, (disabled, level) in previous.items():
            logger = logging.getLogger(name)
            logger.disabled = disabled
            logger.setLevel(level)


class _Session:
    def __init__(self, config: DeliveryConfig | None, transport: _PinnedTransport, client: httpx.Client):
        self.config, self.transport, self.client = config, transport, client

    def request(self, method: str, url: str, *, limit: int = MAX_API_BYTES, **kwargs) -> bytes:
        try:
            with self.client.stream(method, url, follow_redirects=False, **kwargs) as response:
                if not 200 <= response.status_code < 300:
                    if _debug():
                        # Host and status only: the URL path carries the Telegram
                        # token, so it must never reach a log line.
                        log.debug("delivery %s %s -> %s", method,
                                  urlsplit(url).hostname, response.status_code)
                    raise DeliveryError("http_status")
                data = bytearray()
                for chunk in response.iter_bytes():
                    data.extend(chunk)
                    if len(data) > limit:
                        raise DeliveryError("response_too_large")
                return bytes(data)
        except DeliveryError:
            raise
        except Exception as error:
            # Never surface the response body or a token-bearing request URL.
            # httpx messages embed the request URL, which carries the token, so
            # only the exception CLASS is ever recorded - and only under
            # DELIVERY_DEBUG, which makes an otherwise opaque failure diagnosable.
            if _debug():
                log.debug("delivery request failed: %s", type(error).__name__)
            raise DeliveryError("request_failed") from None

    def api(self, method: str, url: str, **kwargs) -> dict:
        try:
            value = json.loads(self.request(method, url, **kwargs))
            if not isinstance(value, dict):
                raise ValueError
            return value
        except DeliveryError:
            raise
        except Exception as error:
            if _debug():
                log.debug("delivery api response invalid: %s", type(error).__name__)
            raise DeliveryError("invalid_response") from None

    def netlify(self, method: str, path: str, **kwargs) -> dict:
        return self.api(method, NETLIFY_API + path,
                        headers={"Authorization": "Bearer " + self.config.netlify_token,
                                 **kwargs.pop("headers", {})}, **kwargs)

    def host(self, artifact: ReportArtifact, attempts: int, interval: float,
             sleep: Callable[[float], None]) -> HostedReport:
        site = self.netlify("GET", f"/sites/{self.config.site_id}")
        if site.get("id") != self.config.site_id:
            raise DeliveryError("site_mismatch")
        created = self.netlify("POST", f"/sites/{self.config.site_id}/deploys",
                              json={"files": {"/index.html": artifact.sha1}, "draft": False})
        deploy = created["id"]
        if created.get("site_id") != self.config.site_id:
            raise DeliveryError("deploy_mismatch")
        if created.get("required"):
            self.netlify("PUT", f"/deploys/{deploy}/files/index.html", content=artifact.data,
                         headers={"Content-Type": "application/octet-stream"})
        for attempt in range(attempts):
            status = self.netlify("GET", f"/deploys/{deploy}")
            if status.get("id") != deploy or status.get("site_id") != self.config.site_id:
                raise DeliveryError("deploy_mismatch")
            if status.get("state") == "ready":
                break
            if attempt + 1 == attempts:
                raise DeliveryError("deploy_not_ready")
            sleep(interval)
        else:
            raise DeliveryError("deploy_not_ready")
        # Verify the canonical published URL, not the per-deploy alias:
        # Netlify answers 401 for `<deploy-id>--site.netlify.app` on a team site
        # even with no password protection, so reading back the alias turned a
        # successful deploy into an HTTP failure. `ssl_url` is the published
        # origin; the deploy id is checked above, so this is the same bytes.
        url = status["ssl_url"].rstrip("/") + "/"
        host = urlsplit(url).hostname
        if not host or not host.endswith(".netlify.app"):
            raise DeliveryError("unsafe_url")
        self.transport.allowed_hosts.add(host)
        actual = self.request("GET", url, limit=MAX_HTML_BYTES,
                              headers={"Accept": "text/html", "Cache-Control": "no-cache"})
        if hashlib.sha256(actual).hexdigest() != artifact.sha256:
            raise DeliveryError("html_mismatch")
        return HostedReport(url, artifact.sha256, deploy)

    def pages(self, artifact: ReportArtifact) -> HostedReport:
        # Only the configured Pages root; never redirects or credential paths.
        url = _pages_url(self.config.public_report_url if self.config else None)
        self.transport.allowed_hosts.add(urlsplit(url).hostname)
        actual = self.request("GET", url, limit=MAX_HTML_BYTES,
                              headers={"Accept": "text/html", "Cache-Control": "no-cache"})
        if hashlib.sha256(actual).hexdigest() != artifact.sha256:
            raise DeliveryError("html_mismatch")
        return HostedReport(url, artifact.sha256, "pages-byte-verified")

    def telegram(self, brief: str, artifact: ReportArtifact | None = None,
                 *, confirmed_text_id: str | None = None) -> ChannelReceipt:
        """Confirm the hosted-link brief, plus an explicitly requested file.

        Link-only is ordinary public delivery. Optional file delivery requires
        the correct filename, size and document ID. A previously confirmed brief
        can be resumed independently when only that optional document is missing.
        """
        # Tracks what the customer actually received. If the text send is
        # confirmed but the document send then fails, that id MUST survive into
        # the receipt: dropping it is what makes a retry re-send the brief and
        # duplicate the announcement.
        delivered_text_id: int | None = None
        try:
            base = "https://api.telegram.org/bot" + self.config.telegram_token
            if confirmed_text_id is not None:
                identifier = int(confirmed_text_id)
            else:
                response = self.api("POST", base + "/sendMessage",
                                    json={"chat_id": self.config.telegram_chat_id, "text": brief,
                                          "link_preview_options": {"is_disabled": True}})
                result = response.get("result", {})
                identifier = result.get("message_id")
                if (response.get("ok") is not True or type(identifier) is not int or identifier <= 0
                        or result.get("chat", {}).get("id") != int(self.config.telegram_chat_id)
                        or result.get("text") != brief):
                    raise DeliveryError("telegram_receipt_invalid")
            delivered_text_id = identifier
            if artifact is not None:
                document = self.api(
                    "POST", base + "/sendDocument",
                    data={"chat_id": self.config.telegram_chat_id, "caption": brief[:1024]},
                    files={"document": (ATTACHMENT_NAME, artifact.data, "text/html")})
                sent = document.get("result", {})
                if (document.get("ok") is not True
                        or type(sent.get("message_id")) is not int or sent["message_id"] <= 0
                        or sent.get("document", {}).get("file_name") != ATTACHMENT_NAME
                        or sent.get("document", {}).get("file_size") != len(artifact.data)
                        or sent.get("chat", {}).get("id") != int(self.config.telegram_chat_id)):
                    raise DeliveryError("telegram_attachment_unverified")
                identifier = f"{identifier}/{sent.get('message_id')}"
            return ChannelReceipt("telegram", str(identifier), "receipt-confirmed")
        except DeliveryError as exc:
            # Report exactly what landed: the brief id if the text was confirmed,
            # otherwise nothing. The error code still says the file is missing.
            return ChannelReceipt("telegram",
                                  str(delivered_text_id) if delivered_text_id else None,
                                  "unconfirmed", exc.code)

    def discord(self, brief: str, artifact: ReportArtifact | None = None) -> ChannelReceipt:
        """Send and independently read back the hosted-link message.

        An explicitly supplied artifact adds a secondary attachment; that mode
        also requires matching filename/size in both send receipt and readback.
        """
        try:
            base = "https://discord.com/api/v10/channels/" + self.config.discord_channel_id + "/messages"
            headers = {"Authorization": "Bot " + self.config.discord_token}
            payload = {"content": brief, "allowed_mentions": {"parse": [], "replied_user": False}}
            if artifact is not None:
                sent = self.api(
                    "POST", base, headers=headers, data={"payload_json": json.dumps(payload)},
                    files={"files[0]": (ATTACHMENT_NAME, artifact.data, "text/html")})
                if not _attachment_verified(sent, artifact):
                    raise DeliveryError("discord_attachment_unverified")
            else:
                sent = self.api("POST", base, headers=headers, json=payload)
            identifier = sent.get("id")
            if (not isinstance(identifier, str) or not re.fullmatch(r"[1-9]\d{16,19}", identifier)
                    or sent.get("channel_id") != self.config.discord_channel_id or sent.get("content") != brief):
                raise DeliveryError("discord_receipt_invalid")
            actual = self.api("GET", base + "/" + identifier, headers=headers)
            if (actual.get("id") != identifier or actual.get("channel_id") != self.config.discord_channel_id
                    or actual.get("content") != brief):
                raise DeliveryError("discord_readback_invalid")
            if artifact is not None and not _attachment_verified(actual, artifact):
                raise DeliveryError("discord_attachment_unverified")
            return ChannelReceipt("discord", identifier, "independently-read-back")
        except DeliveryError as exc:
            return ChannelReceipt("discord", None, "unconfirmed", exc.code)


def verify_pages_report(export_dir: str | Path, *,
                        transport: httpx.BaseTransport | None = None,
                        resolver: Resolver = resolve_public_dns) -> HostedReport:
    """Read back this owned Pages origin only. No deployment, credentials or sends."""
    artifact = load_artifact(export_dir)
    pinned = _PinnedTransport(resolver, transport)
    try:
        with _quiet_http_logs(), httpx.Client(transport=pinned, timeout=20.0,
                                             trust_env=False, follow_redirects=False) as client:
            return _Session(None, pinned, client).pages(artifact)
    except DeliveryError:
        raise
    except Exception:
        raise DeliveryError("readback_failed") from None


def build_brief(artifact: ReportArtifact, hosted: HostedReport) -> str:
    observed = "not supplied" if not artifact.observations else artifact.observations[0]
    if len(artifact.observations) > 1:
        observed += " to " + artifact.observations[-1]
    counts = "; ".join(f"{source}: {count}" for source, count in artifact.source_counts) or "none"
    return (f"Open the public dashboard: {hosted.url}\nRender: {artifact.rendered_at}\nSaved observations: {observed}\n"
            f"Exported discoveries: {sum(count for _, count in artifact.source_counts)} ({counts}).\n"
            "Saved data, not a fresh fetch. Listings are metadata only; inference, free entitlement, "
            "and tool/skill suitability are unverified. Provider statuses, changes, actions and news excluded.\n"
            "Hosted page byte-verified before sharing.")


def deliver_report(export_dir: str | Path, *, environ: Mapping[str, str] | None = None,
                   transport: httpx.BaseTransport | None = None, resolver: Resolver = resolve_public_dns,
                   poll_attempts: int = 30, poll_interval: float = 1.0,
                   sleep: Callable[[float], None] = time.sleep,
                   state_path: str | Path | None = None) -> DeliveryResult:
    """Require verified Cloudflare Pages hosting and BOTH channels.

    Cloudflare Pages is the only host. Netlify is an opt-in, best-effort mirror
    (``NETLIFY_MIRROR_ENABLED=true``) whose credentials are required only when
    enabled; it is never a precondition for publishing, and its failure can
    never withhold a report the primary host already published byte-verified.

    An exception with .result records per-channel outcomes; Telegram's official
    sendMessage receipt is not an independent message readback. No send retries.
    transport/resolver/sleep injection is for deterministic protocol tests only.

    ``state_path`` records what was delivered. Re-running with byte-identical
    content re-verifies the hosted bytes, then reports the previous
    confirmation instead of announcing the same report twice; a changed
    artifact always notifies.
    """
    config = DeliveryConfig.from_env(environ)
    artifact = load_artifact(export_dir)
    # Validate restored receipt state before the first external side effect.
    restored_receipts = load_receipts(state_path)
    _bounded_receipts(restored_receipts, artifact.sha256)
    pinned = _PinnedTransport(resolver, transport)
    try:
        with _quiet_http_logs(), httpx.Client(transport=pinned, timeout=20.0, trust_env=False,
                                              follow_redirects=False) as client:
            session = _Session(config, pinned, client)
            # The required host is verified BEFORE any third-party deploy, so a
            # run that will abort never creates one.
            hosted = session.pages(artifact)
            mirror: MirrorReceipt | None = None
            if config.requested_mirror and not config.mirror_enabled:
                log.warning("netlify mirror unavailable: %s", "missing_config")
                mirror = MirrorReceipt(None, "mirror-unavailable", "missing_config")
            if config.mirror_enabled:
                # Opt-in secondary host only. Catches Exception, not just
                # DeliveryError: a 2xx body missing "id"/"ssl_url" raises
                # KeyError inside host(), and that must stay in this block rather
                # than aborting a report the primary host already published.
                try:
                    mirrored = session.host(artifact, poll_attempts, poll_interval, sleep)
                except DeliveryError as error:
                    # Safe code only, never the provider's message or a token.
                    log.warning("netlify mirror unavailable: %s", error.code)
                    mirror = MirrorReceipt(None, "mirror-unavailable", error.code)
                except Exception:
                    log.warning("netlify mirror unavailable: %s", "mirror_failed")
                    mirror = MirrorReceipt(None, "mirror-unavailable", "mirror_failed")
                else:
                    mirror = MirrorReceipt(mirrored.url, "mirror-verified")
            brief = build_brief(artifact, hosted)
            already = restored_receipts.get(artifact.sha256) or {}
            bindings = _destination_bindings(config)
            suffix = "" if config.include_html_attachment else "_link"
            tg_key, dc_key = "telegram" + suffix, "discord" + suffix
            telegram = (ChannelReceipt("telegram", already[tg_key], "previously-confirmed")
                        if already.get(tg_key) and already.get(tg_key + "_destination") == bindings["telegram"]
                        else session.telegram(brief, artifact if config.include_html_attachment else None,
                            confirmed_text_id=(already.get("telegram_text")
                                if config.include_html_attachment and already.get("telegram_text_destination") == bindings["telegram"]
                                else None)))
            discord = (ChannelReceipt("discord", already[dc_key], "previously-confirmed")
                       if already.get(dc_key) and already.get(dc_key + "_destination") == bindings["discord"]
                       else session.discord(brief, artifact if config.include_html_attachment else None))
            result = DeliveryResult(hosted, brief, telegram, discord, mirror)
            # Persist confirmed channels even when their sibling fails. A later
            # explicit rerun must not repeat a previously confirmed upload.
            save_receipts(state_path, artifact, result, config)
            if not result.success:
                raise DeliveryError("channel_failure", result=result)
            return result
    except DeliveryError:
        raise
    except Exception:
        raise DeliveryError("delivery_failed") from None
