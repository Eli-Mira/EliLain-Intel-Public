"""Preflight or read back the one owned public Pages report; never deploy/send."""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "src/tracker-core"))

from intel_sources.report_delivery import DeliveryError, load_artifact, verify_pages_report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--public-dir', default='./public')
    parser.add_argument('--preflight-only', action='store_true')
    parser.add_argument('--attempts', type=int, choices=range(1, 11), default=5)
    args = parser.parse_args(argv)
    try:
        artifact = load_artifact(args.public_dir)
        receipt = {'bytes': len(artifact.data), 'sha256': artifact.sha256, 'verified': False}
        if not args.preflight_only:
            # Retry read-only GET verification only, never deployment or sends.
            for attempt in range(args.attempts):
                try:
                    hosted = verify_pages_report(args.public_dir)
                    break
                except DeliveryError as error:
                    if error.code not in {'http_status', 'request_failed', 'html_mismatch'} or attempt + 1 == args.attempts:
                        raise
                    time.sleep(2)
            if hosted.sha256 != artifact.sha256:
                raise DeliveryError('artifact_changed')
            receipt.update(url=hosted.url, verified=True)
            output = os.environ.get('GITHUB_OUTPUT')
            if output:
                with Path(output).open('a', encoding='utf-8', newline='\n') as handle:
                    handle.write(f'url={hosted.url}\nsha256={hosted.sha256}\n')
        print(json.dumps(receipt, sort_keys=True))
        return 0
    except DeliveryError as error:
        print(json.dumps({'verified': False, 'code': error.code}), file=sys.stderr)
        return 1
    except (OSError, ValueError):
        print(json.dumps({'verified': False, 'code': 'artifact_unreadable'}), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
