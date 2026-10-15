"""Read-only, broad skill/repository discovery. Nothing is installed or executed."""
import asyncio
import hashlib
from collections import deque
from datetime import datetime, timezone
import re
from urllib.parse import urljoin, urlsplit

import httpx
from bs4 import BeautifulSoup


SKILLS_HOSTS = {'skills.sh', 'www.skills.sh'}
GITHUB_HOSTS = {'github.com'}


def _safe_url(value, base, hosts):
    if not isinstance(value, str) or not value or any(c.isspace() for c in value) or '\\' in value:
        raise ValueError('Unsafe URL syntax')
    if '..' in value.split('/'):
        raise ValueError('Unsafe URL traversal')
    resolved = urljoin(base, value)
    parts = urlsplit(resolved)
    if (parts.scheme != 'https' or parts.hostname not in hosts or parts.username or parts.password
            or parts.port not in (None, 443)):
        raise ValueError('Unsafe URL scheme, host, credentials, or port')
    return resolved


def _path_identity(url, skill=False):
    parts = urlsplit(url)
    if parts.query or parts.fragment:
        raise ValueError('Discovery identity URL cannot contain query or fragment')
    path = parts.path.strip('/')
    pieces = path.split('/')
    if any(not re.fullmatch(r'[A-Za-z0-9_.-]+', p) or p in ('.', '..') for p in pieces):
        raise ValueError('Invalid identifier in URL path')
    if skill:
        if len(pieces) != 3:
            raise ValueError('Invalid skill URL path')
        return path[5:] if pieces[0] == 'site' else path
    if len(pieces) != 2 or pieces[0].lower() in ('login', 'settings', 'trending'):
        raise ValueError('Invalid repository URL path')
    return path


async def _get(client, url, *, hosts, params=None, headers=None):
    # Disable client auto-redirects: each hop must be validated BEFORE contacting it.
    current = _safe_url(url, url, hosts)
    for _ in range(4):
        response = await client.get(current, params=params, headers=headers, timeout=30, follow_redirects=False)
        if response.status_code in (301, 302, 303, 307, 308):
            target = _safe_url(response.headers.get('location'), str(response.url), hosts)
            authenticated = any(key.lower() in ('authorization', 'x-vercel-oidc-token') for key in (headers or {}))
            before, after = urlsplit(current), urlsplit(target)
            if authenticated and (before.scheme, before.hostname, before.port or 443) != (after.scheme, after.hostname, after.port or 443):
                raise ValueError('Authenticated redirect to different origin refused')
            current = target
            params = None
            continue
        if response.status_code == 429:
            raise ValueError(f'HTTP 429 rate limited; Retry-After={response.headers.get("retry-after", "unspecified")}')
        response.raise_for_status()
        return response
    raise ValueError('Too many redirects')


def _html(response):
    soup = BeautifulSoup(response.text, 'html.parser')
    title = soup.title.get_text(' ', strip=True).lower() if soup.title else ''
    if any(term in title for term in ('sign in', 'sign-in', 'login', 'access denied', 'just a moment',
                                     'captcha', 'security checkpoint', 'robot check')):
        raise ValueError('Login or blocked page instead of discovery content')
    return soup


def _number(value):
    match = re.search(r"[\d,.]+\s*[KMB]?", str(value or ""), re.I)
    if not match:
        return None
    text = match.group().replace(',', '').strip().upper()
    factor = {'K': 1000, 'M': 1000000, 'B': 1000000000}.get(text[-1], 1)
    return int(float(text[:-1] if factor != 1 else text) * factor)


def _source(name, url, coverage):
    return {'name': name, 'status': 'error', 'checked_at': datetime.now(timezone.utc).isoformat(),
            'item_count': 0, 'error': None, 'url': url, 'coverage': coverage}


def _text(value, limit=1000):
    return value.strip()[:limit] if isinstance(value, str) else ''


def _integer(value):
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _row(identifier, headline, source, summary, url, evidence):
    evidence['discovery'] = {'kind': 'leaderboard_observation', 'advisory_only': True,
                             'creation_date': None,
                             'newness_semantics': 'first-seen is local observation, not creation'}
    return {'id': identifier, 'headline': headline, 'source': source, 'summary': summary,
            'url': url, 'project_types': [], 'evidence': evidence,
            'fingerprint_data': {'headline': headline, 'summary': summary, 'url': url}}


async def _github(client, periods):
    source = _source('github-trending', 'https://github.com/trending', {
        'scope': 'All repositories rendered on each requested GitHub Trending period page; not all GitHub.',
        'periods': {},
    })
    rows = {}
    if not periods:
        source['status'] = 'skipped'
        source['coverage']['scope'] = 'No GitHub Trending periods requested.'
        return [], source
    for period in periods:
        url = f'https://github.com/trending?since={period}'
        report = {'url': url, 'status': 'error', 'item_count': 0, 'error': None}
        source['coverage']['periods'][period] = report
        try:
            if period not in ('daily', 'weekly', 'monthly'):
                raise ValueError('Unsupported GitHub Trending period')
            response = await _get(client, url, hosts=GITHUB_HOSTS)
            if response.url.path != '/trending' or response.url.params != httpx.QueryParams({'since': period}) or response.url.fragment:
                raise ValueError('GitHub Trending redirect changed requested period or scope')
            soup = _html(response)
            articles = soup.select('article.Box-row')
            if not articles:
                raise ValueError('No GitHub Trending repository rows: empty, blocked, or parser changed')
            for rank, article in enumerate(articles, 1):
                anchor = article.select_one('h2 a[href]')
                if anchor is None:
                    raise ValueError('Missing repository heading link')
                name = _path_identity(_safe_url(anchor['href'], url, GITHUB_HOSTS))
                paragraph = article.select_one('p')
                description = paragraph.get_text(' ', strip=True) if paragraph else ''
                language = article.select_one('[itemprop="programmingLanguage"]')
                total = article.select_one('a[href$="/stargazers"]')
                forks = article.select_one('a[href$="/forks"]')
                growth = article.select_one('span.float-sm-right')
                growth_text = growth.get_text(' ', strip=True) if growth else ''
                identifier = f'github:{name.lower()}'
                evidence = {'repository': name, 'description': description,
                            'language': language.get_text(strip=True) if language else None,
                            'total_stars': _number(total.get_text() if total else None),
                            'forks': _number(forks.get_text() if forks else None), 'periods': {}}
                row = rows.setdefault(identifier, _row(identifier, name, source['name'], description,
                                                       f'https://github.com/{name}', evidence))
                row['evidence']['periods'].setdefault(period, {
                    'rank': rank, 'stars_gained': _number(growth_text), 'stars_text': growth_text})
                report['item_count'] = sum(period in r['evidence']['periods'] for r in rows.values())
            report['status'] = 'ok'
        except (httpx.HTTPError, ValueError) as exc:
            report['error'] = f'{type(exc).__name__}: {exc}'
    source['item_count'] = len(rows)
    reports = list(source['coverage']['periods'].values())
    failures = [r for r in reports if r['status'] != 'ok']
    source['status'] = 'partial' if failures and rows else 'error' if failures else 'ok'
    source['error'] = '; '.join(r['error'] for r in failures) or None
    return list(rows.values()), source


SKILLS_VIEWS = {'all-time': 'https://skills.sh/', 'trending': 'https://skills.sh/trending',
                'hot': 'https://skills.sh/hot'}


async def _public_skills(client):
    source = _source('skills-sh', 'https://skills.sh/', {
        'mode': 'public-html', 'representation': 'server-rendered-initial-html',
        'scope': 'All rows in the three initial server-rendered public leaderboard HTML snapshots only; hydrated browser rows can differ; unrendered/paginated catalog not scanned.',
        'views': {},
    })
    rows = {}
    for view, url in SKILLS_VIEWS.items():
        report = {'url': url, 'status': 'error', 'rendered_rows': 0, 'error': None}
        source['coverage']['views'][view] = report
        try:
            response = await _get(client, url, hosts=SKILLS_HOSTS)
            if response.url.path != urlsplit(url).path or response.url.query or response.url.fragment:
                raise ValueError('Skills leaderboard redirect changed requested view or scope')
            soup = _html(response)
            anchors = [a for a in soup.select('a[href]') if a.select_one('h3')]
            if not anchors:
                raise ValueError('No rendered skills leaderboard rows: empty, blocked, or parser changed')
            for rank, anchor in enumerate(anchors, 1):
                skill_url = _safe_url(anchor['href'], url, SKILLS_HOSTS)
                identifier = _path_identity(skill_url, skill=True)
                path = urlsplit(skill_url).path.strip('/')
                name = anchor.select_one('h3').get_text(' ', strip=True)
                origin = anchor.select_one('p')
                spans = anchor.select('span')
                count_text = spans[1].get_text(strip=True) if len(spans) > 1 else ''
                rank_text = spans[0].get_text(strip=True) if spans else ''
                evidence = {'skill_id': identifier, 'origin': origin.get_text(strip=True) if origin else None,
                            'views': {}}
                row = rows.setdefault(identifier, _row('skills:' + identifier, name, source['name'], '',
                                                        'https://skills.sh/' + path, evidence))
                row['evidence']['views'].setdefault(view, {'rank': _number(rank_text) or rank,
                    'installs': _number(count_text), 'installs_text': count_text,
                    'change_text': spans[2].get_text(strip=True) if len(spans) > 2 else None})
                report['rendered_rows'] += 1
            report['status'] = 'ok'
        except (httpx.HTTPError, ValueError) as exc:
            report['error'] = f'{type(exc).__name__}: {exc}'
    source['item_count'] = len(rows)
    source['status'] = 'partial' if rows else 'error'
    errors = [r['error'] for r in source['coverage']['views'].values() if r['error']]
    source['error'] = '; '.join(errors) or None
    return list(rows.values()), source


API_MAX_PAGES = 3
API_PER_PAGE = 500


async def _api_skills(client, token):
    url = 'https://skills.sh/api/v1/skills'
    source = _source('skills-sh', url, {
        'mode': 'api', 'scope': 'Bounded leaderboard pages from all-time, trending and hot views; not the entire catalog.',
        'max_pages_per_view': API_MAX_PAGES, 'per_page': API_PER_PAGE, 'views': {},
    })
    rows = {}
    for view in SKILLS_VIEWS:
        report = {'url': url, 'status': 'error', 'pages_scanned': [], 'item_count': 0,
                  'rows_scanned': 0, 'has_more': None, 'reported_total': None, 'error': None}
        source['coverage']['views'][view] = report
        try:
            for page in range(API_MAX_PAGES):
                response = await _get(client, url, hosts=SKILLS_HOSTS, params={'view': view, 'page': page,
                    'per_page': API_PER_PAGE}, headers={'Authorization': f'Bearer {token}'})
                payload = response.json()
                if not isinstance(payload, dict) or not isinstance(payload.get('data'), list):
                    raise ValueError('Skills API missing data array')
                pagination = payload.get('pagination')
                if not isinstance(pagination, dict) or not isinstance(pagination.get('hasMore'), bool):
                    raise ValueError('Skills API missing valid pagination.hasMore')
                if not payload['data']:
                    raise ValueError('Skills API returned empty data')
                for index, raw in enumerate(payload['data'], 1):
                    if not isinstance(raw, dict) or not raw.get('id'):
                        raise ValueError('Skills API row missing id')
                    identifier = raw['id']
                    if not isinstance(identifier, str) or len(identifier.split('/')) not in (2, 3):
                        raise ValueError('Invalid Skills API identifier')
                    default_path = ('site/' if len(identifier.split('/')) == 2 else '') + identifier
                    skill_url = _safe_url(raw.get('url') or '/' + default_path, 'https://skills.sh/', SKILLS_HOSTS)
                    if _path_identity(skill_url, skill=True) != identifier:
                        raise ValueError('Skills API identifier/URL mismatch')
                    evidence = {'skill_id': identifier,
                                'origin': _text(raw.get('source')) or identifier.rsplit('/', 1)[0],
                                'source_type': _text(raw.get('sourceType')) or ('github' if len(identifier.split('/')) == 3 else 'well-known'),
                                'is_duplicate': raw.get('isDuplicate') is True, 'views': {}}
                    row = rows.setdefault(identifier, _row('skills:' + identifier,
                        _text(raw.get('name')) or _text(raw.get('slug')) or identifier.rsplit('/', 1)[-1],
                        source['name'], _text(raw.get('description')),
                        'https://skills.sh/' + urlsplit(skill_url).path.strip('/'), evidence))
                    row['evidence']['views'].setdefault(view, {'rank': page * API_PER_PAGE + index,
                        'installs': _integer(raw.get('installs')), 'installs_yesterday': _integer(raw.get('installsYesterday')),
                        'change': _integer(raw.get('change')), 'page': page})
                    report['rows_scanned'] += 1
                    report['item_count'] = sum(view in r['evidence']['views'] for r in rows.values())
                report['pages_scanned'].append(page)
                report['has_more'] = pagination['hasMore']
                report['reported_total'] = pagination.get('total')
                if not report['has_more']:
                    break
            report['status'] = 'partial' if report['has_more'] else 'ok'
        except (httpx.HTTPError, ValueError) as exc:
            report['error'] = f'{type(exc).__name__}: {exc}'
    source['item_count'] = len(rows)
    reports = list(source['coverage']['views'].values())
    source['status'] = 'ok' if all(r['status'] == 'ok' for r in reports) else 'partial' if rows else 'error'
    source['error'] = '; '.join(r['error'] for r in reports if r['error']) or None
    return list(rows.values()), source


TAG_RULES = {
    'web': r'\b(web|frontend|front.end|react|next\.?js|vue|svelte|browser|html|css|website|angular)\b',
    'mobile': r'\b(mobile|android|ios|expo|flutter|swiftui|react.native)\b',
    'data': r'\b(data|sql|database|analytics|etl|pandas|spark|machine.learning|postgres|supabase)\b',
    'automation': r'\b(automat\w*|workflows?|scheduled.tasks?|scrap\w*|n8n|zapier)\b',
    'agents': r'\b(agents?|agentic|llms?|mcp|language.models?|chatbots?)\b',
    'game': r'\b(games?|gaming|unity|unreal|godot)\b',
    'devops': r'\b(devops|docker|kubernetes|deployment|infrastructure|terraform|ci/cd|containers?)\b',
    'design': r'\b(design|ui|ux|figma|typography|accessibility|animation|tailwind)\b',
    'security': r'\b(security|vulnerabilit\w*|penetration|cryptograph\w*)\b',
    'documentation': r'\b(documentation|docs|markdown|technical.writing)\b',
}


def _classify(row):
    matches = []
    fields = {'headline': row['headline'], 'description': row['summary'],
              'topics': ' '.join(row['evidence'].get('topics', []))}
    for tag, pattern in TAG_RULES.items():
        for field, value in fields.items():
            match = re.search(pattern, value, re.I)
            if match:
                matches.append({'tag': tag, 'field': field, 'term': match.group()})
    row['project_types'] = sorted({m['tag'] for m in matches})
    row['evidence']['tag_inference'] = {
        'method': 'observable keyword inference; not verified capability', 'matches': matches}
    row['fingerprint_data'] = {
        'headline': row['headline'], 'summary': row['summary'], 'url': row['url'],
        'project_types': row['project_types'],
        **{key: row['evidence'][key] for key in ('language', 'origin', 'source_type', 'is_duplicate', 'topics')
           if key in row['evidence']},
    }


METADATA_LIMIT = 40

# Selection hints, not capability tags or approval. Match titles/slugs, never a
# publisher allowlist: the same workflow can be supplied by different projects.
# Each group orders specific signals before generic hints; popularity only
# breaks ties within that specificity level. Generic 'audit' is not security.
METADATA_HINTS = {
    'development': (r'\b(develop\w*|debug\w*|refactor\w*|code.review)\b',
                    r'\b(review\w*|planning|workflow\w*)\b'),
    'testing': (r'\b(tests?|testing|tdd|pytest|playwright|verification)\b', r'\bquality\b'),
    'data': (r'\b(data\w*|sql|database\w*|analytics|etl|pandas|postgres\w*|supabase|machine.learning)\b',
             r'\b(llms?|mcp|agents?)\b'),
    'security': (r'\b(secur\w*|vulnerabilit\w*|auth\w*|owasp|cryptograph\w*)\b', r'\baudit\w*\b'),
    'project-stack': (r'\b(python|pyside\w*|pyqt\w*|qt|camera\w*|vision|opencv|gesture\w*|pos|payment\w*|transaction\w*)\b',
                      r'\b(react|frontend|accessibility)\b'),
}


def _metadata_candidates(candidates):
    """Reserve half the slots for workflow hints, then round-robin public views."""
    def rank(row, view):
        value = _integer(row['evidence'].get('views', {}).get(view, {}).get('rank'))
        return value if value is not None and value > 0 else float('inf')

    # A fixed view order and identity tie-breaker make selection independent of
    # arrival/input order. Missing ranks are last, not invented as top ranks.
    ordered = sorted(candidates, key=lambda row: (
        *(rank(row, view) for view in SKILLS_VIEWS), row['id']))
    queues = {group: deque() for group in METADATA_HINTS}
    priorities = {group: {} for group in METADATA_HINTS}
    hints = {}
    for row in ordered:
        fields = {'headline': row['headline'], 'slug': row['evidence']['skill_id'].rsplit('/', 1)[-1]}
        matches = []
        for group, patterns in METADATA_HINTS.items():
            for priority, pattern in enumerate(patterns):
                for field, value in fields.items():
                    match = re.search(pattern, value.replace('_', ' '), re.I)
                    if match:
                        matches.append({'group': group, 'field': field, 'term': match.group()})
                        priorities[group].setdefault(row['id'], priority)
            if row['id'] in priorities[group]:
                queues[group].append(row)
        hints[row['id']] = matches
    # Stable sorting preserves the observed rank/identity ordering within each
    # specificity tier, without letting generic popular titles consume it all.
    queues = {group: deque(sorted(queue, key=lambda row: priorities[group][row['id']]))
              for group, queue in queues.items()}
    selected, seen = [], set()
    budget = max(0, METADATA_LIMIT)

    def take_round_robin(queues, ceiling, prefix):
        while len(selected) < ceiling:
            added = False
            for lane, queue in queues.items():
                while queue and queue[0]['id'] in seen:
                    queue.popleft()
                if queue and len(selected) < ceiling:
                    row = queue.popleft()
                    selected.append(row)
                    seen.add(row['id'])
                    row['evidence']['metadata_selection'] = {
                        'lane': prefix + lane, 'order': len(selected),
                        'hint_matches': hints[row['id']], 'advisory_only': True}
                    added = True
            if not added:
                break

    take_round_robin(queues, (budget + 1) // 2, 'workflow:')
    view_queues = {view: deque(sorted(
        (row for row in ordered if view in row['evidence'].get('views', {})),
        key=lambda row: (rank(row, view), row['id']))) for view in SKILLS_VIEWS}
    take_round_robin(view_queues, budget, 'view:')
    # Metadata-only callers may lack views; still fill unused slots safely.
    take_round_robin({'unranked': deque(ordered)}, budget, 'fallback:')
    return selected


async def _skill_metadata(client, rows, source):
    """Read only meta description and topic labels, never SKILL.md files or code."""
    candidates = [row for row in rows if not row['summary']]
    selected = _metadata_candidates(candidates)
    workflow_selected = sum(row['evidence']['metadata_selection']['lane'].startswith('workflow:') for row in selected)
    report = {'budget': METADATA_LIMIT, 'attempted': 0,
              'enriched': 0, 'descriptions': 0, 'topics_available': 0, 'empty': 0,
              'errors': 0, 'not_scanned': len(candidates) - len(selected),
              'selection': {'method': 'workflow-hints-then-view-round-robin-v1',
                            'candidate_count': len(candidates), 'selected': len(selected),
                            'workflow_budget': (max(0, METADATA_LIMIT) + 1) // 2,
                            'workflow_selected': workflow_selected,
                            'broad_selected': len(selected) - workflow_selected,
                            'hint_groups': list(METADATA_HINTS),
                            'scope': 'Title/slug keyword hints are advisory prioritization, not verified fit; specific signals precede generic hints within each group; remaining slots sample all observed views.'},
              'scope': 'Bounded public skill detail meta descriptions and topics; no scripts, prompts, or external URLs followed.'}
    source['coverage']['metadata'] = report
    for row in candidates:
        row['evidence']['metadata'] = {'status': 'not-scanned', 'reason': 'metadata request budget'}
    semaphore = asyncio.Semaphore(6)
    rate_limited = asyncio.Event()

    async def enrich(row):
        try:
            async with semaphore:
                if rate_limited.is_set():
                    row['evidence']['metadata']['reason'] = 'remote rate limit'
                    report['not_scanned'] += 1
                    return
                report['attempted'] += 1
                response = await _get(client, row['url'], hosts=SKILLS_HOSTS)
            final_url = _safe_url(str(response.url), row['url'], SKILLS_HOSTS)
            if _path_identity(final_url, skill=True) != row['evidence']['skill_id']:
                raise ValueError('Skill detail redirect identity mismatch')
            soup = _html(response)
            main = soup.select_one('main')
            if main is None or main.select_one('h1') is None:
                raise ValueError('Missing skill detail heading; empty, blocked, or parser changed')
            meta = soup.select_one('meta[name="description"]')
            description = meta.get('content', '').strip()[:1000] if meta else ''
            if description.lower().startswith('install the '):
                description = ''  # Generic installation boilerplate is not capability evidence.
            topics = sorted({a.get_text(' ', strip=True)[:120] for a in main.select('a[href^="/topic/"]')
                             if a.get_text(strip=True)})
            row['summary'] = description
            row['evidence']['description'] = description
            row['evidence']['topics'] = topics
            useful = bool(description or topics)
            row['evidence']['metadata'] = {'status': 'ok' if useful else 'empty', 'url': row['url'],
                'final_url': final_url, 'http_status': response.status_code,
                'checked_at': datetime.now(timezone.utc).isoformat(),
                'sha256': hashlib.sha256(response.content).hexdigest(),
                'description_field': 'meta[name=description]', 'description_available': bool(description),
                'topics_available': bool(topics)}
            report['enriched'] += int(useful)
            report['descriptions'] += int(bool(description))
            report['topics_available'] += int(bool(topics))
            report['empty'] += int(not useful)
        except (httpx.HTTPError, ValueError) as exc:
            if 'HTTP 429 rate limited' in str(exc):
                rate_limited.set()
            row['evidence']['metadata'] = {'status': 'error', 'error': f'{type(exc).__name__}: {exc}'}
            report['errors'] += 1

    await asyncio.gather(*(enrich(row) for row in selected))
    if report['errors']:
        if source['status'] == 'ok':
            source['status'] = 'partial'
        note = f"{report['errors']} skill metadata requests failed; leaderboard rows retained"
        source['error'] = '; '.join(e for e in (source['error'], note) if e)


def _redact_secret(value, secret):
    if not isinstance(secret, str) or not secret:
        return value
    if isinstance(value, str):
        return value.replace(secret, '[REDACTED]')
    if isinstance(value, list):
        return [_redact_secret(item, secret) for item in value]
    if isinstance(value, dict):
        return {_redact_secret(key, secret): _redact_secret(item, secret) for key, item in value.items()}
    return value


async def fetch_tools(client: httpx.AsyncClient, skills_token=None,
                      github_periods=('daily', 'weekly', 'monthly')):
    """Fetch advisory discovery using the supplied HTTP client."""
    skills, skills_source = (await _api_skills(client, skills_token) if skills_token
                             else await _public_skills(client))
    if skills_token and any(r['status'] == 'error' for r in skills_source['coverage']['views'].values()):
        fallback, fallback_source = await _public_skills(client)
        merged = {row['id']: row for row in skills}
        for row in fallback:
            if row['id'] in merged:
                for view, detail in row['evidence']['views'].items():
                    merged[row['id']]['evidence']['views'].setdefault(view, detail)
            else:
                merged[row['id']] = row
        skills = list(merged.values())
        api_coverage = skills_source['coverage']
        skills_source['coverage'] = fallback_source['coverage']
        skills_source['coverage']['api'] = api_coverage
        skills_source['coverage']['mode'] = 'api+public-html'
        skills_source['error'] = '; '.join(e for e in (skills_source['error'], fallback_source['error']) if e) or None
        skills_source['status'] = 'partial' if skills else 'error'
        skills_source['item_count'] = len(skills)
    await _skill_metadata(client, skills, skills_source)
    items, github = await _github(client, github_periods)
    for row in skills + items:
        _classify(row)
    return _redact_secret({'items': skills + items, 'sources': [skills_source, github]}, skills_token)
