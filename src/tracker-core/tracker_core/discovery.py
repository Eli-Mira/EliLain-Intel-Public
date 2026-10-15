"""Discovery engine for finding new LLM API providers.

Instead of only watching the seed list, run periodic sweeps that look for
providers we don't have yet. New names go into pending_review queue.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from tracker_core.schema import Provider, VerificationStatus
from tracker_core.pipeline.fetcher import HTTPFetcher


logger = logging.getLogger(__name__)


class DiscoveryEngine:
    """Discover new LLM API providers from multiple sources."""

    def __init__(
        self,
        existing_providers: list[Provider] | None = None,
        cache_dir: Path | str = "./.cache",
        config: dict[str, Any] | None = None
    ):
        self.existing_providers = existing_providers or []
        self.cache_dir = Path(cache_dir)
        self.config = config or {}
        self.fetcher = HTTPFetcher(
            timeout=30.0,
            rate_limit_delay=2.0,
            user_agent="TrackerCore/1.0 (+https://github.com/tracker-core)"
        )
        self._discovered: list[dict[str, Any]] = []

    def get_existing_names(self) -> set[str]:
        """Get set of provider names we already track."""
        return {p.name.lower() for p in self.existing_providers}

    async def discover_all(self) -> list[dict[str, Any]]:
        """Run all discovery sources and return new candidates."""
        self._discovered = []

        # Run discoveries in parallel
        await asyncio.gather(
            self._discover_from_openrouter(),
            self._discover_from_huggingface(),
            self._discover_from_github(),
            self._discover_from_artificial_analysis(),
        )

        # Filter out already-known providers
        existing = self.get_existing_names()
        new_candidates = [
            c for c in self._discovered
            if c.get("name", "").lower() not in existing
        ]

        logger.info(f"Discovery complete: {len(new_candidates)} new candidates")
        return new_candidates

    async def _discover_from_openrouter(self) -> None:
        """Discover providers from OpenRouter's model list."""
        try:
            url = "https://openrouter.ai/api/v1/models"
            result = await self.fetcher.fetch(url)

            if result.success and result.status_code == 200:
                data = json.loads(result.content)
                for model in data.get("data", []):
                    provider = model.get("id", "").split("/")[0] if "/" in model.get("id", "") else model.get("id", "")
                    if provider and len(provider) > 1:
                        self._discovered.append({
                            "name": provider.title(),
                            "source": "openrouter",
                            "url": "https://openrouter.ai",
                            "confidence": 0.6,
                            "reason": f"Found on OpenRouter as provider for {model.get('id')}"
                        })
        except Exception as e:
            logger.warning(f"OpenRouter discovery failed: {e}")

    async def _discover_from_huggingface(self) -> None:
        """Discover from Hugging Face new models API."""
        try:
            # Hugging Face API for new models
            url = "https://huggingface.co/api/models?limit=50&sort=created_at&direction=-1"
            result = await self.fetcher.fetch(url)

            if result.success and result.status_code == 200:
                models = json.loads(result.content)
                ai_keywords = ["llm", "gpt", "claude", "gemini", "llama", "mistral", "ai", "chat"]

                for model in models:
                    model_id = model.get("id", "")
                    model_name = model.get("modelId", "")

                    # Check if it's an AI/LLM model with API endpoint
                    text = f"{model_id} {model_name} {str(model.get('tags', []))}".lower()
                    if any(kw in text for kw in ai_keywords):
                        # Extract potential provider from org
                        parts = model_id.split("/")
                        if len(parts) >= 2:
                            org = parts[0]
                            # Skip if it's a known HF org, not a provider
                            if org not in ["HuggingFaceH4", "facebook", "google", "microsoft"]:
                                self._discovered.append({
                                    "name": org.title(),
                                    "source": "huggingface",
                                    "url": f"https://huggingface.co/{model_id}",
                                    "confidence": 0.4,
                                    "reason": f"AI model discovered: {model_id}"
                                })
        except Exception as e:
            logger.warning(f"HuggingFace discovery failed: {e}")

    async def _discover_from_github(self) -> None:
        """Discover from GitHub repos with 'free llm api' in name/README."""
        try:
            queries = [
                "free llm api",
                "free gpt api",
                "free claude api",
                "free ai api",
                "llm api wrapper"
            ]

            for query in queries:
                url = f"https://api.github.com/search/repositories?q={query}&sort=updated&order=desc&per_page=10"
                result = await self.fetcher.fetch(url)

                if result.success and result.status_code == 200:
                    data = json.loads(result.content)
                    for repo in data.get("items", []):
                        owner = repo.get("owner", {}).get("login", "")
                        if owner and owner not in ["openai", "anthropic", "google", "meta-llama"]:
                            self._discovered.append({
                                "name": owner.title(),
                                "source": "github",
                                "url": repo.get("html_url", ""),
                                "confidence": 0.3,
                                "reason": f"GitHub repo: {repo.get('full_name')}"
                            })
        except Exception as e:
            logger.warning(f"GitHub discovery failed: {e}")

    async def _discover_from_artificial_analysis(self) -> None:
        """Discover from Artificial Analysis leaderboard."""
        try:
            url = "https://artificialanalysis.ai/models"
            result = await self.fetcher.fetch(url)

            if result.success and result.status_code == 200:
                # Parse HTML to extract provider names
                import re
                # Look for provider names in the page
                providers = re.findall(r'provider["\s:]+([A-Za-z0-9_-]+)', result.content, re.I)
                for provider in providers[:20]:
                    if len(provider) > 2:
                        self._discovered.append({
                            "name": provider.title(),
                            "source": "artificial_analysis",
                            "url": "https://artificialanalysis.ai",
                            "confidence": 0.5,
                            "reason": "Found on Artificial Analysis leaderboard"
                        })
        except Exception as e:
            logger.warning(f"Artificial Analysis discovery failed: {e}")

    def get_candidates(self) -> list[dict[str, Any]]:
        """Get discovered candidates."""
        return self._discovered

    def save_candidates(self, output_path: Path | str) -> None:
        """Save discovered candidates to JSON."""
        path = Path(output_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self._discovered, indent=2, default=str))
        logger.info(f"Saved {len(self._discovered)} candidates to {path}")


def run_discovery(
    providers: list[Provider],
    output_path: Path | str | None = None
) -> list[dict[str, Any]]:
    """
    Run discovery engine and return new provider candidates.

    Args:
        providers: Existing providers to exclude
        output_path: Optional path to save candidates

    Returns:
        List of new provider candidates
    """
    import asyncio

    engine = DiscoveryEngine(existing_providers=providers)
    candidates = asyncio.run(engine.discover_all())

    if output_path:
        engine.save_candidates(output_path)

    return candidates

# --- Search Saturation Tracking ---

def _track_saturation(self, new_candidates: list[dict], previous_count: int | None = None) -> dict[str, any]:
    """Track search saturation metrics after a discovery round.

    Returns a dict with saturation metrics for the final report.
    """
    from collections import Counter

    metrics: dict[str, any] = {
        'new_unique_high_quality': 0,
        'new_domains': 0,
        'ranking_stability': 'UNKNOWN',
        'top_k_changes': 0,
        'feature_coverage_improvement': 0.0,
        'source_diversity': 0,
        'confidence_improvement': 0.0,
        'meaningful_evidence_new': 0,
        'score_changes': 0,
        'new_providers': 0,
        'top5_material_change': False,
    }

    if not previous_count and not hasattr(self, '_saturation_history'):
        metrics['status'] = 'initial'
        return metrics

    if previous_count is None:
        previous_count = getattr(self, '_last_candidate_count', 0)

    current_count = len(new_candidates) if new_candidates else 0
    new_count = current_count - previous_count if previous_count else 0

    if new_count > 0:
        metrics['new_unique_high_quality'] = sum(1 for c in new_candidates if c.get('score', 0) >= 70)
        metrics['new_domains'] = len(set(c.get('source_id', '') for c in new_candidates))
        metrics['source_diversity'] = len(set(c.get('source_id', '') for c in new_candidates))

    metrics['meaningful_evidence_new'] = sum(1 for c in new_candidates if c.get('evidence_url'))
    metrics['score_changes'] = new_count

    # Track new providers contributing unique results
    if hasattr(self, '_saturation_history'):
        for prev in self._saturation_history:
            all_providers.update(prev.get('providers', []))
    current_providers = set()
    for c in new_candidates:
        current_providers.add(c.get('source_id', ''))
    new_provider_set = current_providers - all_providers
    metrics['new_providers'] = len(new_provider_set)

    # Update history
    if not hasattr(self, '_saturation_history'):
        self._saturation_history = []
    self._saturation_history.append({
        'candidate_count': current_count,
        'providers': list(current_providers),
        'new_high_quality': metrics['new_unique_high_quality'],
    })
    self._saturation_history = self._saturation_history[-5:]

    # Ranking stability
    if hasattr(self, '_last_top_candidates') and previous_count is not None:
        last_top = set(self._last_top_candidates)
        curr_top = set(c.get('name', '')[:50] for c in new_candidates[:5])
        if last_top or curr_top:
            intersection = len(last_top & curr_top)
            metrics['top_k_changes'] = 5 - intersection if last_top and curr_top else 0
            metrics['ranking_stability'] = 'HIGH' if metrics['top_k_changes'] <= 1 else 'MODERATE' if metrics['top_k_changes'] <= 3 else 'LOW'

    # Confidence improvement
    if current_count > 0:
        metrics['confidence_improvement'] = metrics['new_unique_high_quality'] / current_count
    else:
        metrics['confidence_improvement'] = 0.0

    # Top-K material change
    if previous_count is not None and new_count != previous_count:
        metrics['top5_material_change'] = abs(new_count - previous_count) > 2

    # Store current state for next round
    self._last_candidate_count = current_count
    self._last_top_candidates = [c.get('name', '')[:50] for c in new_candidates[:5]]

    metrics['status'] = 'complete'
    return metrics


def _get_saturation_report(self) -> str:
    """Generate a human-readable saturation report."""
    if not hasattr(self, '_saturation_history') or not self._saturation_history:
        return 'No saturation data — first run or not tracked.\n'

    lines = [
        'Search Saturation Report',
        '=' * 25,
        f'Total rounds tracked: {len(self._saturation_history)}',
        '',
    ]
    for i, entry in enumerate(self._saturation_history):
        lines.append(
            f'Round {i+1}: {entry["candidate_count"]} candidates, '
            f'{entry["new_high_quality"]} high-quality, '
            f'providers: {" ".join(entry["providers"])}'
        )
    lines.extend([
        '',
        'Metrics: new_unique_high_quality, new_domains, source_diversity, ',
        "       confidence_improvement, top_k_changes, top5_material_change",
        f"       'status': '{self._saturation_history[-1].get('status', 'complete')}'",
        '',
    ])
    return '\n'.join(lines)
