"""Deterministic protocol tests. MockTransport is not a live deployment."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import importlib.util
import json
from pathlib import Path

import httpx
import pytest

SITE = "3970e0fe-8564-4903-9a55-c5f8de49fb8b"
DEPLOY = "abc123"
# The bot token appears in the request PATH, so it must never be logged or
# asserted on as a literal in failure messages. Protocol stubs reference the
# path segment through this alias instead of the raw secret value.
# Cloudflare Pages is the only host. Netlify credentials are deliberately absent
# here: the default delivery path must need no Netlify configuration at all.
ENV = {
    "TELEGRAM_BOT_TOKEN": "123456:TEST_TELEGRAM_TOKEN",
    "TELEGRAM_CHAT_ID": "-1001234567890",
    "DISCORD_BOT_TOKEN": "TEST_DISCORD_TOKEN",
    "DISCORD_CHANNEL_ID": "123456789012345678",
}
ENV["TELEGRAM_TOKEN_PATH"] = ENV["TELEGRAM_BOT_TOKEN"]
URL = "https://example.pages.dev/"
URL_HOST = "example.pages.dev"
# Only used by the explicit opt-in mirror tests.
MIRROR_URL = "https://report-fixture.netlify.app/"
MIRROR_URL_HOST = "report-fixture.netlify.app"
# Netlify answers 401 for this per-deploy alias on a team site, so the canonical
# ssl_url above is what must be read back. The real deploy payload carries both.
ALIAS = "https://abc123--report-fixture.netlify.app/"
ALIAS_HOST = "abc123--report-fixture.netlify.app"
MIRROR_ENV = dict(ENV, NETLIFY_AUTH_TOKEN="test-netlify-token", NETLIFY_SITE_ID=SITE,
                  NETLIFY_MIRROR_ENABLED="true")


def parse_multipart(request):
    """Split a multipart/form-data body into plain fields and file parts.

    Returns ``(fields, files)`` where each file is ``(filename, bytes,
    reported_name)``. Used to assert the exact published bytes are uploaded,
    rather than trusting a mocked 200 response.
    """
    boundary = request.headers["content-type"].split("boundary=", 1)[1].encode()
    fields, files = {}, {}
    for part in request.content.split(b"--" + boundary):
        if not part.strip() or part.strip() == b"--":
            continue
        head, _, body = part.partition(b"\r\n\r\n")
        body = body.rstrip(b"\r\n-")
        headers = {}
        for line in head.decode("latin-1").split("\r\n")[1:]:
            key, _, value = line.partition(":")
            headers[key.strip().lower()] = value.strip()
        disposition = headers.get("content-disposition", "")
        name = disposition.split('name="', 1)[1].split('"', 1)[0] if 'name="' in disposition else ""
        if "filename=" in disposition:
            reported = disposition.split('filename="', 1)[1].split('"', 1)[0]
            files[name] = (reported, body, name.encode())
        else:
            fields[name] = body.decode()
    return fields, files


def module():
    assert importlib.util.find_spec("intel_sources.report_delivery") is not None, "verified delivery module is missing"
    from intel_sources import report_delivery
    return report_delivery


@pytest.fixture
def artifact_dir(tmp_path):
    from intel_sources.public_report import build_public_payload, _standalone_html
    from tracker_core.reporter_html import LainReporter
    state = {"sources": [{"name": "github-trending", "status": "ok", "checked_at": "2026-09-30T10:00:00+00:00"}],
             "items": [{"source": "github-trending", "id": "github:example/tool", "headline": "Safe tool",
                        "url": "https://github.com/example/tool", "checked_at": "2026-09-30T10:00:00+00:00"}]}
    payload = build_public_payload(state, now=datetime(2026, 10, 1, 12, tzinfo=timezone.utc))
    directory = tmp_path / "public"
    directory.mkdir()
    (directory / "index.html").write_text(_standalone_html(LainReporter().render(**payload)), encoding="utf-8")
    return directory


class Protocol:
    def __init__(self, data):
        self.data = data
        self.calls = []
        self.brief = None
        self.override = None

    def __call__(self, request):
        self.calls.append(request)
        # The real pipeline pins DNS to a global IP, retaining Host and TLS SNI.
        assert request.url.host == "8.8.8.8"
        host = request.headers["host"]
        assert request.extensions["sni_hostname"] == host
        if self.override:
            response = self.override(request)
            if response is not None:
                return response
        path = request.url.path
        if host == URL_HOST:
            assert request.method == "GET" and path == "/"
            assert "authorization" not in request.headers
            assert "cookie" not in request.headers
            return httpx.Response(200, content=self.data, headers={"content-type": "text/html; charset=utf-8"})
        if host == "api.netlify.com":
            assert request.headers["authorization"] == "Bearer " + MIRROR_ENV["NETLIFY_AUTH_TOKEN"]
            if request.method == "GET" and path == f"/api/v1/sites/{SITE}":
                return httpx.Response(200, json={"id": SITE, "name": "report-fixture"})
            if request.method == "POST" and path == f"/api/v1/sites/{SITE}/deploys":
                body = json.loads(request.content)
                assert body["files"] == {"/index.html": hashlib.sha1(self.data).hexdigest()}
                assert body["draft"] is False
                assert not body.get("functions")
                return httpx.Response(201, json={"id": DEPLOY, "site_id": SITE,
                                              "required": [hashlib.sha1(self.data).hexdigest()], "required_functions": []})
            if request.method == "PUT" and path == f"/api/v1/deploys/{DEPLOY}/files/index.html":
                assert request.content == self.data
                assert request.headers["content-type"] == "application/octet-stream"
                return httpx.Response(200, json={"path": "/index.html"})
            if request.method == "GET" and path == f"/api/v1/deploys/{DEPLOY}":
                return httpx.Response(200, json={"id": DEPLOY, "site_id": SITE, "state": "ready",
                                              "ssl_url": MIRROR_URL.rstrip("/"),
                                              "deploy_ssl_url": ALIAS.rstrip("/")})
        if host in (MIRROR_URL_HOST, ALIAS_HOST):
            assert request.method == "GET" and path == "/"
            assert "authorization" not in request.headers
            assert "cookie" not in request.headers
            return httpx.Response(200, content=self.data, headers={"content-type": "text/html; charset=utf-8"})
        if host == "api.telegram.org":
            assert request.method == "POST"
            if path == "/bot" + ENV["TELEGRAM_TOKEN_PATH"] + "/sendMessage":
                body = json.loads(request.content)
                self.brief = body["text"]
                assert body["chat_id"] == ENV["TELEGRAM_CHAT_ID"]
                assert "parse_mode" not in body
                assert body["link_preview_options"] == {"is_disabled": True}
                return httpx.Response(200, json={"ok": True, "result": {"message_id": 42,
                                              "chat": {"id": int(ENV["TELEGRAM_CHAT_ID"])}, "text": self.brief}})
            if path == "/bot" + ENV["TELEGRAM_TOKEN_PATH"] + "/sendDocument":
                fields, files = parse_multipart(request)
                assert fields["chat_id"] == ENV["TELEGRAM_CHAT_ID"]
                assert files["document"][0] == "index.html"
                assert files["document"][1] == self.data
                self.brief = fields["caption"]
                return httpx.Response(200, json={"ok": True, "result": {"message_id": 43,
                                              "chat": {"id": int(ENV["TELEGRAM_CHAT_ID"])},
                                              "text": self.brief,
                                              "document": {"file_name": files["document"][0],
                                                            "file_size": len(self.data)}}})
        if host == "discord.com":
            assert request.headers["authorization"] == "Bot " + ENV["DISCORD_BOT_TOKEN"]
            if request.method == "POST" and path == "/api/v10/channels/" + ENV["DISCORD_CHANNEL_ID"] + "/messages":
                if request.headers["content-type"].startswith("application/json"):
                    payload = json.loads(request.content)
                    assert payload["allowed_mentions"] == {"parse": [], "replied_user": False}
                    self.brief = payload["content"]
                else:
                    fields, files = parse_multipart(request)
                    self.brief = json.loads(fields["payload_json"])["content"]
                    assert files["files[0]"][0] == "index.html"
                    assert files["files[0]"][1] == self.data
                return httpx.Response(200, json={"id": "999999999999999999", "channel_id": ENV["DISCORD_CHANNEL_ID"],
                                                  "content": self.brief,
                                                  "attachments": [{"filename": "index.html",
                                                                   "size": len(self.data)}]})
            if request.method == "GET" and path == "/api/v10/channels/" + ENV["DISCORD_CHANNEL_ID"] + "/messages/999999999999999999":
                return httpx.Response(200, json={"id": "999999999999999999", "channel_id": ENV["DISCORD_CHANNEL_ID"],
                                                  "content": self.brief,
                                                  "attachments": [{"filename": "index.html",
                                                                   "size": len(self.data)}]})
        raise AssertionError("Unexpected protocol request")


class Guard(Protocol):
    def __init__(self, data, respond):
        super().__init__(data)
        self.respond = respond

    def __call__(self, request):
        override = self.respond(request)
        if not override:
            return super().__call__(request)
        self.calls.append(request)
        return override


def mirror_session(protocol):
    """Drive the opt-in Netlify mirror directly.

    The mirror is best-effort by design, so its internal guards are proven at
    the ``session.host()`` seam rather than through ``deliver_report``, where a
    mirror failure is now correctly absorbed instead of aborting the run.
    """
    from intel_sources.report_delivery import DeliveryConfig, _PinnedTransport, _Session

    config = DeliveryConfig(netlify_token=MIRROR_ENV["NETLIFY_AUTH_TOKEN"], site_id=SITE,
                            telegram_token="t", telegram_chat_id="1",
                            discord_token="d", discord_channel_id="2",
                            mirror_enabled=True)
    pinned = _PinnedTransport(lambda host: ["8.8.8.8"], httpx.MockTransport(protocol))
    client = httpx.Client(transport=pinned, timeout=20.0, trust_env=False, follow_redirects=False)
    return _Session(config, pinned, client), client


def run(directory, protocol, **kwargs):
    return module().deliver_report(directory, environ=ENV, transport=httpx.MockTransport(protocol),
                                   resolver=lambda host: ["8.8.8.8"], sleep=lambda seconds: None, **kwargs)


def test_missing_config_fails_closed_before_any_request(artifact_dir):
    protocol = Protocol((artifact_dir / "index.html").read_bytes())
    env = dict(ENV, DISCORD_CHANNEL_ID="")
    with pytest.raises(module().DeliveryError) as caught:
        module().deliver_report(artifact_dir, environ=env,
                                transport=httpx.MockTransport(protocol), resolver=lambda host: ["8.8.8.8"])
    assert caught.value.code == "missing_config"
    assert protocol.calls == []


def test_unexpected_files_in_export_directory_fail_closed(artifact_dir, tmp_path):
    directory = tmp_path / "leaky"
    directory.mkdir()
    (directory / "index.html").write_text((artifact_dir / "index.html").read_text(encoding="utf-8"), encoding="utf-8")
    (directory / "news.db").write_bytes(b"PRIVATE_DB_CANARY")
    protocol = Protocol((artifact_dir / "index.html").read_bytes())
    with pytest.raises(module().DeliveryError) as caught:
        module().deliver_report(directory, environ=ENV, transport=httpx.MockTransport(protocol),
                                resolver=lambda host: ["8.8.8.8"])
    assert caught.value.code == "unexpected_files"
    assert protocol.calls == []


def test_deployed_html_mismatch_stops_before_any_notification(artifact_dir):
    data = (artifact_dir / "index.html").read_bytes()
    protocol = Guard(data, lambda r: r.headers["host"] == URL_HOST
                     and httpx.Response(200, content=data + b"<!-- tampered -->"))
    with pytest.raises(module().DeliveryError) as caught:
        run(artifact_dir, protocol)
    assert caught.value.code == "html_mismatch"
    assert protocol.brief is None
    assert not any(r.headers["host"] in {"api.telegram.org", "discord.com"} for r in protocol.calls)


def test_redirecting_hosted_url_is_not_followed(artifact_dir):
    data = (artifact_dir / "index.html").read_bytes()
    protocol = Guard(data, lambda r: r.headers["host"] == URL_HOST
                     and httpx.Response(302, headers={"location": "https://example.com/"}))
    with pytest.raises(module().DeliveryError) as caught:
        run(artifact_dir, protocol)
    assert caught.value.code == "http_status"


def test_private_or_foreign_hosted_url_is_rejected(artifact_dir):
    data = (artifact_dir / "index.html").read_bytes()
    protocol = Guard(data, lambda r: r.headers["host"] == "api.netlify.com"
                     and r.url.path == "/api/v1/deploys/" + DEPLOY
                     and httpx.Response(200, json={"id": DEPLOY, "site_id": SITE, "state": "ready",
                                                   "ssl_url": "https://evil.example.com/"}))
    session, client = mirror_session(protocol)
    try:
        with pytest.raises(module().DeliveryError) as caught:
            session.host(module().load_artifact(artifact_dir), 2, 0.0, lambda seconds: None)
    finally:
        client.close()
    assert caught.value.code == "unsafe_url"


def test_non_global_dns_answer_is_rejected(artifact_dir):
    data = (artifact_dir / "index.html").read_bytes()
    protocol = Protocol(data)
    with pytest.raises(module().DeliveryError) as caught:
        module().deliver_report(artifact_dir, environ=ENV, transport=httpx.MockTransport(protocol),
                                resolver=lambda host: ["127.0.0.1"])
    assert caught.value.code == "unsafe_dns"
    assert protocol.calls == []


def test_partial_channel_failure_fails_closed_with_receipts(artifact_dir):
    data = (artifact_dir / "index.html").read_bytes()
    protocol = Guard(data, lambda r: r.headers["host"] == "discord.com" and r.method == "POST"
                     and httpx.Response(403, json={"message": "PRIVATE_DISCORD_ERROR_CANARY"}))
    with pytest.raises(module().DeliveryError) as caught:
        run(artifact_dir, protocol)
    assert caught.value.code == "channel_failure"
    result = caught.value.result
    assert result.telegram.confirmation == "receipt-confirmed"
    assert result.discord.confirmation == "unconfirmed"
    assert result.discord.error_code == "http_status"
    assert result.success is False
    # No retry: exactly one Discord attempt and no second Telegram post.
    assert [r.method for r in protocol.calls if r.headers["host"] == "discord.com"] == ["POST"]


def test_discord_readback_mismatch_is_not_reported_as_delivered(artifact_dir):
    data = (artifact_dir / "index.html").read_bytes()
    protocol = Guard(data, lambda r: r.headers["host"] == "discord.com" and r.method == "GET"
                     and httpx.Response(200, json={"id": "999999999999999999",
                                                   "channel_id": ENV["DISCORD_CHANNEL_ID"], "content": "PRIVATE_EDIT_CANARY"}))
    with pytest.raises(module().DeliveryError) as caught:
        run(artifact_dir, protocol)
    assert caught.value.code == "channel_failure"
    assert caught.value.result.discord.confirmation == "unconfirmed"
    assert caught.value.result.discord.error_code == "discord_readback_invalid"


def test_telegram_without_official_readback_is_receipt_confirmed_only(artifact_dir):
    data = (artifact_dir / "index.html").read_bytes()
    protocol = Protocol(data)
    result = run(artifact_dir, protocol)
    assert result.telegram.confirmation == "receipt-confirmed"
    assert not any(r.headers["host"] == "api.telegram.org" and r.method == "GET" for r in protocol.calls)


def test_brief_stays_within_limit_and_carries_no_markdown_or_unsafe_text(artifact_dir):
    data = (artifact_dir / "index.html").read_bytes()
    result = run(artifact_dir, Protocol(data))
    brief = result.brief
    assert len(brief) < 1500
    assert "PRIVATE_" not in brief and "://github.com" not in brief
    assert not any(mark in brief for mark in ("*", "_", "`", "[", "#", "<"))
    assert brief.count(URL) == 1


def test_secrets_never_appear_in_errors_or_receipts(artifact_dir, caplog):
    data = (artifact_dir / "index.html").read_bytes()
    protocol = Guard(data, lambda r: r.headers["host"] == "api.telegram.org"
                     and httpx.Response(401, json={"description": "PRIVATE_TELEGRAM_ERROR_CANARY"}))
    with pytest.raises(module().DeliveryError) as caught:
        with caplog.at_level("DEBUG"):
            run(artifact_dir, protocol)
    combined = str(caught.value) + str(caught.value.result) + repr(caught.value.result) + caplog.text
    assert caught.value.code == "channel_failure"
    assert caught.value.result.telegram.error_code == "http_status"
    for secret in ENV.values():
        assert secret not in combined
    for canary in ("PRIVATE_TELEGRAM_ERROR_CANARY", "PRIVATE_DISCORD_ERROR_CANARY"):
        assert canary not in combined


def test_oversized_hosted_response_is_refused(artifact_dir):
    data = (artifact_dir / "index.html").read_bytes()
    protocol = Guard(data, lambda r: r.headers["host"] == URL_HOST
                     and httpx.Response(200, content=b"x" * (4 * 1024 * 1024 + 1)))
    with pytest.raises(module().DeliveryError) as caught:
        run(artifact_dir, protocol)
    assert caught.value.code == "response_too_large"


def test_deploy_state_is_polled_with_backoff_and_never_assumed_ready(artifact_dir):
    data = (artifact_dir / "index.html").read_bytes()
    states = iter(["preparing", "processing"])
    protocol = Guard(data, lambda r: r.headers["host"] == "api.netlify.com"
                     and r.url.path == "/api/v1/deploys/" + DEPLOY
                     and httpx.Response(200, json={"id": DEPLOY, "site_id": SITE, "state": next(states),
                                                   "deploy_ssl_url": MIRROR_URL.rstrip("/")}))
    slept = []
    session, client = mirror_session(protocol)
    try:
        with pytest.raises(module().DeliveryError) as caught:
            session.host(module().load_artifact(artifact_dir), 2, 1.0, slept.append)
    finally:
        client.close()
    assert caught.value.code == "deploy_not_ready"
    assert slept == [1.0]
    assert not any(r.headers["host"] in {"api.telegram.org", "discord.com"} for r in protocol.calls)


def test_foreign_deploy_id_in_status_is_rejected(artifact_dir):
    data = (artifact_dir / "index.html").read_bytes()
    protocol = Guard(data, lambda r: r.headers["host"] == "api.netlify.com"
                     and r.url.path == "/api/v1/deploys/" + DEPLOY
                     and httpx.Response(200, json={"id": "PRIVATE_DEPLOY_CANARY", "site_id": SITE,
                                                   "state": "ready", "deploy_ssl_url": MIRROR_URL.rstrip("/")}))
    session, client = mirror_session(protocol)
    try:
        with pytest.raises(module().DeliveryError) as caught:
            session.host(module().load_artifact(artifact_dir), 2, 0.0, lambda seconds: None)
    finally:
        client.close()
    assert caught.value.code == "deploy_mismatch"
    assert "PRIVATE_DEPLOY_CANARY" not in str(caught.value)


def test_real_sanitized_export_delivers_a_bounded_brief(artifact_dir, tmp_path_factory):
    real = Path(__file__).parent.parent / "public"
    if not (real / "index.html").is_file():
        pytest.skip("no exported public/index.html in this worktree")
    directory = tmp_path_factory.mktemp("real") / "public"
    directory.mkdir()
    data = (real / "index.html").read_bytes()
    (directory / "index.html").write_bytes(data)
    if len(list(directory.iterdir())) != 1:
        pytest.skip("export directory holds more than the single sanitized file")
    result = run(directory, Protocol(data))
    assert result.hosted.sha256 == hashlib.sha256(data).hexdigest()
    assert len(result.brief) < 1500
    assert result.brief.count(URL) == 1
    assert "metadata only" in result.brief and "not a fresh fetch" in result.brief


def test_cli_reports_safe_failure_code_and_no_credentials(artifact_dir, capsys, monkeypatch):
    import importlib.util
    spec = importlib.util.spec_from_file_location("publish_report_cli", Path(__file__).parent.parent / "scripts/publish_report.py")
    assert spec is not None, "publish_report CLI is missing"
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    monkeypatch.setenv("NETLIFY_AUTH_TOKEN", "cli-netlify-secret")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "cli-telegram-secret")
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "cli-discord-secret")
    monkeypatch.setattr(cli, "deliver_report", lambda *a, **k: (_ for _ in ()).throw(
        cli.DeliveryError("channel_failure")))
    code = cli.main(["--export-dir", str(artifact_dir), "--json"])
    captured = capsys.readouterr()
    assert code == 1
    assert "channel_failure" in captured.out
    assert "secret" not in captured.out + captured.err


def test_single_file_digest_readback_then_both_channel_receipts(artifact_dir):
    data = (artifact_dir / "index.html").read_bytes()
    protocol = Protocol(data)
    result = run(artifact_dir, protocol)
    assert result.success is True
    assert result.hosted.url == URL
    assert result.hosted.sha256 == hashlib.sha256(data).hexdigest()
    assert result.telegram.confirmation == "receipt-confirmed"
    # Ordinary R08 public delivery sends a hosted link, not a document.
    assert result.telegram.message_id == "42"
    assert result.discord.confirmation == "independently-read-back"
    assert result.discord.message_id == "999999999999999999"
    assert len(result.brief) < 1500
    assert "Render: 2026-10-01T12:00:00+00:00" in result.brief
    assert "Saved observations: 2026-09-30T10:00:00+00:00" in result.brief
    assert "github-trending: 1" in result.brief
    assert "metadata only" in result.brief
    assert "not a fresh fetch" in result.brief
    assert URL in result.brief
    assert [r.headers["host"] for r in protocol.calls] == [
        URL_HOST, "api.telegram.org", "discord.com", "discord.com"], \
        "the default path must contact Cloudflare Pages and the channels, never Netlify"
    assert (artifact_dir / "index.html").read_bytes() == data


def test_unexpected_transport_exception_is_reported_by_class_only(monkeypatch, caplog):
    """A swallowed exception must still be diagnosable without leaking the URL.

    The delivery path deliberately replaces every unexpected exception with a
    safe `request_failed` code, which made a real CI failure impossible to
    diagnose. When DELIVERY_DEBUG is set, the exception *class name* is logged
    so the failure is identifiable; the message and URL never are, because
    httpx messages embed the token-bearing request URL.
    """
    import logging

    from intel_sources.report_delivery import DeliveryError, _Session, _quiet_http_logs

    class Exploding:
        def stream(self, *args, **kwargs):
            raise RuntimeError("connect failed for https://api.telegram.org/bot123:SECRET/")

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    session = _Session(None, None, Exploding())
    monkeypatch.setenv("DELIVERY_DEBUG", "1")
    with caplog.at_level(logging.DEBUG, logger="intel_sources.report_delivery"):
        with _quiet_http_logs():
            with pytest.raises(DeliveryError) as caught:
                session.request("GET", "https://api.telegram.org/bot123:SECRET/sendMessage")
    assert caught.value.code == "request_failed"
    logged = caplog.text
    assert "RuntimeError" in logged
    assert "SECRET" not in logged
    assert "SECRET" not in str(caught.value)


def test_debug_log_identifies_host_and_status_without_token(monkeypatch, caplog):
    """The failure record must say WHICH host answered with WHICH status.

    Knowing only that an exception occurred left a real deploy failure
    undiagnosable. Log method, hostname and status code - never the path,
    which carries the Telegram token, and never the body.
    """
    import logging

    from intel_sources.report_delivery import DeliveryError, _Session, _quiet_http_logs

    class Response:
        status_code = 404

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def iter_bytes(self):
            return iter([b""])

    class Client:
        def stream(self, method, url, **kwargs):
            return Response()

    session = _Session(None, None, Client())
    monkeypatch.setenv("DELIVERY_DEBUG", "1")
    with caplog.at_level(logging.DEBUG, logger="intel_sources.report_delivery"):
        with _quiet_http_logs():
            with pytest.raises(DeliveryError) as caught:
                session.request("GET", "https://api.telegram.org/bot123:SECRET/sendMessage")
    assert caught.value.code == "http_status"
    logged = caplog.text
    assert "api.telegram.org" in logged, "the failing host must be identifiable"
    assert "404" in logged, "the failing status must be identifiable"
    assert "SECRET" not in logged
    assert "/bot123" not in logged, "the token-bearing path must never be logged"


def test_debug_log_emits_even_when_root_logger_is_at_warning(monkeypatch, caplog):
    """DEBUG must survive a default WARNING root logger.

    The first attempt at host/status logging never appeared in CI at all:
    `log.debug()` on a logger with no level of its own inherits the root
    logger's WARNING, so the line was silently dropped and the failure stayed
    opaque. The delivery logger must set its own level under DELIVERY_DEBUG.
    """
    import logging

    from intel_sources.report_delivery import DeliveryError, _Session, _quiet_http_logs, log

    class Response:
        status_code = 503

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def iter_bytes(self):
            return iter([b""])

    class Client:
        def stream(self, method, url, **kwargs):
            return Response()

    root = logging.getLogger()
    previous = root.level
    root.setLevel(logging.WARNING)
    try:
        monkeypatch.setenv("DELIVERY_DEBUG", "1")
        session = _Session(None, None, Client())
        with caplog.at_level(logging.DEBUG, logger="intel_sources.report_delivery"):
            with _quiet_http_logs():
                with pytest.raises(DeliveryError):
                    session.request("GET", "https://example.netlify.app/")
        # Assert the emitted record, not logger internals: pytest's caplog
        # context resets handler levels on exit, so `log.level` afterwards is
        # not evidence of anything.
        records = [r for r in caplog.records if r.name == log.name]
        assert records, "the delivery logger must emit a record"
        assert records[-1].getMessage() == (
            "delivery GET example.netlify.app -> 503")
    finally:
        root.setLevel(previous)


def test_pinned_address_prefers_reachable_ipv4_and_reports_families():
    """Pin a reachable address, and prefer IPv4 when a host is dual-stack.

    The transport pinned `addresses[0]` from getaddrinfo, whose ordering is
    platform-dependent: this Windows host returns IPv4 only, while a
    dual-stack CI runner may order an unreachable IPv6 literal first. Pinning
    one arbitrary answer turned that into an opaque ConnectError.

    Every candidate must still be validated as global - preferring IPv4 is an
    ordering choice, not a relaxation of the DNS safety rule.
    """
    import ipaddress

    from intel_sources.report_delivery import DeliveryError, _PinnedTransport

    answers = ["2606:4700::1", "104.18.32.7", "172.64.80.1"]
    seen = {}

    class Recorder:
        def handle_request(self, request):
            seen["host"] = request.url.host
            raise RuntimeError("stop here")

        def close(self):
            pass

    # api.netlify.com is in the module allowlist, so this exercises pinning
    # rather than the allowlist guard.
    transport = _PinnedTransport(lambda host: answers, Recorder())
    request = httpx.Request("GET", "https://api.netlify.com/api/v1/user")
    with pytest.raises(RuntimeError):
        transport.handle_request(request)

    pinned = seen["host"].strip("[]")
    assert ipaddress.ip_address(pinned).version == 4, "must prefer a reachable IPv4 literal"
    assert pinned == "104.18.32.7"

    # Preferring IPv4 is an ordering choice, not a relaxation: a non-global
    # answer anywhere in the set is still refused outright.
    unsafe = _PinnedTransport(lambda host: ["2606:4700::1", "127.0.0.1"], Recorder())
    with pytest.raises(DeliveryError) as caught:
        unsafe.handle_request(httpx.Request("GET", "https://api.netlify.com/api/v1/user"))
    assert caught.value.code == "unsafe_dns"


def test_hosted_readback_uses_the_canonical_site_url_not_the_deploy_url():
    """Verify the URL that is actually published, not the per-deploy alias.

    Netlify answers 401 for the `<deploy-id>--site.netlify.app` alias on a
    team site even with no password protection, while the canonical
    `ssl_url` serves the same published bytes publicly. Reading back the
    alias meant a successful deploy was reported as an HTTP failure.
    """
    import httpx

    from intel_sources.report_delivery import DeliveryConfig, _Session

    body = b"<html>published bytes</html>"
    requested = []

    class Response:
        status_code = 200

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def iter_bytes(self):
            return iter([body])

    class Client:
        def stream(self, method, url, **kwargs):
            requested.append(url)
            return Response()

    config = DeliveryConfig(netlify_token="t", site_id="s", telegram_token="tg",
                            telegram_chat_id="1", discord_token="d", discord_channel_id="2")
    transport = type("T", (), {"allowed_hosts": set()})()
    session = _Session(config, transport, Client())

    deploy = {
        "id": "dep1",
        "site_id": "s",
        "state": "ready",
        "required": [],
        # The per-deploy alias answers 401; the canonical URL is what is published.
        "ssl_url": MIRROR_URL,
        "deploy_ssl_url": "https://dep1--example.netlify.app",
    }

    def fake_netlify(method, path, **kwargs):
        if method == "GET" and path.startswith("/sites/"):
            return {"id": "s"}
        if method == "POST":
            return deploy
        return deploy

    session.netlify = fake_netlify

    class Artifact:
        data = body
        sha1 = "sha1"
        sha256 = __import__("hashlib").sha256(body).hexdigest()

    hosted = session.host(Artifact(), 2, 0.0, lambda seconds: None)
    assert hosted.url == MIRROR_URL
    assert any("dep1--" not in url for url in requested)
