"""Main entry point for Eli-Lain AI Tracker.

Wires together: free LLM provider tracking (freellmapi integration),
GitHub tool discovery, news aggregation, and HTML report generation.
"""

from __future__ import annotations

import asyncio
import logging
import hashlib
import json
import tempfile
import os
import sys
import httpx
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

# Add paths
sys.path.insert(0, str(Path(__file__).parent / "src" / "tracker-core"))
sys.path.insert(0, str(Path(__file__).parent / "src"))

from tracker_core.schema import NewsItem, SourceType
from tracker_core.reporter_html import get_html_reporter
from tracker_core.news_health import aggregate_news_outcomes, build_news_health
from tracker_core.cache import FileCache
from intel_sources.store import IntelStore, parse_time
from intel_sources.decisions import provider_actions, provider_changes
from intel_sources.catalogs import fetch_catalogs
from intel_sources.tools import fetch_tools

from news_tracker.adapters import get_all_adapters
from news_tracker.dedup_filter import deduplicate_and_filter
from news_tracker.summarizer import summarize_news, validate_config
from news_tracker.scoring import score_and_categorize, group_by_category, Scorer
from news_tracker.health import SourceHealthTracker
from news_tracker.store_sqlite import SQLiteStore
from tracker_core.notifier import get_notifier
from news_tracker.importance import score_importance

# New: free LLM provider tracking
from freellmapi_tracker import (
    FreeProvider,
    ProviderStatus,
    ProviderTier,
    FreellmapiIntegrationType,
    FreeProviderTracker,
    ProviderDiscovery,
    AutoProber,
)

logger = logging.getLogger(__name__)


def _apply_importance_score(item: NewsItem) -> NewsItem:
    """Apply importance scoring to an item."""
    from news_tracker.importance import score_importance
    importance = score_importance(item)
    item.relevance_score = (item.relevance_score * 0.7) + (importance * 0.3)
    return item


class NewsTracker:
    """Main tracker for AI/tech news + free LLM providers + GitHub tools."""

    def __init__(
        self,
        data_dir: Path | str = "./data",
        cache_dir: Path | str = "./.cache",
        reports_dir: Path | str = "./reports",
        theme: str = "lain",
        summarizer_config: dict[str, Any] | None = None,
        failure_threshold: int = 3,
        source_timeout_seconds: float = 45.0,
        freellmapi_db_path: str | Path | None = None,
        freellmapi_url: str | None = None,
    ):
        self.data_dir = Path(data_dir)
        self.cache_dir = Path(cache_dir)
        self.reports_dir = Path(reports_dir)
        self.theme = theme
        self.summarizer_config = summarizer_config or {}
        self.freellmapi_db_path = freellmapi_db_path or os.getenv("FREELLMAPI_DB_PATH") or self.data_dir / "freeapi.db"
        self.freellmapi_url = freellmapi_url or os.getenv("FREELLMAPI_URL") or "http://localhost:3001"
        self.source_timeout_seconds = source_timeout_seconds

        # Initialize components
        self.sqlite = SQLiteStore(self.data_dir / "news.db")
        self.cache = FileCache(self.cache_dir)
        self.reporter = get_html_reporter(theme)
        self.health = SourceHealthTracker(
            failure_threshold=failure_threshold,
            state_path=str(self.data_dir / "health.json"),
        )

        # Load persisted health state
        persisted_health = self.sqlite.load_source_health()
        for name, h in persisted_health.items():
            health_record = self.health.get_or_create(name)
            health_record.consecutive_failures = h.get("consecutive_failures", 0)
            # Older SQLite rows have no cooldown deadline: never restore an
            # indefinite disabled flag. JSON health state carries current deadlines.
            health_record.is_disabled = False
            health_record.total_items = h.get("total_items", 0)
            health_record.last_success = parse_time(h.get("last_success"))
            health_record.last_failure = parse_time(h.get("last_failure"))
        self.health.load_state()
        self.feed_health = SourceHealthTracker(state_path=str(self.data_dir / "feed_health.json"))
        self.feed_health.load_state()
        self.feed_results: dict[str, dict[str, Any]] = {}
        self.source_results: dict[str, dict[str, Any]] = {}

        # Notifier
        notif_config = {
            "channel": os.getenv("NOTIFICATION_CHANNEL", "none").replace(" ", "").split("\n")[0],
            "telegram_bot_token": os.getenv("TELEGRAM_BOT_TOKEN", "").replace(" ", "").split("\n")[0],
            "telegram_chat_id": os.getenv("TELEGRAM_CHAT_ID", "").strip().split("\n")[0],
            "discord_bot_token": os.getenv("DISCORD_BOT_TOKEN", "").replace(" ", "").split("\n")[0],
            "discord_channel_id": os.getenv("DISCORD_CHANNEL_ID", "").strip().split("\n")[0],
            "email_host": os.getenv("EMAIL_HOST", "smtp.gmail.com"),
            "email_port": int(os.getenv("EMAIL_PORT") or "587"),
            "email_user": os.getenv("EMAIL_USER", "").strip(),
            "email_password": os.getenv("EMAIL_PASSWORD", "").replace(" ", ""),
            "email_from": os.getenv("EMAIL_FROM", os.getenv("EMAIL_USER", "")).strip(),
            "email_to": [e.strip() for e in os.getenv("EMAIL_TO", "").split(",") if e.strip()],
        }
        self.notifier = get_notifier(notif_config)

        # Breaking news threshold
        self.breaking_threshold = 0.7
        self.last_run_date: datetime | None = None

        # NEW: Provider tracker
        self.provider_tracker = FreeProviderTracker(
            freellmapi_db_path=self.freellmapi_db_path,
            state_path=self.data_dir / "provider_state.json",
        )

        # NEW: GitHub tool discovery
        self.tool_discovery = ProviderDiscovery()
        self.intel_store = IntelStore(self.data_dir / "intel_state.json")
        self.last_report_path: Path | None = None

    # ── Provider tracking methods ──────────────────────────────────────

    def load_providers(self) -> int:
        """Load providers from freellmapi DB."""
        count = self.provider_tracker.load_from_db()
        self.provider_tracker.load_state()
        return count

    async def probe_providers(self) -> int:
        """Run the auto-prober on all enabled providers. Returns count updated."""
        prober = AutoProber(
            base_url=self.freellmapi_url,
            api_key=os.getenv("FREELLMAPI_API_KEY"),
            bound_model_routes=self.provider_tracker.load_probe_catalog(qualified=True),
        )
        previous = [p.to_dict() for p in self.provider_tracker.providers.values()]
        providers = [p for p in self.provider_tracker.providers.values() if p.probe_enabled and p.status != ProviderStatus.DISABLED]
        results = await prober.probe_all(providers, concurrency=3)

        updated = 0
        for platform, result in results.items():
            provider = self.provider_tracker.providers.get(platform)
            if provider:
                provider = self.provider_tracker.apply_probe_result(platform, result)
                sanitized = result.to_dict()
                provider.probe_evidence.append({key: sanitized[key] for key in (
                    "attempted_models", "response_category", "checked_at", "probe_source",
                    "error_message", "remaining_quota", "quota_limit", "quota_reset_at",
                )})
                updated += 1

        self.provider_tracker.save_state()
        changes = provider_changes(previous, [p.to_dict() for p in self.provider_tracker.providers.values()])
        path = self.data_dir / "provider_changes.json"
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as handle:
                temporary = Path(handle.name)
                json.dump({"changes": changes}, handle, ensure_ascii=False)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        logger.info(f"Probed {len(results)} providers, {updated} updated")
        return updated

    def _action_for(self, provider: FreeProvider, result: Any) -> str:
        """Generate the next-step action string."""
        status, reason = result.to_status()
        if status == ProviderStatus.LIVE:
            extra = ""
            if result.remaining_quota is not None:
                extra = f" ({result.remaining_quota} remaining)"
            return f"No action needed — free tier active{extra}"
        if status == ProviderStatus.BROKEN_KEY:
            dash = self._dash_url(provider.platform)
            return f"Key returning {result.http_status or 401}. Get a fresh key from {dash} and update freellmapi."
        if status == ProviderStatus.EXHAUSTED:
            if "monthly" in provider.reset_schedule.lower():
                return f"Credit exhausted. Resets monthly ({provider.reset_schedule})."
            if "daily" in provider.reset_schedule.lower():
                return f"Daily quota exhausted. Resets daily ({provider.reset_schedule})."
            return f"Quota exhausted: {reason}. Check {self._dash_url(provider.platform)}."
        if status == ProviderStatus.PAYWALLED:
            return "Provider moved behind paywall. Disable in freellmapi or upgrade."
        if status == ProviderStatus.DEGRADED:
            return f"Degraded: {reason}. May recover — monitor."
        return f"Status unknown ({reason}). Verify manually at {self._dash_url(provider.platform)}."

    @staticmethod
    def _dash_url(platform: str) -> str:
        dashboards = {
            "cloudflare": "dash.cloudflare.com",
            "groq": "console.groq.com/keys",
            "cohere": "dashboard.cohere.com/api-keys",
            "mistral": "console.mistral.ai/api-keys",
            "google": "aistudio.google.com/apikey",
            "zhipu": "open.bigmodel.cn/usercenter/apikeys",
            "huggingface": "huggingface.co/settings/tokens",
            "openrouter": "openrouter.ai/settings/keys",
            "nvidia": "build.nvidia.com/dashboard",
            "cerebras": "cloud.cerebras.ai",
        }
        return dashboards.get(platform, f"{platform}.com")

    def provider_status_rows(self) -> list[dict[str, Any]]:
        """Render current verdict separately from historical status and check age."""
        rows = []
        for p in self.provider_tracker.providers.values():
            snapshot = p.to_dict()
            status = p.effective_status()
            if not p.probe_source and status != ProviderStatus.DISABLED:
                status = ProviderStatus.UNKNOWN
            status_class = {
                ProviderStatus.LIVE: "stable", ProviderStatus.DEGRADED: "updated",
                ProviderStatus.BROKEN_KEY: "dead", ProviderStatus.EXHAUSTED: "dead",
                ProviderStatus.PAYWALLED: "dead", ProviderStatus.UNKNOWN: "unknown",
                ProviderStatus.DISABLED: "disabled",
            }[status]
            description = []
            if p.tier_details:
                description.append("Registry tier (unverified): " + p.tier_details)
            if p.reset_schedule:
                description.append("Registry reset (unverified): " + p.reset_schedule)
            description.append(snapshot["status_reason"] or "No provider-specific completion verdict")
            if status == ProviderStatus.UNKNOWN:
                description.append("Last-known status: " + p.status.value + "; not current availability proof")
            rows.append({
                "name": p.name, "platform": p.platform, "status": status_class,
                "actual_status": status.value, "last_known_status": p.status.value,
                "description": " · ".join(description), "freshness": p.freshness(),
                "checked_at": snapshot["checked_at"], "probe_source": p.probe_source,
                "evidence": snapshot["probe_evidence"],
            })
        priority = {"dead": 0, "updated": 1, "unknown": 2, "stable": 3, "disabled": 4}
        return sorted(rows, key=lambda r: (priority.get(r["status"], 99), r["name"].lower()))

    # ── GitHub tool discovery methods ──────────────────────────────────

    async def discover_tools(self) -> list[dict[str, Any]]:
        """Refresh broad model/provider/skill/tool discovery, retaining failed-source cache."""
        # A dedicated credential-free client prevents one source's auth leaking to another.
        async with httpx.AsyncClient(timeout=30, headers={"User-Agent": "EliLain-Intel/3"}) as client:
            catalogs = await fetch_catalogs(
                client, freellmapi_url=self.freellmapi_url,
                api_key=os.getenv("FREELLMAPI_API_KEY") or None,
                unorouter_api_key=os.getenv("UNOROUTER_API_KEY") or None,
                include_local=os.getenv("GITHUB_ACTIONS") != "true",
            )
            tools = await fetch_tools(client, skills_token=os.getenv("VERCEL_OIDC_TOKEN") or None)
        state = self.intel_store.update(
            catalogs["items"] + tools["items"], catalogs["sources"] + tools["sources"],
        )
        return state["items"]

    # ── Existing news pipeline (unchanged) ─────────────────────────────

    async def fetch_all(self) -> list[NewsItem]:
        """Fetch news from all sources with circuit breaker."""
        logger.info("Fetching news from all sources...")
        adapters = get_all_adapters({
            "feed_health_state_path": str(self.data_dir / "feed_health.json"),
            "feed_health": self.feed_health,
        })
        self.feed_results = {}
        self.source_results = {}
        all_items: list[NewsItem] = []

        for adapter in adapters:
            if not self.health.is_source_enabled(adapter.name):
                logger.info(f"Skipping {adapter.name} (circuit broken)")
                self.source_results[adapter.name] = {"status": "cooldown", "items": 0}
                continue
            # Adapters may be reused; only outcomes produced by this invocation count.
            adapter.feed_results = {}
            try:
                items = await asyncio.wait_for(adapter.fetch_all(), timeout=self.source_timeout_seconds)
                results = getattr(adapter, "feed_results", {})
                outcome = aggregate_news_outcomes(results)
                self.source_results[adapter.name] = {
                    "status": outcome["status"] if results else "healthy" if items else "failed",
                    "items": len(items),
                }
                if items or any(row.get("status") == "healthy" for row in results.values()):
                    all_items.extend(items)
                    self.health.record_success(adapter.name, len(items))
                else:
                    self.health.record_failure(adapter.name, "Empty result")
            except asyncio.TimeoutError:
                message = f"Source collection timed out after {self.source_timeout_seconds}s; other sources continued"
                logger.warning("%s: %s", adapter.name, message)
                self.health.record_failure(adapter.name, message)
                self.source_results[adapter.name] = {"status": "failed", "error": "timeout"}
            except Exception as e:
                logger.error("Failed to fetch from %s (%s)", adapter.name, type(e).__name__)
                self.health.record_failure(adapter.name, type(e).__name__)
                self.source_results[adapter.name] = {"status": "failed", "error": "fetch_failure"}
            finally:
                self.feed_results[adapter.name] = dict(getattr(adapter, "feed_results", {}))

        try:
            self.health.save_state()
            self.feed_health.save_state()
        except OSError:
            logger.warning("Health state persistence failed; fetched observations retained")
        logger.info(f"Total fetched: {len(all_items)} items")
        for source_name in self.health._sources:
            source = self.health._sources[source_name]
            self.sqlite.save_source_health(source_name, {
                "consecutive_failures": source.consecutive_failures,
                "last_success": source.last_success.isoformat() if source.last_success else None,
                "last_failure": source.last_failure.isoformat() if source.last_failure else None,
                "is_disabled": source.is_disabled,
                "total_items": source.total_items,
            })
        return all_items

    async def process(self, items: list[NewsItem]) -> tuple[list[NewsItem], dict[str, Any]]:
        context: dict[str, Any] = {}
        unique_items, dedup_stats = deduplicate_and_filter(items)
        context["dedup"] = dedup_stats
        summarized_items = await summarize_news(unique_items, self.summarizer_config)
        scored_items = score_and_categorize(summarized_items)
        scored_items = [_apply_importance_score(item) for item in scored_items]
        relevant_items = [item for item in scored_items if item.is_relevant]
        context["relevant_count"] = len(relevant_items)
        return relevant_items, context

    def _split_breaking_news(self, items: list[NewsItem]) -> tuple[list[NewsItem], list[NewsItem]]:
        breaking, regular = [], []
        for item in items:
            (breaking if item.relevance_score >= self.breaking_threshold else regular).append(item)
        return breaking, regular

    async def push_breaking_news(self, items: list[NewsItem]) -> bool:
        if not items:
            return True
        message = "🚨 **BREAKING NEWS** 🚨\n\n"
        for item in items[:5]:
            message += f"**{item.title}**\n"
            if item.summary:
                message += f"{item.summary[:200]}\n"
            message += f"[Read more]({item.url})\n\n"
        try:
            sent = await self.notifier.send(message)
            if sent:
                logger.info(f"Pushed {len(items)} breaking news items")
            return sent
        except Exception as e:
            logger.error(f"Failed to push breaking news: {e}")
            return False

    async def run(self, date: datetime | None = None, push_breaking: bool = True) -> dict[str, Any]:
        """Run a full news tracking cycle."""
        run_date = date or datetime.now(timezone.utc)
        self.last_run_date = run_date
        logger.info(f"Running news tracker for {run_date.strftime('%Y-%m-%d')}")

        items = await self.fetch_all()
        processed_items, context = await self.process(items)
        # A restored cache replays previously stored stories. Only genuinely new
        # observations may raise a breaking alert, otherwise every daily run
        # re-sends the same headline as breaking news.
        known_urls = self.sqlite.existing_urls(processed_items)
        new_items = [item for item in processed_items if str(item.url) not in known_urls]
        context["replayed_count"] = len(processed_items) - len(new_items)
        inserted = self.sqlite.save_items(processed_items)
        context["inserted"] = inserted

        breaking_items, regular_items = self._split_breaking_news(new_items)
        context["breaking_count"] = len(breaking_items)
        context["regular_count"] = len(regular_items)

        if breaking_items and push_breaking:
            await self.push_breaking_news(breaking_items)

        logger.info(f"Completed: {inserted} new, {len(breaking_items)} breaking, {len(regular_items)} regular")
        return {
            "date": run_date.isoformat(),
            "total_fetched": len(items),
            "breaking": len(breaking_items),
            "regular": len(regular_items),
            "health_table": self.get_health_table(),
            "feed_health_table": self.get_feed_health_table(),
            **context,
        }

    # ── Report generation ──────────────────────────────────────────────

    def generate_report(
        self,
        date: datetime | None = None,
        output_path: Path | str | None = None,
        include_providers: bool = True,
        include_news: bool = True,
        include_tools: bool = True,
        news_limit: int = 1000,
    ) -> str:
        """Generate HTML report with providers, news, and tools.

        `news_limit` is a caller-supplied safety ceiling, not a silent default:
        anything it excludes is disclosed in the news section heading.
        """
        run_date = date or self.last_run_date or datetime.now(timezone.utc)

        # Provider section
        api_items: list[dict[str, Any]] = []
        if include_providers:
            api_items = self.provider_status_rows()

        # News + tools section
        news_items: list[dict[str, Any]] = []
        news_saved_total = 0
        if include_news:
            # get_items defaults to limit=100. A day that saved more than that
            # used to render exactly 100 and claim "100 stories", which is
            # indistinguishable from a genuinely quiet day. Ask for everything
            # observed in the window and disclose the retained total instead.
            items_raw = self.sqlite.get_items(days=1, limit=news_limit)
            news_saved_total = self.sqlite.count_items(days=1)
            for item in items_raw:
                news_items.append({
                    "headline": item.get("title", ""),
                    "source": item.get("source", "unknown"),
                    "summary": item.get("summary") or "",
                    "url": item.get("url", "#"),
                    "published_date": item.get("published_date"),
                    "fetched_date": item.get("fetched_date"),
                })

        intel = self.intel_store.read()
        tool_items = [dict(item, evidence=[item["evidence"]] if isinstance(item.get("evidence"), dict) else item.get("evidence", [])) for item in intel["items"]] if include_tools else []
        change_items = intel["changes"] if include_tools else []
        source_items = intel["sources"] if include_tools else []
        if include_providers:
            path = self.data_dir / "provider_changes.json"
            if path.exists():
                now = datetime.now(timezone.utc)
                for change in json.loads(path.read_text(encoding="utf-8")).get("changes", []):
                    checked = parse_time(change.get("checked_at"))
                    if checked and 0 <= (now - checked).total_seconds() <= 24 * 3600:
                        change_items = [change, *change_items]
        from intel_sources.retention import latest_report_path
        # Routine runs replace one latest file instead of appending a dated
        # archive; an explicit caller path stays exactly that caller's file.
        output_path = output_path or latest_report_path(self.reports_dir)
        recommendations: dict[str, Any] = {}
        if include_tools:
            # Advisory metadata-only views; never replace the broad inventory.
            from intel_sources.recommendations import evaluate_projects
            evaluated = evaluate_projects(intel["items"], now=datetime.now(timezone.utc))
            # Persist the evaluation used by this report, not a one-off audit.
            # This is internal state only; the public exporter has its own boundary.
            evaluation_path = self.data_dir / "project_evaluations.json"
            evaluation_artifact = {
                "result": evaluated,
                "discovery_snapshot": intel["items"],
                "provenance": {
                    "source_inventory_count": len(intel["items"]),
                    "input_state_sha256": hashlib.sha256(
                        self.intel_store.path.read_bytes() if self.intel_store.path.exists()
                        else json.dumps(intel, sort_keys=True).encode("utf-8")).hexdigest(),
                    "generated_by": "main.NewsTracker.generate_report",
                },
            }
            temporary_evaluation = None
            try:
                with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8",
                        dir=evaluation_path.parent, prefix=".project_evaluations-",
                        suffix=".tmp", delete=False) as handle:
                    temporary_evaluation = Path(handle.name)
                    json.dump(evaluation_artifact, handle, ensure_ascii=False)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary_evaluation, evaluation_path)
            finally:
                if temporary_evaluation is not None:
                    temporary_evaluation.unlink(missing_ok=True)
            reviewed = {entry["id"] for group in evaluated["groups"].values()
                        for name in ("shortlist", "deferred", "excluded") for entry in group[name]}
            recommendations = {
                "recommendation_groups": evaluated["groups"],
                "recommendation_coverage": {
                    "reviewed_count": len(reviewed),
                    "discovery_count": evaluated["coverage"]["discovery_count"],
                    "inventory_count": evaluated["inventory_count"],
                    "shortlist_limit_per_project": evaluated["coverage"]["shortlist_limit_per_project"],
                },
            }
        html = self.reporter.render(
            api_items=api_items,
            news_items=news_items,
            news_saved_total=news_saved_total,
            report_date=run_date.strftime("%Y-%m-%d"),
            generated_at=datetime.now(timezone.utc).strftime("%H:%M UTC"),
            persona_image_url=None,
            tool_items=tool_items,
            change_items=change_items,
            source_items=source_items,
            news_health_items=build_news_health(
                {key: vars(value) for key, value in self.health._sources.items()},
                {key: vars(value) for key, value in self.feed_health._sources.items()},
                self.feed_results,
                source_results=self.source_results,
                now=self.feed_health._now(),
            ) if include_news else [],
            action_items=provider_actions([p.to_dict() for p in self.provider_tracker.providers.values()]) if include_providers else [],
            baseline=intel["baseline"],
            runtime_source="GitHub Actions" if os.getenv("GITHUB_ACTIONS") == "true" else "Local host",
            **recommendations,
        )

        from intel_sources.retention import write_latest_text
        # Atomic replace: a failed write preserves the previous good report.
        write_latest_text(output_path, html)
        self.last_report_path = Path(output_path)
        logger.info(f"Report saved to {output_path}")
        return html

    # ── Convenience methods ────────────────────────────────────────────

    def list_recent(self, days: int = 7) -> list[dict]:
        return self.sqlite.get_items(days=days, limit=50)

    def get_weekly_summary(self, weeks: int = 1) -> dict[str, Any]:
        return self.sqlite.get_weekly_summary(weeks=weeks)

    def get_health_table(self) -> list[dict[str, Any]]:
        rows = self.health.get_health_table()
        for row in rows:
            results = self.feed_results.get(row["source"], {})
            counts: dict[str, int] = {}
            for result in results.values():
                status = result.get("status", "unknown")
                counts[status] = counts.get(status, 0) + 1
            row["feed_status_counts"] = counts
            row["feed_count"] = len(results)
            if counts.get("partial") or (counts.get("healthy") and any(status != "healthy" for status in counts)):
                row["status"] = "partial"
            elif counts and not counts.get("healthy"):
                row["status"] = "failed" if counts.get("failed") else "cooldown" if counts.get("cooldown") else "not_attempted"
        return rows

    def get_feed_health_table(self) -> list[dict[str, Any]]:
        return self.feed_health.get_health_table()


async def async_full_audit(tracker: NewsTracker) -> dict[str, Any]:
    """Run the full EliLain audit: load providers → probe → discover tools → report."""
    print("\n=== ELILAIN FULL AUDIT ===")
    print(f"[1/4] Loading providers from freellmapi DB...")
    count = tracker.load_providers()
    print(f"      Loaded {count} providers")

    # Step 2: probe (skip in CI — the provider service runs locally, not on GitHub runners)
    previous = [p.to_dict() for p in tracker.provider_tracker.providers.values()]
    if os.getenv("GITHUB_ACTIONS") == "true":
        print(f"[2/4] Skipping probes (CI environment — freellmapi is local-only). Using persisted state.")
        probed = 0
    else:
        print(f"[2/4] Probing all enabled providers via {tracker.freellmapi_url}...")
        probed = await tracker.probe_providers()
        print(f"      Probed and updated {probed} providers")

    print("[3/4] Discovering models, providers, skills and trending tools...")
    tools = await tracker.discover_tools()
    print(f"      Found {len(tools)} candidate tools")

    print(f"[4/4] Generating HTML report...")
    tracker.generate_report(include_providers=True, include_news=True, include_tools=True)
    report_path = tracker.last_report_path
    print(f"      Report: {report_path}")

    # Notify only newly observed probe changes, never replay history in CI.
    changes = provider_changes(previous, [p.to_dict() for p in tracker.provider_tracker.providers.values()]) if probed else []

    # Show the provider table inline
    print("\n--- Provider Status ---")
    print(f"{'Name':<14} {'Status':<12} {'Detail'}")
    print("-" * 60)
    for row in tracker.provider_status_rows():
        print(f"{row['name']:<14} [{row['status']:<8}] {row['description'][:50]}")

    if tools:
        print("\n--- Top Tools Discovered ---")
        for t in tools[:10]:
            print(f"{t['headline']}\n     {t['summary'][:80]}")
            print(f"     {t['url']}")

    # Status-change notification (minimalist)
    if changes:
        msg = "\n".join("  • " + c["headline"] for c in changes[:8])
        try:
            await tracker.notifier.send(f"⚡ EliLain status change:\n{msg}")
        except Exception as e:
            logger.warning(f"Status-change notification failed: {e}")

    return {
        "providers_loaded": count,
        "providers_probed": probed,
        "tools_found": len(tools),
        "report": str(report_path),
        "status_changes": len(changes),
    }


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Eli-Lain AI Tracker — v2")
    parser.add_argument(
            "action",
            choices=["run", "report", "list", "today", "health", "summary",
                      "providers", "probe", "discover", "audit", "export-public", "publish", "daily", "dashboard"],
            help="Action to perform",
        )
    parser.add_argument("--theme", choices=["default", "lain"], default="lain", help="Report theme")
    parser.add_argument("--days", type=int, default=7, help="Days to look back")
    parser.add_argument("--date", help="Specific date for report (YYYY-MM-DD)")
    parser.add_argument("--data-dir", default="./data")
    parser.add_argument("--reports-dir", default="./reports")
    parser.add_argument("--freellmapi-db", default=None, help="Optional local database; defaults to FREELLMAPI_DB_PATH or data-dir/freeapi.db")
    parser.add_argument("--freellmapi-url", default=None, help="Optional service URL; defaults to FREELLMAPI_URL or loopback port 3001")
    parser.add_argument("--public-dir", default="./public",
                        help="Directory holding the sanitized public index.html export")
    parser.add_argument("--no-export", action="store_true",
                        help="Skip rebuilding the sanitized export before publishing")
    parser.add_argument("--enable-ai-summary", action="store_true",
                        help="Explicitly enable configured AI summaries; may send observed news to an API")

    args = parser.parse_args(argv)
    summarizer_config = ({"api_key": os.getenv("SUMMARIZER_API_KEY"),
                          "model": os.getenv("SUMMARIZER_MODEL"),
                          "base_url": os.getenv("SUMMARIZER_API_URL")}
                         if args.enable_ai_summary else None)
    if summarizer_config is not None and "SUMMARIZER_PROVIDER" in os.environ:
        summarizer_config["provider"] = os.environ["SUMMARIZER_PROVIDER"]

    if summarizer_config is not None:
        try:
            validate_config(summarizer_config, require_explicit=True)
        except ValueError as error:
            # Validator messages contain setting names only, never private values.
            parser.error(str(error))

    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")

    tracker = NewsTracker(
        data_dir=args.data_dir,
        reports_dir=args.reports_dir,
        theme=args.theme,
        freellmapi_db_path=args.freellmapi_db,
        freellmapi_url=args.freellmapi_url,
        summarizer_config=summarizer_config,
    )

    if args.action == "run":
        result = asyncio.run(tracker.run())
        print(f"Run complete: {result['total_fetched']} fetched, {result['regular']} regular")

    elif args.action == "report":
        date = None
        if args.date:
            date = datetime.strptime(args.date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        tracker.load_providers()
        html = tracker.generate_report(date=date, include_providers=True, include_news=True)
        print(f"Report generated ({len(html)} bytes)")

    elif args.action == "list":
        items = tracker.list_recent(days=args.days)
        print(f"\nRecent news ({len(items)}):")
        for item in items:
            print(f"[{item.get('source','?')}] {item.get('title','')}")
            if item.get("summary"):
                print(f"  {item['summary'][:100]}")
            print()

    elif args.action == "today":
        try:
            result = asyncio.run(tracker.run())
            tracker.load_providers()
            tracker.generate_report()
            print(f"Tracker complete: {result['relevant_count']} relevant items")
        except Exception as e:
            import traceback
            traceback.print_exc()
            exit(1)

    elif args.action == "health":
        table = tracker.get_health_table()
        print(f"\n{'Source':<20} {'Status':<12} {'Fails':<8} {'Items':<8}")
        print("-" * 50)
        for row in table:
            print(f"{row['source']:<20} {row['status']:<12} {row['consecutive_failures']:<8} {row['total_items']:<8}")

    elif args.action == "summary":
        summary = tracker.get_weekly_summary(weeks=1)
        print(f"\nWeekly ({summary['total_items']} items)")
        for cat in summary["top_categories"]:
            print(f"  {cat['category']}: {cat['count']}")

    # ── NEW: Provider tracking actions ─────────────────────────────────

    elif args.action == "providers":
        count = tracker.load_providers()
        print(f"\nLoaded {count} providers from freellmapi DB\n")
        print(f"{'Name':<16} {'Current status':<14} {'Freshness':<12} {'Detail'}")
        print("-" * 80)
        for row in tracker.provider_status_rows():
            print(f"{row['name']:<16} [{row['actual_status']:<10}] {row['freshness']:<12} {row['description'][:100]}")
        print("\n--- Next actions ---")
        for action in provider_actions([p.to_dict() for p in tracker.provider_tracker.providers.values()]):
            print(f"[{action['priority']}] {action['title']}: {action['action']}")

    elif args.action == "probe":
        tracker.load_providers()
        probed = asyncio.run(tracker.probe_providers())
        print(f"\nProbed {probed} providers\n")
        for p in tracker.provider_tracker.providers.values():
            if p.status != ProviderStatus.UNKNOWN:
                icon = "✓" if p.status == ProviderStatus.LIVE else "✗"
                print(f"  {icon} {p.name:<14} [{p.status.value}] {p.status_reason}")
        tracker.generate_report(include_providers=True, include_news=False)
        print(f"\nHTML report generated.")

    elif args.action == "discover":
        tools = asyncio.run(tracker.discover_tools())
        print(f"\nObserved {len(tools)} model/provider/skill/repository discoveries (see source coverage in report)\n")
        for t in tools:
            print(f"  ⭐ {t['headline']}")
            print(f"     {t['summary'][:90]}")
            print(f"     {t['url']}\n")

    elif args.action == "audit":
        asyncio.run(async_full_audit(tracker))

    elif args.action == "export-public":
        # Saved internal inputs only: no fetching, no deployment, no notification.
        import hashlib
        from intel_sources.public_report import export_public_report
        try:
            artifact = export_public_report(Path(args.data_dir) / "intel_state.json",
                                            Path(args.data_dir) / "news.db", args.public_dir)
        except (OSError, ValueError, TypeError):
            print("export-public failed: saved inputs or export directory were rejected", file=sys.stderr)
            return 1
        data = artifact.read_bytes()
        print(json.dumps({"artifact": str(artifact), "sha256": hashlib.sha256(data).hexdigest(),
                          "bytes": len(data)}, sort_keys=True))
        return 0

    elif args.action == "publish":
        # Hosting and both notification channels; exits non-zero unless fully verified.
        if not args.no_export:
            if main(["export-public", "--data-dir", args.data_dir,
                     "--public-dir", args.public_dir, "--theme", args.theme]):
                return 1
        from intel_sources.report_delivery import (ATTACHMENT_NAME, DeliveryError, deliver_report)
        try:
            result = deliver_report(args.public_dir,
                                    state_path=Path(args.data_dir) / "delivery_receipts.json")
        except DeliveryError as error:
            # Report the safe code only; never echo tokens, hosts internals or paths.
            print(json.dumps({"delivered": False, "code": error.code}, sort_keys=True), file=sys.stderr)
            return 1
        print(json.dumps({
            "delivered": result.success,
            "url": result.hosted.url,
            "sha256": result.hosted.sha256,
            "attachment": ATTACHMENT_NAME,
            "telegram": result.telegram.confirmation,
            "discord": result.discord.confirmation,
        }, sort_keys=True))
        return 0 if result.success else 1

    elif args.action == "daily":
        # Run the new daily intel scan path with explicit public-dir
        from intel_sources.daily_automation import run_daily
        result = asyncio.run(run_daily(settings_path=None, data_dir=args.data_dir, public_dir=args.public_dir))
        print(f"daily ok report_date={result['report_date']} public={result['public_path']} selected={result['counts'].get('selected',0)}")
        return 0

    elif args.action == "dashboard":
        # Minimal dashboard placeholder; real implementation is deferred but route must exist
        print("dashboard route registered")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
