"""Gate: fail CI if the daily report produced zero selected items.

Validates that the latest history entry is bound to the EXACT generated daily
HTML artifact on disk: format (via load_artifact), counts agreement, metadata
timestamp, and the artifact SHA persisted by run_daily after export. Stale
history, empty/arbitrary/unrelated/stale HTML, and tampered history all fail.
"""
import argparse
from datetime import datetime
import json
from pathlib import Path
import sys
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "src/tracker-core"))

from bs4 import BeautifulSoup


def _load_history(hist_path: Path):
    lines = hist_path.read_text(encoding="utf-8").strip().splitlines()
    if not lines:
        return None, "empty history.jsonl"
    try:
        last = json.loads(lines[-1])
    except json.JSONDecodeError:
        return None, "history.jsonl last line is not valid JSON"
    if not isinstance(last, dict):
        return None, "history.jsonl last entry is not an object"
    return last, None


def _bind_to_artifact(last: dict, artifact) -> tuple[bool, str]:
    """Return (ok, reason) validating the history entry against the artifact."""
    counts = last.get("counts") or {}
    selected = counts.get("selected", 0)
    if type(selected) is not int or selected <= 0:
        return False, "history selected is not a positive integer"
    # Metadata must carry the artifact SHA the producer persisted.
    metadata = last.get("metadata") or {}
    sha = metadata.get("artifact_sha256")
    if not isinstance(sha, str) or len(sha) != 64:
        return False, "history metadata missing artifact_sha256"
    if sha != artifact.sha256:
        return False, "history SHA does not match the on-disk artifact"
    # Counts must agree with the artifact's actual card count.
    artifact_total = sum(c for _, c in artifact.source_counts)
    if artifact_total != selected:
        return False, (
            f"history selected={selected} != artifact card count {artifact_total}"
        )
    # Metadata timestamp must match the artifact's rendered_at.
    rendered_meta = metadata.get("rendered_at") or last.get("generated_at")
    if not isinstance(rendered_meta, str) or not rendered_meta or rendered_meta != artifact.rendered_at:
        return False, "history timestamp does not match the artifact"
    if "generated_at" in last and last["generated_at"] != artifact.rendered_at:
        return False, "history generation timestamp does not match artifact"
    try:
        rendered = datetime.fromisoformat(artifact.rendered_at)
        if rendered.utcoffset() is None:
            return False, "artifact generation timezone is unknown"
        report_date = rendered.astimezone(ZoneInfo("Africa/Cairo")).date().isoformat()
    except (TypeError, ValueError, KeyError):
        return False, "artifact generation timestamp is invalid"
    if last.get("report_date") != report_date:
        return False, "history report_date does not match artifact generation date"
    kind = BeautifulSoup(artifact.data, "html.parser").select_one('meta[name="elilain-report-kind" i]')
    if kind is None or kind.get("content") != "daily-digest-v1":
        return False, "artifact is not the daily digest format"
    return True, None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", default="data", help="repository data directory")
    parser.add_argument("--public-dir", default="public", help="public export directory")
    args = parser.parse_args(argv)
    hist = Path(args.data_dir) / "daily_state" / "history.jsonl"
    if not hist.exists():
        print("FAIL: no history.jsonl", flush=True)
        return 1
    last, err = _load_history(hist)
    if err:
        print(f"FAIL: {err}", flush=True)
        return 1
    public_idx = Path(args.public_dir) / "index.html"
    if not public_idx.exists():
        print("FAIL: public/index.html artifact is missing", flush=True)
        return 1
    try:
        from intel_sources.report_delivery import load_artifact
        artifact = load_artifact(Path(args.public_dir))
    except Exception as exc:
        print(f"FAIL: public/index.html is not a valid daily artifact ({type(exc).__name__})", flush=True)
        return 1
    ok, err = _bind_to_artifact(last, artifact)
    if not ok:
        print(f"FAIL: history not bound to current artifact: {err}", flush=True)
        return 1
    print(
        f"OK: {last['counts'].get('selected')} items selected, artifact bound "
        f"(sha256 {artifact.sha256[:12]}...)",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

