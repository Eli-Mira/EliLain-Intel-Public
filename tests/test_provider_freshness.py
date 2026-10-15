from datetime import datetime, timedelta, timezone

from freellmapi_tracker.models import FreeProvider, ProviderStatus


def test_timestamp_aliases_are_utc_roundtrip_without_mutating_old_state():
    old = {"name": "Groq", "platform": "groq", "status": "live",
           "last_verified": "2026-09-30T12:00:00", "last_verified_source": "probe"}
    original = dict(old)
    provider = FreeProvider.from_dict(old)
    assert provider.checked_at == datetime(2026, 9, 30, 12, tzinfo=timezone.utc)
    assert provider.last_verified == provider.checked_at
    assert provider.probe_source == "local:freellmapi"
    assert old == original
    assert FreeProvider.from_dict(provider.to_dict()).checked_at == provider.checked_at
    shallow = dict(old, last_verified_source="health_check")
    assert FreeProvider.from_dict(shallow).freshness() == "unverified"
    assert FreeProvider.from_dict(dict(old, last_verified=None)).checked_at is None
    offset = FreeProvider("Groq", "groq", checked_at=datetime.fromisoformat("2026-09-30T15:00:00+03:00"))
    assert offset.checked_at.utcoffset() == timedelta(0)
    assert offset.freshness(datetime(2026, 9, 30, 12)) == "fresh"
    assert offset.freshness(datetime(2026, 9, 29, 12, tzinfo=timezone.utc)) == "stale"


def test_freshness_does_not_turn_unverified_or_stale_status_live():
    now = datetime(2026, 9, 30, 12, tzinfo=timezone.utc)
    provider = FreeProvider("Groq", "groq", status=ProviderStatus.LIVE)
    assert provider.freshness(now) == "unverified"
    assert provider.effective_status(now) == ProviderStatus.UNKNOWN
    provider.checked_at = now - timedelta(hours=23)
    assert provider.freshness(now) == "fresh"
    assert provider.effective_status(now) == ProviderStatus.LIVE
    provider.checked_at = now - timedelta(hours=25)
    assert provider.freshness(now) == "stale"
    assert provider.effective_status(now) == ProviderStatus.UNKNOWN
    provider.status = ProviderStatus.DISABLED
    assert provider.effective_status(now) == ProviderStatus.DISABLED
    assert provider.checked_at == now - timedelta(hours=25)
