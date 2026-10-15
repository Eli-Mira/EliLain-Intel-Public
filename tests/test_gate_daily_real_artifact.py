"""Exercise the daily gate with actual producer/loader bytes, never a mocked loader."""
import importlib.util
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

from intel_sources.daily_public import export_digest
from intel_sources.report_delivery import load_artifact

NOW = datetime(2026, 10, 7, 10, tzinfo=timezone.utc)


def gate():
    path = Path(__file__).parents[1] / 'scripts/gate_daily_nonempty.py'
    spec = importlib.util.spec_from_file_location('daily_real_artifact_gate', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def bind(directory, public, artifact):
    directory = directory / 'daily_state'
    directory.mkdir(parents=True, exist_ok=True)
    record = {'generated_at': artifact.rendered_at, 'report_date': '2026-10-07',
              'counts': {'selected': sum(count for _, count in artifact.source_counts)},
              'metadata': {'public_path': str(public / 'index.html'),
                           'artifact_sha256': artifact.sha256,
                           'rendered_at': artifact.rendered_at}}
    (directory / 'history.jsonl').write_text(json.dumps(record) + '\n', encoding='utf-8')


@pytest.fixture
def daily(tmp_path):
    digest = {'schema_version': 1, 'report_date': '2026-10-07', 'generated_at': NOW.isoformat(),
              'entries': [{'id': 'github:example/data-tool', 'source_id': 'github-trending',
                           'name': 'Data automation', 'category': 'devtools',
                           'what': 'A Python data-processing tool automates recurring development workflows.',
                           'why': 'Useful for recurring data-processing tasks.',
                           'source_url': 'https://github.com/example/data-tool',
                           'observed_at': NOW.isoformat(),
                           'link_verification': {'status': 'verified', 'checked_at': NOW.isoformat(),
                                                 'url': 'https://github.com/example/data-tool', 'http_status': 200},
                           'free': {'status': 'unknown', 'detail': 'Not assessed.'},
                           'api': {'status': 'unknown', 'detail': 'Not assessed.'},
                           'score': 60, 'scoring_breakdown': 'Fixture score.'}],
              'sources': [], 'counts': {'fetched': 1, 'eligible': 1, 'rejected': 0, 'selected': 1}}
    public = tmp_path / 'public'
    export_digest(digest, public)
    artifact = load_artifact(public)
    data = tmp_path / 'data'
    bind(data, public, artifact)
    return data, public, artifact


def test_real_producer_artifact_passes(daily):
    data, public, artifact = daily
    assert sum(count for _, count in artifact.source_counts) == 1
    assert gate().main(['--data-dir', str(data), '--public-dir', str(public)]) == 0


def test_standalone_gate_loads_project_without_inherited_pythonpath(daily, tmp_path):
    data, public, _ = daily
    script = Path(__file__).parents[1] / 'scripts/gate_daily_nonempty.py'
    env = dict(os.environ)
    env.pop('PYTHONPATH', None)
    result = subprocess.run(
        [sys.executable, '-I', str(script), '--data-dir', str(data), '--public-dir', str(public)],
        cwd=tmp_path, env=env, capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert '1 items selected, artifact bound' in result.stdout


def test_real_legacy_artifact_cannot_satisfy_daily_gate(tmp_path):
    from intel_sources.public_report import build_public_payload, _standalone_html
    from tracker_core.reporter_html import LainReporter
    state = {'sources': [{'name': 'github-trending', 'status': 'ok', 'checked_at': NOW.isoformat()}],
             'items': [{'source': 'github-trending', 'id': 'github:example/tool', 'headline': 'Safe tool',
                        'url': 'https://github.com/example/tool', 'checked_at': NOW.isoformat()}]}
    public = tmp_path / 'public'
    public.mkdir()
    payload = build_public_payload(state, now=NOW)
    (public / 'index.html').write_text(_standalone_html(LainReporter().render(**payload)), encoding='utf-8')
    artifact = load_artifact(public)
    assert sum(count for _, count in artifact.source_counts) > 0
    data = tmp_path / 'data'
    bind(data, public, artifact)
    assert gate().main(['--data-dir', str(data), '--public-dir', str(public)]) == 1


def test_boolean_selected_is_not_a_count(daily):
    data, public, _ = daily
    history = data / 'daily_state/history.jsonl'
    record = json.loads(history.read_text())
    record['counts']['selected'] = True
    history.write_text(json.dumps(record) + '\n', encoding='utf-8')
    assert gate().main(['--data-dir', str(data), '--public-dir', str(public)]) == 1


@pytest.mark.parametrize('case', ['missing_timestamp', 'wrong_generated_at', 'wrong_report_date'])
def test_daily_history_dates_must_bind_to_artifact(daily, case):
    data, public, _ = daily
    history = data / 'daily_state/history.jsonl'
    record = json.loads(history.read_text())
    if case == 'missing_timestamp':
        record.pop('generated_at')
        record['metadata'].pop('rendered_at')
    elif case == 'wrong_generated_at':
        record['generated_at'] = '2026-10-06T10:00:00+00:00'
    else:
        record['report_date'] = '2026-10-06'
    history.write_text(json.dumps(record) + '\n', encoding='utf-8')
    assert gate().main(['--data-dir', str(data), '--public-dir', str(public)]) == 1


@pytest.mark.parametrize('case', ['count', 'timestamp', 'loader_error'])
def test_gate_failure_never_echoes_untrusted_values(daily, case, monkeypatch, capsys):
    data, public, _ = daily
    canary = 'PRIVATE_TEST_VALUE_DO_NOT_PRINT'
    history = data / 'daily_state/history.jsonl'
    record = json.loads(history.read_text())
    if case == 'count':
        record['counts']['selected'] = canary
    elif case == 'timestamp':
        record['metadata']['rendered_at'] = canary
    else:
        def fail(_):
            raise ValueError(canary)
        monkeypatch.setattr('intel_sources.report_delivery.load_artifact', fail)
    history.write_text(json.dumps(record) + '\n', encoding='utf-8')
    assert gate().main(['--data-dir', str(data), '--public-dir', str(public)]) == 1
    captured = capsys.readouterr()
    assert canary not in captured.out + captured.err
