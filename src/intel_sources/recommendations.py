"""Read-only project suitability views over IntelStore discovery rows."""
from datetime import datetime, timezone
from copy import deepcopy
import json
import re
from urllib.parse import urlsplit

from .store import parse_time

PROJECTS = {
    'desktop-app': 'Desktop application: native PySide6, transactional correctness and operator UX.',
    'vision-app': 'Vision application: camera snapshots, latency and safe OS input.',
    'data-ai': 'Data/AI: database performance, retrieval and model development.',
}


TDD_ID = 'skills:obra/superpowers/test-driven-development'
TDD_REASONS = {
    'desktop-app': 'Regression-first work on Decimal totals, split bills and transaction rollback; does not validate a payment terminal.',
    'vision-app': 'Write fake-camera and fake-OS-driver replays for stale snapshots and neutral-release gating before changing input routing.',
    'arcade-game': 'Use deterministic collision, combo scoring and spawn-clock tests before changing slicing behavior; frame pacing still needs measurement.',
    'data-ai': 'Test ETL schema contracts, retrieval evaluation fixtures and model adapter failure paths before pipeline changes.',
}


# Exact identities are reviewed individually; tags/stars/leaderboard rank never select.
PROFILES = {
    TDD_ID: {
        project: {
            'reason': reason, 'axes': {'problem_fit': 3, 'stack_fit': 2, 'adoption_safety': 3},
            'constraints': ['Guidance requires manual adaptation to the local harness; not a runtime dependency.',
                            'Agent inference charges and commercial support terms were not checked.'],
            'next_validation': 'Author a project regression test, observe RED then GREEN; measure runtime separately.',
        } for project, reason in TDD_REASONS.items()
    },
    'skills:obra/superpowers/systematic-debugging': {
        'desktop-app': {
            'reason': 'Trace transaction ownership, detached ORM objects and duplicated Qt signals before fixing order-screen failures.',
            'axes': {'problem_fit': 3, 'stack_fit': 2, 'adoption_safety': 3},
            'constraints': ['Workflow guidance, not a Qt or payment integration.', 'Reproduce with a disposable database; never modify live receipts.'],
            'next_validation': 'Capture a failing split-bill or signal-lifecycle test and prove rollback and Decimal invariants.',
        },
        'vision-app': {
            'reason': 'Separate detector, immutable frame publication and OS-action consumers when tracing stale-tip or camera-retry failures.',
            'axes': {'problem_fit': 3, 'stack_fit': 2, 'adoption_safety': 3},
            'constraints': ['Not a detector or latency improvement.', 'Use fake camera and armed fake drivers; no unattended OS input.'],
            'next_validation': 'Replay loss/reacquisition and neutral-release traces; verify zero unintended fake-driver calls.',
        },
        'arcade-game': {
            'reason': 'Isolate input sampling, swept collision and spawn timing before attributing missed slices to rendering.',
            'axes': {'problem_fit': 3, 'stack_fit': 2, 'adoption_safety': 3},
            'constraints': ['Engine and render stack are unconfirmed.', 'Workflow guidance does not establish frame rate or collision correctness.'],
            'next_validation': 'Replay a fast pointer crossing and low-frame-rate spawn case with a fixed clock.',
        },
    },
    'skills:mattpocock/skills/domain-modeling': {
        'desktop-app': {
            'reason': 'Clarify order, bill, payment and refund boundaries before changing split/merge table semantics.',
            'axes': {'problem_fit': 3, 'stack_fit': 2, 'adoption_safety': 3},
            'constraints': ['Conceptual guidance must be mapped to existing SQLAlchemy session boundaries.', 'Does not prove fiscal or payment compliance.'],
            'next_validation': 'Review state transitions and add rollback, rounding and partial-payment tests.',
        },
        'arcade-game': {
            'reason': 'Model fruit lifecycle, combo windows and bomb penalties independently of sprites and animation.',
            'axes': {'problem_fit': 3, 'stack_fit': 2, 'adoption_safety': 3},
            'constraints': ['Engine is unknown; no framework migration recommended.', 'A state model does not replace swept-collision or timing tests.'],
            'next_validation': 'Test spawn-to-slice transitions, double-hit suppression and combo expiry with a fixed clock.',
        },
    },
    'skills:mattpocock/skills/codebase-design': {
        'vision-app': {
            'reason': 'Keep detector ownership, frozen frame snapshots and action routing as explicit boundaries rather than cross-frame getters.',
            'axes': {'problem_fit': 3, 'stack_fit': 2, 'adoption_safety': 3},
            'constraints': ['Conceptual architecture review, not real-camera validation.', 'Preserve single timestamp, fresh accepted snapshot and neutral-release safety gates.'],
            'next_validation': 'Prove detached frame bundles, loss reset and replay parity with fake camera/OS drivers.',
        },
    },
    'skills:supabase/agent-skills/supabase-postgres-best-practices': {
        'data-ai': {
            'reason': 'Review SQL schema, query plans, connection exhaustion and row isolation for Postgres-backed data pipelines.',
            'axes': {'problem_fit': 3, 'stack_fit': 3, 'adoption_safety': 3},
            'constraints': ['Conditional on PostgreSQL; not portable SQLite tuning.', 'Hosting, inference and support pricing were not checked; a license is not a free service entitlement.'],
            'next_validation': 'Use representative sanitized data and EXPLAIN plans; test migration rollback and tenant isolation.',
        },
    },
    'github:pytorch/pytorch': {
        'data-ai': {
            'reason': 'Candidate tensor/autograd runtime for local model-development experiments, not an inference-provider availability check.',
            'axes': {'problem_fit': 3, 'stack_fit': 3, 'adoption_safety': 1},
            'constraints': ['Windows/Python wheel, accelerator and driver compatibility are unverified.', 'Compute, model/data licenses and support costs remain unknown.'],
            'next_validation': 'With separate approval, validate a pinned wheel, CPU smoke test and reproducible target-model memory/latency measurement.',
        },
    },
    'github:vectifyai/pageindex': {
        'data-ai': {
            'reason': 'Evaluate document-tree retrieval as an alternative for structured long-document RAG with page-level evidence.',
            'axes': {'problem_fit': 3, 'stack_fit': 3, 'adoption_safety': 1},
            'constraints': ['Repository description is a self-reported retrieval approach, not benchmark evidence.', 'External model credentials, token charges, document privacy and provider compatibility need review.'],
            'next_validation': 'Build a labeled document/question set and compare citation accuracy, token cost and latency against the current baseline.',
        },
    },
    'github:mksglu/context-mode': {
        'data-ai': {
            'reason': 'Inspect bounded tool-output handling for data/AI agent sessions; vendor context-reduction claims are not measured here.',
            'axes': {'problem_fit': 2, 'stack_fit': 2, 'adoption_safety': 1},
            'constraints': ['MCP/hooks, runtime support and sandbox isolation require separate review.', 'Never treat a routing hook as proof of safety; provider charges and support terms are unknown.'],
            'next_validation': 'Review hook scope and data retention, then measure context size on representative tools in an isolated approved harness.',
        },
    },
}


EXCLUSIONS = {
    'skills:vercel-labs/agent-browser/agent-browser': {
        'desktop-app': 'Browser automation cannot exercise native PySide6 widgets, receipt devices or payment terminals.',
        'vision-app': 'Browser automation is not a camera or native OS-action replay harness.',
        'arcade-game': 'Browser/game engine is unconfirmed; DOM automation alone does not validate canvas collision or frame pacing.',
        'data-ai': 'A browser task and isolated credential policy must be specified before adopting automation into a data pipeline.',
    },
    'skills:vercel-labs/agent-skills/vercel-react-best-practices': {
        'desktop-app': 'React/Next.js rendering guidance is not a native PySide6/QSS implementation.',
        'vision-app': 'React component performance is not the Python camera/inference path.',
        'arcade-game': 'React stack is unconfirmed; component rendering advice does not demonstrate game-loop performance.',
        'data-ai': 'No React/Next.js interface requirement established for the data/AI pipeline.',
    },
    'skills:vercel-labs/agent-skills/web-design-guidelines': {
        'desktop-app': 'Web-specific DOM/CSS checks do not validate PySide6 focus, QSS or native accessibility; conceptual transfer needs manual review.',
        'vision-app': 'Web UI compliance does not evaluate detector latency or input safety.',
        'arcade-game': 'Web stack is unconfirmed; generic interface checks do not test slicing physics.',
        'data-ai': 'Web interface auditing is outside the stated database/retrieval/model workstream.',
    },
    'github:vercel/next.js': {
        'desktop-app': 'A React web framework is not compatible with the existing native PySide6 UI without an explicit migration.',
        'vision-app': 'A web framework is not a native camera detector or OS input component.',
        'arcade-game': 'Game engine is unconfirmed; no justification for a web-framework migration.',
        'data-ai': 'No web-serving requirement established; avoid adding a framework solely for trend position.',
    },
    'skills:supabase/agent-skills/supabase-postgres-best-practices': {
        'desktop-app': 'The desktop app uses SQLAlchemy, but its PostgreSQL backend is unconfirmed; Postgres-specific tuning is conditional, not a migration recommendation.',
        'vision-app': 'No PostgreSQL requirement in the camera/snapshot/action path.',
        'arcade-game': 'No PostgreSQL requirement established for local collision and scoring.',
    },
    'github:pytorch/pytorch': {
        'desktop-app': 'No model-training need established for transactional desktop application; a GPU runtime adds unrelated deployment complexity.',
        'vision-app': 'No new landmark-training need established; replacing the detector would require replay parity and latency evidence.',
        'arcade-game': 'No learned-physics/input model requirement; do not add an ML runtime for deterministic slicing.',
    },
    'github:vectifyai/pageindex': {
        'desktop-app': 'Long-document retrieval does not solve receipt transaction or Qt lifecycle correctness.',
        'vision-app': 'Document retrieval is outside the real-time detector/action loop.',
        'arcade-game': 'Document RAG does not solve collision, spawn or render timing.',
    },
    'github:mksglu/context-mode': {
        'desktop-app': 'An agent routing hook is not a desktop-app runtime dependency and must not be placed on the transaction path.',
        'vision-app': 'Context routing cannot establish camera latency or safe OS actions.',
        'arcade-game': 'Agent context compression is not a game-loop performance component.',
    },
}


def _discovery_error(item):
    identifier = item['id']
    kind, path = identifier.split(':', 1)
    pieces = path.split('/')
    count_ok = len(pieces) == 2 if kind == 'github' else len(pieces) in (2, 3)
    if not count_ok or any(not re.fullmatch(r'[A-Za-z0-9_.-]+', piece) or piece in ('.', '..') for piece in pieces):
        return 'Malformed discovery identifier; no normalization performed.'
    expected_source = 'github-trending' if kind == 'github' else 'skills-sh'
    if item.get('source') != expected_source:
        return 'Discovery source does not match identifier kind.'
    expected_url = ('https://github.com/' + path if kind == 'github'
                    else 'https://skills.sh/' + ('site/' if len(pieces) == 2 else '') + path)
    url = item.get('url')
    if not isinstance(url, str) or (url.lower() != expected_url.lower() if kind == 'github' else url != expected_url):
        return 'Discovery URL is unsafe or does not match the literal identifier.'
    if not isinstance(item.get('evidence', {}), dict):
        return 'Discovery evidence must be an object.'
    return None


CAPABILITY_PATHS = {
    'skills:obra/superpowers/test-driven-development': 'skills/test-driven-development/SKILL.md',
    'skills:obra/superpowers/systematic-debugging': 'skills/systematic-debugging/SKILL.md',
    'skills:mattpocock/skills/domain-modeling': 'skills/engineering/domain-modeling/SKILL.md',
    'skills:mattpocock/skills/codebase-design': 'skills/engineering/codebase-design/SKILL.md',
    'skills:supabase/agent-skills/supabase-postgres-best-practices': 'skills/supabase-postgres-best-practices/SKILL.md',
    'skills:vercel-labs/agent-browser/agent-browser': 'skills/agent-browser/SKILL.md',
    'skills:vercel-labs/agent-skills/vercel-react-best-practices': 'skills/react-best-practices/SKILL.md',
    'skills:vercel-labs/agent-skills/web-design-guidelines': 'skills/web-design-guidelines/SKILL.md',
}


def _official_metadata(item):
    raw = item.get('evidence', {}).get('official_metadata', {})
    if not raw:
        return {}
    rejected = {'status': 'rejected', 'reason': 'Official metadata provenance is not bound to the observed identity.'}
    if not isinstance(raw, dict):
        return rejected
    repo = '/'.join(item['id'].split(':', 1)[1].split('/')[:2])
    api = 'https://api.github.com/repos/' + repo
    if str(raw.get('repository', '')).lower() != repo.lower() or str(raw.get('url', '')).lower() != api.lower():
        return rejected
    capability_url = raw.get('capability_url')
    if capability_url:
        if not isinstance(capability_url, str) or any(c.isspace() for c in capability_url) or '\\' in capability_url:
            return rejected
        parts = urlsplit(capability_url)
        prefix = '/' + repo.lower() + '/'
        if (parts.scheme != 'https' or parts.netloc != 'raw.githubusercontent.com' or parts.query or parts.fragment
                or not parts.path.lower().startswith(prefix) or '..' in parts.path.split('/')
                or '%' in parts.path or not re.fullmatch(r'/[A-Za-z0-9_.\-/]+', parts.path)):
            return rejected
        expected_path = CAPABILITY_PATHS.get(item['id'])
        if expected_path and '/'.join(parts.path.strip('/').split('/')[3:]) != expected_path:
            return rejected
    if raw.get('capability_description') and not capability_url:
        return rejected
    urls = raw.get('source_urls', [])
    allowed = {str(raw.get('url', '')), capability_url}
    if not isinstance(urls, list) or not urls or any(not isinstance(url, str) or url not in allowed for url in urls):
        return rejected
    if raw.get('url') not in urls or (capability_url and capability_url not in urls):
        return rejected
    keys = {'status', 'checked_at', 'repository', 'url', 'source_urls', 'description', 'license', 'archived', 'disabled',
            'pushed_at', 'language', 'default_branch', 'sha256', 'http_status', 'capability_description',
            'capability_url', 'capability_sha256', 'capability_checked_at', 'capability_http_status',
            'capability_error', 'description_truncated', 'capability_description_truncated'}
    return {key: deepcopy(raw[key]) for key in sorted(keys & raw.keys())}


def _discovery_evidence(item):
    fields = {'repository', 'description', 'language', 'total_stars', 'forks', 'periods', 'skill_id',
              'origin', 'source_type', 'views', 'topics', 'metadata', 'tag_inference', 'discovery'}
    evidence = item.get('evidence', {})
    return {
        **{key: deepcopy(item.get(key)) for key in ('source', 'summary', 'url', 'checked_at', 'first_seen')},
        'fields': {key: deepcopy(evidence[key]) for key in sorted(fields & evidence.keys())},
        'semantics': 'observed metadata; not capability or integration proof',
    }


def _freshness(observation, now, *, official=False):
    checked = parse_time(observation.get('checked_at'))
    if checked is None or checked > now:
        return 'unverified'
    if official and observation.get('status') != 'ok':
        return 'unverified'
    if observation.get('freshness') == 'unverified':
        return 'unverified'
    if (observation.get('not_observed_in_latest_fetch') or observation.get('freshness') == 'stale'
            or (now - checked).total_seconds() > 86400):
        return 'stale'
    return 'fresh'


def evaluate_projects(items: list[dict], now: datetime | None = None) -> dict:
    """Build bounded advisory views from IntelStore ``items`` (never its payload).

    Only literal github:/skills: identities with matching source/URL are reviewed.
    Optional ``evidence.official_metadata`` must be a caller-fetched, timestamped
    repository/API record bound to that exact identity and capability document.
    This function performs no HTTP, file writes, installs or repository execution.
    Pass an aware datetime for reproducible results; otherwise UTC now is used.
    Metadata suitability confidence is capped at medium, never integration proof.
    """
    now = now or datetime.now(timezone.utc)
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise ValueError('now must be a timezone-aware datetime')
    now = now.astimezone(timezone.utc)
    groups = {key: {'project': label, 'shortlist': [], 'deferred': [], 'excluded': []} for key, label in PROJECTS.items()}
    discovery, invalid_rows = {}, []
    for item in sorted(items, key=lambda item: json.dumps(item, sort_keys=True, default=str)):
        if not isinstance(item, dict) or not isinstance(item.get('id'), str):
            invalid_rows.append({'id': item.get('id') if isinstance(item, dict) else None, 'reason': 'Row must be an object with a string id.'})
            continue
        identifier = item['id']
        if not identifier.startswith(('skills:', 'github:')):
            continue
        error = _discovery_error(item)
        if error:
            invalid_rows.append({'id': identifier, 'reason': error})
            continue
        previous = discovery.get(identifier)
        epoch = datetime.min.replace(tzinfo=timezone.utc)
        if previous is None or (parse_time(item.get('checked_at')) or epoch) >= (parse_time(previous.get('checked_at')) or epoch):
            discovery[identifier] = item
    for item in discovery.values():
        # Paused project rules remain archived, but never enter active outputs.
        profile = {project: rule for project, rule in PROFILES.get(item.get('id'), {}).items() if project in groups}
        excluded = {project: reason for project, reason in EXCLUSIONS.get(item.get('id'), {}).items() if project in groups}
        if not profile and not excluded:
            continue
        official = _official_metadata(item)
        discovery_freshness = _freshness(item, now)
        official_freshness = _freshness(official, now, official=True)
        capability = official.get('capability_description') if item['id'].startswith('skills:') else official.get('description')
        corroborated = bool(discovery_freshness == official_freshness == 'fresh' and capability)
        license_id = official.get('license') if official.get('status') == 'ok' else None
        blocker = ('Official metadata marks the repository archived; re-check maintenance before adoption.' if official.get('archived') is True
                   else 'Official metadata marks the repository disabled; do not adopt without a successful re-check.' if official.get('disabled') is True
                   else 'Official skill capability metadata is unavailable; resolve the fetch error before adoption.' if official.get('capability_error')
                   else None)
        if blocker:
            excluded = {**excluded, **{project: blocker for project in profile}}
            profile = {}
        for project, rule in profile.items():
            groups[project]['shortlist'].append({
                **deepcopy(rule), 'score': sum(rule['axes'].values()),
                'id': item['id'], 'headline': item.get('headline'),
                'source_urls': list(dict.fromkeys([item['url']] + official.get('source_urls', []))),
                'evaluation_status': 'metadata-only', 'integration_tested': False,
                'confidence': 'medium' if corroborated else 'low',
                'confidence_reason': ('Fresh discovery and official self-reported capability support suitability only; no installed integration test.'
                                      if corroborated else f'Discovery={discovery_freshness}; official metadata={official_freshness}; capability may be missing. Suitability is provisional; no installed integration test.'),
                'license': license_id if license_id not in (None, '', 'NOASSERTION') else 'unknown',
                'cost': 'unknown', 'support': 'unknown',
                'freshness': {'discovery': discovery_freshness, 'official_metadata': official_freshness},
                'evidence': {'discovery': _discovery_evidence(item),
                             'official_metadata': deepcopy(official)},
            })
        for project, reason in excluded.items():
            groups[project]['excluded'].append({
                'id': item['id'], 'headline': item.get('headline'), 'reason': reason,
                'source_urls': list(dict.fromkeys([item['url']] + official.get('source_urls', []))),
                'evaluation_status': 'metadata-only', 'integration_tested': False,
                'confidence': 'low', 'confidence_reason': 'Known project mismatch or unconfirmed prerequisite; no installed integration test.',
                'license': license_id if license_id not in (None, '', 'NOASSERTION') else 'unknown',
                'cost': 'unknown', 'support': 'unknown',
                'freshness': {'discovery': discovery_freshness, 'official_metadata': official_freshness},
                'constraints': [reason], 'next_validation': 'Confirm the project prerequisite before any adoption review.',
                'evidence': {'discovery': _discovery_evidence(item),
                             'official_metadata': deepcopy(official)},
            })
    for group in groups.values():
        group['excluded'].sort(key=lambda selection: selection['id'])
        ranked = sorted(group['shortlist'], key=lambda selection: (-selection['score'], selection['confidence'] != 'medium', selection['id']))
        group['shortlist'] = ranked[:4]
        group['deferred'] = ranked[4:]
    return {
        'evaluated_at': now.isoformat(), 'inventory_count': len(items),
        'coverage': {'inventory_replaced': False, 'shortlist_limit_per_project': 4,
                     'discovery_count': len(discovery), 'invalid_rows': invalid_rows,
                     'unreviewed_ids': sorted(set(discovery) - (set(PROFILES) | set(EXCLUSIONS)))},
        'groups': groups,
    }
