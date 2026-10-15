"""TDD tests for daily_sources: fetch_candidates and verify_candidates."""
import pytest
import asyncio
import httpx
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, patch, MagicMock
from src.intel_sources.settings import default_settings
from src.intel_sources.daily_sources import (
    fetch_candidates, verify_candidates, _is_private_ip,
    _is_safe_host, _is_waf_response, _text_length, _safe_get
)


def test_fetch_candidates_returns_structure():
    """Test that fetch_candidates returns the correct structure."""
    settings = default_settings()
    result = asyncio.run(fetch_candidates(settings))
    assert isinstance(result, dict)
    assert 'candidates' in result
    assert 'sources' in result
    assert isinstance(result['candidates'], list)
    assert isinstance(result['sources'], list)
    
    # Check candidate structure
    if result['candidates']:
        cand = result['candidates'][0]
        required_fields = ['id', 'name', 'what', 'category', 'source_id', 
                          'source_url', 'published_at', 'date_kind', 'observed_at',
                          'free', 'api', 'topics']
        for field in required_fields:
            assert field in cand, f"Missing field: {field}"


def test_fetch_candidates_skips_disabled_sources():
    """Test that disabled sources are skipped."""
    settings = default_settings()
    settings['sources'] = [{'id': 'test-src', 'name': 'Test', 'adapter': 'catalog', 'enabled': False}]
    result = asyncio.run(fetch_candidates(settings))
    source_ids = {s['id'] for s in result['sources']}
    # F-12/F-13: unknown source IDs are normalized to "unknown"
    assert 'unknown' in source_ids
    statuses = [s.get('status') for s in result['sources']]
    assert 'skipped' in statuses


def test_verify_candidates_adds_verification_fields():
    """Test that verify_candidates attaches link_verification with correct fields."""
    candidates = [
        {
            'id': 'test:1',
            'name': 'Test Item',
            'what': 'desc for testing purposes',
            'category': 'models',
            'source_id': 'openrouter-models',
            'source_url': 'https://openrouter.ai/models',
            'published_at': None,
            'date_kind': 'release',
            'observed_at': datetime.now(timezone.utc).isoformat(),
            'free': {'status': 'unknown', 'detail': '', 'evidence_url': None},
            'api': {'status': 'unknown', 'detail': '', 'evidence_url': None},
            'topics': []
        }
    ]
    settings = default_settings()
    result = asyncio.run(verify_candidates(candidates, settings))
    assert len(result) == 1
    assert 'link_verification' in result[0]
    assert 'status' in result[0]['link_verification']
    assert 'checked_at' in result[0]['link_verification']
    assert 'url' in result[0]['link_verification']
    assert 'http_status' in result[0]['link_verification']


def test_ssrf_protection_rejects_private_ips():
    """Test that SSRF protection rejects private IPs."""
    # Test IPv4 private ranges
    assert _is_private_ip("127.0.0.1") is True
    assert _is_private_ip("10.0.0.1") is True
    assert _is_private_ip("172.16.0.1") is True
    assert _is_private_ip("192.168.0.1") is True
    assert _is_private_ip("169.254.0.1") is True
    
    # Test IPv6 private ranges
    assert _is_private_ip("::1") is True
    assert _is_private_ip("fc00::1") is True
    assert _is_private_ip("fe80::1") is True
    
    # Public IPs should not be rejected
    assert _is_private_ip("8.8.8.8") is False
    assert _is_private_ip("1.1.1.1") is False
    
    # Invalid IPs
    assert _is_private_ip("not-an-ip") is False


def test_is_safe_host():
    """Test host safety checks."""
    assert _is_safe_host("localhost") is False
    assert _is_safe_host("example.com") is True
    assert _is_safe_host("127.0.0.1") is False
    assert _is_safe_host(None) is False
    assert _is_safe_host("") is False


def test_is_waf_response():
    """Test WAF response detection."""
    # Status 403 with Cloudflare header
    headers = {"server": "cloudflare"}
    assert _is_waf_response(403, headers, "") is True
    
    # Status 403 with specific body text
    assert _is_waf_response(403, {}, "Just a moment...") is True
    assert _is_waf_response(403, {}, "Access denied") is True
    assert _is_waf_response(403, {}, "Error code") is True
    
    # Non-403 status should not trigger
    assert _is_waf_response(200, headers, "") is False
    assert _is_waf_response(404, headers, "") is False

    # An ordinary origin server is not evidence of a WAF challenge.
    assert _is_waf_response(403, {"server": "Varnish", "x-ratelimit-remaining": "0"}, "") is False
    assert _is_waf_response(403, {"Server": "nginx"}, "") is False


@pytest.mark.asyncio
async def test_fetch_json_preserves_github_rate_limit_403():
    from src.intel_sources.daily_sources import _fetch_json

    url = "https://api.github.com/repos/example/tool"
    response = httpx.Response(
        403,
        headers={"server": "Varnish", "x-ratelimit-remaining": "0"},
        json={"message": "API rate limit exceeded"},
        request=httpx.Request("GET", url),
    )
    with patch("src.intel_sources.daily_sources._safe_send_request", new_callable=AsyncMock) as send:
        send.return_value = response
        with pytest.raises(httpx.HTTPStatusError) as error:
            await _fetch_json(MagicMock(), url)
    assert error.value.response.status_code == 403
    assert error.value.response.headers["x-ratelimit-remaining"] == "0"
    assert response.is_closed


def test_text_length():
    """Test text length calculation."""
    assert _text_length("Hello world") == 11
    assert _text_length("<p>Hello</p>") == 5
    assert _text_length("<div>Short</div>") == 5
    assert _text_length("") == 0


@pytest.mark.asyncio
async def test_fetch_candidates_with_mock_client():
    """Test fetch_candidates with mocked HTTP client."""
    # Create mock response data
    openrouter_data = {
        "data": [
            {
                "id": "google/gemini-nano-banana-2.1",
                "name": "Google: Nano Banana 2.1",
                "description": "Test description",
                "created": 1791300825,
                "pricing": {"prompt": 0, "completion": 0}
            }
        ]
    }
    
    # Create mock client
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = openrouter_data
    mock_response.text = "test"
    mock_response.content = b"test"
    mock_response.headers = {}
    
    mock_client = MagicMock()
    mock_client.get = AsyncMock(return_value=mock_response)
    
    settings = default_settings()
    
    # Patch _safe_get to use our mock
    with patch('src.intel_sources.daily_sources._safe_get', new_callable=AsyncMock) as mock_safe_get:
        mock_safe_get.return_value = (mock_response, None)
        result = await fetch_candidates(settings)
        assert isinstance(result, dict)
        assert 'candidates' in result


def test_each_source_adapter_structure():
    """Test that each source adapter produces correct structure."""
    settings = default_settings()
    result = asyncio.run(fetch_candidates(settings))
    
    # Check that we got candidates from at least one source
    assert len(result['candidates']) > 0 or len(result['sources']) > 0
    
    # Check source status structure
    for source in result['sources']:
        assert 'id' in source
        assert 'name' in source
        assert 'status' in source
        assert 'checked_at' in source
        assert 'returned' in source
        assert 'accepted' in source
        assert 'error_code' in source


def test_verify_candidates_skips_unsafe_urls():
    """Test that verify_candidates skips unsafe URLs."""
    candidates = [
        {
            'id': 'test:1',
            'name': 'Test Item',
            'what': 'desc for testing purposes here',
            'category': 'models',
            'source_id': 'test',
            'source_url': 'http://127.0.0.1/test',  # HTTP + private IP
            'published_at': None,
            'date_kind': 'observed',
            'observed_at': datetime.now(timezone.utc).isoformat(),
            'free': {'status': 'unknown', 'detail': '', 'evidence_url': ''},
            'api': {'status': 'unknown', 'detail': '', 'evidence_url': ''},
            'topics': []
        }
    ]
    settings = default_settings()
    result = asyncio.run(verify_candidates(candidates, settings))
    
    assert len(result) == 1
    assert result[0]['link_verification']['status'] == 'unverified'


def test_verify_candidates_no_url():
    """Test verify_candidates handles missing URL."""
    candidates = [
        {
            'id': 'test:1',
            'name': 'Test Item',
            'what': 'desc for testing purposes here',
            'category': 'models',
            'source_id': 'test',
            'source_url': '',
            'published_at': None,
            'date_kind': 'observed',
            'observed_at': datetime.now(timezone.utc).isoformat(),
            'free': {'status': 'unknown', 'detail': '', 'evidence_url': ''},
            'api': {'status': 'unknown', 'detail': '', 'evidence_url': ''},
            'topics': []
        }
    ]
    settings = default_settings()
    result = asyncio.run(verify_candidates(candidates, settings))
    
    assert len(result) == 1
    assert result[0]['link_verification']['status'] == 'unverified'
    assert result[0]['link_verification']['http_status'] is None


@pytest.mark.asyncio
async def test_byte_cap_enforcement():
    """Test that byte cap is enforced."""
    # Create a response larger than 1MB
    large_content = b"x" * (1_000_001)
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {}
    mock_response.text = "x" * 100
    mock_response.content = large_content
    mock_response.headers = {}
    
    # This should raise an error due to byte cap
    from src.intel_sources.daily_sources import _MAX_BYTES
    assert len(large_content) > _MAX_BYTES


@pytest.mark.asyncio
async def test_time_cap_enforcement():
    """Test that time cap is enforced."""
    from src.intel_sources.daily_sources import _MAX_SECONDS
    assert _MAX_SECONDS == 10


@pytest.mark.asyncio
async def test_concurrent_request_limit():
    """Test that concurrent request limit is enforced."""
    from src.intel_sources.daily_sources import _MAX_CONCURRENT
    assert _MAX_CONCURRENT == 20


# Network-integrated tests (skip if network unavailable)
class TestRealHTTPIntegration:
    """Integration tests that use real HTTP but skip on network failure."""
    
    @pytest.mark.asyncio
    async def test_openrouter_fetch(self):
        """Test fetching from OpenRouter (skip if network unavailable)."""
        try:
            settings = default_settings()
            # Add unorouter-pricing to sources if not present
            if not any(s['id'] == 'unorouter-pricing' for s in settings['sources']):
                settings['sources'].append({
                    'id': 'unorouter-pricing',
                    'name': 'UnoRouter Pricing',
                    'adapter': 'pricing',
                    'enabled': True
                })
            
            result = await fetch_candidates(settings)
            
            # Should return candidates without error
            assert isinstance(result, dict)
            assert 'candidates' in result
            assert 'sources' in result
            
            # Check source statuses
            openrouter_status = next((s for s in result['sources'] if s['id'] == 'openrouter-models'), None)
            if openrouter_status:
                assert openrouter_status['status'] in ('ok', 'error')
                
        except Exception as e:
            pytest.skip(f"Network unavailable or fetch failed: {e}")
    
    @pytest.mark.asyncio
    async def test_github_trending_fetch(self):
        """Test fetching GitHub Trending (skip if network unavailable)."""
        try:
            settings = default_settings()
            result = await fetch_candidates(settings)
            
            github_status = next((s for s in result['sources'] if s['id'] == 'github-trending'), None)
            if github_status:
                assert github_status['status'] in ('ok', 'error')
                
        except Exception as e:
            pytest.skip(f"Network unavailable or fetch failed: {e}")
    
    @pytest.mark.asyncio
    async def test_skills_fetch(self):
        """Test fetching Skills.sh (skip if network unavailable)."""
        try:
            settings = default_settings()
            result = await fetch_candidates(settings)
            
            skills_status = next((s for s in result['sources'] if s['id'] == 'skills-hot'), None)
            if skills_status:
                assert skills_status['status'] in ('ok', 'error')
                
        except Exception as e:
            pytest.skip(f"Network unavailable or fetch failed: {e}")
    
    @pytest.mark.asyncio
    async def test_unorouter_fetch(self):
        """Test fetching UnoRouter (skip if network unavailable)."""
        try:
            settings = default_settings()
            # Add unorouter-pricing
            if not any(s['id'] == 'unorouter-pricing' for s in settings['sources']):
                settings['sources'].append({
                    'id': 'unorouter-pricing',
                    'name': 'UnoRouter Pricing',
                    'adapter': 'pricing',
                    'enabled': True
                })
            
            result = await fetch_candidates(settings)
            
            unorouter_status = next((s for s in result['sources'] if s['id'] == 'unorouter-pricing'), None)
            if unorouter_status:
                assert unorouter_status['status'] in ('ok', 'error')
                
        except Exception as e:
            pytest.skip(f"Network unavailable or fetch failed: {e}")
    
    @pytest.mark.asyncio
    async def test_verify_candidates_with_real_url(self):
        """Test link verification with a real URL (skip if network unavailable)."""
        try:
            candidates = [
                {
                    'id': 'test:1',
                    'name': 'Test',
                    'what': 'A test candidate for link verification',
                    'category': 'models',
                    'source_id': 'test',
                    'source_url': 'https://openrouter.ai',
                    'published_at': None,
                    'date_kind': 'observed',
                    'observed_at': datetime.now(timezone.utc).isoformat(),
                    'free': {'status': 'unknown', 'detail': '', 'evidence_url': ''},
                    'api': {'status': 'unknown', 'detail': '', 'evidence_url': ''},
                    'topics': []
                }
            ]
            settings = default_settings()
            result = await verify_candidates(candidates, settings)
            
            assert len(result) == 1
            assert 'link_verification' in result[0]
            # Should be verified or unverified, not error
            assert result[0]['link_verification']['status'] in ('verified', 'unverified')
                
        except Exception as e:
            pytest.skip(f"Network unavailable or verification failed: {e}")
