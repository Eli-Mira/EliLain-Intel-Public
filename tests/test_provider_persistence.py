import sqlite3
from datetime import datetime, timezone

from freellmapi_tracker.models import FreeProvider, ProviderProbeResult

from freellmapi_tracker.models import ProviderStatus, FreellmapiIntegrationType
from freellmapi_tracker.tracker import FreeProviderTracker


def make_db(path):
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE api_keys (id INTEGER, platform TEXT, label TEXT, status TEXT, enabled INTEGER, api_key TEXT)")
        conn.executemany("INSERT INTO api_keys VALUES (?, ?, ?, ?, ?, ?)", [
            (73, "groq", "active", "healthy", 1, "fixture-not-a-key"),
            (91, "google", "disabled", "healthy", 0, "fixture-not-a-key"),
            (118, "new-platform", "unhealthy", "unhealthy", 1, "fixture-not-a-key"),
        ])


def test_db_uses_real_key_ids_and_never_calls_gateway_health_live(tmp_path, monkeypatch):
    db = tmp_path / "router.db"
    make_db(db)
    original = db.read_bytes()
    connect = sqlite3.connect
    calls, queries = [], []
    def readonly_connect(database, **kwargs):
        calls.append((database, kwargs))
        conn = connect(database, **kwargs)
        conn.set_trace_callback(queries.append)
        return conn
    monkeypatch.setattr(sqlite3, "connect", readonly_connect)
    tracker = FreeProviderTracker(db, tmp_path / "state.json")
    assert tracker.load_from_db() == 3
    groq = tracker.providers["groq"]
    assert groq.freellmapi_key_id == 73
    assert groq.status == ProviderStatus.UNKNOWN
    assert groq.checked_at is None
    assert groq.probe_enabled is True
    assert tracker.providers["google"].status == ProviderStatus.DISABLED
    assert tracker.providers["google"].probe_enabled is False
    assert tracker.providers["new-platform"].status == ProviderStatus.UNKNOWN
    assert groq.freellmapi_integration == FreellmapiIntegrationType.IN_CATALOG_KEY_IN_DB
    assert "mode=ro" in calls[0][0] and calls[0][1]["uri"] is True
    assert not any("api_key " in q or "SELECT *" in q for q in queries)
    assert db.read_bytes() == original


def test_missing_database_is_not_created(tmp_path, caplog):
    db = tmp_path / "missing.db"
    assert FreeProviderTracker(db).load_from_db() == 0
    assert not db.exists()
    assert "not found" in caplog.text.lower()


def test_apply_probe_result_persists_exact_observation_and_merges_db_state(tmp_path):
    db, state = tmp_path / "router.db", tmp_path / "state.json"
    make_db(db)
    checked = datetime(2026, 9, 30, 10, tzinfo=timezone.utc)
    writer = FreeProviderTracker(db, state)
    writer.load_from_db()
    result = ProviderProbeResult("groq", True, "tiny", timestamp=checked)
    result.evidence = [{"http_status": 200, "model": "tiny"}]
    result.probe_source = "local:freellmapi"
    writer.apply_probe_result("groq", result)
    writer.save_state()
    reader = FreeProviderTracker(db, state)
    reader.load_from_db()
    reader.load_state()
    p = reader.providers["groq"]
    assert p.status == ProviderStatus.LIVE
    assert p.checked_at == checked == p.last_verified
    assert p.probe_source == "local:freellmapi"
    assert p.probe_evidence == result.evidence
    assert p.freellmapi_key_id == 73
    assert "No action needed" in p.freellmapi_action
    assert reader.providers["google"].status == ProviderStatus.DISABLED
    reader.load_from_db()
    assert reader.providers["groq"].checked_at == checked
    assert reader.providers["groq"].status == ProviderStatus.LIVE


async def test_recoverable_failed_providers_are_reprobed_with_result_time(tmp_path, monkeypatch):
    tracker = FreeProviderTracker(tmp_path / "missing.db", tmp_path / "state.json")
    checked = datetime(2026, 9, 29, tzinfo=timezone.utc)
    states = [ProviderStatus.DEGRADED, ProviderStatus.EXHAUSTED, ProviderStatus.BROKEN_KEY, ProviderStatus.PAYWALLED, ProviderStatus.DISABLED]
    for state in states:
        tracker.providers[state.value] = FreeProvider(state.value, state.value, status=state)
    seen = []
    async def probe(provider):
        seen.append(provider.platform)
        return ProviderProbeResult(provider.platform, True, "tiny", timestamp=checked,
                                   evidence=[{"http_status": 200}])
    monkeypatch.setattr(tracker, "_probe_single", probe)
    await tracker.update_providers()
    assert set(seen) == {s.value for s in states if s != ProviderStatus.DISABLED}
    assert tracker.providers["exhausted"].status == ProviderStatus.LIVE
    assert tracker.providers["exhausted"].last_verified == checked
    assert tracker.providers["exhausted"].checked_at == checked
    assert tracker.probe_results["exhausted"].evidence == [{"http_status": 200}]
    assert tracker.providers["disabled"].checked_at is None


def test_database_enabled_and_key_identity_win_in_both_load_orders(tmp_path):
    db, state = tmp_path / "router.db", tmp_path / "state.json"
    make_db(db)
    writer = FreeProviderTracker(db, state)
    writer.providers["google"] = FreeProvider("Google", "google", status=ProviderStatus.LIVE,
        freellmapi_key_id=6, checked_at=datetime(2026, 9, 30, tzinfo=timezone.utc))
    writer.save_state()
    reader = FreeProviderTracker(db, state)
    reader.load_state()
    assert reader.load_from_db() == 3
    assert reader.providers["google"].status == ProviderStatus.DISABLED
    assert reader.providers["google"].freellmapi_key_id == 91
    assert not reader.providers["google"].probe_enabled
    # Synthetic router fixture only: enabled rows take precedence over disabled duplicates.
    with sqlite3.connect(db) as conn:
        conn.execute("INSERT INTO api_keys VALUES (1, 'groq', 'old-disabled', 'healthy', 0, 'fixture')")
        conn.execute("UPDATE api_keys SET enabled=0 WHERE id=73")
    reader.load_from_db()
    assert reader.providers["groq"].status == ProviderStatus.DISABLED
    reader.load_state()
    assert reader.providers["google"].status == ProviderStatus.DISABLED
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE api_keys SET enabled=1 WHERE id=73")
    reader.load_from_db()
    assert reader.providers["groq"].freellmapi_key_id == 73
    assert reader.providers["groq"].status == ProviderStatus.UNKNOWN
    assert reader.providers["groq"].probe_enabled


def test_old_state_cannot_restore_disabled_or_different_key_as_verified(tmp_path):
    db, state = tmp_path / "router.db", tmp_path / "state.json"
    make_db(db)
    checked = datetime(2026, 9, 30, tzinfo=timezone.utc)
    writer = FreeProviderTracker(db, state)
    writer.providers["groq"] = FreeProvider("Groq", "groq", status=ProviderStatus.LIVE,
                                           freellmapi_key_id=2, checked_at=checked)
    writer.providers["new-platform"] = FreeProvider("New", "new-platform", status=ProviderStatus.DISABLED,
                                                    freellmapi_key_id=118, checked_at=checked)
    writer.save_state()
    reader = FreeProviderTracker(db, state)
    reader.load_from_db()
    reader.load_state()
    assert reader.providers["groq"].status == ProviderStatus.UNKNOWN
    assert reader.providers["groq"].checked_at is None
    assert reader.providers["new-platform"].status == ProviderStatus.UNKNOWN
    assert reader.providers["new-platform"].probe_enabled
    # An older observation must not overwrite the current run.
    reader.apply_probe_result("groq", ProviderProbeResult("groq", False, "tiny", http_status=429,
                                                        error_type="rate_limit", checked_at=checked))
    reader.load_state()
    assert reader.providers["groq"].status == ProviderStatus.DEGRADED


def test_state_merges_old_probe_aliases_without_losing_metadata_or_newer_checks(tmp_path):
    import json
    db, state = tmp_path / "router.db", tmp_path / "state.json"
    make_db(db)
    old = {"name": "Groq", "platform": "groq", "status": "exhausted", "last_verified": "2026-09-29T12:00:00Z",
           "last_verified_source": "probe", "notes": "Operator context", "tags": ["tracked"]}
    state.write_text(json.dumps({"providers": {"groq": old}}))
    reader = FreeProviderTracker(db, state)
    reader.load_from_db()
    reader.load_state()
    p = reader.providers["groq"]
    assert p.checked_at == datetime(2026, 9, 29, 12, tzinfo=timezone.utc)
    assert p.status == ProviderStatus.EXHAUSTED
    assert p.notes == "Operator context"
    assert p.tags == ["tracked"]
    reader.apply_probe_result("groq", ProviderProbeResult("groq", True, "tiny", checked_at=datetime(2026, 9, 30, tzinfo=timezone.utc)))
    reader.load_state()
    assert p.status == ProviderStatus.LIVE
    assert p.checked_at == datetime(2026, 9, 30, tzinfo=timezone.utc)
    before = p.checked_at
    reader.save_state()
    reader.load_state()
    assert p.checked_at == before


def test_failed_database_read_always_closes_readonly_connection(tmp_path, monkeypatch):
    db = tmp_path / "invalid-schema.db"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE unrelated (id INTEGER)")
    closed = []
    connect = sqlite3.connect
    class TrackedConnection(sqlite3.Connection):
        def close(self):
            closed.append(True)
            super().close()
    def connection(database, **kwargs):
        return connect(database, factory=TrackedConnection, **kwargs)
    monkeypatch.setattr(sqlite3, "connect", connection)
    assert FreeProviderTracker(db).load_from_db() == 0
    assert closed == [True]
