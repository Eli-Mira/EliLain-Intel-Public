"""Prober for testing freellmapi providers."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from typing import Any

import httpx

from .models import FreeProvider, ProviderProbeResult, ProviderStatus, sanitize_evidence


logger = logging.getLogger(__name__)


# Default lightweight test prompts per provider category
DEFAULT_TEST_PAYLOAD = {
    "messages": [{"role": "user", "content": "1"}],
    "max_tokens": 5,
}

# Best lightweight model per platform for low-cost probing
# Legacy suggestions only; probing requires membership in the current catalog.
LIGHTWEIGHT_MODELS = {
    "cohere": "command-r-08-2024",          # c4ai-aya-expanse-32b works; command is faster
    "groq": "llama-3.1-8b-instant",
    "mistral": "codestral-2508",
    "cloudflare": "@cf/aisingapore/gemma-sea-lion-v4-27b-it",  # exists in DB
    "github": "gpt-4o-mini",               # GitHub Models free tier
    "cerebras": "llama-3.1-8b",
    "openrouter": "google/gemini-2.0-flash-exp:free",
    "zhipu": "glm-4.5-flash",              # confirmed in DB
    "agnes": "agnes-2.5-flash",            # confirmed in DB
    "aion": "aion-labs/aion-2.0",           # confirmed in DB
    "sealion": "aisingapore/Gemma-SEA-LION-v4-27B-IT",  # confirmed in DB
    "routeway": "glm-4.5-flash",           # generic fallback
    "navy": "deepseek-v4-flash",           # generic fallback
    "electronhub": "codestral-latest",     # generic fallback
    "bai": "codestral-latest",             # generic fallback
    "bazaarlink": "deepseek/deepseek-v4-flash-0731free:free",  # confirmed in DB
    "requesty": "google/gemma-4-31b-it",   # confirmed in DB
    "reka": "reka-flash",                  # key-based ID in DB (19)
    "anyapi": "codestral-latest",
    "huggingface": "CohereLabs/aya-expanse-32b",  # confirmed in DB
    "nvidia": "google/diffusiongemma-26b-a4b-it",  # confirmed in DB
    "opencode": "codestral-latest",
    "google": "gemini-2.5-flash",          # confirmed in DB
    "nara": "agnes-2.5-flash",             # confirmed in DB
    "llm7": "codestral-latest",            # confirmed in DB
    "xkiro": "mistralai/codestral-2508",   # confirmed in DB
    "unorouter": "deepseek-v4-flash:free", # confirmed in DB
    "waterfall": "glm-4.5-flash",
    "blaze": "gemini-2.0-flash",
    "custom": "MiniMax-M2",               # confirmed in DB
}

# Platform → list of fallback models (used when primary returns 404)
PLATFORM_FALLBACK_MODELS = {
    "cloudflare": ["@cf/qwen/qwen3-30b-a3b-fp8", "@cf/meta/llama-4-scout-17b-16e-instruct"],
    "groq": ["llama-3.1-8b-instant", "llama-3.3-70b-specdec"],
    "mistral": ["codestral-2508", "mistral-small-latest"],
    "google": ["gemini-2.5-flash", "gemini-3.5-flash"],
    "cohere": ["command-r-08-2024", "c4ai-aya-expanse-32b"],
}


def _get_model_for_platform(platform: str) -> str | None:
    """Return the best test model for a platform, or None."""
    return LIGHTWEIGHT_MODELS.get(platform)


class AutoProber:
    """Probe freellmapi providers with lightweight requests."""
    
    def __init__(
        self,
        base_url: str = "http://localhost:3001",
        api_key: str | None = None,
        timeout: float = 15.0,
        transport: httpx.AsyncBaseTransport | None = None,
        model_catalog: dict[str, list[str]] | None = None,
        bound_model_routes: dict[str, list[str]] | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        # Unified client credential only; never fall back to a bundled secret.
        self.api_key = api_key if api_key is not None else os.getenv("FREELLMAPI_API_KEY", "")
        self.timeout = timeout
        self.transport = transport
        self.model_catalog = model_catalog or {}
        # Exact DB-derived member IDs, not generic HTTP owned_by or group slugs.
        # Supply only after confirming the local router supports member pinning.
        self.bound_model_routes = bound_model_routes
    
    async def probe_provider(
        self,
        provider: FreeProvider,
        model: str | None = None,
    ) -> ProviderProbeResult:
        """Probe a single provider with a minimal request.

        Tries the primary model first; on 404 (model not in catalog)
        falls back to alternative models for that platform.
        """
        if provider.status == ProviderStatus.DISABLED or not provider.probe_enabled:
            return ProviderProbeResult(provider.platform, False, "none", error_type="disabled",
                                       response_category="disabled", error_message="Probing disabled")
        if not self.api_key:
            logger.warning("Unified router auth missing; provider keys are unverified")
            return ProviderProbeResult(provider.platform, False, "none", error_type="config",
                                       error_message="Configure unified router authentication",
                                       response_category="router_auth")
        category, status = "", None
        try:
            async with httpx.AsyncClient(timeout=self.timeout, transport=self.transport) as client:
                response = await client.get(f"{self.base_url}/v1/models",
                                            headers={"Authorization": f"Bearer {self.api_key}"})
            status = response.status_code
            if status in (401, 403):
                category = "router_auth"
            elif status != 200:
                category = "router_error"
            else:
                catalog = response.json().get("data")
                if not isinstance(catalog, list) or not all(isinstance(row, dict) and isinstance(row.get("id"), str) and isinstance(row.get("platforms", []), list) for row in catalog):
                    category = "invalid_catalog"
        except httpx.TimeoutException:
            category = "timeout"
        except httpx.RequestError:
            category = "network"
        except (ValueError, AttributeError, TypeError):
            category = "invalid_catalog"
        if category:
            log = logger.warning if category in ("router_auth", "invalid_catalog") else logger.error
            log("Router catalog unavailable (%s); provider keys remain unverified", category)
            return ProviderProbeResult(provider.platform, False, "none", http_status=status,
                error_type=category, response_category=category,
                error_message="Router catalog unavailable; provider not verified",
                evidence=[{"phase": "catalog", "http_status": status, "category": category}])
        if self.bound_model_routes is not None:
            # The router lists group slugs but also accepts exact member pins.
            # An explicitly supplied (even empty) binding map is authoritative:
            # never widen it to a shared group or a different upstream's models.
            prefix = provider.platform + ":"
            available = [candidate for candidate in self.bound_model_routes.get(provider.platform, [])
                         if isinstance(candidate, str) and candidate.startswith(prefix)
                         and candidate[len(prefix):].strip()
                         and (provider.platform != "custom" or (
                             "#" in candidate[len(prefix):]
                             and all(part.strip() for part in candidate[len(prefix):].rsplit("#", 1))))]
            candidates = []
            for preferred in (model, provider.probe_model):
                if preferred:
                    for candidate in available:
                        upstream = candidate[len(prefix):].split("#", 1)[0]
                        if preferred in (candidate, upstream) and self.api_key not in candidate and candidate not in candidates:
                            candidates.append(candidate)
            for candidate in available:
                if self.api_key not in candidate and candidate not in candidates:
                    candidates.append(candidate)
        else:
            available = [row["id"] for row in catalog if row.get("owned_by") == provider.platform
                         or provider.platform in row.get("platforms", [])]
            listed = {row["id"] for row in catalog}  # Gateway flags are not inference proof.
            for candidate in self.model_catalog.get(provider.platform, []):
                if candidate in listed and candidate not in available:
                    available.append(candidate)
            candidates = []
            for candidate in (model, provider.probe_model, *available):
                if candidate and self.api_key not in candidate and candidate in available and candidate not in candidates:
                    candidates.append(candidate)
        if not candidates:
            return ProviderProbeResult(provider.platform, False, "none", error_type="config",
                                       error_message="No provider models in router catalog",
                                       response_category="no_models")
        attempted, evidence = [], []
        for candidate in candidates:
            result = await self._do_probe(candidate, provider.platform)
            attempted.append(candidate)
            result.error_message = sanitize_evidence(result.error_message, (self.api_key,))
            result.evidence = sanitize_evidence(result.evidence, (self.api_key,))
            if not result.evidence:
                result.evidence = [{"model": candidate, "http_status": result.http_status}]
            for item in result.evidence:
                item.update(phase="completion", category=result.response_category,
                            checked_at=result.checked_at.isoformat(), response_time_ms=result.response_time_ms)
            evidence.extend(result.evidence)
            result.attempted_models = list(attempted)
            result.evidence = list(evidence)
            if result.http_status != 404:
                return result
        return result
    
    async def _do_probe(self, test_model: str, platform: str) -> ProviderProbeResult:
        """Run a single probe request against freellmapi."""
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "X-FreeLLM-Cache": "off",
        }
        
        payload = {
            "model": test_model,
            "messages": [{"role": "user", "content": "1"}],
            "max_tokens": 5,
        }
        
        start_time = time.time()
        
        try:
            async with httpx.AsyncClient(timeout=self.timeout, transport=self.transport) as client:
                response = await client.post(
                    f"{self.base_url}/v1/chat/completions",
                    headers=headers,
                    json=payload,
                )
                
                duration_ms = int((time.time() - start_time) * 1000)
                
                # Success
                if response.status_code == 200:
                    routed = response.headers.get("x-routed-via", "")
                    expected_model = test_model
                    prefix = platform + ":"
                    if self.bound_model_routes is not None and test_model.startswith(prefix):
                        expected_model = test_model[len(prefix):].split("#", 1)[0]
                    category = ""
                    if not routed:
                        category = "route_unverified"
                    elif routed != f"{platform}/{expected_model}":
                        category = "route_mismatch"
                    elif response.headers.get("x-freellm-cache", "").lower() == "hit":
                        category = "cached_response"
                    else:
                        try:
                            choices = response.json().get("choices")
                            message = choices[0].get("message") if isinstance(choices, list) and choices and isinstance(choices[0], dict) else None
                            content = message.get("content") if isinstance(message, dict) else None
                            if not isinstance(content, str) or not content.strip():
                                category = "invalid_response"
                        except (ValueError, AttributeError, TypeError):
                            category = "invalid_response"
                    if category:
                        return ProviderProbeResult(platform, False, test_model, http_status=200,
                            error_type=category, response_category=category,
                            error_message="Router response did not verify a fresh completion from this provider",
                            evidence=[{"model": test_model, "http_status": 200, "routed_via": routed, "category": category}])
                    # Extract quota headers if present
                    remaining = self._extract_remaining(response.headers)
                    limit = self._extract_limit(response.headers)
                    reset_at = response.headers.get("x-ratelimit-reset") or response.headers.get("x-ratelimit-reset-requests")
                    
                    return ProviderProbeResult(
                        provider_platform=platform,
                        success=True,
                        model_tested=test_model,
                        http_status=200,
                        response_category="success",
                        evidence=[{"model": test_model, "http_status": 200,
                                   "routed_via": response.headers.get("x-routed-via", "")}],
                        remaining_quota=remaining,
                        quota_limit=limit,
                        quota_reset_at=reset_at,
                        response_time_ms=duration_ms,
                    )
                
                # Handle errors
                try:
                    err_json = response.json()
                    error = err_json.get("error", {})
                    if isinstance(error, dict):
                        err_msg = str(error.get("message", "Request rejected"))
                        err_type = str(error.get("type", "unknown"))
                        err_code = str(error.get("code", ""))
                    else:
                        err_msg, err_type, err_code = str(error), "unknown", ""
                except Exception:
                    err_msg = response.text[:200]
                    err_type = "raw_error"
                    err_code = ""
                
                # HTTP categories take precedence over incidental diagnostic words.
                err_msg = sanitize_evidence(str(err_msg), (self.api_key,))
                msg_lower = (err_msg + " " + err_code + " " + err_type).lower()
                code = response.status_code
                if code in (401, 403) and any(word in msg_lower for word in ("unified", "router auth", "missing bearer")):
                    classified_type = "router_auth"
                elif code in (401, 403):
                    classified_type = "auth"
                elif 500 <= code < 600:
                    classified_type = "server_error"
                elif code == 404:
                    classified_type = "model_not_found"
                elif code == 402 or any(word in msg_lower for word in ("insufficient_quota", "insufficient quota", "quota exhausted", "credits exhausted", "out of credits", "daily quota", "monthly quota")):
                    classified_type = "quota"
                elif code == 429:
                    classified_type = "rate_limit"
                else:
                    classified_type = "unknown"
                
                return ProviderProbeResult(
                    provider_platform=platform,
                    success=False,
                    model_tested=test_model,
                    http_status=response.status_code,
                    response_category=classified_type,
                    evidence=[{"model": test_model, "http_status": response.status_code}],
                    error_type=classified_type,
                    error_message=err_msg[:300],
                    response_time_ms=duration_ms,
                )
                
        except httpx.TimeoutException:
            return ProviderProbeResult(
                provider_platform=platform,
                success=False,
                model_tested=test_model,
                error_type="timeout",
                response_category="timeout",
                error_message=f"Request timed out after {self.timeout}s",
                response_time_ms=int((time.time() - start_time) * 1000),
            )
        except Exception as e:
            return ProviderProbeResult(
                provider_platform=platform,
                success=False,
                model_tested=test_model,
                error_type="network",
                response_category="network",
                error_message="Network request failed; provider unverified",
                response_time_ms=int((time.time() - start_time) * 1000),
            )
    
    async def probe_all(
        self,
        providers: list[FreeProvider],
        concurrency: int = 3,
        delay_between: float = 0.5,
    ) -> dict[str, ProviderProbeResult]:
        """Probe all enabled providers with rate limiting."""
        results: dict[str, ProviderProbeResult] = {}
        sem = asyncio.Semaphore(concurrency)
        
        async def _probe_one(p: FreeProvider):
            if not p.probe_enabled or p.status == ProviderStatus.DISABLED:
                return
            async with sem:
                await asyncio.sleep(delay_between)
                logger.info(f"Probing provider: {p.name} ({p.platform})")
                res = await self.probe_provider(p)
                results[p.platform] = res
        
        tasks = [_probe_one(p) for p in providers if p.probe_enabled and p.status != ProviderStatus.DISABLED]
        await asyncio.gather(*tasks, return_exceptions=True)
        
        return results
    
    def _extract_remaining(self, headers: httpx.Headers) -> int | None:
        """Extract remaining quota from response headers."""
        candidates = [
            "x-ratelimit-remaining-requests",
            "x-ratelimit-remaining-tokens",
            "x-ratelimit-remaining",
            "ratelimit-remaining",
        ]
        for c in candidates:
            val = headers.get(c)
            if val is not None:
                try:
                    return int(val)
                except ValueError:
                    pass
        return None
    
    def _extract_limit(self, headers: httpx.Headers) -> int | None:
        """Extract quota limit from response headers."""
        candidates = [
            "x-ratelimit-limit-requests",
            "x-ratelimit-limit-tokens",
            "x-ratelimit-limit",
            "ratelimit-limit",
        ]
        for c in candidates:
            val = headers.get(c)
            if val is not None:
                try:
                    return int(val)
                except ValueError:
                    pass
        return None