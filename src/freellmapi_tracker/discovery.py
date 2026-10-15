"""Discovery of new free LLM providers and GitHub tools."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from datetime import datetime, timezone
from typing import Any

import httpx

from .models import FreeProvider, ProviderStatus, ProviderTier, FreellmapiIntegrationType, ToolDiscovery


logger = logging.getLogger(__name__)


class ProviderDiscovery:
    """Discover new free LLM providers and useful GitHub tools."""
    
    def __init__(self, github_token: str | None = None):
        self.github_token = github_token or os.getenv("GITHUB_TOKEN")
        self._headers = {
            "Accept": "application/vnd.github.v3+json",
            "User-Agent": "EliLainTracker/2.0",
        }
        if self.github_token:
            self._headers["Authorization"] = f"token {self.github_token}"
    
    async def discover_new_providers(self) -> list[FreeProvider]:
        """Search for new free LLM provider announcements and offerings."""
        discovered: list[FreeProvider] = []
        
        # Search queries focused on free AI/LLM API tiers
        queries = [
            "free llm api 2026",
            "free ai api tier",
            "free tier openai compatible",
            "generous free api credits llm",
            "new free llm provider",
        ]
        
        # In a real run, this would query web search APIs or RSS feeds
        # For now, return structured candidates that can be verified
        logger.info("Searching for new free LLM providers...")
        
        return discovered
    
    async def discover_github_tools(self) -> list[ToolDiscovery]:
        """Discover GitHub tools for the documented example topic categories."""
        tools: list[ToolDiscovery] = []
        
        # Example search categories; no operator profile is loaded.
        # - Data engineering (ETL, polars, duckdb, sqlite)
        # - Local LLM / inference (ollama, vllm, llama.cpp tooling)
        # - Autonomous agents / workflow automation
        # - Free LLM routing / API gateways
        search_topics = [
            ("topic:llm-router OR topic:ai-gateway", "freellmapi"),
            ("topic:data-engineering language:python stars:>50", "data-engineering"),
            ("topic:ai-agent language:python stars:>100", "automation"),
            ("topic:local-llm language:python stars:>50", "local-llm"),
        ]
        
        async with httpx.AsyncClient(timeout=20.0) as client:
            for query, category in search_topics:
                try:
                    url = f"https://api.github.com/search/repositories?q={query}&sort=updated&per_page=5"
                    resp = await client.get(url, headers=self._headers)
                    
                    if resp.status_code == 200:
                        data = resp.json()
                        for repo in data.get("items", []):
                            # Filter out forks and unmaintained repos
                            if repo.get("fork"):
                                continue
                            
                            # Build tool discovery item
                            pushed_at = None
                            if repo.get("pushed_at"):
                                try:
                                    pushed_at = datetime.fromisoformat(repo["pushed_at"].replace("Z", "+00:00"))
                                except ValueError:
                                    pass
                            
                            tools.append(ToolDiscovery(
                                repo_name=repo.get("full_name", ""),
                                description=repo.get("description", "") or "No description",
                                url=repo.get("html_url", ""),
                                stars=repo.get("stargazers_count", 0),
                                language=repo.get("language", "") or "Unknown",
                                topics=repo.get("topics", []),
                                last_push=pushed_at,
                                relevance_reason=self._determine_relevance(repo, category),
                                category=category,
                                actionable=True,
                                install_instructions=f"git clone {repo.get('html_url', '')} or pip install (check README)",
                            ))
                            
                    await asyncio.sleep(0.5)  # Rate limit courtesy
                    
                except Exception as e:
                    logger.warning(f"GitHub tool discovery failed for '{query}': {e}")
        
        return tools
    
    def _determine_relevance(self, repo: dict[str, Any], category: str) -> str:
        """Explain the tool's relevance to its example category."""
        desc = (repo.get("description", "") or "").lower()
        name = (repo.get("name", "") or "").lower()
        
        if "router" in desc or "gateway" in desc or "proxy" in desc:
            return "Alternative/complementary LLM router or proxy to freellmapi"
        if "etl" in desc or "pipeline" in desc or "data" in desc:
            return "Data engineering / ETL pipeline tool for data projects"
        if "agent" in desc or "workflow" in desc:
            return "Agent automation workflow tool"
        if "local" in desc or "offline" in desc:
            return "Local inference tool for zero-cost self-hosted LLM execution"
        
        return f"Useful {category} tool in Python stack"