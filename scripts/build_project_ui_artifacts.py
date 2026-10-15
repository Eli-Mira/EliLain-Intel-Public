"""Latest-only public UI verification artifact; saved observations, never a fetch."""
from __future__ import annotations
import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
sys.path.insert(0, str(ROOT / 'src/tracker-core'))
from intel_sources.public_report import build_public_payload, export_public_report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('output_dir', type=Path)
    parser.add_argument('--state', type=Path, default=ROOT / 'data/intel_state.json')
    parser.add_argument('--news-db', type=Path, default=ROOT / 'data/news.db')
    args = parser.parse_args()
    before = {p: hashlib.sha256(p.read_bytes()).hexdigest()
              for p in (args.state, args.news_db) if p.is_file()}
    now = datetime.now(timezone.utc)
    payload = build_public_payload(json.loads(args.state.read_text(encoding='utf-8')), now=now)
    public = export_public_report(args.state, args.news_db, args.output_dir / 'public', now=now)
    assert before == {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in before}, 'inputs changed'
    print(json.dumps({'public': {
        'path': str(public), 'sha256': hashlib.sha256(public.read_bytes()).hexdigest(),
        'bytes': public.stat().st_size, 'exported': len(payload['tool_items']),
        'inventory_total': payload['inventory_total'],
        'source_counts': dict(Counter(row['source'] for row in payload['tool_items'])),
        'provenance': 'Saved-state regeneration, not a fresh collection or hosted artifact',
        'inputs_unchanged': True}}, indent=2, sort_keys=True))


if __name__ == '__main__':
    main()
