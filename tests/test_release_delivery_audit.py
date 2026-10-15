"""Release delivery audit: offline guards and strict reproductions of open gaps.

No live sends/deploys. Xfails are acceptance defects, not customer acceptance.
Remove each marker after its production fix; strict XPASS requests that cleanup.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path

import httpx
import pytest

from test_report_delivery import (  # reuse the realistic existing protocol fixture
    ENV, Protocol, Guard, URL_HOST, artifact_dir, module, parse_multipart, run,
)

ROOT = Path(__file__).resolve().parents[1]


def test_explicit_attachment_option_delivers_exact_file_to_both_channels(artifact_dir):
    data = (artifact_dir / "index.html").read_bytes()
    protocol = Protocol(data)
    result = module().deliver_report(artifact_dir, environ={**ENV, "PUBLIC_REPORT_ATTACHMENT": "true"},
        transport=httpx.MockTransport(protocol), resolver=lambda _: ["8.8.8.8"])
    documents = [r for r in protocol.calls if r.method == "POST"
                 and r.headers["host"] == "api.telegram.org" and r.url.path.endswith("/sendDocument")]
    uploads = [r for r in protocol.calls if r.method == "POST"
               and r.headers["host"] == "discord.com"
               and r.headers.get("content-type", "").startswith("multipart/form-data")]
    assert len(documents) == len(uploads) == 1, "the standalone file was not attached on both channels"
    for request in documents + uploads:
        _, files = parse_multipart(request)
        uploaded = next(iter(files.values()))
        assert uploaded[0] == "index.html"
        assert uploaded[1] == data, "the uploaded bytes differ from the published artifact"
        assert hashlib.sha256(uploaded[1]).hexdigest() == result.hosted.sha256
    assert result.telegram.message_id == "42/43", "telegram receipt must reference the document message"
    assert result.discord.confirmation == "independently-read-back"
    # The brief text is still sent; the file is additional, not a replacement.
    assert any(r.method == "POST" and r.headers["host"] == "api.telegram.org"
               and r.url.path.endswith("/sendMessage") for r in protocol.calls)


@pytest.mark.parametrize("status", [401, 403, 429, 500, 503])
def test_release_host_http_failure_stops_before_channel_requests(artifact_dir, status):
    data = (artifact_dir / "index.html").read_bytes()
    protocol = Guard(data, lambda r: r.headers["host"] == URL_HOST
                     and httpx.Response(status, content=b"PRIVATE_SERVER_BODY_CANARY"))
    with pytest.raises(module().DeliveryError) as caught:
        run(artifact_dir, protocol)
    assert caught.value.code == "http_status"
    assert "PRIVATE_SERVER_BODY_CANARY" not in str(caught.value)
    assert not any(r.headers["host"] in {"api.telegram.org", "discord.com"}
                   for r in protocol.calls)


def test_release_telegram_failure_still_records_discord_confirmation(artifact_dir):
    data = (artifact_dir / "index.html").read_bytes()
    def reject_telegram(request):
        if request.headers["host"] == "api.telegram.org":
            # Record attempted text even when Telegram rejects it; the shared
            # protocol compares Discord's body against that same brief.
            protocol.brief = json.loads(request.content)["text"]
            return httpx.Response(429, json={"ok": False})
        return None

    protocol = Guard(data, reject_telegram)
    with pytest.raises(module().DeliveryError) as caught:
        run(artifact_dir, protocol)
    assert caught.value.code == "channel_failure"
    assert caught.value.result.telegram.error_code == "http_status"
    assert caught.value.result.discord.confirmation == "independently-read-back"
    assert sum(r.headers["host"] == "api.telegram.org" for r in protocol.calls) == 1


def test_release_republishing_identical_artifact_does_not_duplicate_briefs(artifact_dir, tmp_path):
    state = tmp_path / "delivery_receipts.json"
    protocol = Protocol((artifact_dir / "index.html").read_bytes())
    first = run(artifact_dir, protocol, state_path=state)
    assert first.telegram.confirmation == "receipt-confirmed"
    after_first = len([r for r in protocol.calls if r.method == "POST"
                       and r.headers["host"] in {"api.telegram.org", "discord.com"}])
    second = run(artifact_dir, protocol, state_path=state)
    assert second.telegram.confirmation == "previously-confirmed"
    assert second.discord.confirmation == "previously-confirmed"
    assert second.success is True
    posts = [r for r in protocol.calls if r.method == "POST"
             and r.headers["host"] in {"api.telegram.org", "discord.com"}]
    assert len(posts) == after_first, "an identical artifact was announced a second time"
    # The hosted bytes are still re-verified on the repeat run.
    assert any(r.headers["host"] == URL_HOST and r.method == "GET" for r in protocol.calls)
    # A changed artifact must notify again.
    index = artifact_dir / "index.html"
    index.write_text(index.read_text(encoding="utf-8").replace("</body>", "<!--changed--></body>"),
                     encoding="utf-8")
    changed = Protocol(index.read_bytes())
    third = run(artifact_dir, changed, state_path=state)
    assert third.telegram.confirmation == "receipt-confirmed"


def test_release_no_export_rejects_active_remote_dependency(artifact_dir):
    index = artifact_dir / "index.html"
    # Negative-test payload only: never rendered, deployed, or requested.
    index.write_text(index.read_text(encoding="utf-8").replace(
        "</body>", '<script src="https://example.com/audit-canary.js"></script></body>'), encoding="utf-8")
    with pytest.raises(module().DeliveryError):
        module().load_artifact(artifact_dir)


def test_release_rejects_oversized_input_before_deployment(artifact_dir):
    index = artifact_dir / "index.html"
    with index.open("ab") as handle:
        handle.write(b" " * module().MAX_HTML_BYTES)
    protocol = Protocol(index.read_bytes())
    with pytest.raises(module().DeliveryError) as caught:
        run(artifact_dir, protocol)
    assert caught.value.code == "artifact_too_large"
    assert protocol.calls == [], "an oversized artifact was uploaded before being refused"


def test_release_legacy_telegram_transport_exception_never_logs_token(monkeypatch, caplog):
    from tracker_core.notifier import TelegramNotifier
    canary = "SYNTHETIC_TOKEN_CANARY"

    class ExplodingClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, *args, **kwargs):
            raise httpx.ConnectError("https://api.telegram.org/bot" + canary + "/sendMessage")

    monkeypatch.setattr(httpx, "AsyncClient", ExplodingClient)
    assert asyncio.run(TelegramNotifier(canary, "-1").send("audit")) is False
    assert canary not in caplog.text


def test_release_legacy_multi_requires_all_selected_channels():
    from tracker_core.notifier import MultiNotifier

    class StubNotifier:
        name = "audit-stub"

        def __init__(self, result):
            self.result = result

        async def send(self, message):
            return self.result

    notifier = MultiNotifier([StubNotifier(True), StubNotifier(False)])
    assert asyncio.run(notifier.send("audit")) is False


def test_release_restored_news_does_not_replay_breaking_notification(tmp_path, monkeypatch):
    import main
    from news_tracker.store_sqlite import SQLiteStore
    from tracker_core.schema import NewsItem, SourceType

    monkeypatch.setenv("NOTIFICATION_CHANNEL", "none")
    tracker = main.NewsTracker.__new__(main.NewsTracker)
    tracker.sqlite = SQLiteStore(tmp_path / "news.db")
    tracker.breaking_threshold = 0.7
    messages = []
    item = NewsItem(source=SourceType.NEWS, title="Audit fixture", url="https://example.com/audit",
                    relevance_score=0.9, is_relevant=True)

    class Recorder:
        async def send(self, message):
            messages.append(message)
            return True

    async def fetch():
        return [item]

    async def process(items):
        return items, {"relevant_count": len(items)}

    tracker.notifier = Recorder()
    tracker.fetch_all = fetch
    tracker.process = process
    tracker.get_health_table = lambda: []
    tracker.get_feed_health_table = lambda: []
    try:
        first = asyncio.run(tracker.run())
        tracker.sqlite.close()
        tracker.sqlite = SQLiteStore(tmp_path / "news.db")
        second = asyncio.run(tracker.run())
        assert first["inserted"] == 1 and second["inserted"] == 0
        assert len(messages) == 1
    finally:
        tracker.sqlite.close()


def test_release_workflow_restores_daily_state_only():
    """The workflow scopes cache to approved public daily data only.

    Legacy today/audit touched private project evaluations, news DB, and
    provider state — those were removed because the daily producer is the
    sole publisher consumer. Private paths must no longer be cached.
    """
    import yaml
    workflow = yaml.safe_load((ROOT / ".github/workflows/news-tracker-daily.yml").read_text(encoding="utf-8"))
    steps = workflow["jobs"]["run-elilain-audit"]["steps"]
    cache = next(s for s in steps if s.get("uses", "").startswith(("actions/cache@", "actions/cache/restore@")))
    cached = cache["with"]["path"].splitlines()
    # Approved public daily paths must be present.
    for required in ("data/daily_state/history.jsonl", "data/daily_state/raw_evidence.json"):
        assert required in cached, f"{required} is not cached"
    # Private state paths must NOT be cached.
    for forbidden in ("data/feed_health.json", "data/health.json", "data/intel_state.json",
                      "data/provider_state.json", "data/provider_changes.json",
                      "data/project_evaluations.json", "data/news.db"):
        assert forbidden not in cached, f"{forbidden} must not be cached; scope is daily public only"


def test_release_http_logging_flags_restore_after_exception(monkeypatch):
    import logging
    from intel_sources.report_delivery import _quiet_http_logs

    httpx_log = logging.getLogger("httpx")
    httpcore_log = logging.getLogger("httpcore")
    monkeypatch.setattr(httpx_log, "disabled", False)
    monkeypatch.setattr(httpcore_log, "disabled", True)
    with pytest.raises(RuntimeError):
        with _quiet_http_logs():
            assert httpx_log.disabled and httpcore_log.disabled
            raise RuntimeError("synthetic audit failure")
    assert httpx_log.disabled is False
    assert httpcore_log.disabled is True


def test_release_secret_bearing_httpcore_child_logs_are_suppressed(caplog):
    import logging
    from intel_sources.report_delivery import _quiet_http_logs

    child = logging.getLogger("httpcore.http11")
    with caplog.at_level(logging.DEBUG, logger=child.name):
        with _quiet_http_logs():
            child.debug("SYNTHETIC_WIRE_TOKEN_CANARY")
    assert "SYNTHETIC_WIRE_TOKEN_CANARY" not in caplog.text


def test_release_actual_reporter_consumes_src_templates_not_tracker_core_copy():
    from tracker_core.reporter_html import LainReporter
    template = LainReporter().env.get_template("report_template_lain.html")
    assert Path(template.filename).resolve() == (ROOT / "src/templates/report_template_lain.html").resolve()
