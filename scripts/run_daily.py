"""CLI entry point for the daily scan."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from intel_sources.daily_automation import run_daily


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description='Run daily intel scan')
    parser.add_argument('--settings', type=str, default=None, help='Path to settings JSON')
    parser.add_argument('--data-dir', type=str, default='data', help='Base data directory')
    parser.add_argument('--public-dir', type=str, default=None, help='Exact public export directory (default: data_dir/public)')
    args = parser.parse_args(argv)

    try:
        result = asyncio.run(run_daily(settings_path=args.settings, data_dir=args.data_dir, public_dir=args.public_dir))
    except Exception as exc:
        print(f"Daily run failed: {exc}", file=sys.stderr)
        return 1

    print(f"report_date={result['report_date']} public={result['public_path']} selected={result['counts'].get('selected',0)}")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
