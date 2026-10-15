"""Main tracker: loads existing providers from freellmapi DB and probes/verifies them."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .models import FreeProvider, ProviderStatus, ProviderTier, FreellmapiIntegrationType, ProviderProbeResult, aware_utc, sanitize_evidence
from .prober import AutoProber


logger = logging.getLogger(__name__)


class FreeProviderTracker:
    """Track and verify freellmapi providers' free tier status."""
    
    # Operator tier/quota annotations are not shipped with the public source.
    PROVIDER_REGISTRY: dict[str, dict[str, Any]] = {}

    def __init__(
        self,
        freellmapi_db_path: str | Path = "./data/freeapi.db",
        state_path: str | Path = "./data/provider_state.json",
    ):
        self.db_path = Path(freellmapi_db_path)
        self.state_path = Path(state_path)
        self.providers: dict[str, FreeProvider] = {}
        self.probe_results: dict[str, ProviderProbeResult] = {}
    
    def load_from_db(self) -> int:
        """Load existing providers from freellmapi SQLite DB."""
        conn = None
        try:
            import sqlite3
            if not self.db_path.is_file():
                logger.warning("freellmapi DB not found")
                return 0
            conn = sqlite3.connect(self.db_path.resolve().as_uri() + "?mode=ro", uri=True)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA query_only=ON")
            cur = conn.cursor()
            
            rows = cur.execute("SELECT id, platform, label, status, enabled FROM api_keys ORDER BY enabled DESC, id").fetchall()
            loaded = 0
            
            seen = set()
            for row in rows:
                platform = row["platform"]
                if platform in seen:
                    continue
                seen.add(platform)
                db_provider = self._build_provider_from_db(platform, row)
                current = self.providers.get(platform)
                if current is None:
                    self.providers[platform] = db_provider
                else:
                    same_key = current.freellmapi_key_id == db_provider.freellmapi_key_id
                    was_disabled = current.status == ProviderStatus.DISABLED
                    for name in ("freellmapi_key_id", "freellmapi_key_label", "freellmapi_integration", "probe_enabled"):
                        setattr(current, name, getattr(db_provider, name))
                    if not same_key or was_disabled:
                        current.checked_at = current.last_verified = None
                        current.probe_evidence = []
                        current.probe_source = current.last_verified_source = ""
                        current.status = ProviderStatus.UNKNOWN
                        current.status_reason = "Key configuration changed; re-probe required"
                        current.freellmapi_action = "Re-probe provider"
                    if db_provider.status == ProviderStatus.DISABLED:
                        current.status = ProviderStatus.DISABLED
                loaded += 1
            
            logger.info(f"Loaded {loaded} providers from freellmapi DB")
            return loaded
            
        except FileNotFoundError:
            logger.error(f"freellmapi DB not found at {self.db_path}")
            return 0
        except Exception as e:
            logger.error("Failed to read provider DB (%s)", type(e).__name__)
            return 0
        finally:
            if conn is not None:
                conn.close()
    
    def _build_provider_from_db(self, platform: str, row: Any) -> FreeProvider:
        """Build FreeProvider from DB row."""
        key_info = self.PROVIDER_REGISTRY.get(platform, {})
        key_id = row["id"]
        
        # Determine integration type
        if key_id is not None:
            if platform in ["cohere", "groq", "mistral", "cloudflare", "github", 
                          "cerebras", "openrouter", "zhipu", "huggingface", "google"]:
                integration = FreellmapiIntegrationType.IN_CATALOG_KEY_IN_DB
            elif platform in ["reka", "waterfall", "blaze", "custom_minimax"]:
                integration = FreellmapiIntegrationType.IN_REGISTRY_NOT_CATALOG
            else:
                integration = FreellmapiIntegrationType.IN_CATALOG_KEY_IN_DB
        else:
            integration = FreellmapiIntegrationType.IN_CATALOG_NO_KEY
        
        enabled = row["enabled"] == 1
        status = ProviderStatus.UNKNOWN if enabled else ProviderStatus.DISABLED
        
        return FreeProvider(
            name=platform.replace("_", " ").title(),
            platform=platform,
            tier_type=key_info.get("tier_type", ProviderTier.UNKNOWN),
            tier_details=key_info.get("tier_details", ""),
            reset_schedule=key_info.get("reset_schedule", ""),
            status=status,
            freellmapi_key_id=key_id,
            freellmapi_integration=integration,
            freellmapi_key_label=row["label"] if row["label"] else "",
            probe_enabled=enabled,
            probe_model=LIGHTWEIGHT_MODELS.get(platform, ""),
        )
    
    def load_probe_catalog(self, *, qualified: bool = False) -> dict[str, list[str]]:
        """Read enabled bindings; optionally pin the router's exact member IDs.

        v0.13.2 lists unified group slugs, not these member IDs. A bare model ID
        can fail over to another upstream; ``platform:model_id`` pins a member.
        Keep the default raw mapping for legacy callers; qualified mappings must
        be passed as AutoProber.bound_model_routes, not as HTTP-listed aliases.
        """
        import re
        import sqlite3
        if not self.db_path.is_file():
            return {}
        conn = None
        try:
            conn = sqlite3.connect(self.db_path.resolve().as_uri() + "?mode=ro", uri=True)
            conn.execute("PRAGMA query_only=ON")
            columns = {r[1] for r in conn.execute("PRAGMA table_info(models)")}
            scope_column = "endpoint_scope" if "endpoint_scope" in columns else "''"
            order_column = "id" if "id" in columns else "model_id"
            rows = conn.execute(f"SELECT platform, model_id, key_id, {scope_column} FROM models WHERE enabled=1 ORDER BY {order_column}").fetchall()
            catalog = {}
            for platform, model_id, key_id, scope in rows:
                provider = self.providers.get(platform)
                if not (provider and provider.probe_enabled and provider.status != ProviderStatus.DISABLED
                        and (key_id is None or (type(key_id) is int and key_id == provider.freellmapi_key_id))
                        and isinstance(model_id, str) and model_id.strip()):
                    continue
                candidate = model_id
                if qualified:
                    candidate = f"{platform}:{model_id}"
                    if platform == "custom":
                        # Do not probe an unscoped relay or select another endpoint.
                        if key_id is None or not isinstance(scope, str) or not scope.strip():
                            continue
                        without_scheme = re.sub(r"^[a-z][a-z0-9+.-]*://", "", scope, flags=re.I)
                        handle = re.sub(r"[^a-z0-9.]+", "-", without_scheme.lower()).strip("-.") or "endpoint"
                        candidate += f"#{handle}"
                if candidate not in catalog.setdefault(platform, []):
                    catalog[platform].append(candidate)
            return catalog
        except sqlite3.Error as exc:
            logger.warning("Model binding catalog unavailable (%s)", type(exc).__name__)
            return {}
        finally:
            if conn is not None:
                conn.close()

    def apply_probe_result(self, platform: str, result: ProviderProbeResult) -> FreeProvider:
        """Apply the observation timestamp, never the rendering/loading time."""
        if result.provider_platform != platform:
            raise ValueError("Probe platform does not match target provider")
        provider = self.providers[platform]
        self.probe_results[platform] = result
        provider.checked_at = aware_utc(getattr(result, "checked_at", None) or result.timestamp)
        provider.last_verified = provider.checked_at
        provider.probe_source = getattr(result, "probe_source", "local:freellmapi")
        provider.last_verified_source = "probe"
        provider.probe_evidence = sanitize_evidence(result.evidence)
        if provider.status != ProviderStatus.DISABLED:
            provider.status, provider.status_reason = result.to_status()
            provider.freellmapi_action = self._generate_action(provider, result)
        return provider

    async def update_providers(self) -> list[FreeProvider]:
        """Update all tracked providers with latest status."""
        updates = []
        
        for platform, provider in self.providers.items():
            # Check if we should probe
            if provider.probe_enabled and provider.status != ProviderStatus.DISABLED:
                result = await self._probe_single(provider)
                if result:
                    self.apply_probe_result(platform, result)
            
            updates.append(provider)
        
        return updates
    
    async def _probe_single(self, provider: FreeProvider) -> ProviderProbeResult | None:
        """Probe a single provider and return result."""
        prober = AutoProber(bound_model_routes=self.load_probe_catalog(qualified=True))
        return await prober.probe_provider(provider)
    
    def _generate_action(self, provider: FreeProvider, result: ProviderProbeResult) -> str:
        """Generate actionable next steps based on probe result."""
        status, reason = result.to_status()
        
        if status == ProviderStatus.LIVE:
            return "No action needed for verified completion; free entitlement and reset limits are not established by this probe"
        elif status == ProviderStatus.BROKEN_KEY:
            return f"Key invalid ({result.http_status}). Get new key from: {self._get_dashboard_url(provider.platform)}"
        elif status == ProviderStatus.EXHAUSTED:
            if provider.tier_type == ProviderTier.CREDIT_DOLLARS and "monthly" in provider.reset_schedule.lower():
                return f"Credit exhausted. Resets monthly (next renewal: {provider.reset_schedule})"
            elif provider.tier_type == ProviderTier.DAILY_TOKENS:
                return f"Daily quota exhausted. Resets daily at {provider.reset_schedule}"
            return f"Quota exhausted ({reason}). Check provider dashboard for limits."
        elif status == ProviderStatus.PAYWALLED:
            return f"Provider moved behind paywall. Remove key or switch to paid tier."
        elif status == ProviderStatus.DEGRADED:
            return f"Provider degraded ({reason}). May recover automatically."
        else:
            return f"Status unknown ({reason}). Verify manually."
    
    def _get_dashboard_url(self, platform: str) -> str:
        """Get provider dashboard URL for key management."""
        dashboards = {
            "cloudflare": "https://dash.cloudflare.com/",
            "groq": "https://console.groq.com/keys",
            "cohere": "https://dashboard.cohere.com/api-keys",
            "mistral": "https://console.mistral.ai/api-keys",
            "google": "https://aistudio.google.com/apikey",
            "zhipu": "https://open.bigmodel.cn/usercenter/apikeys",
            "huggingface": "https://huggingface.co/settings/tokens",
        }
        return dashboards.get(platform, f"https://{platform}.com/")
    
    def save_state(self):
        """Persist provider state to JSON."""
        state = {
            "providers": {p.platform: p.to_dict() for p in self.providers.values()},
            "last_updated": datetime.now(timezone.utc).isoformat(),
        }
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(json.dumps(state, indent=2), encoding="utf-8")
        logger.info(f"Saved provider state to {self.state_path}")
    
    def load_state(self):
        """Load persisted provider state."""
        if self.state_path.exists():
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
            for platform, pdata in data.get("providers", {}).items():
                persisted = FreeProvider.from_dict(pdata)
                current = self.providers.get(platform)
                if current is None:
                    self.providers[platform] = persisted
                    continue
                for name in ("notes", "tags", "dashboard_url", "key_format", "discovered_from", "discovery_date", "is_new", "probe_model", "probe_cost_estimate"):
                    if name in pdata:
                        setattr(current, name, getattr(persisted, name))
                # DB owns identity, key availability and enabled flags. State owns probes.
                if persisted.freellmapi_key_id not in (None, current.freellmapi_key_id) or persisted.status == ProviderStatus.DISABLED:
                    continue
                if persisted.checked_at and (current.checked_at is None or persisted.checked_at >= current.checked_at):
                    for name in ("checked_at", "last_verified", "probe_source", "probe_evidence", "last_verified_source"):
                        setattr(current, name, getattr(persisted, name))
                    if current.status != ProviderStatus.DISABLED:
                        for name in ("status", "status_reason", "freellmapi_action"):
                            setattr(current, name, getattr(persisted, name))
            logger.info(f"Loaded {len(self.providers)} providers from state")


# Light weight models map (shared with prober)
LIGHTWEIGHT_MODELS = {
    "cloudflare": "@cf/qwen/qwen3-30b-a3b-fp8",
    "groq": "llama-3.1-8b-instant",
    "google": "gemini-2.0-flash-exp",
    "reka": "reka-flash",
    "mistral": "mistral-small-latest",
    "cohere": "command-r-08-2024",
    "huggingface": "meta-llama/Llama-3.2-3B-Instruct",
    "openrouter": "google/gemini-2.0-flash-exp:free",
    "cerebras": "llama3.1-8b",
    "nvidia": "meta/llama-3.1-8b-instruct",
    "zhipu": "glm-4-flash",
    "custom": "MiniMax-Text-01",
}