"""Publish the sanitized single-file export and notify both channels.

Exit 0 means the hosted HTML byte-matched the export and BOTH channels
confirmed. Any partial outcome exits 1 with a safe code. This command never
prints credentials, response bodies or token-bearing request URLs.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "src/tracker-core"))

from intel_sources.report_delivery import DeliveryError, deliver_report  # noqa: E402

__all__ = ["DeliveryError", "deliver_report", "main"]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify the sanitized index.html on Cloudflare Pages, then notify both channels, "
                                                 "verify the hosted bytes, then send the brief to Telegram and Discord.")
    parser.add_argument("--export-dir", type=Path, default=ROOT / "public",
                        help="Directory containing only the sanitized index.html (default: public)")
    parser.add_argument("--state-path", type=Path, default=ROOT / "data/delivery_receipts.json",
                        help="Persistent bounded channel confirmations (default: data/delivery_receipts.json)")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--attach-html", action="store_const", const="true", dest="attachment",
                      help="Explicit secondary standalone HTML export, in addition to the hosted link")
    mode.add_argument("--link-only", action="store_const", const="false", dest="attachment",
                      help="Explicitly disable attachments even if PUBLIC_REPORT_ATTACHMENT=true")
    parser.add_argument("--poll-attempts", type=int, default=30)
    parser.add_argument("--poll-interval", type=float, default=1.0)
    parser.add_argument("--json", action="store_true", help="Print a machine-readable receipt summary")
    args = parser.parse_args(argv)
    env = dict(os.environ)
    if args.attachment is not None:
        env["PUBLIC_REPORT_ATTACHMENT"] = args.attachment
    try:
        result = deliver_report(args.export_dir, poll_attempts=args.poll_attempts,
                                poll_interval=args.poll_interval, state_path=args.state_path, environ=env)
    except DeliveryError as exc:
        payload = {"ok": False, "code": exc.code}
        if exc.result is not None:
            payload["hosted_url"] = exc.result.hosted.url
            payload["telegram"] = {"confirmation": exc.result.telegram.confirmation,
                                   "message_id": exc.result.telegram.message_id,
                                   "error_code": exc.result.telegram.error_code}
            payload["discord"] = {"confirmation": exc.result.discord.confirmation,
                                  "message_id": exc.result.discord.message_id,
                                  "error_code": exc.result.discord.error_code}
        # Machine-readable receipts always go to stdout so the workflow can
        # parse one stream; the exit code carries the failure.
        print(json.dumps(payload, sort_keys=True) if args.json else f"Delivery failed: {exc.code}")
        return 1
    print(json.dumps({"ok": True, "hosted_url": result.hosted.url, "sha256": result.hosted.sha256,
                      "deploy_id": result.hosted.deploy_id,
                      "telegram": {"confirmation": result.telegram.confirmation, "message_id": result.telegram.message_id},
                      "discord": {"confirmation": result.discord.confirmation, "message_id": result.discord.message_id},
                      "brief": result.brief}, sort_keys=True) if args.json
          else f"Published {result.hosted.url}\ntelegram: {result.telegram.confirmation} {result.telegram.message_id}\n"
               f"discord: {result.discord.confirmation} {result.discord.message_id}\n{result.brief}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
