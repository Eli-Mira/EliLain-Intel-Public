"""R10 integration: the routine internal report is latest-only, not a dated archive."""
from pathlib import Path

import main


def tracker_at(tmp_path):
    return main.NewsTracker(
        data_dir=tmp_path / 'data',
        cache_dir=tmp_path / 'cache',
        reports_dir=tmp_path / 'reports',
        freellmapi_db_path=tmp_path / 'missing.db',
    )


def test_routine_reports_replace_one_latest_file_and_never_accumulate(tmp_path):
    tracker = tracker_at(tmp_path)
    for _ in range(3):
        tracker.generate_report(include_news=False)
    files = sorted(p.name for p in (tmp_path / 'reports').iterdir())
    assert files == ['latest.html']
    assert Path(tracker.last_report_path).name == 'latest.html'


def test_explicit_user_output_path_is_preserved_and_untouched_by_retention(tmp_path):
    tracker = tracker_at(tmp_path)
    chosen = tmp_path / 'exports' / 'my-review.html'
    html = tracker.generate_report(include_news=False, output_path=chosen)
    assert chosen.read_text(encoding='utf-8') == html
    # An explicit user export is its own artifact; the routine reports
    # directory is not even created for it.
    assert not (tmp_path / 'reports').exists()
