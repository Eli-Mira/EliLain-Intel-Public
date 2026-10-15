"""Gate nonempty daily report — positive/negative controls + binding verification."""
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now_iso():
    return datetime(2026, 10, 7, 10, 0, tzinfo=timezone.utc).isoformat()


def _make_mock_artifact(sha256: str, rendered_at: str, selected: int):
    artifact = MagicMock()
    artifact.sha256 = sha256
    artifact.rendered_at = rendered_at
    artifact.source_counts = [("github-trending", selected)]
    artifact.data = b'<meta name="elilain-report-kind" content="daily-digest-v1">'
    return artifact


# ---------------------------------------------------------------------------
# Positive control
# ---------------------------------------------------------------------------

def test_gate_passes_when_history_matches_current_artifact(tmp_path):
    from scripts.gate_daily_nonempty import main
    now_iso = _now_iso()
    data_dir = tmp_path / "data"
    public_dir = tmp_path / "public"
    state_dir = data_dir / "daily_state"
    state_dir.mkdir(parents=True, exist_ok=True)
    public_dir.mkdir(parents=True, exist_ok=True)
    sha = hashlib.sha256(b"<html><body>test</body></html>").hexdigest()
    selected = 3
    hist_file = state_dir / "history.jsonl"
    entry = {
        "counts": {"selected": selected},
        "report_date": "2026-10-07",
        "metadata": {
            "artifact_sha256": sha,
            "rendered_at": now_iso,
            "settings_hash": "abc",
        },
    }
    hist_file.write_text(json.dumps(entry) + "\n", encoding="utf-8")
    (public_dir / "index.html").write_bytes(b"<html><body>test</body></html>")
    with patch("intel_sources.report_delivery.load_artifact",
               return_value=_make_mock_artifact(sha, now_iso, selected)):
        code = main(["--data-dir", str(data_dir), "--public-dir", str(public_dir)])
    assert code == 0, f"gate should pass when history is bound to current artifact, got: {code}"


# ---------------------------------------------------------------------------
# Negative controls
# ---------------------------------------------------------------------------

def test_gate_fails_when_no_history_jsonl(tmp_path):
    from scripts.gate_daily_nonempty import main
    data_dir = tmp_path / "data"
    public_dir = tmp_path / "public"
    data_dir.mkdir(parents=True, exist_ok=True)
    public_dir.mkdir(parents=True, exist_ok=True)
    (public_dir / "index.html").write_bytes(b"ok")
    with patch("intel_sources.report_delivery.load_artifact"):
        code = main(["--data-dir", str(data_dir), "--public-dir", str(public_dir)])
    assert code != 0


def test_gate_fails_when_history_selected_zero(tmp_path):
    from scripts.gate_daily_nonempty import main
    data_dir = tmp_path / "data"
    public_dir = tmp_path / "public"
    state_dir = data_dir / "daily_state"
    state_dir.mkdir(parents=True, exist_ok=True)
    public_dir.mkdir(parents=True, exist_ok=True)
    (public_dir / "index.html").write_bytes(b"ok")
    hist_file = state_dir / "history.jsonl"
    entry = {"counts": {"selected": 0}, "report_date": "2026-10-07"}
    hist_file.write_text(json.dumps(entry) + "\n", encoding="utf-8")
    with patch("intel_sources.report_delivery.load_artifact"):
        code = main(["--data-dir", str(data_dir), "--public-dir", str(public_dir)])
    assert code != 0


def test_gate_fails_when_missing_artifact_sha256(tmp_path):
    from scripts.gate_daily_nonempty import main
    data_dir = tmp_path / "data"
    public_dir = tmp_path / "public"
    state_dir = data_dir / "daily_state"
    state_dir.mkdir(parents=True, exist_ok=True)
    public_dir.mkdir(parents=True, exist_ok=True)
    (public_dir / "index.html").write_bytes(b"ok")
    hist_file = state_dir / "history.jsonl"
    entry = {"counts": {"selected": 3}, "report_date": "2026-10-07"}
    hist_file.write_text(json.dumps(entry) + "\n", encoding="utf-8")
    with patch("intel_sources.report_delivery.load_artifact"):
        code = main(["--data-dir", str(data_dir), "--public-dir", str(public_dir)])
    assert code != 0


def test_gate_fails_when_sha_mismatch(tmp_path):
    from scripts.gate_daily_nonempty import main
    data_dir = tmp_path / "data"
    public_dir = tmp_path / "public"
    state_dir = data_dir / "daily_state"
    state_dir.mkdir(parents=True, exist_ok=True)
    public_dir.mkdir(parents=True, exist_ok=True)
    (public_dir / "index.html").write_bytes(b"ok")
    sha = hashlib.sha256(b"ok").hexdigest()
    hist_file = state_dir / "history.jsonl"
    entry = {
        "counts": {"selected": 3},
        "report_date": "2026-10-07",
        "metadata": {"artifact_sha256": "bbbb" * 16, "rendered_at": _now_iso()},
    }
    hist_file.write_text(json.dumps(entry) + "\n", encoding="utf-8")
    with patch("intel_sources.report_delivery.load_artifact",
               return_value=_make_mock_artifact(sha, _now_iso(), 3)):
        code = main(["--data-dir", str(data_dir), "--public-dir", str(public_dir)])
    assert code != 0


def test_gate_fails_when_counts_mismatch(tmp_path):
    from scripts.gate_daily_nonempty import main
    data_dir = tmp_path / "data"
    public_dir = tmp_path / "public"
    state_dir = data_dir / "daily_state"
    state_dir.mkdir(parents=True, exist_ok=True)
    public_dir.mkdir(parents=True, exist_ok=True)
    (public_dir / "index.html").write_bytes(b"ok")
    sha = hashlib.sha256(b"ok").hexdigest()
    hist_file = state_dir / "history.jsonl"
    entry = {
        "counts": {"selected": 99},
        "report_date": "2026-10-07",
        "metadata": {"artifact_sha256": sha, "rendered_at": _now_iso()},
    }
    hist_file.write_text(json.dumps(entry) + "\n", encoding="utf-8")
    with patch("intel_sources.report_delivery.load_artifact",
               return_value=_make_mock_artifact(sha, _now_iso(), 3)):
        code = main(["--data-dir", str(data_dir), "--public-dir", str(public_dir)])
    assert code != 0


def test_gate_fails_when_timestamp_mismatch(tmp_path):
    from scripts.gate_daily_nonempty import main
    data_dir = tmp_path / "data"
    public_dir = tmp_path / "public"
    state_dir = data_dir / "daily_state"
    state_dir.mkdir(parents=True, exist_ok=True)
    public_dir.mkdir(parents=True, exist_ok=True)
    (public_dir / "index.html").write_bytes(b"ok")
    sha = hashlib.sha256(b"ok").hexdigest()
    bad_ts = "2025-01-01T00:00:00+00:00"
    hist_file = state_dir / "history.jsonl"
    entry = {
        "counts": {"selected": 3},
        "report_date": "2026-10-07",
        "metadata": {"artifact_sha256": sha, "rendered_at": bad_ts},
    }
    hist_file.write_text(json.dumps(entry) + "\n", encoding="utf-8")
    with patch("intel_sources.report_delivery.load_artifact",
               return_value=_make_mock_artifact(sha, _now_iso(), 3)):
        code = main(["--data-dir", str(data_dir), "--public-dir", str(public_dir)])
    assert code != 0


def test_gate_fails_when_invalid_public_html(tmp_path):
    from scripts.gate_daily_nonempty import main
    data_dir = tmp_path / "data"
    public_dir = tmp_path / "public"
    state_dir = data_dir / "daily_state"
    state_dir.mkdir(parents=True, exist_ok=True)
    public_dir.mkdir(parents=True, exist_ok=True)
    (public_dir / "index.html").write_text("not-valid-html", encoding="utf-8")
    sha = hashlib.sha256(b"not-valid-html").hexdigest()
    hist_file = state_dir / "history.jsonl"
    entry = {
        "counts": {"selected": 3},
        "report_date": "2026-10-07",
        "metadata": {"artifact_sha256": sha, "rendered_at": _now_iso()},
    }
    hist_file.write_text(json.dumps(entry) + "\n", encoding="utf-8")
    with patch("intel_sources.report_delivery.load_artifact") as mock_load:
        mock_load.side_effect = Exception("invalid artifact")
        code = main(["--data-dir", str(data_dir), "--public-dir", str(public_dir)])
    assert code != 0


def test_gate_fails_when_history_empty(tmp_path):
    from scripts.gate_daily_nonempty import main
    data_dir = tmp_path / "data"
    public_dir = tmp_path / "public"
    state_dir = data_dir / "daily_state"
    state_dir.mkdir(parents=True, exist_ok=True)
    public_dir.mkdir(parents=True, exist_ok=True)
    (public_dir / "index.html").write_bytes(b"<html>ok</html>")
    (state_dir / "history.jsonl").write_text("", encoding="utf-8")
    with patch("intel_sources.report_delivery.load_artifact"):
        code = main(["--data-dir", str(data_dir), "--public-dir", str(public_dir)])
    assert code != 0
