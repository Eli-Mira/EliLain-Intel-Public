"""Exercise latest-only writes on actual temporary files."""
import importlib
import importlib.util
import json
from pathlib import Path

import pytest


def api():
    assert importlib.util.find_spec('intel_sources.retention'), 'latest-only retention API is missing'
    return importlib.import_module('intel_sources.retention')


def test_repeated_report_and_summary_writes_are_latest_only(tmp_path):
    retention = api()
    archive = tmp_path / 'reports' / 'eli-lain_20260101_010101.html'
    archive.parent.mkdir()
    archive.write_bytes(b'historical report must survive')
    measurements = []
    for run in range(100):
        report = retention.latest_report_path(archive.parent)
        retention.write_latest_text(report, '<html>' + str(run).zfill(3) + '</html>')
        retention.write_latest_json(tmp_path / 'data' / 'search_latest.json', {'current': [str(run).zfill(3)]})
        files = [p for p in tmp_path.rglob('*') if p.is_file()]
        measurements.append((len(files), sum(p.stat().st_size for p in files)))
    assert report.name == 'latest.html'
    assert len(set(measurements)) == 1
    assert archive.read_bytes() == b'historical report must survive'
    assert json.loads((tmp_path / 'data/search_latest.json').read_text()) == {'current': ['099']}
    print({'runs': 100, 'file_count': measurements[-1][0], 'bytes': measurements[-1][1], 'includes_existing_archive': True})


def test_latest_writes_refuse_symlinks_and_preserve_output_on_failure(tmp_path, monkeypatch):
    retention = api()
    target = tmp_path / 'latest.html'
    target.write_text('previous good report')
    with pytest.raises(ValueError, match='output_too_large'):
        retention.write_latest_text(target, 'oversized', max_bytes=2)
    assert target.read_text() == 'previous good report'
    def fail(*args):
        raise OSError('synthetic disk failure')
    monkeypatch.setattr(retention.os, 'replace', fail)
    with pytest.raises(OSError):
        retention.write_latest_text(target, 'new report')
    assert target.read_text() == 'previous good report'
    assert not (tmp_path / 'latest.html.tmp').exists()
    monkeypatch.undo()
    alias = tmp_path / 'alias'
    import os
    import subprocess
    if os.name == 'nt':
        created = subprocess.run(['cmd', '/c', 'mklink', '/J', str(alias), str(tmp_path)], capture_output=True)
        assert created.returncode == 0, 'synthetic junction creation failed'
    else:
        alias.symlink_to(tmp_path, target_is_directory=True)
    try:
        with pytest.raises(ValueError, match='unsafe_output_path'):
            retention.write_latest_text(alias / 'latest.html', 'unsafe')
        assert target.read_text() == 'previous good report'
    finally:
        if os.name == 'nt':
            alias.rmdir()
        else:
            alias.unlink()


def test_cleanup_discovery_lists_exact_archives_without_touching_state(tmp_path):
    retention = api()
    for directory in ('reports', 'data', 'public', '.cache'):
        (tmp_path / directory).mkdir()
    candidates = ['reports/eli-lain_20260101_010101.html', 'data/news_20260101.json', '.cache/search_20260101.json']
    protected = ['reports/latest.html', 'public/index.html', 'data/news.db', 'data/intel_state.json',
                 'data/delivery_receipts.json', 'data/feed_health.json', '.env', 'config.yaml', '.cache/unknown.json']
    for name in candidates + protected:
        (tmp_path / name).write_bytes(b'UNCHANGED')
    before = {str(p.relative_to(tmp_path)): p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}
    manifest = retention.discover_local_archives(tmp_path)
    assert manifest['dry_run'] is True
    assert manifest['deletion_supported'] is False
    assert {entry['path'] for entry in manifest['candidates']} == {str((tmp_path / name).absolute()) for name in candidates}
    assert manifest['candidate_bytes'] == 27
    assert all(len(entry['sha256']) == 64 for entry in manifest['candidates'])
    after = {str(p.relative_to(tmp_path)): p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}
    assert after == before


def test_default_output_budget_accommodates_full_internal_inventory(tmp_path):
    retention = api()
    # Existing internal reports exceed the public export's separate 4 MiB cap.
    text = 'x' * (5 * 1024 * 1024)
    target = retention.write_latest_text(tmp_path / 'latest.html', text)
    assert target.stat().st_size == len(text)


def test_cleanup_cli_is_dry_run_only(tmp_path, capsys):
    script = Path(__file__).resolve().parents[1] / 'scripts/cleanup_local_outputs.py'
    assert script.is_file(), 'safe dry-run CLI is missing'
    spec = importlib.util.spec_from_file_location('cleanup_local_outputs', script)
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    assert cli.main(['--root', str(tmp_path), '--dry-run', '--json']) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload['dry_run'] and not payload['deletion_supported']
    with pytest.raises(SystemExit):
        cli.main(['--root', str(tmp_path), '--delete'])
    assert list(tmp_path.iterdir()) == []


def test_repeated_synthetic_pipeline_plateaus_and_restart_skips_sends(tmp_path, artifact_dir):
    from datetime import timedelta
    from intel_sources.store import IntelStore
    from intel_sources.report_delivery import load_receipts
    from test_link_first_delivery import LinkProtocol, send
    from test_bounded_intel_state import NOW, row, sources
    retention = api()
    base = (artifact_dir / 'index.html').read_text(encoding='utf-8')
    export = tmp_path / 'current-public'
    state_path = tmp_path / 'current-data/intel.json'
    receipts = tmp_path / 'current-data/receipts.json'
    report = retention.latest_report_path(tmp_path / 'current-reports')
    samples = []
    for i in range(90):
        state = IntelStore(state_path).update([row('stable'), row(i)], sources(), now=NOW + timedelta(hours=i))
        html = base.replace('</body>', f'<!--synthetic-run-{i:03d}--></body>')
        retention.write_latest_text(export / 'index.html', html)
        retention.write_latest_text(report, html)
        result = send(export, LinkProtocol((export / 'index.html').read_bytes()), receipts)
        assert result.success
        files = [p for p in tmp_path.rglob('*') if p.is_file() and not p.is_relative_to(artifact_dir)]
        samples.append((len(files), sum(p.stat().st_size for p in files)))
    restarted = LinkProtocol((export / 'index.html').read_bytes())
    result = send(export, restarted, receipts)
    assert result.telegram.confirmation == result.discord.confirmation == 'previously-confirmed'
    assert len(restarted.calls) == 1
    assert len(load_receipts(receipts)) == 64
    assert len(state['items']) == 2
    assert all(count == 4 for count, _ in samples)
    assert max(size for _, size in samples[70:]) - min(size for _, size in samples[70:]) < 128
    print({'pipeline_runs': 90, 'files': samples[-1][0], 'bytes_last': samples[-1][1],
           'plateau_min': min(size for _, size in samples[70:]), 'plateau_max': max(size for _, size in samples[70:]),
           'restart_network_calls': len(restarted.calls), 'restart_message_posts': 0})


from test_report_delivery import artifact_dir
