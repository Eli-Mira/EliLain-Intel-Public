"""Summarization module for news items."""

from __future__ import annotations

import json
import logging
import ipaddress
import re
from urllib.parse import urlsplit
from datetime import datetime, timezone
from typing import Any

import httpx

from tracker_core.schema import NewsItem


logger = logging.getLogger(__name__)

DEFAULT_API_URL = "http://localhost:3001/v1/chat/completions"

# Enough headroom for a reasoning model to think and still return the JSON array.
MAX_COMPLETION_TOKENS = 4096
DEFAULT_COMPLETION_TOKENS = 1000
MAX_RESPONSE_BYTES = 65536
MAX_CONTENT_CHARS = 32768
MAX_SUMMARY_CHARS = 2000
MAX_DEDUP_CHARS = 128


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON field")
        result[key] = value
    return result


def _bounded_json(text: str | bytes) -> Any:
    if len(text) > MAX_RESPONSE_BYTES:
        raise ValueError("oversized response")
    return json.loads(text, object_pairs_hook=_unique_object)

REASONING_MARKERS = ("minicpm", "deepseek", "qwq", "r1", "thinking", "reason")

PROVIDERS = ("openai-compatible", "openai", "anthropic")
_LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}


def _is_loopback(url: str) -> bool:
    """Only unambiguous loopback literals qualify for keyless inference."""
    try:
        return urlsplit(url).hostname in _LOOPBACK_HOSTS
    except (ValueError, TypeError):
        return False


def validate_config(config: dict[str, Any], *, require_explicit: bool = False) -> None:
    """Validate private settings without repairing or including their values in errors."""
    provider = config.get("provider", "openai-compatible")
    if provider not in PROVIDERS:
        raise ValueError("SUMMARIZER_PROVIDER must be openai-compatible, openai or anthropic")
    model = config.get("model", None if require_explicit else "free-tier")
    if (not isinstance(model, str) or not 1 <= len(model) <= 256
            or any(ord(c) < 33 or ord(c) > 126 for c in model)):
        raise ValueError("SUMMARIZER_MODEL must be a nonempty literal ASCII model identifier")
    endpoint = config.get("base_url", None if require_explicit else DEFAULT_API_URL)
    try:
        if (not isinstance(endpoint, str) or not 1 <= len(endpoint) <= 2048
                or any(ord(c) < 33 or ord(c) > 126 for c in endpoint)
                or any(c in endpoint for c in "\\?#%")):
            raise ValueError
        parsed = urlsplit(endpoint)
        host = parsed.hostname
        if (not host or parsed.username is not None or parsed.password is not None
                or parsed.netloc.endswith(":")
                or (parsed.port is not None and not 1 <= parsed.port <= 65535)
                or not (parsed.scheme == "https" or parsed.scheme == "http" and _is_loopback(endpoint))):
            raise ValueError
        if (not endpoint.startswith(parsed.scheme + "://") or parsed.netloc != parsed.netloc.lower()
                or not re.fullmatch(r"/[A-Za-z0-9._~!$&'()*+,;=:@/-]*", parsed.path)
                or any(part in {".", ".."} for part in parsed.path.split("/"))
                or (parsed.port is not None and parsed.netloc.rsplit(":", 1)[1] != str(parsed.port))):
            raise ValueError
        # Reject shorthand/numeric addresses rather than normalizing them into IPs.
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            if (re.fullmatch(r"[0-9.]+", host) or host.startswith("0x")
                    or len(host) > 253 or not all(re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", label)
                                                for label in host.split("."))):
                raise ValueError
        else:
            if address.is_unspecified:
                raise ValueError
    except (ValueError, TypeError):
        raise ValueError("SUMMARIZER_API_URL must be literal HTTPS (or loopback HTTP), without credentials, query or fragment") from None
    key = config.get("api_key")
    if key is not None and key != "" and (not isinstance(key, str) or len(key) > 4096
                                          or any(ord(c) < 33 or ord(c) > 126 for c in key)):
        raise ValueError("SUMMARIZER_API_KEY must be a private literal ASCII credential without whitespace")
    if not key and (provider != "openai-compatible" or not _is_loopback(endpoint)):
        raise ValueError("SUMMARIZER_API_KEY is required except for loopback openai-compatible inference")
    if "max_tokens" in config and (type(config["max_tokens"]) is not int
                                   or not 1 <= config["max_tokens"] <= MAX_COMPLETION_TOKENS):
        raise ValueError("AI summary max_tokens must be an integer from 1 to 4096")



class Summarizer:
    """Generate summaries for news items using LLM."""
    
    def __init__(self, config: dict[str, Any] | None = None):
        """
        Args:
            config: Configuration dict with keys:
                - provider: openai-compatible (default), openai, or anthropic
                - model: Literal API model identifier (legacy default: "free-tier")
                - api_key: Private credential; never read implicitly from env
                - base_url: Complete request URL (legacy default: local router)
                - max_tokens: Integer output ceiling, 1-4096
        """
        self.config = config or {}
        self.provider = self.config.get("provider", "openai-compatible")
        self.model = self.config.get("model", "free-tier")
        self.api_key = self.config.get("api_key")
        self.base_url = self.config.get("base_url")
        # A keyless endpoint must be unambiguous loopback, and only the
        # compatible adapter permits it. A local router can still forward to
        # paid upstreams: its listen address alone is not free/local proof.
        self.endpoint = self.base_url or DEFAULT_API_URL
        self.keyless_endpoint = _is_loopback(self.endpoint)
        # Reasoning-style local models need a larger completion budget and reject
        # an explicit low temperature.
        self.model_reasoning = any(
            marker in str(self.model).lower() for marker in REASONING_MARKERS)
        self.max_tokens = self.config.get(
            "max_tokens", MAX_COMPLETION_TOKENS if self.model_reasoning else DEFAULT_COMPLETION_TOKENS)
    
    async def summarize_items(
        self,
        items: list[NewsItem],
        batch_size: int = 5
    ) -> list[NewsItem]:
        """
        Generate summaries for news items in batches.
        
        Args:
            items: List of NewsItem to summarize
            batch_size: Number of items to summarize per API call
            
        Returns:
            List of NewsItem with summaries filled in
        """
        if type(batch_size) is not int or not 1 <= batch_size <= 5:
            raise ValueError("AI summary batch_size must be an integer from 1 to 5")
        for i in range(0, len(items), batch_size):
            batch = items[i:i + batch_size]
            
            # Build prompt
            prompt = self._build_prompt(batch)
            
            # Call summarization API
            summary = await self._call_llm(prompt)
            
            # Validate the complete batch before mutating any observed item.
            # Raw lines, partial arrays and wrong types are not summaries.
            try:
                if (not isinstance(summary, str) or len(summary) > MAX_CONTENT_CHARS
                        or any(value and value in summary for value in (self.api_key, self.endpoint))):
                    raise ValueError("invalid private response")
                summaries = _bounded_json(summary)
                if not isinstance(summaries, list) or len(summaries) != len(batch):
                    raise ValueError("invalid batch")
                for row in summaries:
                    if not isinstance(row, dict) or any(
                        not isinstance(row.get(key), str) or not row[key].strip()
                        or len(row[key]) > bound
                        or any(value and value in row[key] for value in (self.api_key, self.endpoint))
                        for key, bound in (("summary", MAX_SUMMARY_CHARS), ("dedup_key", MAX_DEDUP_CHARS))
                    ):
                        raise ValueError("invalid summary")
            except (ValueError, TypeError, RecursionError):
                raise RuntimeError("AI summarization response invalid") from None
            for item, row in zip(batch, summaries):
                item.summary = row["summary"].strip()
                item.dedup_key = row["dedup_key"].strip()

            logger.info(f"Summarized items {i+1}-{min(i+batch_size, len(items))}/{len(items)}")
        
        return items
    
    def _build_prompt(self, items: list[NewsItem]) -> str:
        """Build prompt for summarization."""
        item_texts = []
        for item in items:
            text = f"- {item.title[:1000]}"
            if item.summary:
                text += f"\n  Context: {item.summary[:200]}"
            if item.category:
                text += f"\n  Category: {item.category.value}"
            item_texts.append(text)
        
        prompt = f"""You are a news aggregator. Summarize each item in 1-2 sentences maximum.

Items to summarize:
{chr(10).join(item_texts)}

Return JSON array with objects containing:
- "summary": brief 1-2 sentence summary
- "dedup_key": short key for deduplication (first 30 chars of title, lowercased)

Example output:
[{{"summary": "OpenAI released GPT-4o, a new multimodal model...", "dedup_key": "openai releases gpt-4o"}}, ...]

Only return valid JSON, no markdown formatting."""
        
        return prompt
    
    async def _call_llm(self, prompt: str) -> str:
        """Call LLM API for summarization."""
        # Defaults to the local router; both it and Ollama are keyless.
        api_url = self.endpoint
        
        headers = {
            "Content-Type": "application/json"
        }
        if self.provider == "anthropic":
            headers["x-api-key"] = self.api_key
            headers["anthropic-version"] = "2023-06-01"
        elif self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        
        payload = {
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
            # Existing local reasoning models may use the entire output budget
            # before returning JSON. More headroom does not guarantee success or
            # establish the cost/routing policy of a caller-selected endpoint.
            "max_tokens": self.max_tokens,
        }
        # Reasoning models reject an explicit temperature of 0.3 on some
        # backends; only send it when it is meaningful.
        if self.provider == "openai":
            payload["max_completion_tokens"] = payload.pop("max_tokens")
        elif self.provider == "openai-compatible" and not self.model_reasoning:
            payload["temperature"] = 0.3
        
        try:
            validate_config(self.config)
            # HTTPX INFO and HTTPCore DEBUG include private URLs/headers. Keep
            # those library traces disabled, including in programmatic consumers.
            logging.getLogger("httpx").setLevel(logging.WARNING)
            logging.getLogger("httpcore").setLevel(logging.WARNING)
            async with httpx.AsyncClient(timeout=30.0, follow_redirects=False, trust_env=False) as client:
                async with client.stream("POST", api_url, json=payload, headers=headers) as response:
                    response.raise_for_status()
                    body = bytearray()
                    async for chunk in response.aiter_bytes():
                        if len(body) + len(chunk) > MAX_RESPONSE_BYTES:
                            raise ValueError("oversized response")
                        body.extend(chunk)
                    data = _bounded_json(bytes(body))
                if self.provider == "anthropic":
                    if data.get("stop_reason") != "end_turn":
                        raise ValueError("incomplete response")
                    blocks = data["content"]
                    if not blocks or any(block.get("type") != "text" for block in blocks):
                        raise ValueError("invalid response content")
                    content = "".join(block["text"] for block in blocks)
                    if not content.strip():
                        raise ValueError("invalid response content")
                    return content
                choice = data["choices"][0]
                if choice.get("finish_reason", "stop") != "stop" or choice["message"].get("refusal"):
                    raise ValueError("incomplete response")
                content = choice["message"]["content"]
                if not isinstance(content, str) or not content.strip():
                    raise ValueError("invalid response content")
                return content
        except Exception as e:
            logger.error("Summarization unavailable (%s); preserving observed news", type(e).__name__)
            raise RuntimeError("AI summarization unavailable") from None
    
    def summarize_manually(self, items: list[NewsItem]) -> list[NewsItem]:
        """Fallback: generate simple summaries without LLM."""
        for item in items:
            if not item.summary:
                item.summary = "Title excerpt (not AI summary): " + item.title
        return items


async def summarize_news(
    items: list[NewsItem],
    config: dict[str, Any] | None = None
) -> list[NewsItem]:
    """
    Main function to summarize news items.
    
    Args:
        items: List of NewsItem to summarize
        config: Optional configuration
        
    Returns:
        List of NewsItem with summaries
    """
    summarizer = Summarizer(config)
    # Opt-in stays opt-in. An EMPTY config means the caller never enabled AI
    # summaries (the CLI default, and every non-opt-in path), and must make zero
    # requests - that guarantee is load-bearing, so a reachable default endpoint
    # must not switch it on by itself.
    #
    # "The endpoint needs no credential" and "we are allowed to call it" are two
    # different questions. Answering the first must not silently answer the second.
    if not config:
        return summarizer.summarize_manually(items)
    # Within an explicitly enabled setup, a keyless LOCAL endpoint is legitimate
    # (Ollama/FreeLLMAPI need no credential). A keyless REMOTE endpoint is not,
    # because that would be an uncredentialed call to a third party.
    if not summarizer.api_key and not summarizer.keyless_endpoint:
        return summarizer.summarize_manually(items)
    
    # Try LLM summarization first
    try:
        items = await summarizer.summarize_items(items)
    except Exception as e:
        logger.warning("LLM summarization failed (%s); using observed excerpts", type(e).__name__)
        items = summarizer.summarize_manually(items)
    
    return items
