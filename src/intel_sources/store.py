"""Persist discovery baselines without mistaking observation for a launch."""
from __future__ import annotations

import hashlib
import json
import re
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

MAX_UNOBSERVED_PER_SOURCE = 1024
MAX_IDENTITY_HISTORY = 64


def _fingerprint(row: dict) -> str:
    encoded = json.dumps(row.get("fingerprint_data"), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def parse_time(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        except (ValueError, TypeError):
            return None
    if parsed.tzinfo is None:
        return None  # Ambiguous legacy local timestamps are not verification.
    return parsed.astimezone(timezone.utc)


class IntelStore:
    def __init__(self, path: str | Path, max_age_hours: float = 24):
        self.path = Path(path)
        self.max_age_hours = max_age_hours

    @staticmethod
    def _validate_state(state: Any) -> None:
        # Reject partial snapshots rather than silently inventing a baseline.
        if not isinstance(state, dict) or any(not isinstance(state.get(key), list) for key in ('items', 'changes', 'sources')):
            raise ValueError('Invalid intel state structure')
        if any(key in state and type(state[key]) is not bool for key in ('initialized', 'baseline')):
            raise ValueError('Invalid intel state flags')
        for key in ('items', 'changes'):
            if any(not isinstance(row, dict) or any(not isinstance(row.get(field), str) or not row[field] for field in ('id', 'source')) for row in state[key]):
                raise ValueError('Invalid intel item structure')
        if any(not isinstance(row, dict) or not isinstance(row.get('name'), str) or not row['name'] for row in state['sources']):
            raise ValueError('Invalid intel source structure')
        history = state.get('identity_history', [])
        if not isinstance(history, list) or len(history) > MAX_IDENTITY_HISTORY:
            raise ValueError('Invalid intel identity history')
        identifiers = set()
        for row in history:
            if (not isinstance(row, dict) or set(row) != {'id', 'source', 'first_seen', 'fingerprint_sha256'}
                    or any(not isinstance(row.get(key), str) or not 0 < len(row[key]) <= 2048 for key in row)
                    or not re.fullmatch(r'[a-f0-9]{64}', row['fingerprint_sha256'])
                    or parse_time(row['first_seen']) is None or row['id'] in identifiers):
                raise ValueError('Invalid intel identity history')
            identifiers.add(row['id'])

    def read(self, now: datetime | None = None) -> dict[str, Any]:
        now = now or datetime.now(timezone.utc)
        if not self.path.exists():
            return {'items': [], 'changes': [], 'sources': [], 'baseline': True}
        state = json.loads(self.path.read_text(encoding='utf-8'))
        self._validate_state(state)
        by_source = {s['name']: s for s in state['sources']}
        for item in state['items']:
            checked = parse_time(item.get('checked_at'))
            item['freshness'] = 'unverified' if checked is None else ('fresh' if 0 <= (now - checked).total_seconds() <= self.max_age_hours * 3600 else 'stale')
            if by_source.get(item['source'], {}).get('status') not in {'ok', 'partial'} or item.get('not_observed_in_latest_fetch'):
                item['freshness'] = 'stale' if checked else 'unverified'
        return state

    def update(self, items: list[dict], sources: list[dict], now: datetime | None = None) -> dict[str, Any]:
        now = now or datetime.now(timezone.utc)
        previous = self.read(now=now)
        initialized = bool(previous.get('initialized', False))
        old_rows = {r['id']: r for r in previous['items']}
        history = {r['id']: r for r in previous.get('identity_history', [])}
        rows, changes = [], []
        by_source = {s['name']: s for s in sources}
        incoming = {}
        for item in items:
            row = dict(item)
            identifier = row['id']
            if identifier in incoming:
                existing = incoming[identifier]
                row['project_types'] = sorted(set(existing.get('project_types', [])) | set(row.get('project_types', [])))
                row['evidence'] = {**existing.get('evidence', {}), **row.get('evidence', {})}
            incoming[identifier] = row
        for item in incoming.values():
            row = dict(item)
            old = old_rows.get(row['id'])
            identity = old or history.get(row['id'])
            row['first_seen'] = identity.get('first_seen', now.isoformat()) if identity else now.isoformat()
            row['checked_at'] = by_source.get(row['source'], {}).get('checked_at')
            changed = (old.get('fingerprint_data') != row.get('fingerprint_data') if old else
                       identity.get('fingerprint_sha256') != _fingerprint(row) if identity else True)
            if initialized and changed:
                change_type = 'newly_observed' if identity is None else 'updated'
                changes.append({**row, 'change_type': change_type})
            rows.append(row)
        seen = {r['id'] for r in rows}
        observed_sources = {row['source'] for row in incoming.values()}
        unobserved_counts: dict[str, int] = {}
        omitted = 0
        for identifier, old in old_rows.items():
            if identifier not in seen:
                # Only an explicit, nonempty successful snapshot replaces its
                # obsolete inventory. Failure/partial/absent/empty sources are
                # not evidence that previously observed rows disappeared.
                if old['source'] in observed_sources and by_source.get(old['source'], {}).get('status') == 'ok':
                    continue
                source = old['source']
                # Bound only historical siblings of a newly observed partial
                # snapshot. A wholly failed/absent/empty fetch cannot discard
                # the last good current inventory. Incoming rows are never capped.
                if source in observed_sources:
                    count = unobserved_counts.get(source, 0)
                    if count >= MAX_UNOBSERVED_PER_SOURCE:
                        omitted += 1
                        continue
                    unobserved_counts[source] = count + 1
                cached = dict(old)
                cached['not_observed_in_latest_fetch'] = True
                rows.append(cached)
        state = {'items': rows, 'changes': changes, 'sources': sources, 'baseline': not initialized, 'initialized': initialized or bool(items)}
        retained = {row['id'] for row in rows}
        # Removed full rows become compact identity metadata, never raw snapshots.
        # The finite recent-identity horizon cannot establish all-time novelty.
        for identifier, old in old_rows.items():
            if identifier not in retained:
                history.pop(identifier, None)
                history[identifier] = {'id': identifier, 'source': old['source'],
                    'first_seen': old.get('first_seen', now.isoformat()), 'fingerprint_sha256': _fingerprint(old)}
        state['identity_history'] = [entry for identifier, entry in history.items() if identifier not in retained][-MAX_IDENTITY_HISTORY:]
        state['retention'] = {'unobserved_limit_per_source': MAX_UNOBSERVED_PER_SOURCE,
                              'omitted_unobserved': omitted, 'identity_history_limit': MAX_IDENTITY_HISTORY}
        self._validate_state(state)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = None
        try:
            with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=self.path.parent, prefix=self.path.name + '.', suffix='.tmp', delete=False) as handle:
                temp_path = Path(handle.name)
                json.dump(state, handle, indent=2, ensure_ascii=False)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, self.path)
        finally:
            if temp_path is not None:
                temp_path.unlink(missing_ok=True)
        return self.read(now=now)
