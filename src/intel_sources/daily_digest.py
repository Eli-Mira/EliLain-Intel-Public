"""Daily digest builder with strict gates and explainable scoring."""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

_SECRET_RE = re.compile(
    r'(?i)(api[_-]?key|sk-[A-Za-z0-9_-]{6,}|gh[pousr]_[A-Za-z0-9]{16,}|'
    r'github_pat_[A-Za-z0-9_]{20,}|bearer\s+[A-Za-z0-9._~+/=-]{12,}|'
    r'password\s*[=:]?\s*\S+)'
)
_PATH_RE = re.compile(
    r'''https?://[^\s<>"']+|(?<![a-zA-Z])[a-zA-Z]:[/\\]|/(?:home|tmp|var|etc|root|usr)/''',
    re.IGNORECASE,
)
_CONTROL_RE = re.compile(r'[\x00-\x1f\x7f]')
# Only strip real HTML tags (letter after <) and closing tags, preserve angle-bracket tech text like v<2.0>
_HTML_TAG_RE = re.compile(r'<[a-zA-Z][^>]*>.*?</[a-zA-Z][^>]*>', re.DOTALL)
_PROTOCOL_RE = re.compile(r'^([a-zA-Z][a-zA-Z0-9+.\-]*):', re.IGNORECASE)
_GENERIC_WHY_RE = re.compile(
    r'(?i)^\s*(relevant|important|useful|interesting|good|bad|new|old|updated|'
    r'related|trending|popular|top|best|recommended|see above|none|n/a|unknown|'
    r'placeholder|generic|vague|short|brief|minimal|none|not applicable)\s*$'
)
_APPROVED_DAILY_SOURCES = {'openrouter-models', 'github-trending', 'skills-hot', 'unorouter-pricing'}
_ALLOWED_URL_HOSTS = {
    'openrouter.ai', 'github.com', 'skills.sh', 'api.unorouter.com', 'unorouter.com',
    'api.github.com',  # bounded public license metadata lookup, uncredentialed
    'example.com', 'test.invalid',
}


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except Exception:
        return None


def _score_item(
    cand: dict[str, Any],
    settings: dict[str, Any],
    now: datetime,
    source_total_map: dict[str, int] | None = None,
    source_verified_map: dict[str, int] | None = None,
) -> tuple[int, dict[str, int]]:
    """Score a candidate 0-100 with explainable breakdown."""
    source_total_map = source_total_map or {}
    source_verified_map = source_verified_map or {}

    # Recency: 20 (today) down to 5 (10+ days)
    published = _parse_iso(cand.get('published_at'))
    if published:
        age_days = (now - published).days
        if age_days <= 0:
            recency = 20
        elif age_days == 1:
            recency = 18
        elif age_days <= 3:
            recency = 15
        elif age_days <= 7:
            recency = 10
        else:
            recency = 5
    else:
        recency = 0

    # Free tier: 15 if open-source or free-tier AND has public evidence URL
    # Policy note: 'public' access alone does NOT establish license or free API
    # entitlement; only explicitly evidenced open-source (OSI license metadata)
    # or explicit free-tier pricing earns the bonus. Public-only and unknown
    # controls get free_tier=0 (honest, not padded).
    free_status = (cand.get('free') or {}).get('status', '')
    free_evidence = (cand.get('free') or {}).get('evidence_url', '')
    free_tier = 15 if free_status in ('open-source', 'free-tier') and free_evidence else 0

    # API available: 10 if available AND has public evidence URL
    api_status = (cand.get('api') or {}).get('status', '')
    api_evidence = (cand.get('api') or {}).get('evidence_url', '')
    api_available = 10 if api_status == 'available' and api_evidence else 0

    # Topic match: 5 per matched topic, capped at 15
    topics = cand.get('topics') or []
    settings_topics = set(settings.get('topics') or [])
    matched = sum(1 for t in topics if t in settings_topics)
    topic_match = min(15, matched * 5)

    # Source quality: based on verified-link ratio (quantity ≠ authority)
    source_id = cand.get('source_id', '')
    if not source_id:
        source_quality = 0
    else:
        total = source_total_map.get(source_id, 0)
        verified = source_verified_map.get(source_id, 0)
        if total == 0:
            source_quality = 0
        else:
            source_quality = int(20 * verified / total)

    # Category priority: priority * 2
    category = cand.get('category', '')
    priorities = settings.get('priorities') or {}
    priority = priorities.get(category, 0)
    category_priority = priority * 2

    components = {
        'recency': recency,
        'free_tier': free_tier,
        'api_available': api_available,
        'topic_match': topic_match,
        'source_quality': source_quality,
        'category_priority': category_priority,
    }
    total = min(100, sum(components.values()))
    components['total'] = total
    return total, components


def _sanitize_text(value: Any) -> str:
    """Sanitize a text value for public projection."""
    if not isinstance(value, str):
        return ''
    # Match whole HTTP URLs first so their path segments are not local-path tokens.
    value = _PATH_RE.sub(
        lambda match: match.group() if match.group().lower().startswith(('https://', 'http://')) else '[PATH REDACTED]',
        value,
    )
    value = _SECRET_RE.sub('[REDACTED]', value)
    value = _CONTROL_RE.sub('', value)
    # Only strip real HTML tags (starting with letter), preserve tech text like v<2.0>
    value = _HTML_TAG_RE.sub('', value)
    return value.strip()


def _check_gates(cand: dict[str, Any], settings: dict[str, Any], now: datetime) -> str | None:
    """Return rejection reason or None if eligible."""
    categories_allowed = set(settings.get('categories', []))
    if cand.get('category') not in categories_allowed:
        return 'unsupported category'
    if cand.get('date_kind') == 'unknown':
        return 'observed only date'
    lv = cand.get('link_verification', {})
    if lv.get('status') != 'verified':
        return 'link not verified'
    # Reject suspicious URL protocols before scoring/rendering
    source_url = cand.get('source_url', '')
    proto_match = _PROTOCOL_RE.match(source_url)
    if proto_match:
        proto = proto_match.group(1).lower()
        if proto in ('javascript', 'data', 'vbscript', 'file'):
            return 'unsafe url protocol'
    # Validate source ID is in approved daily families
    source_id = cand.get('source_id', '')
    if source_id and source_id not in _APPROVED_DAILY_SOURCES:
        return 'unapproved source id'
    # Validate URL host is in approved families
    if source_url:
        host = source_url.split('//')[1].split('/')[0].lower() if '://' in source_url else ''
        if host and host not in _ALLOWED_URL_HOSTS:
            return 'unapproved source host'
    published = _parse_iso(cand.get('published_at'))
    if not published:
        return 'missing published_at'
    # Handle naive datetimes: treat them as UTC for comparison
    if published.tzinfo is None:
        published = published.replace(tzinfo=timezone.utc)
    # Use actual seconds for future date detection, not floor days
    diff_seconds = (now - published).total_seconds()
    if diff_seconds < 0:
        return 'future date'
    max_age_days = settings.get('max_age_days', 7)
    if diff_seconds > max_age_days * 86400:
        return 'too old'
    what = (cand.get('what') or '').strip()
    if len(what) < 20:
        return 'empty description'
    # Require meaningful why, not generic placeholder
    why = (cand.get('why') or '').strip()
    if not why or _GENERIC_WHY_RE.match(why):
        return 'generic why'
    free_detail = (cand.get('free') or {}).get('detail', '')
    api_detail = (cand.get('api') or {}).get('detail', '')
    for text in (free_detail, api_detail, source_url):
        if _SECRET_RE.search(text):
            return 'secret evidence'
    return None


def build_digest(
    collection: dict[str, Any],
    settings: dict[str, Any],
    now: datetime | None = None,
) -> dict[str, Any]:
    """Build a daily digest from verified candidates."""
    now = now or datetime.now(timezone.utc)
    candidates = collection.get('candidates', [])
    sources_info = collection.get('sources', [])
    min_score = settings.get('min_score', 60)
    max_items = settings.get('max_items', 5)

    eligible = []
    rejected: list[dict[str, str]] = []
    source_total_map: dict[str, int] = {}
    source_verified_map: dict[str, int] = {}
    # Precompute per-source total and verified-link counts (order-independent)
    for cand in candidates:
        sid = cand.get('source_id', '')
        if sid:
            source_total_map[sid] = source_total_map.get(sid, 0) + 1
            if cand.get('link_verification', {}).get('status') == 'verified':
                source_verified_map[sid] = source_verified_map.get(sid, 0) + 1

    for cand in candidates:
        reason = _check_gates(cand, settings, now)
        if reason:
            rejected.append({'id': cand.get('id', ''), 'reason': reason})
            continue
        score, breakdown = _score_item(cand, settings, now, source_total_map, source_verified_map)
        if score < min_score:
            rejected.append({'id': cand.get('id', ''), 'reason': 'score below threshold'})
            continue
        eligible.append((score, breakdown, cand))

    eligible.sort(key=lambda x: x[0], reverse=True)

    seen_ids: set[str] = set()
    scored = []
    for score, breakdown, cand in eligible:
        # Dedup by canonical original-story URL only (per contract ruling)
        ident_key = cand.get('source_url', '')
        if ident_key in seen_ids:
            rejected.append({'id': cand.get('id', ''), 'reason': 'duplicate source url'})
            continue
        seen_ids.add(ident_key)
        projected = {
            'id': _sanitize_text(cand.get('id', '')),
            'name': _sanitize_text(cand.get('name', '')),
            'what': _sanitize_text(cand.get('what', '')),
            'why': _sanitize_text(cand.get('why', '')),
            'category': _sanitize_text(cand.get('category', '')),
            'source_id': _sanitize_text(cand.get('source_id', '')),
            'source_url': _sanitize_text(cand.get('source_url', '')),
            'link_verification': cand.get('link_verification', {}),
            'observed_at': _sanitize_text(cand.get('observed_at', '')),
            'free': {
                'status': _sanitize_text((cand.get('free') or {}).get('status', '')),
                'detail': _sanitize_text((cand.get('free') or {}).get('detail', '')),
            },
            'api': {
                'status': _sanitize_text((cand.get('api') or {}).get('status', '')),
                'detail': _sanitize_text((cand.get('api') or {}).get('detail', '')),
            },
            'score': score,
            'scoring_breakdown': breakdown,
        }
        scored.append(projected)

    selected = scored[:max_items]
    counts = {
        'fetched': len(candidates),
        'eligible': len(eligible),
        'rejected': len(rejected),
        'selected': len(selected),
    }

    try:
        cairo_tz = ZoneInfo('Africa/Cairo')
        report_date = now.astimezone(cairo_tz).date().isoformat()
    except Exception:
        report_date = now.date().isoformat()

    digest = {
        'schema_version': 1,
        'report_date': report_date,
        'generated_at': now.isoformat(),
        'items': selected,
        'sources': sources_info,
        'counts': counts,
    }
    logger.info(f"Digest built: {counts}")
    return digest


def render_digest(digest: dict[str, Any]) -> str:
    """Render digest to HTML using the daily_digest template."""
    from pathlib import Path
    from jinja2 import Environment, select_autoescape

    template_path = Path(__file__).parent.parent / 'templates' / 'daily_digest.html'
    if not template_path.exists():
        raise ValueError('daily digest template not found')

    template_source = template_path.read_text(encoding='utf-8')
    env = Environment(autoescape=select_autoescape(['html', 'xml']))
    tmpl = env.from_string(template_source)
    safe_digest = _sanitize_digest_for_render(digest)
    # Rename 'items' to 'entries' to avoid dict.items() method collision in Jinja
    # Build history list if provided, otherwise empty
    history_list = digest.get('history', [])
    safe_digest['entries'] = safe_digest.pop('items', safe_digest.get('entries', []))
    history = {"reports": history_list}
    render_context = {"digest": safe_digest, "history": history}
    html = tmpl.render(**render_context)
    return html


def _sanitize_digest_for_render(digest: dict[str, Any]) -> dict[str, Any]:
    """Create a sanitized copy of the digest safe for Jinja rendering."""
    safe = {}
    for key, value in digest.items():
        if key == 'items':
            safe[key] = [_sanitize_item_for_render(item) for item in value]
        elif key == 'sources':
            safe[key] = value
        elif isinstance(value, str):
            safe[key] = _sanitize_text(value)
        else:
            safe[key] = value
    return safe


def _sanitize_item_for_render(item: dict[str, Any]) -> dict[str, Any]:
    """Sanitize a single digest item for rendering."""
    safe = {}
    for key, value in item.items():
        if key == 'link_verification':
            safe[key] = value
        elif key == 'scoring_breakdown':
            safe[key] = value
        elif key in ('free', 'api'):
            safe[key] = {
                'status': _sanitize_text((value or {}).get('status', '')),
                'detail': _sanitize_text((value or {}).get('detail', '')),
            }
        elif isinstance(value, str):
            safe[key] = _sanitize_text(value)
        else:
            safe[key] = value
    return safe
