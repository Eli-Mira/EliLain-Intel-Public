"""Latest-only generated outputs. Historical cleanup is discovery-only."""
from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any

MAX_OUTPUT_BYTES = 16 * 1024 * 1024


def latest_report_path(reports_dir: str | Path) -> Path:
    """The one routine internal report; explicit user exports remain separate."""
    return Path(reports_dir) / 'latest.html'


def _safe_path(path: Path) -> None:
    # Inspect every existing component without resolving away symlinks/junctions.
    for part in (path, *path.parents):
        try:
            stat = part.lstat()
        except FileNotFoundError:
            continue
        if part.is_symlink() or getattr(stat, 'st_file_attributes', 0) & 0x400:
            raise ValueError('unsafe_output_path')


def write_latest_text(path: str | Path, text: str, *, max_bytes: int = MAX_OUTPUT_BYTES) -> Path:
    """Replace a routine output atomically; never create a dated archive."""
    target = Path(path)
    data = text.encode('utf-8')
    if len(data) > max_bytes:
        raise ValueError('output_too_large')
    _safe_path(target.absolute())
    temporary = target.with_name(target.name + '.tmp')
    _safe_path(temporary.absolute())
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        with temporary.open('wb') as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    return target


def write_latest_json(path: str | Path, value: Any, *, max_bytes: int = MAX_OUTPUT_BYTES) -> Path:
    """Persist current structured results, not raw search/HTTP response archives."""
    return write_latest_text(path, json.dumps(value, ensure_ascii=False, sort_keys=True) + '\n', max_bytes=max_bytes)


_ARCHIVE_NAME = re.compile(r'(?:eli-lain_|elain_|news_)[0-9]{8}(?:_[0-9]{6})?\.html|news_[0-9]{8}\.json|(?:search|raw-search|snapshot)_[0-9]{8}(?:_[0-9]{6})?\.json')


def discover_local_archives(root: str | Path) -> dict[str, Any]:
    """Read exact top-level archive files only; no recursive scans or deletion.

    Unknown files, all databases/configuration/current state/receipts and links
    are excluded. Calling this does not create directories or persist a manifest.
    """
    base = Path(root).absolute()
    _safe_path(base)
    if not base.is_dir():
        raise ValueError('missing_cleanup_root')
    candidates, excluded = [], []
    for name in ('reports', 'data', '.cache'):
        directory = base / name
        if not directory.exists():
            continue
        try:
            _safe_path(directory)
        except ValueError:
            excluded.append({'path': str(directory), 'reason': 'linked_directory'})
            continue
        if not directory.is_dir():
            continue
        for path in sorted(directory.iterdir()):
            try:
                _safe_path(path)
            except ValueError:
                excluded.append({'path': str(path), 'reason': 'linked_path'})
                continue
            if not path.is_file() or not _ARCHIVE_NAME.fullmatch(path.name):
                excluded.append({'path': str(path), 'reason': 'not_known_archive'})
                continue
            digest = hashlib.sha256()
            size = 0
            with path.open('rb') as handle:
                while chunk := handle.read(65536):
                    size += len(chunk)
                    digest.update(chunk)
            candidates.append({'path': str(path), 'bytes': size, 'sha256': digest.hexdigest()})
    return {'dry_run': True, 'deletion_supported': False, 'root': str(base),
            'scope': ['reports/*', 'data/*', '.cache/*'], 'candidates': candidates,
            'candidate_count': len(candidates), 'candidate_bytes': sum(row['bytes'] for row in candidates),
            'excluded': excluded, 'protected': ['public/index.html', 'reports/latest.html',
                'all databases', 'configuration', 'current state', 'cooldowns', 'delivery receipts',
                'unknown files', 'linked paths', 'nested directories']}
