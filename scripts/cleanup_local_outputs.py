"""Discover exact legacy local archive candidates. Never deletes any file."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))

from intel_sources.retention import discover_local_archives  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description='Read-only archive inventory; deletion is not supported.')
    parser.add_argument('--root', type=Path, default=ROOT, help='Exact tracker root to inspect, without recursion')
    parser.add_argument('--dry-run', action='store_true', help='Explicit spelling of the only supported mode')
    parser.add_argument('--json', action='store_true', help='Print exact paths, byte counts, SHA256 and exclusions')
    args = parser.parse_args(argv)
    try:
        manifest = discover_local_archives(args.root)
    except (ValueError, OSError):
        print(json.dumps({'ok': False, 'code': 'cleanup_scope_unreadable', 'dry_run': True}))
        return 1
    if args.json:
        print(json.dumps(manifest, sort_keys=True))
    else:
        print(f"DRY RUN ONLY: {manifest['candidate_count']} candidates, {manifest['candidate_bytes']} bytes; nothing deleted.")
        for entry in manifest['candidates']:
            print(f"{entry['path']} | {entry['bytes']} bytes | sha256={entry['sha256']}")
        print('Excludes databases, configuration, current state, receipts, unknown files, linked paths and nested directories.')
        print('Any later historical deletion requires review and approval of these exact paths/hashes.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
