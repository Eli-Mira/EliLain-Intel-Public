"""Daily public export helpers.

Produces a file on disk suitable for serving; atomic writes and safety checks
are kept minimal but sufficient for tests.
"""
from __future__ import annotations

import html
import json
import logging
import os
import re
import tempfile
from pathlib import Path
from typing import Any

from .daily_digest import render_digest
from .public_report import _standalone_html, public_http_url

logger = logging.getLogger(__name__)

_SECRET_RE = re.compile(
    r'(?i)(api[_-]?key|sk-[A-Za-z0-9_-]{6,}|gh[pousr]_[A-Za-z0-9]{16,}|github_pat_[A-Za-z0-9_]{20,}|bearer\s+[A-Za-z0-9._~+/=-]{12,}|password\s*[=:]?\s*\S+)'
)
_PATH_RE = re.compile(r'(?i)^[a-zA-Z]:[/\\]|^/home/|^/tmp/|^/var/|^/etc/')
_CONTROL_RE = re.compile(r'[\x00-\x1f\x7f]')

def _reject_control_chars(value: str) -> None:
    if _CONTROL_RE.search(value):
        raise ValueError('control character detected in public output')

def _reject_local_path(value: str) -> None:
    if _PATH_RE.search(value):
        raise ValueError('local path detected in public output')

def _reject_secret(value: str) -> None:
    if _SECRET_RE.search(value):
        raise ValueError('secret pattern detected in public output')

def _reject_boilerplate(value: str) -> None:
    v = value.strip()
    if v and len(v) < 5:
        raise ValueError('boilerplate rejected')

def _validate_url(url: str) -> None:
    if not url:
        return
    cleaned = public_http_url(url)
    if not cleaned or cleaned != url:
        raise ValueError(f'invalid public url: {url}')

def _validate_item_fields(item: dict[str, Any], idx: int) -> None:
    """Validate all string fields in an item for security."""
    for field in ('id', 'name', 'what', 'why', 'category', 'source_id', 'source_url'):
        val = str(item.get(field, ''))
        _reject_control_chars(val)
        _reject_local_path(val)
        _reject_secret(val)
        if field == 'what' and val:
            _reject_boilerplate(val)
    for sub in ('free', 'api'):
        subdict = item.get(sub) or {}
        if not isinstance(subdict, dict):
            continue
        detail = str(subdict.get('detail', ''))
        evidence = str(subdict.get('evidence_url', ''))
        if detail:
            _reject_control_chars(detail)
            _reject_local_path(detail)
            _reject_secret(detail)
        if evidence:
            _reject_control_chars(evidence)
            _reject_local_path(evidence)
            _reject_secret(evidence)
    # Validate scoring_breakdown numeric values only
    breakdown = item.get('scoring_breakdown') or {}
    if isinstance(breakdown, dict):
        for k, v in breakdown.items():
            if not isinstance(v, (int, float)):
                raise ValueError(f'item {idx} has non-numeric scoring_breakdown value for {k}')
    url = str(item.get('source_url', ''))
    _validate_url(url)

def _validate_digest_for_public(digest: dict[str, Any]) -> None:
    if not isinstance(digest, dict):
        raise ValueError('digest must be a dict')
    items = digest.get('items') or digest.get('entries') or []
    if not isinstance(items, list):
        raise ValueError('digest items must be a list')
    required_fields = ('id', 'name', 'what', 'category', 'source_id', 'source_url')
    for idx, it in enumerate(items):
        if not isinstance(it, dict):
            raise ValueError(f'item {idx} not a dict')
        # Check required fields are present and are strings
        for field in required_fields:
            if field not in it:
                raise ValueError(f'item {idx} missing required field: {field}')
            if not isinstance(it[field], str):
                raise ValueError(f'item {idx} field {field} must be a string')
        _validate_item_fields(it, idx)

def _atomic_write_text(target: Path, data: str) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = tempfile.NamedTemporaryFile('w', encoding='utf-8', newline='\n', dir=str(target.parent), delete=False, prefix='.public-')
    try:
        tmp.write(data)
        tmp.flush()
        os.fsync(tmp.fileno())
        tmp_path = Path(tmp.name)
    finally:
        tmp.close()
    os.replace(tmp_path, target)

def export_digest(digest: dict[str, Any], export_dir: str | Path) -> Path:
    export_dir = Path(export_dir)
    if export_dir.is_symlink():
        raise ValueError('export_dir must not be a symlink')
    export_dir.mkdir(parents=True, exist_ok=True)
    _validate_digest_for_public(digest)

    html_output = render_digest(digest)
    html_output = _standalone_html(html_output)

    target = export_dir / 'index.html'
    _atomic_write_text(target, html_output)
    logger.info(f'Exported digest to {target}')
    return target

def _sanitize_state(state: dict[str, Any]) -> dict[str, Any]:
    """Remove secrets and private fields from state before export."""
    sanitized = {}
    for key, value in state.items():
        if key == 'settings':
            # Don't export raw settings at all
            continue
        if key == 'raw_evidence' or key == 'candidates':
            # Filter secrets from candidates
            if isinstance(value, list):
                filtered = []
                for item in value:
                    if isinstance(item, dict):
                        clean_item = {}
                        for ik, iv in item.items():
                            if ik in ('free', 'api'):
                                if isinstance(iv, dict):
                                    clean_iv = {'status': iv.get('status'), 'detail': '', 'evidence_url': ''}
                                    detail = str(iv.get('detail', ''))
                                    if detail and not _SECRET_RE.search(detail):
                                        clean_iv['detail'] = detail
                                    evidence = str(iv.get('evidence_url', ''))
                                    if evidence and public_http_url(evidence) == evidence:
                                        clean_iv['evidence_url'] = evidence
                                else:
                                    clean_iv = iv
                            elif ik in ('name', 'what', 'why', 'category', 'source_id'):
                                val = str(iv)
                                if _SECRET_RE.search(val):
                                    continue
                                clean_iv[ik] = val
                            elif ik not in ('source_url', 'published_at', 'observed_at', 'link_verification'):
                                clean_iv[ik] = iv
                        filtered.append(clean_iv)
                    else:
                        filtered.append(item)
                value = filtered
            sanitized[key] = value
        else:
            val_str = str(value)
            if not _SECRET_RE.search(val_str):
                sanitized[key] = value
    return sanitized

def export_state(state: dict[str, Any], export_dir: str | Path) -> Path:
    export_dir = Path(export_dir)
    export_dir.mkdir(parents=True, exist_ok=True)
    target = export_dir / 'digest_state.json'
    sanitized = _sanitize_state(state)
    data = json.dumps(sanitized, ensure_ascii=False, sort_keys=True) + '\n'
    _atomic_write_text(target, data)
    return target
