"""Fail-closed public HTML boundary. Never hand internal dictionaries to a renderer."""
from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation, localcontext
from contextlib import closing
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile
from typing import Any
from urllib.parse import urlsplit

from .store import parse_time

MAX_STATE_BYTES = 16 * 1024 * 1024
MAX_PUBLIC_ITEMS = 10_000
MAX_PUBLIC_PAYLOAD_BYTES = 8 * 1024 * 1024
MAX_PUBLIC_HTML_BYTES = 16 * 1024 * 1024
PUBLIC_SOURCES = {
    "openrouter-models": ("https://openrouter.ai/api/v1/models", "Public model listing; not inference verification."),
    "openrouter-providers": ("https://openrouter.ai/api/v1/providers", "Public hosting-provider listing; not account availability."),
    "unorouter-models": ("https://api.unorouter.com/api/pricing/catalog", "Public pricing catalog; not key-accessible inventory."),
    "github-trending": ("https://github.com/trending", "Observed trending pages only; not all GitHub repositories."),
    "skills-sh": ("https://skills.sh", "Observed public leaderboards only; not the full skill catalog."),
}
PROJECT_TYPES = frozenset({"agents", "audio", "automation", "data", "design", "devops",
    "documentation", "embeddings", "image-generation", "mobile", "multimodal", "reasoning",
    "rerank", "security", "speech", "text", "tools", "transcription", "video", "vision", "web"})


def public_http_url(value: Any) -> str:
    """Allow unambiguous public DNS HTTP(S) links, unchanged; no network/DNS calls.

    Queries, fragments, percent escapes, non-default ports and all IP literals are
    intentionally disallowed rather than trying to recognize every secret format.
    Exported discoveries are further restricted to their source's exact hostname.
    """
    if not isinstance(value, str) or not value or len(value) > 600:
        return ""
    if any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in value):
        return ""
    if any(c in value for c in "\\<>\"'`%?#"):
        return ""
    # A URL reaching this point is pure ASCII word/path punctuation. Anything
    # outside that set (curly quotes, ellipsis, unicode homoglyphs) is a
    # redaction or obfuscation marker, not a usable public link.
    if not re.fullmatch(r"[A-Za-z0-9\-._~:/?=,!$&'()*+]+", value):
        return ""
    # Match credential SHAPES, not substrings: real vendor keys are a fixed
    # prefix plus a long opaque body, and named secrets appear in `name=value`
    # form. A bare substring test discarded legitimate public catalog rows whose
    # names merely contain those words (skills.sh/.../ask-matt,
    # .../email-and-password-best-practices). The bodies are long enough to
    # clear ordinary words: a real `sk-` key is far longer than `sk-matt`.
    if re.search(r"(?i)(?<![\w-])(?:sk-[A-Za-z0-9_-]{6,}|gh[pousr]_[A-Za-z0-9]{16,}"
                 r"|github_pat_[A-Za-z0-9_]{20,}|bearer\s+[A-Za-z0-9._~+/=-]{12,})", value):
        return ""
    if re.search(r"(?i)\b(?:api[_-]?key|access[_-]?token|password|secret|token)"
                 r"\s*[=:]\s*[^\s&]{3,}", value):
        return ""
    try:
        parsed = urlsplit(value)
        host = parsed.hostname or ""
        if parsed.scheme not in {"http", "https"} or parsed.username is not None or parsed.password is not None:
            return ""
        if parsed.port not in {None, 80 if parsed.scheme == "http" else 443}:
            return ""
        if not re.fullmatch(r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}", host):
            return ""  # Also rejects integer, hex, shorthand and IPv6 host encodings.
        if host.endswith((".localhost", ".local", ".localdomain", ".internal", ".lan", ".home", ".home.arpa",
                          ".corp", ".intranet", ".test", ".invalid", ".onion")):
            return ""
        if any(part in {".", ".."} for part in parsed.path.split("/")):
            return ""
    except ValueError:
        return ""
    return value


def private_repository_ids() -> tuple[str, ...]:
    return tuple(value.strip().casefold() for value in
                 os.getenv("PRIVATE_REPOSITORIES", "example-org/private-repository").split(",")
                 if value.strip())


def _public_identity(row: dict[str, Any], source: str) -> tuple[str, str] | None:
    """Bind each public identifier to its expected catalog host and detail path."""
    rules = {
        "openrouter-models": ("openrouter:model:", "openrouter.ai", "/", 2),
        "openrouter-providers": ("openrouter:provider:", "openrouter.ai", "/provider/", 1),
        "unorouter-models": ("unorouter:model:", "unorouter.com", "/en/models/", 1),
        "github-trending": ("github:", "github.com", "/", 2),
        "skills-sh": ("skills:", "skills.sh", "/", 3),
    }
    prefix, host, path_prefix, segments = rules[source]
    identifier = row.get("id")
    url = public_http_url(row.get("url"))
    if not isinstance(identifier, str) or not identifier.startswith(prefix) or not url:
        return None
    slug = identifier[len(prefix):]
    # Explicit operator exclusions win over forged cached visibility labels.
    if source == "github-trending" and slug.casefold() in private_repository_ids():
        return None
    parts = slug.split("/")
    # Only OpenRouter's first (vendor) segment has a documented alias marker.
    # Keep the literal identifier/path; never decode escapes or relax other sources.
    validated_parts = [part[1:] if source == 'openrouter-models' and index == 0 and part.startswith('~') else part
                       for index, part in enumerate(parts)]
    if len(parts) != segments or any(not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,119}", part) for part in validated_parts):
        return None
    parsed = urlsplit(url)
    expected = path_prefix + slug
    matches = parsed.path.casefold() == expected.casefold() if source == "github-trending" else parsed.path == expected
    if parsed.scheme != "https" or parsed.hostname != host or not matches:
        return None
    return identifier, url


def _public_detail_identity(identifier: str, value: Any, source: str) -> bool:
    """Allow the approved Skills.sh detail host alias, never a listing alias."""
    url = public_http_url(value)
    if not url:
        return False
    parsed = urlsplit(url)
    if source == 'skills-sh' and parsed.hostname == 'www.skills.sh':
        # Validate the exact owner/repo/slug with the unchanged listing rules.
        # This candidate is only for validation; keep the original listing link.
        url = parsed._replace(netloc='skills.sh').geturl()
    return _public_identity({'id': identifier, 'url': url}, source) is not None


def _public_title(value: Any, identifier: str, source: str) -> str:
    # Titles are the only selected free text. Never use summaries, errors or raw evidence.
    if (isinstance(value, str) and 0 < len(value) <= 180
            and not value.startswith(("/", "~/"))
            and not any(ord(c) < 32 or ord(c) == 127 for c in value)
            and not any(c in value for c in "<>\\`=@")
            and not re.search(r"(?i)(?:https?://|file:|localhost|[a-z]:/|/users/|/home/|/tmp/|sk-|gh[pousr]_|github_pat_|bearer|password|api[_ -]?key|access[_ -]?token|\btoken\b|secret|account[_ -]?id|key[_ -]?id|traceback|\b\d{1,3}(?:\.\d{1,3}){3}\b|\.(?:internal|local|localhost|lan|home|corp)\b|\[[a-f0-9:]+\])", value)):
        return value
    # A safe identity was already bound to the public detail-page URL.
    prefix_lengths = {"openrouter-models": len("openrouter:model:"),
                      "openrouter-providers": len("openrouter:provider:"),
                      "unorouter-models": len("unorouter:model:"),
                      "github-trending": len("github:"), "skills-sh": len("skills:")}
    return identifier[prefix_lengths[source]:]


# Only closed vocabularies and bounded numbers cross the public metadata seam.
# Cached prose, evidence URLs, pricing overrides and arbitrary nested fields do not.
MODALITIES = frozenset({'text', 'image', 'audio', 'video', 'file', 'embeddings', 'speech', 'transcription', 'rerank', 'decisions'})
PARAMETERS = frozenset({'tools', 'tool_choice', 'reasoning', 'reasoning_effort', 'include_reasoning',
    'response_format', 'structured_outputs', 'temperature', 'top_p', 'top_k', 'seed',
    'max_tokens', 'max_completion_tokens', 'stop', 'frequency_penalty', 'presence_penalty',
    'repetition_penalty', 'logprobs', 'top_logprobs', 'min_p', 'web_search_options',
    'parallel_tool_calls', 'verbosity', 'logit_bias', 'top_a', 'prediction'})
COUNTRIES = frozenset('US GB DE FR CA AU CN JP KR SG IN NL FI SE NO CH IE AE IL BR ES IT PL TW HK RU ZA'.split())
LANGUAGES = frozenset({'Python', 'TypeScript', 'JavaScript', 'Rust', 'Go', 'Shell', 'C++', 'C',
    'PLSQL', 'Java', 'HTML', 'CSS', 'C#', 'Ruby', 'PHP', 'Swift', 'Kotlin', 'Dart', 'Lua',
    'R', 'Scala', 'Julia', 'Elixir', 'Haskell', 'Zig', 'Jupyter Notebook', 'Dockerfile', 'Vue'})
CATALOG_TAGS = frozenset({'Text', 'Tools', 'Vision', 'Reasoning', 'Audio', 'Video', 'Image',
    'Embeddings', 'Embedding', 'Open Weights', 'Task', 'Deprecated', 'Cache', 'ComputerUse', 'Files', 'WebSearch', '1M', '2M', '128K', '200K'})
ENDPOINT_FORMATS = frozenset({'openai', 'anthropic', 'gemini', 'responses', 'chat', 'embeddings', 'images', 'audio',
    'embedding', 'image-generation', 'aihorde', 'openai-alpha-search', 'openai-response', 'openai-response-compact', 'typesafe-decisions'})


def _public_metadata(row: dict[str, Any], source: str, now: datetime) -> list[dict[str, str]]:
    """Reconstruct typed public catalog fields; NEVER copy an evidence dictionary.

    Id/URL binding is checked by the caller. Units are source-specific: OpenRouter
    USD/token is converted to USD/million; UnoRouter already reports USD/million.
    Every label is static; every value is an enum, boolean or bounded number.
    """
    evidence = row.get('evidence')
    if not isinstance(evidence, dict):
        return []
    result = []

    def emit(label: str, value: str) -> None:
        result.append({'label': label, 'value': value})

    def integer(label: str, value: Any, *, suffix: str = '', minimum: int = 0,
                maximum: int = 10_000_000_000) -> None:
        if type(value) is int and minimum <= value <= maximum:
            emit(label, f'{value:,}{suffix}')

    def enum(label: str, value: Any, allowed: frozenset[str]) -> None:
        if isinstance(value, str) and value in allowed:
            emit(label, value)

    def enums(label: str, values: Any, allowed: frozenset[str]) -> None:
        if isinstance(values, list) and len(values) <= 100:
            selected = sorted({v for v in values if isinstance(v, str) and v in allowed})
            if selected:
                emit(label, ', '.join(selected))

    def flag(label: str, value: Any) -> None:
        if type(value) is bool:
            emit(label, str(value).lower())

    def calendar_date(label: str, value: Any) -> None:
        if isinstance(value, str) and re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
            try:
                date.fromisoformat(value)
            except ValueError:
                return
            emit(label, value)

    def price(label: str, value: Any, *, per_token: bool = False, per_request: bool = False,
              unit: str | None = None) -> None:
        if type(value) not in (str, int, float):
            return
        text = str(value)
        if len(text) > 40 or not re.fullmatch(r'\d+(?:\.\d+)?(?:[eE][+-]?\d{1,2})?', text):
            return
        try:
            with localcontext() as context:
                context.prec = 48
                amount = Decimal(text)
                if not amount.is_finite() or not 0 <= amount <= 1_000_000:
                    return
                if per_token:
                    amount *= 1_000_000
                # Prevent giant decimals and hidden underflow turning into "free".
                if amount != 0 and amount < Decimal('0.000000000001'):
                    return
                formatted = format(amount, 'f')
                if '.' in formatted:
                    formatted = formatted.rstrip('0').rstrip('.')
                emit(label, f'USD {formatted} / {unit or ("request" if per_request else "million tokens")}')
        except InvalidOperation:
            return

    if source == 'openrouter-models':
        calendar_date('Catalog expiration date', evidence.get('expiration_date'))
        calendar_date('Catalog knowledge cutoff', evidence.get('knowledge_cutoff'))
        alias = evidence.get('alias_target')
        if isinstance(alias, dict) and isinstance(alias.get('slug'), str):
            slug = alias['slug']
            if _public_identity({'id': 'openrouter:model:' + slug, 'url': 'https://openrouter.ai/' + slug}, source):
                emit('Catalog alias target', slug)
        reasoning = evidence.get('reasoning')
        if isinstance(reasoning, dict):
            for key, label in [('mandatory', 'Reasoning mandatory'), ('default_enabled', 'Reasoning default enabled'),
                               ('supports_max_tokens', 'Reasoning supports max tokens')]:
                flag(label, reasoning.get(key))
            efforts = frozenset({'none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max'})
            enums('Reasoning efforts', reasoning.get('supported_efforts'), efforts)
            enum('Reasoning default effort', reasoning.get('default_effort'), efforts)
        integer('Context', evidence.get('context_length'), suffix=' tokens', minimum=1, maximum=100_000_000)
        top = evidence.get('top_provider')
        if isinstance(top, dict):
            integer('Max output', top.get('max_completion_tokens'), suffix=' tokens', minimum=1, maximum=100_000_000)
        architecture = evidence.get('architecture')
        if isinstance(architecture, dict):
            enums('Input modalities', architecture.get('input_modalities'), MODALITIES)
            enums('Output modalities', architecture.get('output_modalities'), MODALITIES)
        enums('Supported parameters', evidence.get('supported_parameters'), PARAMETERS)
        pricing = evidence.get('pricing')
        if isinstance(pricing, dict):
            price('Listed input price', pricing.get('prompt'), per_token=True)
            price('Listed output price', pricing.get('completion'), per_token=True)
            for key, label in [('input_cache_read', 'Listed cache read price'), ('input_cache_write', 'Listed cache write price'),
                ('input_cache_write_1h', 'Listed 1h cache write price'), ('audio', 'Listed audio input price'),
                ('audio_output', 'Listed audio output price'), ('image_token', 'Listed image token price'),
                ('input_audio_cache', 'Listed audio cache price'), ('internal_reasoning', 'Listed internal reasoning price')]:
                price(label, pricing.get(key), per_token=True)
            price('Listed request price', pricing.get('request'), per_request=True)
            price('Listed input image price', pricing.get('image'), unit='input image')
            price('Listed output image price', pricing.get('image_output'), unit='output image')
            price('Listed web search price', pricing.get('web_search'), unit='web search')
            if isinstance(pricing.get('overrides'), list) and pricing['overrides']:
                emit('Pricing scope', 'Base rates; tiered pricing present — consult source')
    elif source == 'openrouter-providers':
        enum('Headquarters country', evidence.get('headquarters'), COUNTRIES)
        enums('Datacenter countries', evidence.get('datacenters'), COUNTRIES)
    elif source == 'unorouter-models':
        enum('Model type', evidence.get('type'), frozenset({'text', 'image', 'audio', 'video', 'embedding', 'embeddings'}))
        enums('Catalog tags', evidence.get('tags'), CATALOG_TAGS)
        enums('Endpoint formats', evidence.get('supported_endpoint_types'), ENDPOINT_FORMATS)
        pricing = evidence.get('pricing')
        if isinstance(pricing, dict):
            if pricing.get('is_fixed_price') is False:
                price('Listed input price', pricing.get('input_price'))
                price('Listed output price', pricing.get('output_price'))
                price('Catalog original input price', pricing.get('original_input_price'))
                price('Catalog original output price', pricing.get('original_output_price'))
            elif pricing.get('is_fixed_price') is True:
                price('Listed fixed price', pricing.get('fixed_price'), per_request=True)
                price('Catalog original fixed price', pricing.get('original_fixed_price'), per_request=True)
                emit('Fixed pricing scope', 'Catalog request rate; task and variant billing conditions unverified')
        for key, label in [('is_free', 'Catalog free flag'), ('online', 'Catalog online flag'), ('chat', 'Catalog chat flag')]:
            if type(evidence.get(key)) is bool:
                emit(label, str(evidence[key]).lower())
    elif source == 'github-trending':
        enum('Language', evidence.get('language'), LANGUAGES)
        integer('Stars', evidence.get('total_stars'))
        integer('Forks', evidence.get('forks'))
        periods = evidence.get('periods')
        if isinstance(periods, dict):
            for period in ('daily', 'weekly', 'monthly'):
                entry = periods.get(period)
                if isinstance(entry, dict):
                    integer(f'{period} rank', entry.get('rank'), minimum=1, maximum=100_000)
                    integer(f'{period} stars gained', entry.get('stars_gained'))
    elif source == 'skills-sh':
        views = evidence.get('views')
        if isinstance(views, dict):
            for view in ('all-time', 'trending', 'hot'):
                entry = views.get(view)
                if isinstance(entry, dict):
                    integer(f'{view} rank', entry.get('rank'), minimum=1, maximum=10_000_000)
                    integer(f'{view} installs', entry.get('installs'))
    if source in ('github-trending', 'skills-sh'):
        detail = evidence.get('metadata')
        if isinstance(detail, dict) and all(
            _public_detail_identity(row['id'], detail[key], source)
            for key in ('url', 'final_url') if key in detail):
            enum('Detail metadata status', detail.get('status'), frozenset({'ok', 'empty', 'error', 'not-scanned', 'not-attempted'}))
            observed = _timestamp(detail.get('checked_at'), now)
            if observed:
                emit('Detail observed at', observed)
            integer('Detail HTTP status', detail.get('http_status'), minimum=100, maximum=599)
            digest = detail.get('sha256')
            if isinstance(digest, str) and re.fullmatch(r'[a-f0-9]{64}', digest):
                emit('Detail body SHA-256', digest)
            flag('Detail description available', detail.get('description_available'))
            flag('Detail topics available', detail.get('topics_available'))
            enum('Detail description field', detail.get('description_field'), frozenset({'meta[name=description]'}))
    return result


def _timestamp(value: Any, now: datetime) -> str | None:
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})", value):
        return None
    parsed = parse_time(value)
    return value if isinstance(value, str) and parsed is not None and parsed <= now else None


def _public_windows(source: str, row: dict[str, Any]) -> str:
    """Report only public window names, status enums and bounded row counts."""
    coverage = row.get('coverage')
    if not isinstance(coverage, dict):
        return ''
    family, windows, count_key = (
        ('periods', ('daily', 'weekly', 'monthly'), 'item_count') if source == 'github-trending'
        else ('views', ('all-time', 'trending', 'hot'), 'rendered_rows'))
    if source not in ('github-trending', 'skills-sh') or not isinstance(coverage.get(family), dict):
        return ''
    details = []
    for window in windows:
        entry = coverage[family].get(window)
        if not isinstance(entry, dict):
            continue
        status = entry.get('status')
        status = status if status in ('ok', 'partial', 'error', 'skipped') else 'unverified'
        count = entry.get(count_key)
        rows = f'{count:,} rows' if type(count) is int and 0 <= count <= 10_000_000 else 'rows unavailable'
        details.append(f'{window}: {status} ({rows})')
    return ' Saved public windows: ' + '; '.join(details) + '.' if details else ''


def _public_source_details(row: dict[str, Any]) -> str:
    """Source diagnostics are claims about collection, never operational proof."""
    coverage = row.get('coverage')
    if not isinstance(coverage, dict):
        return ''
    details = []
    for key, label in [('returned_count', 'returned'), ('accepted_count', 'accepted'),
                       ('invalid_count', 'invalid'), ('duplicate_count', 'duplicate'), ('declared_total', 'declared total')]:
        value = coverage.get(key)
        if type(value) is int and 0 <= value <= 10_000_000:
            details.append(f'{label}: {value:,}')
    for key, label in [('has_more', 'has more'), ('pagination_followed', 'pagination followed')]:
        if type(coverage.get(key)) is bool:
            details.append(f'{label}: {str(coverage[key]).lower()}')
    if coverage.get('output_modalities') == 'all':
        details.append('output modalities: all')
    for key, label in [('http_cache_age_seconds', 'HTTP cache age'), ('http_cache_max_age_seconds', 'HTTP cache max age')]:
        value = coverage.get(key)
        if type(value) is int and 0 <= value <= 10_000_000_000:
            details.append(f'{label}: {value:,} seconds')
    cache = coverage.get('http_cache_freshness')
    if isinstance(cache, str) and cache in ('within-max-age', 'exceeds-max-age', 'unknown'):
        details.append(f'HTTP cache freshness: {cache}')
    metadata = coverage.get('metadata')
    if isinstance(metadata, dict):
        for key in ('budget', 'attempted', 'enriched', 'descriptions', 'topics_available', 'empty', 'errors', 'not_scanned'):
            value = metadata.get(key)
            if type(value) is int and 0 <= value <= 10_000_000:
                details.append(f'detail {key.replace("_", " ")}: {value:,}')
    return ' Saved collection diagnostics: ' + '; '.join(details) + '.' if details else ''


def build_public_payload(state: dict[str, Any], *, now: datetime | None = None,
                         max_items_per_source: int | None = None) -> dict[str, Any]:
    """Project saved public discovery observations into the unchanged Lain contract."""
    if (not isinstance(state, dict) or not isinstance(state.get("items"), list)
            or not isinstance(state.get("sources"), list)):
        raise ValueError("saved state must contain items and sources lists")
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("render time must include a timezone")
    now = now.astimezone(timezone.utc)
    if max_items_per_source is not None and (type(max_items_per_source) is not int or not 1 <= max_items_per_source <= 200):
        raise ValueError("export limit must be an integer from 1 to 200")
    sources = {s["name"]: s for s in state.get("sources", [])
               if isinstance(s, dict) and isinstance(s.get("name"), str) and s["name"] in PUBLIC_SOURCES}
    private_sources = {name for name, source in sources.items()
                       if isinstance(source.get('coverage'), dict)
                       and (source['coverage'].get('mode') == 'api'
                            or source['coverage'].get('scope') in ('key-accessible', 'local-aggregated'))}
    sources = {name: source for name, source in sources.items() if name not in private_sources}
    tools = []
    counts = {name: 0 for name in PUBLIC_SOURCES}
    seen = set()
    saved_counts = {name: 0 for name in PUBLIC_SOURCES}
    for row in state['items']:
        if isinstance(row, dict) and isinstance(row.get('source'), str) and row['source'] in saved_counts:
            saved_counts[row['source']] += 1
    candidates = [r for r in state.get("items", []) if isinstance(r, dict)
                  and isinstance(r.get("source"), str) and isinstance(r.get("id"), str)]
    for row in sorted(candidates, key=lambda r: (r["source"], r["id"])):
        source = row.get("source")
        if source not in PUBLIC_SOURCES or source in private_sources:
            continue
        raw_evidence = row.get('evidence')
        if isinstance(raw_evidence, dict):
            # Public names/URLs cannot authorize an explicitly key-scoped origin.
            scope = raw_evidence.get('catalog_scope')
            provenance = raw_evidence.get('provenance')
            if (scope is not None and scope != 'public') or (
                isinstance(provenance, dict) and provenance.get('kind') in (
                    'key_scoped_catalog', 'local_aggregated_catalog')):
                continue
        identity = _public_identity(row, source)
        if identity is None or (source, identity[0]) in seen:
            continue
        identifier, url = identity
        seen.add((source, identifier))
        checked = _timestamp(row.get("checked_at"), now)
        first = _timestamp(row.get("first_seen"), now)
        evidence = {"observed_at": checked or "not supplied", "first_observed_at": first or "not supplied"}
        freshness = "unverified" if checked is None else "fresh" if (
            (now - parse_time(checked)).total_seconds() <= 24 * 3600
            and sources.get(source, {}).get("status") in ("ok", "partial")
            and not row.get("not_observed_in_latest_fetch")) else "stale"
        summary = ("Public listing. Inference and free entitlement unverified."
                   if source in {"openrouter-models", "openrouter-providers", "unorouter-models"}
                   else "Public discovery listing; advisory only, not evaluated or executed.")
        tools.append({"id": identifier, "headline": _public_title(row.get("headline"), identifier, source), "source": source,
                      "url": url, "summary": summary,
                      "project_types": sorted({t for t in row.get("project_types", [])
                                               if isinstance(t, str) and t in PROJECT_TYPES})
                          if isinstance(row.get("project_types"), list) else [],
                      "freshness": freshness, "first_observed_at": first, "evidence": [evidence]})
        # No cross-source popularity score. Models/providers expose no such metric.
        metric = None
        basis = {'github-trending': 'GitHub total stars',
                 'skills-sh': 'Skills.sh all-time installs'}.get(source, 'Popularity unavailable for this source')
        if isinstance(raw_evidence, dict):
            if source == 'github-trending':
                metric, basis = raw_evidence.get('total_stars'), 'GitHub total stars'
            elif source == 'skills-sh':
                views = raw_evidence.get('views')
                all_time = views.get('all-time') if isinstance(views, dict) else None
                metric = all_time.get('installs') if isinstance(all_time, dict) else None
                basis = 'Skills.sh all-time installs'
        if type(metric) is not int or not 0 <= metric <= 10_000_000_000:
            metric = None
        tools[-1]['popularity'] = {'value': metric, 'basis': basis}
        metadata = _public_metadata(row, source, now)
        if metadata:
            tools[-1]['public_metadata'] = metadata
        counts[source] += 1
        if len(tools) > MAX_PUBLIC_ITEMS:
            raise ValueError('public inventory exceeds row limit')
    full_inventory = tools
    inventory_counts = dict(counts)
    # Personalized evaluation belongs only to explicitly requested internal views.
    tools = []
    counts = {name: 0 for name in PUBLIC_SOURCES}
    for row in full_inventory:
        if max_items_per_source is None or counts[row["source"]] < max_items_per_source:
            tools.append(row)
            counts[row["source"]] += 1
    public_sources = []
    for name in sorted(set(sources) | {name for name, count in inventory_counts.items() if count}):
        row = sources.get(name, {})
        url, coverage = PUBLIC_SOURCES[name]
        status = row.get("status") if row.get("status") in ("ok", "partial", "error", "skipped") else "unverified"
        public_sources.append({"name": name, "status": status, "checked_at": _timestamp(row.get("checked_at"), now),
                               "item_count": counts[name], "url": url,
                               "coverage": (f"{coverage} " + (
                                            "Complete validated saved inventory; counts are exported rows. " if max_items_per_source is None else
                                            f"Export bounded to {max_items_per_source} rows per source by caller; counts are exported rows. ") +
                                            f"Valid saved public inventory: {inventory_counts[name]:,} rows."
                                            f" Retained validated public inventory: {inventory_counts[name]:,} rows; exported: {counts[name]:,}; "
                                            f"omitted by caller cap: {inventory_counts[name] - counts[name]:,}."
                                            f" Saved source rows: {saved_counts[name]:,}; excluded by public policy or duplicate identity: "
                                            f"{saved_counts[name] - inventory_counts[name]:,}."
                                            + _public_windows(name, row) + _public_source_details(row))})
    payload = {"inventory_total": len(full_inventory), "api_items": [], "news_items": [], "tool_items": tools, "source_items": public_sources,
            "change_items": [], "action_items": [], "report_date": now.strftime("%Y-%m-%d"),
            "generated_at": now.isoformat(), "persona_image_url": None, "baseline": False,
            "runtime_source": "Public sanitized export of saved observations; not a fresh fetch. Provider statuses, changes, actions and news are excluded, not evaluated by this export. Private source coverage is omitted."}
    if len(json.dumps(payload, ensure_ascii=False, separators=(',', ':')).encode('utf-8')) > MAX_PUBLIC_PAYLOAD_BYTES:
        raise ValueError('public payload exceeds maximum size')
    return payload


def read_saved_inputs(state_path: str | Path, news_db_path: str | Path, *,
                      news_limit: int = 100) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Read saved inputs only. Returned news stays internal and is never rendered."""
    if type(news_limit) is not int or not 1 <= news_limit <= 200:
        raise ValueError("news read limit must be an integer from 1 to 200")
    state_path, news_db_path = Path(state_path), Path(news_db_path)
    with state_path.open("rb") as handle:
        data = handle.read(MAX_STATE_BYTES + 1)
    if len(data) > MAX_STATE_BYTES:
        raise ValueError("saved state exceeds maximum size")
    state = json.loads(data.decode("utf-8"))
    news = []
    if news_db_path.is_file():
        with closing(sqlite3.connect(news_db_path.resolve().as_uri() + "?mode=ro", uri=True)) as conn:
            conn.execute("PRAGMA query_only=ON")
            conn.row_factory = sqlite3.Row
            news = [dict(row) for row in conn.execute(
                "SELECT source, title, url, published_date, fetched_date, summary "
                "FROM news_items ORDER BY fetched_date DESC, id DESC LIMIT ?", (news_limit,))]
    return state, news


def _check_export_directory(directory: Path) -> None:
    # Reject symlinks and Windows junction/reparse-point ancestors, not just index.
    for path in (directory.absolute(), *directory.absolute().parents):
        if path.is_symlink() or (path.exists() and getattr(path.lstat(), "st_file_attributes", 0) & 0x400):
            raise ValueError("export directory must not contain links or reparse points")
    if directory.exists():
        if not directory.is_dir():
            raise ValueError("export directory must be a directory")
        for entry in directory.iterdir():
            if entry.name != "index.html" or not entry.is_file() or entry.is_symlink():
                raise ValueError("export directory may contain only a regular index.html")
            if getattr(entry.lstat(), "st_file_attributes", 0) & 0x400:
                raise ValueError("index.html must not be a reparse point")


def _standalone_html(html: str) -> str:
    """Keep the original markup/styles/filter; block all automatic network access."""
    from bs4 import BeautifulSoup

    if len(html.encode('utf-8')) > MAX_PUBLIC_HTML_BYTES:
        raise ValueError('public HTML exceeds maximum size')
    soup = BeautifulSoup(html, "html.parser")
    if soup.select("script[src], link, iframe, object, embed, form, base, audio, video, source"):
        raise ValueError("public template contains an external or active dependency")
    from tracker_core.branding import avatar_data_uri, AVATAR_ALT
    images = soup.select('img')
    if len(images) > 1 or any(
        dict(image.attrs) != {'src': avatar_data_uri(), 'alt': AVATAR_ALT}
        for image in images
    ):
        raise ValueError("public template contains an untrusted image dependency")
    if any(re.search(r"(?i)(?:url\s*\(|@import)", style.get_text()) for style in soup.select("style")):
        raise ValueError("public template contains a CSS dependency")
    # Only explicitly owned report sections with existing targets bypass HTTP validation.
    anchors = {'#main', '#discoveries', '#source-health', '#public-scope', '#daily-digest', '#report-history'}
    if any(public_http_url(a.get("href")) != a.get("href")
           and not (a.get('href') in anchors and soup.select_one(a['href']) is not None)
           for a in soup.select("a[href]")):
        raise ValueError("public template contains an unsafe link dependency")
    if any(key.lower().startswith("on") for tag in soup.find_all(True) for key in tag.attrs):
        raise ValueError("public template contains an active event dependency")
    script_hashes = ["'sha256-" + base64.b64encode(hashlib.sha256(script.get_text().encode("utf-8")).digest()).decode("ascii") + "'"
                     for script in soup.select("script")]
    policy = ("default-src 'none'; script-src " + (" ".join(script_hashes) or "'none'") +
              "; style-src 'unsafe-inline'; connect-src 'none'; img-src " + ("data:" if images else "'none'") + "; font-src 'none'; "
              "object-src 'none'; media-src 'none'; base-uri 'none'; form-action 'none'")
    if '<meta charset="UTF-8">' not in html:
        raise ValueError("public template head dependency is unsupported")
    standalone = html.replace('<meta charset="UTF-8">', '<meta charset="UTF-8">\n'
                        f'<meta http-equiv="Content-Security-Policy" content="{policy}">\n'
                        '<meta name="referrer" content="no-referrer">', 1)
    if len(standalone.encode('utf-8')) > MAX_PUBLIC_HTML_BYTES:
        raise ValueError('public HTML exceeds maximum size')
    return standalone


def export_public_report(state_path: str | Path, news_db_path: str | Path, export_dir: str | Path,
                         *, now: datetime | None = None, max_items_per_source: int | None = None) -> Path:
    """Produce a single Lain index.html from explicitly projected saved state."""
    from tracker_core.reporter_html import LainReporter

    directory = Path(export_dir)
    _check_export_directory(directory)
    state, _internal_news = read_saved_inputs(state_path, news_db_path)
    payload = build_public_payload(state, now=now, max_items_per_source=max_items_per_source)
    from jinja2 import TemplateError
    try:
        rendered = LainReporter().render(**payload, public_export=True)
    except TemplateError:
        raise ValueError('public template unavailable') from None
    html = _standalone_html(rendered)
    if len(html.encode('utf-8')) > MAX_PUBLIC_HTML_BYTES:
        raise ValueError('public HTML exceeds maximum size')
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / "index.html"
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="\n", dir=directory,
                                         prefix=".public-", suffix=".tmp", delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(html)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return target
