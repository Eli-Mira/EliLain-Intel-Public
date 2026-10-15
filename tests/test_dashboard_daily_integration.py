"""Tests for daily-integrated persistent cockpit — strict TDD, Task D.

These tests exercise the REAL daily pipeline through the dashboard callback
and verify restart-persistent settings/history, server-scoped single-flight,
CSRF loopback safety, and real bounded history consumption.
"""
from __future__ import annotations

import json
import tempfile
import time
import urllib.request
import urllib.error
from pathlib import Path

import pytest

from intel_sources.dashboard import serve_dashboard, _scan_state
from intel_sources.daily_automation import run_daily


@pytest.fixture
def task_d_dirs():
    """Create temp dirs matching real project layout."""
    with tempfile.TemporaryDirectory() as tmpdir:
        p = Path(tmpdir)
        settings_path = p / "settings.json"
        data_dir = p / "data"
        data_dir.mkdir()
        history_dir = data_dir / "daily_state"
        history_dir.mkdir(parents=True)
        public_dir = data_dir / "public"
        public_dir.mkdir(parents=True)
        templates_dir = p / "templates"
        templates_dir.mkdir()
        (templates_dir / "intel_cockpit.html").write_text(
            '<html><body><input name="csrf_token" value="{{ csrf_token }}">'
            '<button id="scan" onclick="startScan()">Run Scan Now</button>'
            '<div id="status"></div><div id="history"></div>'
            '</body></html>',
            encoding="utf-8",
        )
        yield {
            "tmp_path": p,
            "settings_path": settings_path,
            "data_dir": data_dir,
            "history_dir": history_dir,
            "public_dir": public_dir,
            "templates_dir": templates_dir,
        }


import threading

@pytest.fixture
def task_d_server(task_d_dirs, monkeypatch):
    """Start dashboard with real run_daily callback."""
    import intel_sources.dashboard as dashboard_mod
    from intel_sources.dashboard import _scan_state
    # Reset shared state before each test
    with _scan_state["lock"]:
        _scan_state["running"] = False
        _scan_state["status"] = "idle"
        _scan_state["last_error"] = None
        _scan_state["last_report"] = None

    class _RecordingThreading:
        """Fixture-local threading facade for intel_sources.dashboard.

        Records every Thread it constructs so teardown can join the real
        callback workers instead of polling shared scan state. Only the
        dashboard module's threading is replaced; global threading is
        untouched, and Lock/Event/etc. delegate to the real threading module.
        """
        def __init__(self):
            self._real = threading
            self.created_threads = []
            self._lock = threading.Lock()

        def Thread(self, *args, **kwargs):
            t = threading.Thread(*args, **kwargs)
            if getattr(t._target, "__name__", "") == "run_scan_worker":
                with self._lock:
                    self.created_threads.append(t)
            return t

        def __getattr__(self, name):
            return getattr(self._real, name)

        def callback_threads(self):
            # Thread.run deletes _target; retain handles before starting.
            with self._lock:
                return tuple(self.created_threads)

    recorder = _RecordingThreading()
    monkeypatch.setattr("intel_sources.dashboard.threading", recorder)

    d = task_d_dirs
    run_count = {"n": 0}

    def run_scan(settings):
        """Real daily pipeline callback — returns actual summary."""
        import asyncio
        result = asyncio.run(
            run_daily(
                settings_path=d["settings_path"],
                data_dir=str(d["data_dir"]),
                public_dir=str(d["public_dir"]),
            )
        )
        run_count["n"] += 1
        return result

    try:
        srv = serve_dashboard(
            settings_path=str(d["settings_path"]),
            history_dir=str(d["history_dir"]),
            run_scan=run_scan,
            host="127.0.0.1",
            port=0,
            templates_dir=str(d["templates_dir"]),
        )
        time.sleep(0.4)
        port = srv.server_address[1]
        base_url = f"http://127.0.0.1:{port}"
        yield {"base_url": base_url, "server": srv, "run_count": run_count, "dirs": d}

        # Stop the HTTP server from admitting new requests first, then prove the
        # actual callback worker threads have terminated before touching shared
        # scan state. A leaked daemon worker could otherwise flip state for a
        # later fixture, so join + assert is the teardown, not a poll.
        srv.shutdown()
        srv.server_close()
        for t in recorder.callback_threads():
            t.join(timeout=30)
        for t in recorder.callback_threads():
            assert not t.is_alive(), "callback worker leaked alive after teardown"
        # Reset shared scan state only after workers have terminated.
        with _scan_state["lock"]:
            _scan_state["running"] = False
            _scan_state["status"] = "idle"
            _scan_state["last_error"] = None
            _scan_state["last_report"] = None
    finally:
        dashboard_mod.threading = threading


@pytest.fixture(autouse=True)
def _mock_fetch_seam(monkeypatch):
    """Isolate real run_daily callbacks from external network by patching the
    native fetch/transport seam (fetch_candidates and verify_candidates) with
    deterministic synthetic data. run_daily and the dashboard callback still run
    end-to-end; only the upstream network transport is replaced. The synthetic
    candidate uses the `now` supplied by run_daily at the fetch seam so that
    publication/observation timestamps track the real callback clock."""
    from datetime import datetime, timezone

    CANDIDATE_TEMPLATE = {
        "id": "openrouter:openai/gpt-6.1-sol",
        "name": "OpenAI GPT",
        "what": "OpenAI GPT is a large language model available through the OpenRouter API for text generation.",
        "category": "models",
        "source_id": "openrouter-models",
        "source_url": "https://openrouter.ai/openai/gpt-6.1-sol",
        "date_kind": "release",
        "why": "OpenRouter lists this free-tier large language model for API access.",
        "free": {"status": "free-tier", "detail": "Free tier available via OpenRouter", "evidence_url": "https://openrouter.ai/openai/gpt-6.1-sol"},
        "api": {"status": "available", "detail": "REST API", "evidence_url": "https://openrouter.ai/openai/gpt-6.1-sol"},
        "topics": ["models"],
        "link_verification": {"status": "verified", "url": "https://openrouter.ai/openai/gpt-6.1-sol", "http_status": 200},
    }
    FAKED_SOURCES_TEMPLATE = [
        {"id": "openrouter-models", "name": "OpenRouter Models", "status": "ok", "returned": 1, "accepted": 1, "error_code": None}
    ]

    async def fake_fetch_cfg(settings, now=None):
        now = now or datetime.now(timezone.utc)
        now_iso = now.isoformat()
        candidate = {**CANDIDATE_TEMPLATE, "published_at": now_iso, "observed_at": now_iso, "link_verification": {**CANDIDATE_TEMPLATE["link_verification"], "checked_at": now_iso}}
        sources = [{**FAKED_SOURCES_TEMPLATE[0], "checked_at": now_iso}]
        return {"candidates": [candidate], "sources": sources}

    async def fake_verify(candidates, settings, now=None):
        return candidates

    monkeypatch.setattr("intel_sources.daily_automation.fetch_candidates", fake_fetch_cfg)
    monkeypatch.setattr("intel_sources.daily_automation.verify_candidates", fake_verify)


def _get_csrf(base_url: str) -> str:
    import urllib.request, re
    req = urllib.request.Request(base_url + "/")
    req.add_header("Origin", "http://127.0.0.1:8768")
    req.add_header("Host", "127.0.0.1:8768")
    with urllib.request.urlopen(req) as resp:
        html = resp.read().decode()
    m = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert m, "CSRF token missing"
    return m.group(1)


def _request(method: str, base_url: str, path: str, *,
             origin: str = "http://127.0.0.1:8768",
             host: str = "127.0.0.1:8768",
             token: str | None = None,
             body: bytes | None = None,
             content_type: str = "application/json") -> tuple[int, bytes]:
    req = urllib.request.Request(base_url + path, data=body, method=method)
    req.add_header("Origin", origin)
    req.add_header("Host", host)
    if token:
        req.add_header("X-CSRF-Token", token)
    if body and content_type:
        req.add_header("Content-Type", content_type)
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


class TestServerScopedState:
    """Contract: scan state is server-scoped, not reset per request."""

    def test_scan_state_lives_in_shared_dict(self, task_d_server):
        """_scan_state dict is shared across handler instances."""
        s = task_d_server
        csrf = _get_csrf(s["base_url"])
        # Start scan
        body = json.dumps({}).encode()
        status, _ = _request("POST", s["base_url"], "/api/scan",
                             token=csrf, body=body)
        assert status == 200
        # Check shared state — running=True on shared dict
        with _scan_state["lock"]:
            assert _scan_state["running"] is True

    def test_two_clients_single_flight(self, task_d_server):
        """Second concurrent scan client gets 409."""
        s = task_d_server
        csrf1 = _get_csrf(s["base_url"])
        csrf2 = _get_csrf(s["base_url"])
        body = json.dumps({}).encode()
        req1 = urllib.request.Request(
            s["base_url"] + "/api/scan", data=body, method="POST")
        req1.add_header("Origin", "http://127.0.0.1:8768")
        req1.add_header("Host", "127.0.0.1:8768")
        req1.add_header("X-CSRF-Token", csrf1)
        req1.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req1) as resp:
            assert resp.status == 200
        # Second request
        status, resp_body = _request("POST", s["base_url"], "/api/scan",
                                     token=csrf2, body=body)
        assert status == 409
        data = json.loads(resp_body)
        assert "already running" in data.get("error", "").lower()

    def test_status_reflects_shared_running_state(self, task_d_server):
        """GET /api/status reflects server-scoped running state."""
        s = task_d_server
        csrf = _get_csrf(s["base_url"])
        body = json.dumps({}).encode()
        _request("POST", s["base_url"], "/api/scan", token=csrf, body=body)
        # Poll until running
        for _ in range(30):
            status, body_b = _request("GET", s["base_url"], "/api/status")
            data = json.loads(body_b)
            if data.get("running"):
                break
            time.sleep(0.1)
        else:
            pytest.fail("status never showed running")
        assert data["running"] is True
        assert data["status"] in ("started", "running", "pending")
        # Must NOT leak raw paths/settings/tracebacks
        raw = json.dumps(data)
        assert "traceback" not in raw.lower()


class TestRealDailyCallback:
    """Contract: run_scan callback produces real daily summary/artifact."""

    def test_scan_callback_returns_real_summary(self, task_d_server):
        """POST /api/scan via real run_daily returns summary shape."""
        s = task_d_server
        d = s["dirs"]
        # Write valid settings first so run_daily has something to load
        settings = {
            "schema_version": 1,
            "sources": [
                {"id": "openrouter-models", "name": "OpenRouter Models",
                 "adapter": "openrouter-models", "enabled": True}
            ],
            "topics": [], "priorities": {},
            "categories": ["models", "free-api", "agents", "mcp",
                           "skills", "devtools", "news"],
            "max_items": 5, "max_age_days": 7, "min_score": 60,
            "schedule": {"timezone": "Africa/Cairo", "hour": 10, "minute": 0},
        }
        save_settings_path = d["settings_path"]
        from intel_sources.settings import save_settings as _ss
        _ss(save_settings_path, settings)
        # Start scan
        csrf = _get_csrf(s["base_url"])
        body = json.dumps({}).encode()
        status, _ = _request("POST", s["base_url"], "/api/scan",
                             token=csrf, body=body)
        assert status == 200
        # Wait for the real callback to complete and increment run_count.
        # Polling on `running==False` alone is racy: a leaked daemon worker
        # from an earlier fixture can flip running False before this scan's
        # worker has started, producing a false "completed" while run_count
        # remains 0. We wait until the actual callback has fired AND the
        # dashboard worker has reached terminal completed/not-running state.
        deadline = time.time() + 60
        while time.time() < deadline:
            st, sb = _request("GET", s["base_url"], "/api/status")
            sd = json.loads(sb)
            if sd.get("status") == "failed":
                pytest.fail(f"scan failed: {sd.get('last_error')}")
            if s["run_count"]["n"] >= 1 and sd.get("status") == "completed" and not sd.get("running"):
                break
            time.sleep(0.5)
        else:
            pytest.fail("scan callback never completed")
        # Verify callback was invoked (run_count > 0)
        assert s["run_count"]["n"] >= 1
        # Verify history.jsonl has real entry with actual counts/date/generated
        hist = d["history_dir"] / "history.jsonl"
        assert hist.exists(), "history.jsonl not produced"
        lines = hist.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) >= 1
        entry = json.loads(lines[-1])
        assert "report_date" in entry
        assert "generated_at" in entry
        assert "counts" in entry
        assert "metadata" in entry

    def test_callback_success_only_with_real_summary(self, task_d_server):
        """Callback success requires real summary shape — no error-as-success."""
        s = task_d_server
        d = s["dirs"]
        # Write settings that will work
        settings = {
            "schema_version": 1,
            "sources": [
                {"id": "openrouter-models", "name": "OpenRouter Models",
                 "adapter": "openrouter-models", "enabled": True}
            ],
            "topics": [], "priorities": {},
            "categories": ["models", "free-api", "agents", "mcp",
                           "skills", "devtools", "news"],
            "max_items": 5, "max_age_days": 7, "min_score": 60,
            "schedule": {"timezone": "Africa/Cairo", "hour": 10, "minute": 0},
        }
        from intel_sources.settings import save_settings as _ss
        _ss(d["settings_path"], settings)
        csrf = _get_csrf(s["base_url"])
        body = json.dumps({}).encode()
        _request("POST", s["base_url"], "/api/scan", token=csrf, body=body)
        # Wait for the real callback to complete and increment run_count.
        # Polling on `running==False` alone is racy: a leaked daemon worker
        # from an earlier fixture can flip running False before this scan's
        # worker has started, producing a false "completed" while run_count
        # remains 0. We wait until the actual callback has fired AND the
        # dashboard worker has reached terminal completed/not-running state.
        deadline = time.time() + 60
        while time.time() < deadline:
            st, sb = _request("GET", s["base_url"], "/api/status")
            sd = json.loads(sb)
            if sd.get("status") == "failed":
                pytest.fail(f"scan failed: {sd.get('last_error')}")
            if s["run_count"]["n"] >= 1 and sd.get("status") == "completed" and not sd.get("running"):
                break
            time.sleep(0.5)
        else:
            pytest.fail("scan callback never completed")
        status, sb = _request("GET", s["base_url"], "/api/status")
        data = json.loads(sb)
        # Status must be completed (not failed) when real pipeline succeeds
        assert data["status"] == "completed"
        assert data["last_error"] is None
        assert data["last_report"] is not None


class TestRestartPersistentHistory:
    """Contract: history.jsonl survives server restart; settings persist."""

    def test_history_jsonl_persists_across_restart(self, task_d_dirs):
        """Start server, run scan, shut down, restart — history retained."""
        d = task_d_dirs
        run_count = {"n": 0}

        def run_scan(settings):
            import asyncio
            result = asyncio.run(
                run_daily(
                    settings_path=d["settings_path"],
                    data_dir=str(d["data_dir"]),
                    public_dir=str(d["public_dir"]),
                )
            )
            run_count["n"] += 1
            return result

        # First server
        srv1 = serve_dashboard(
            settings_path=str(d["settings_path"]),
            history_dir=str(d["history_dir"]),
            run_scan=run_scan,
            host="127.0.0.1", port=0,
            templates_dir=str(d["templates_dir"]),
        )
        time.sleep(0.3)
        port1 = srv1.server_address[1]
        base1 = f"http://127.0.0.1:{port1}"
        # Write settings, run scan
        settings = {
            "schema_version": 1,
            "sources": [{"id": "openrouter-models", "name": "OpenRouter Models",
                         "adapter": "openrouter-models", "enabled": True}],
            "topics": [], "priorities": {},
            "categories": ["models", "free-api", "agents", "mcp",
                           "skills", "devtools", "news"],
            "max_items": 5, "max_age_days": 7, "min_score": 60,
            "schedule": {"timezone": "Africa/Cairo", "hour": 10, "minute": 0},
        }
        from intel_sources.settings import save_settings as _ss
        _ss(d["settings_path"], settings)
        csrf = _get_csrf(base1)
        _request("POST", base1, "/api/scan", token=csrf,
                 body=json.dumps({}).encode())
        # Wait for completion
        for _ in range(60):
            try:
                req = urllib.request.Request(base1 + "/api/status")
                req.add_header("Origin", "http://127.0.0.1:8768")
                req.add_header("Host", "127.0.0.1:8768")
                with urllib.request.urlopen(req) as resp:
                    sd = json.loads(resp.read().decode())
                if not sd.get("running"):
                    break
            except Exception:
                pass
            time.sleep(0.5)
        srv1.shutdown()
        srv1.server_close()
        # History file exists
        hist = d["history_dir"] / "history.jsonl"
        assert hist.exists()
        lines1 = hist.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines1) >= 1
        # Restart server
        run_count2 = {"n": 0}
        srv2 = serve_dashboard(
            settings_path=str(d["settings_path"]),
            history_dir=str(d["history_dir"]),
            run_scan=run_scan,
            host="127.0.0.1", port=0,
            templates_dir=str(d["templates_dir"]),
        )
        time.sleep(0.3)
        port2 = srv2.server_address[1]
        base2 = f"http://127.0.0.1:{port2}"
        # History still visible after restart
        status, sb = _request("GET", base2, "/api/history")
        assert status == 200
        hd = json.loads(sb)
        assert len(hd["reports"]) >= len(lines1)
        srv2.shutdown()
        srv2.server_close()

    def test_settings_persist_across_restart(self, task_d_dirs):
        """Saved sources/topics/priorities survive server restart."""
        d = task_d_dirs
        run_scan = lambda s: {"report_date": "2026-01-01",
                              "generated_at": "2026-01-01T00:00:00+00:00",
                              "public_path": "", "counts": {},
                              "settings_hash": "", "delayed_start": False,
                              "intended_slot": "2026-01-01T00:00:00+00:00"}
        srv = serve_dashboard(
            settings_path=str(d["settings_path"]),
            history_dir=str(d["history_dir"]),
            run_scan=run_scan,
            host="127.0.0.1", port=0,
            templates_dir=str(d["templates_dir"]),
        )
        time.sleep(0.2)
        port = srv.server_address[1]
        base = f"http://127.0.0.1:{port}"
        # Save settings via POST
        csrf = _get_csrf(base)
        settings = {
            "schema_version": 1,
            "sources": [{"id": "openrouter-models", "name": "OpenRouter Models",
                         "adapter": "openrouter-models", "enabled": True}],
            "topics": ["AI", "security"],
            "priorities": {"models": 3, "agents": 5, "free-api": 2,
                           "mcp": 1, "skills": 4, "devtools": 1, "news": 3},
            "categories": ["models", "free-api", "agents", "mcp",
                           "skills", "devtools", "news"],
            "max_items": 5, "max_age_days": 7, "min_score": 60,
            "schedule": {"timezone": "Africa/Cairo", "hour": 10, "minute": 0},
        }
        status, sb = _request("POST", base, "/api/settings",
                              token=csrf,
                              body=json.dumps(settings).encode())
        assert status == 200
        srv.shutdown()
        srv.server_close()
        # Settings file persisted on disk
        assert d["settings_path"].exists()
        saved = json.loads(d["settings_path"].read_text(encoding="utf-8"))
        assert saved["topics"] == ["AI", "security"]
        assert saved["priorities"]["models"] == 3
        assert saved["priorities"]["agents"] == 5
        # Reload via new server — values still there
        srv2 = serve_dashboard(
            settings_path=str(d["settings_path"]),
            history_dir=str(d["history_dir"]),
            run_scan=run_scan,
            host="127.0.0.1", port=0,
            templates_dir=str(d["templates_dir"]),
        )
        time.sleep(0.2)
        port2 = srv2.server_address[1]
        base2 = f"http://127.0.0.1:{port2}"
        status2, sb2 = _request("GET", base2, "/api/settings")
        loaded = json.loads(sb2)
        assert loaded["topics"] == ["AI", "security"]
        assert loaded["priorities"]["agents"] == 5
        srv2.shutdown()
        srv2.server_close()


class TestCSRFAndOriginSafety:
    """Contract: CSRF loopback/Host/Origin/nonce/body-bound safety."""

    def test_cross_origin_scan_rejected(self, task_d_server):
        """POST /api/scan from wrong Origin is rejected."""
        s = task_d_server
        csrf = _get_csrf(s["base_url"])
        status, sb = _request("POST", s["base_url"], "/api/scan",
                              origin="http://evil.example.com",
                              host="127.0.0.1:8768", token=csrf,
                              body=json.dumps({}).encode())
        assert status == 403

    def test_missing_csrf_rejected(self, task_d_server):
        """POST /api/scan without CSRF token rejected."""
        s = task_d_server
        status, sb = _request("POST", s["base_url"], "/api/scan",
                              body=json.dumps({}).encode())
        assert status == 403

    def test_nonce_single_use(self, task_d_server):
        """CSRF token consumed on first use; second use rejected."""
        s = task_d_server
        csrf = _get_csrf(s["base_url"])
        body = json.dumps({}).encode()
        # First use
        status1, _ = _request("POST", s["base_url"], "/api/scan",
                              token=csrf, body=body)
        assert status1 == 200
        # Replay same token
        status2, _ = _request("POST", s["base_url"], "/api/scan",
                              token=csrf, body=body)
        assert status2 == 403

    def test_malformed_body_rejected(self, task_d_server):
        """POST with malformed JSON body returns 400."""
        s = task_d_server
        csrf = _get_csrf(s["base_url"])
        status, sb = _request("POST", s["base_url"], "/api/settings",
                              token=csrf,
                              body=b"not-json{",
                              content_type="application/json")
        assert status == 400

    def test_report_id_traversal_rejected(self, task_d_server):
        """Path traversal in report id rejected."""
        s = task_d_server
        status, sb = _request("GET", s["base_url"],
                              "/reports/../../etc/passwd")
        assert status in (400, 403, 404)


class TestBoundedHistoryConsumption:
    """Contract: real history.jsonl consumed with bounded count/size."""

    def test_history_from_jsonl_not_dummy_html(self, task_d_dirs):
        """GET /api/history reads history.jsonl, not report_*.html files."""
        d = task_d_dirs
        # Write history.jsonl directly (simulating daily run)
        hist = d["history_dir"] / "history.jsonl"
        entries = []
        for i in range(3):
            entry = {
                "report_date": f"2026-01-0{i+1}",
                "generated_at": f"2026-01-0{i+1}T10:00:00+00:00",
                "counts": {"selected": i * 2, "collected": i * 5},
                "metadata": {"settings_hash": f"sha{i}", "delayed_start": False},
            }
            entries.append(entry)
        hist.write_text("\n".join(json.dumps(e) for e in entries) + "\n",
                        encoding="utf-8")
        run_scan = lambda s: {"report_date": "2026-01-01",
                              "generated_at": "2026-01-01T00:00:00+00:00",
                              "public_path": "", "counts": {},
                              "settings_hash": "", "delayed_start": False,
                              "intended_slot": "2026-01-01T00:00:00+00:00"}
        srv = serve_dashboard(
            settings_path=str(d["settings_path"]),
            history_dir=str(d["history_dir"]),
            run_scan=run_scan,
            host="127.0.0.1", port=0,
            templates_dir=str(d["templates_dir"]),
        )
        time.sleep(0.2)
        port = srv.server_address[1]
        base = f"http://127.0.0.1:{port}"
        status, sb = _request("GET", base, "/api/history")
        assert status == 200
        data = json.loads(sb)
        assert "reports" in data
        assert len(data["reports"]) == 3
        # Each entry carries real daily fields, not dummy placeholders
        for r in data["reports"]:
            assert "report_date" in r
            assert "generated_at" in r
            assert "selected_count" in r
            assert r["url"].startswith("/reports/")
        # No dummy HTML report files should have been created
        html_files = list(d["history_dir"].glob("report_*.html"))
        assert len(html_files) == 0, "Dummy HTML reports should not be created"
        srv.shutdown()
        srv.server_close()

    def test_history_bounded_count(self, task_d_dirs):
        """History endpoint enforces bounded count (90-entry limit)."""
        d = task_d_dirs
        hist = d["history_dir"] / "history.jsonl"
        # Write 100 entries
        lines = []
        for i in range(100):
            entry = {"report_date": f"2026-01-{i+1:02d}",
                     "generated_at": f"2026-01-{i+1:02d}T10:00:00+00:00",
                     "counts": {}, "metadata": {}}
            lines.append(json.dumps(entry))
        hist.write_text("\n".join(lines) + "\n", encoding="utf-8")
        run_scan = lambda s: {"report_date": "2026-01-01",
                              "generated_at": "2026-01-01T00:00:00+00:00",
                              "public_path": "", "counts": {},
                              "settings_hash": "", "delayed_start": False,
                              "intended_slot": "2026-01-01T00:00:00+00:00"}
        srv = serve_dashboard(
            settings_path=str(d["settings_path"]),
            history_dir=str(d["history_dir"]),
            run_scan=run_scan,
            host="127.0.0.1", port=0,
            templates_dir=str(d["templates_dir"]),
        )
        time.sleep(0.2)
        port = srv.server_address[1]
        base = f"http://127.0.0.1:{port}"
        status, sb = _request("GET", base, "/api/history")
        data = json.loads(sb)
        # Bounded: returned count should be at most 90 (the enforced limit)
        assert len(data["reports"]) <= 90
        srv.shutdown()
        srv.server_close()