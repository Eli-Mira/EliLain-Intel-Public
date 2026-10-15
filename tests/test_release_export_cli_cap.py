"""Exercise the standalone export CLI against an inventory larger than 100."""
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

from bs4 import BeautifulSoup
import pytest

ROOT = Path(__file__).resolve().parents[1]
INVENTORY_SIZE = 137
OBSERVED = '2026-09-30T10:00:00+00:00'


@pytest.fixture
def saved_inventory(tmp_path):
    rows = [{'id': f'openrouter:model:vendor/model-{i:03}',
             'url': f'https://openrouter.ai/vendor/model-{i:03}',
             'source': 'openrouter-models', 'headline': f'Model {i:03}',
             'checked_at': OBSERVED, 'first_seen': OBSERVED, 'evidence': {}}
            for i in range(INVENTORY_SIZE)]
    state = {'items': rows, 'sources': [{'name': 'openrouter-models', 'status': 'ok',
                                       'checked_at': OBSERVED}]}
    path = tmp_path / 'inventory.json'
    path.write_text(json.dumps(state), encoding='utf-8')
    return path


def run_cli(saved_inventory, tmp_path, *extra_args):
    before = saved_inventory.read_bytes()
    missing_db = tmp_path / 'missing.db'
    output = tmp_path / 'public'
    result = subprocess.run(
        [sys.executable, str(ROOT / 'scripts/export_public_report.py'),
         '--state', str(saved_inventory), '--news-db', str(missing_db),
         '--output-dir', str(output), *extra_args],
        cwd=ROOT, env={**os.environ, 'NOTIFICATION_CHANNEL': 'none'},
        capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    assert not result.stderr
    receipt = json.loads(result.stdout)
    artifact = output / 'index.html'
    assert Path(receipt['artifact']) == artifact.resolve()
    data = artifact.read_bytes()
    assert receipt['sha256'] == hashlib.sha256(data).hexdigest()
    assert receipt['bytes'] == len(data)
    assert saved_inventory.read_bytes() == before
    assert not missing_db.exists()
    assert [entry.name for entry in output.iterdir()] == ['index.html']
    soup = BeautifulSoup(data.decode('utf-8'), 'html.parser')
    dom_counts = Counter(row['data-source'] for row in soup.select('#discoveries .discovery-item'))
    assert receipt['source_counts'] == dict(dom_counts)
    return receipt, soup


def test_standalone_cli_default_exports_complete_inventory(saved_inventory, tmp_path):
    receipt, soup = run_cli(saved_inventory, tmp_path)
    assert receipt['source_counts'] == {'openrouter-models': INVENTORY_SIZE}
    rows = soup.select('#discoveries .discovery-item')
    links = [row.select_one('a[href]')['href'] for row in rows]
    assert set(links) == {f'https://openrouter.ai/vendor/model-{i:03}' for i in range(INVENTORY_SIZE)}
    coverage = soup.select_one('#source-health').get_text(' ', strip=True)
    assert 'Complete validated saved inventory' in coverage
    assert 'Retained validated public inventory: 137 rows; exported: 137; omitted by caller cap: 0.' in coverage
    assert 'Saved source rows: 137; excluded by public policy or duplicate identity: 0.' in coverage


@pytest.mark.parametrize('cap', [17, 100])
def test_standalone_cli_explicit_cap_still_limits_and_discloses_omissions(saved_inventory, tmp_path, cap):
    receipt, soup = run_cli(saved_inventory, tmp_path, '--max-items-per-source', str(cap))
    assert receipt['source_counts'] == {'openrouter-models': cap}
    rows = soup.select('#discoveries .discovery-item')
    links = [row.select_one('a[href]')['href'] for row in rows]
    assert set(links) == {f'https://openrouter.ai/vendor/model-{i:03}' for i in range(cap)}
    coverage = soup.select_one('#source-health').get_text(' ', strip=True)
    assert f'Export bounded to {cap} rows per source by caller' in coverage
    assert (f'Retained validated public inventory: {INVENTORY_SIZE} rows; exported: {cap}; '
            f'omitted by caller cap: {INVENTORY_SIZE - cap}.') in coverage
    assert 'Saved source rows: 137; excluded by public policy or duplicate identity: 0.' in coverage
