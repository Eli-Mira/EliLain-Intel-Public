"""Export sanitized HTML only. This command never fetches, deploys or notifies."""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "src/tracker-core"))

from intel_sources.public_report import export_public_report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read saved internal inputs and export a single sanitized public index.html; no fresh fetch or deployment.")
    parser.add_argument("--state", type=Path, default=ROOT / "data/intel_state.json")
    parser.add_argument("--news-db", type=Path, default=ROOT / "data/news.db")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "public")
    parser.add_argument("--max-items-per-source", type=int, default=None)
    args = parser.parse_args(argv)
    try:
        artifact = export_public_report(args.state, args.news_db, args.output_dir,
                                        max_items_per_source=args.max_items_per_source)
        # Read the exact final file rather than claiming what the renderer intended.
        from bs4 import BeautifulSoup
        data = artifact.read_bytes()
        soup = BeautifulSoup(data.decode("utf-8"), "html.parser")
        counts = Counter(row["data-source"] for row in soup.select("#discoveries .discovery-item"))
        print(json.dumps({"artifact": str(artifact.resolve()), "sha256": hashlib.sha256(data).hexdigest(),
                          "bytes": len(data), "source_counts": dict(sorted(counts.items()))}, sort_keys=True))
        return 0
    except (OSError, ValueError, TypeError, sqlite3.Error):
        # Do not echo internal paths, credentials, JSON fragments or database traces.
        print("Public export failed; inputs or destination were rejected.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
