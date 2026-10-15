"""Compatibility entry point for bounded, fail-closed hosted report verification.

Configure PUBLIC_REPORT_URL explicitly; this wrapper has no operator URL and
uses the same single-file and exact-byte checks as the publishing path.
"""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from verify_pages_report import main

if __name__ == "__main__":
    raise SystemExit(main())
