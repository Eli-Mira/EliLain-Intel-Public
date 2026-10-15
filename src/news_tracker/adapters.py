"""Source adapters for the news tracker."""

from __future__ import annotations

import asyncio
import json
import hashlib
import logging
import os
import time
from email.utils import parsedate_to_datetime
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote

from dotenv import load_dotenv
load_dotenv()  # Load .env from cwd

import httpx

from tracker_core.schema import NewsItem, SourceType, Category
from tracker_core.pipeline.fetcher import HTTPFetcher, FetchResult
from news_tracker.health import SourceHealthTracker


logger = logging.getLogger(__name__)
_DEFAULT_FEED_HEALTH = SourceHealthTracker()


class _FeedFailure(Exception):
    """A safe classification, never a URL or upstream exception message."""
    def __init__(self, code: str, retry_after_seconds: float | None = None):
        super().__init__(code)
        self.retry_after_seconds = retry_after_seconds


class _FeedHTTPFetcher(HTTPFetcher):
    """Return HTTP errors immediately; feed-level policy owns cooldown/retries."""
    async def fetch(self, url: str, headers=None, params=None, **kwargs) -> FetchResult:
        started = time.monotonic()
        domain = url.split("/")[2]
        if not hasattr(self, "_host_locks"):
            self._host_locks = {}
        lock = self._host_locks.setdefault(domain, asyncio.Lock())
        async with lock:
            await self._maybe_throttle(domain)
            self._last_request_time[domain] = time.monotonic()
        try:
            async with httpx.AsyncClient(timeout=self.timeout, max_redirects=5) as client:
                async with client.stream("GET", url, headers={"User-Agent": self.user_agent, **(headers or {})}, params=params, follow_redirects=True) as response:
                    body = bytearray()
                    async for chunk in response.aiter_bytes():
                        if len(body) + len(chunk) > getattr(self, "max_response_bytes", 1048576):
                            raise _FeedFailure("response_too_large")
                        body.extend(chunk)
                    content = bytes(body).decode(response.encoding or "utf-8", errors="replace")
            self._last_request_time[domain] = time.monotonic()
            return FetchResult(content, dict(response.headers), response.status_code, str(response.url), (time.monotonic() - started) * 1000, response.status_code == 200)
        except httpx.HTTPError as exc:
            return FetchResult("", {}, 0, url, (time.monotonic() - started) * 1000, False, type(exc).__name__)



class BaseNewsAdapter(ABC):
    """Base adapter for news sources."""
    
    name: str = "base"
    source_type: SourceType = SourceType.GITHUB
    
    def __init__(self, config: dict[str, Any] | None = None):
        self.config = config or {}
        if "feed_health" in self.config:
            self.feed_health = self.config["feed_health"]
        elif self.config.get("feed_health_state_path"):
            self.feed_health = SourceHealthTracker(state_path=self.config["feed_health_state_path"])
            self.feed_health.load_state()
        else:
            self.feed_health = _DEFAULT_FEED_HEALTH
        self.feed_timeout_seconds = float(self.config.get("feed_timeout_seconds", 5.0))
        self.feed_budget_seconds = float(self.config.get("feed_budget_seconds", 35.0))
        self.feed_concurrency = max(1, int(self.config.get("feed_concurrency", 4)))
        self.feed_results: dict[str, dict[str, Any]] = {}
        self.fetcher = _FeedHTTPFetcher(
            timeout=self.config.get("timeout", 30.0),
            rate_limit_delay=self.config.get("rate_limit_delay", 1.0)
        )
    
    @abstractmethod
    async def fetch(self) -> list[dict[str, Any]]:
        """Fetch raw items from source."""
        ...
    
    @abstractmethod
    def parse_item(self, raw: dict[str, Any]) -> NewsItem:
        """Parse a single raw item into NewsItem."""
        ...
    
    async def fetch_all(self) -> list[NewsItem]:
        """Fetch and parse all items from source."""
        raw_items = await self.fetch()
        items = []
        for raw in raw_items:
            try:
                item = self.parse_item(raw)
                items.append(item)
            except Exception as e:
                logger.warning("Failed to parse item from %s: %s", self.name, type(e).__name__)
        return items
    
    def get_fetch_url(self) -> str:
        """Return base URL for caching."""
        return f"https://{self.name}"

    def _feed_key(self, identity: str) -> str:
        # Never put private destinations, query credentials or exception text in health.
        return f"{self.name}:{hashlib.sha256(identity.encode()).hexdigest()[:16]}"

    async def _collect_feeds(self, feeds, *, budget: float | None = None) -> list[dict[str, Any]]:
        """Bound individual operations and preserve completed sibling results."""
        budget = self.feed_budget_seconds if budget is None else budget
        if budget <= 0:
            for identity, _ in feeds:
                self.feed_results[self._feed_key(identity)] = {"status": "not_attempted", "items": 0}
            return []
        semaphore = asyncio.Semaphore(self.feed_concurrency)
        started = set()

        async def collect(identity, operation):
            key = self._feed_key(identity)
            if not self.feed_health.is_source_enabled(key):
                self.feed_results[key] = {"status": "cooldown", "items": 0}
                return []
            async with semaphore:
                started.add(key)
                retry_after = None
                async def validated_operation():
                    raw_items = await operation()
                    if not isinstance(raw_items, list):
                        raise _FeedFailure("invalid_items")
                    valid = []
                    rejected = 0
                    for raw in raw_items:
                        try:
                            self.parse_item(raw)
                        except Exception as exc:
                            rejected += 1
                            logger.warning("Failed to parse item from %s: %s", self.name, type(exc).__name__)
                        else:
                            valid.append(raw)
                    if rejected and not valid:
                        raise _FeedFailure("invalid_items")
                    return valid, rejected
                try:
                    items, rejected = await asyncio.wait_for(validated_operation(), self.feed_timeout_seconds)
                except asyncio.TimeoutError:
                    error = "timeout"
                except Exception as exc:
                    error = str(exc) if isinstance(exc, _FeedFailure) else type(exc).__name__
                    retry_after = exc.retry_after_seconds if isinstance(exc, _FeedFailure) else None
                else:
                    if rejected:
                        self.feed_health.record_failure(key, "invalid_items")
                        self.feed_results[key] = {"status": "partial", "items": len(items), "parse_failures": rejected, "error": "invalid_items"}
                    else:
                        self.feed_health.record_success(key, len(items))
                        self.feed_results[key] = {"status": "healthy", "items": len(items)}
                    return items
                self.feed_health.record_failure(key, error, retry_after_seconds=retry_after)
                self.feed_results[key] = {"status": "failed", "items": 0, "error": error}
                return []

        tasks = [(self._feed_key(identity), asyncio.create_task(collect(identity, operation))) for identity, operation in feeds]
        if not tasks:
            return []
        try:
            _, pending = await asyncio.wait([task for _, task in tasks], timeout=budget)
            for key, task in tasks:
                if task in pending:
                    task.cancel()
                    if key in started:
                        self.feed_health.record_failure(key, "collection_deadline")
                        self.feed_results[key] = {"status": "failed", "items": 0, "error": "collection_deadline"}
                    else:
                        self.feed_results[key] = {"status": "not_attempted", "items": 0}
            results = await asyncio.gather(*(task for _, task in tasks), return_exceptions=True)
            return [item for result in results if isinstance(result, list) for item in result]
        finally:
            for _, task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*(task for _, task in tasks), return_exceptions=True)
            try:
                self.feed_health.save_state()
            except OSError:
                # Optional bookkeeping must never discard completed observations,
                # and its failure detail may contain private paths.
                logger.warning("Feed health persistence failed; collected results retained")

    async def _checked_fetch(self, url: str, *, allow_statuses: tuple[int, ...] = (), **kwargs) -> FetchResult:
        result = await self.fetcher.fetch(url, **kwargs)
        if result.status_code not in allow_statuses and (not result.success or result.status_code != 200):
            retry_after = None
            if result.status_code == 429:
                value = result.headers.get("retry-after", result.headers.get("Retry-After", "300"))
                try:
                    retry_after = float(value)
                except (TypeError, ValueError):
                    try:
                        retry_after = (parsedate_to_datetime(value) - self.feed_health._now()).total_seconds()
                    except (TypeError, ValueError, OverflowError):
                        retry_after = 300.0
                retry_after = max(1.0, min(retry_after, 86400.0))
            raise _FeedFailure(f"http_{result.status_code}" if result.status_code else "transport_failure", retry_after)
        return result

    def _parse_feed(self, content):
        import feedparser
        feed = feedparser.parse(content)
        if not feed.get("version") or feed.get("bozo"):
            raise _FeedFailure("invalid_feed")
        return feed


class GitHubAdapter(BaseNewsAdapter):
    """Fetch from GitHub releases and trending repos."""
    
    name = "github"
    source_type = SourceType.GITHUB
    
    def __init__(self, config: dict[str, Any] | None = None):
        super().__init__(config)
        self.topics = (config or {}).get("topics", ["ai", "llm", "gpt", "claude", "openai", "anthropic", "meta-llama"])
        self.orgs = (config or {}).get("orgs", [
            "openai", "anthropic-ai", "google-deepmind", "mistralai",
            "meta-llama", "microsoft", "cerebras", "groq", "togethercomputer"
        ])
        self._headers = {
            "Accept": "application/vnd.github.v3+json",
            "User-Agent": "TrackerCore/1.0"
        }
        # Read token from config or environment
        token = (config or {}).get("github_token") or os.getenv("GITHUB_TOKEN")
        if token:
            self._headers["Authorization"] = f"token {token}"
    
    async def fetch(self) -> list[dict[str, Any]]:
        """Collect search, org listings and releases with individual deadlines."""
        self.feed_results = {}
        started = time.monotonic()
        release_feeds = []
        search_url = "https://api.github.com/search/repositories?q=ai+llm&sort=updated&per_page=15"

        async def fetch_search():
            result = await self._checked_fetch(search_url, headers=self._headers)
            data = json.loads(result.content)
            return [{
                "source": "github", "title": f"{repo.get('full_name', '')} — {repo.get('description') or 'No description'}",
                # Repository updated_at is not a publication/release timestamp.
                "url": repo.get("html_url", ""), "published_date": None,
                "body": (repo.get("description") or "")[:300], "raw": repo,
            } for repo in data["items"][:15]]

        async def fetch_release(repo_name):
            url = f"https://api.github.com/repos/{repo_name}/releases/latest"
            result = await self._checked_fetch(url, headers=self._headers, allow_statuses=(404,))
            # GitHub's documented 404 means no latest release, not a broken feed.
            if result.status_code == 404:
                return []
            release = json.loads(result.content)
            return [{
                "source": "github", "title": release.get("name") or release.get("tag_name", repo_name),
                "url": release.get("html_url", ""), "published_date": release.get("published_at"),
                "body": (release.get("body") or "")[:500], "raw": release,
            }]

        async def fetch_org(org):
            url = f"https://api.github.com/orgs/{org}/repos?per_page=100&type=public"
            result = await self._checked_fetch(url, headers=self._headers)
            repos = json.loads(result.content)
            if not isinstance(repos, list):
                raise _FeedFailure("invalid_repository_list")
            for repo in repos[:10]:
                name = repo.get("full_name")
                if name:
                    release_feeds.append((f"https://api.github.com/repos/{name}/releases/latest", lambda n=name: fetch_release(n)))
            return []

        feeds = [(search_url, fetch_search)] + [(f"https://api.github.com/orgs/{org}/repos?per_page=100&type=public", lambda o=org: fetch_org(o)) for org in self.orgs]
        items = await self._collect_feeds(feeds)
        # All stages share one adapter budget; completed search/listings survive.
        items.extend(await self._collect_feeds(release_feeds, budget=self.feed_budget_seconds - (time.monotonic() - started)))
        return items
    
    def parse_item(self, raw: dict[str, Any]) -> NewsItem:
        """Parse raw GitHub item into NewsItem."""
        title = raw.get("title", "Untitled")
        
        # Determine category
        category = self._classify_category(title, raw.get("body", ""))
        
        return NewsItem(
            source=SourceType.GITHUB,
            title=title,
            url=raw.get("url", ""),
            published_date=datetime.fromisoformat(raw["published_date"].replace("Z", "+00:00")) if raw.get("published_date") else None,
            category=category,
            summary=(raw.get("body") or "")[:300] or None,
            raw_data={"source": "github", "item": raw}
        )
    
    def _classify_category(self, title: str, body: str) -> Category | None:
        """Classify GitHub item into category."""
        text = f"{title} {body}".lower()
        
        if any(kw in text for kw in ["model", "release", "gpt", "claude", "gemini", "llama"]):
            return Category.MODEL_RELEASE
        if any(kw in text for kw in ["tool", "library", "sdk", "framework", "api"]):
            return Category.TOOL_LAUNCH
        if any(kw in text for kw in ["funding", "raise", "series a", "series b", "valuation"]):
            return Category.FUNDING
        return Category.TOOL_LAUNCH  # Default to tool launch


class RedditAdapter(BaseNewsAdapter):
    """Fetch from Reddit subreddits via RSS or PRAW."""

    name = "reddit"
    source_type = SourceType.REDDIT

    SUBREDDITS = [
        "MachineLearning",
        "LocalLLaMA",
        "artificial",
        "OpenAI",
        "ChatGPT",
        "GenerativeAI"
    ]

    def __init__(self, config: dict[str, Any] | None = None):
        super().__init__(config)
        self._use_praw = False
        self._praw_instance = None
        self._praw_factory = None
        self._configuration_error = None

        client_id = (config or {}).get("reddit_client_id")
        client_secret = (config or {}).get("reddit_client_secret")
        user_agent = (config or {}).get("reddit_user_agent", "ai-tech-news-tracker/1.0")

        if client_id is not None or client_secret is not None:
            if not all(isinstance(value, str) and value.strip() for value in (client_id, client_secret)):
                self._configuration_error = "reddit_configuration_invalid"
                return
            try:
                import praw
                self._praw_factory = lambda: praw.Reddit(
                    client_id=client_id,
                    client_secret=client_secret,
                    user_agent=user_agent,
                    check_for_async=False,
                    requestor_kwargs={"timeout": self.feed_timeout_seconds},
                    ratelimit_seconds=0,
                )
                self._praw_instance = self._praw_factory()
                self._use_praw = True
                logger.info("Reddit adapter using PRAW (authenticated)")
            except ImportError:
                self._configuration_error = "reddit_client_unavailable"
            except Exception:
                self._configuration_error = "reddit_configuration_invalid"
        else:
            logger.info("Reddit adapter using RSS (anonymous, rate-limited)")

    async def fetch(self) -> list[dict[str, Any]]:
        """Fetch posts from tracked subreddits."""
        items = []

        if self._configuration_error:
            self.feed_results = {}
            async def unavailable():
                raise _FeedFailure(self._configuration_error)
            return await self._collect_feeds([(f"praw:r/{subreddit}", unavailable) for subreddit in self.SUBREDDITS])
        if self._use_praw and self._praw_instance:
            items = await self._fetch_praw()
        else:
            items = await self._fetch_rss()

        return items

    async def _fetch_praw(self) -> list[dict[str, Any]]:
        """Use a separate PRAW client per worker; PRAW sessions are not thread-safe."""
        self.feed_results = {}

        async def fetch_subreddit(subreddit):
            def listing():
                client = self._praw_factory() if self._praw_factory else self._praw_instance
                try:
                    posts = list(client.subreddit(subreddit).hot(limit=10))
                    return [{
                        "source": "reddit", "title": post.title,
                        "url": f"https://reddit.com{post.permalink}",
                        "published_date": datetime.fromtimestamp(post.created_utc, tz=timezone.utc),
                        "body": post.selftext[:500] if post.selftext else "",
                        "author": post.author.name if post.author else "unknown",
                        "subreddit": subreddit, "score": post.score, "num_comments": post.num_comments,
                        "raw": {"id": post.id, "subreddit": subreddit},
                    } for post in posts]
                finally:
                    if self._praw_factory:
                        client.close()
            return await asyncio.to_thread(listing)

        return await self._collect_feeds([(f"praw:r/{subreddit}", lambda s=subreddit: fetch_subreddit(s)) for subreddit in self.SUBREDDITS])

    async def _fetch_rss(self) -> list[dict[str, Any]]:
        """Fetch subreddits independently; the HTTP fetcher applies host throttling."""
        self.feed_results = {}

        async def fetch_subreddit(subreddit):
            url = f"https://www.reddit.com/r/{subreddit}/.rss"
            result = await self._checked_fetch(url)
            feed = self._parse_feed(result.content)
            return [{
                "source": "reddit", "title": entry.get("title", "Untitled"),
                "url": entry.get("link", ""), "published_date": entry.get("published_parsed"),
                "body": entry.get("summary", "")[:500], "author": entry.get("author", "unknown"),
                "subreddit": subreddit, "raw": entry,
            } for entry in feed.entries[:10]]

        return await self._collect_feeds([(f"https://www.reddit.com/r/{subreddit}/.rss", lambda s=subreddit: fetch_subreddit(s)) for subreddit in self.SUBREDDITS])
    
    def parse_item(self, raw: dict[str, Any]) -> NewsItem:
        """Parse raw Reddit item into NewsItem."""
        # Convert published date
        pub_date = None
        if isinstance(raw.get("published_date"), datetime):
            pub_date = raw["published_date"]
        elif raw.get("published_date"):
            try:
                pub_date = datetime(*raw["published_date"][:6], tzinfo=timezone.utc)
            except (ValueError, TypeError):
                pass
        
        return NewsItem(
            source=SourceType.REDDIT,
            title=raw.get("title", "Untitled"),
            url=raw.get("url", ""),
            published_date=pub_date,
            category=self._classify_category(raw.get("title", ""), raw.get("body", "")),
            summary=(raw.get("body") or "")[:300] or None,
            raw_data={"source": "reddit", "subreddit": raw.get("subreddit")}
        )
    
    def _classify_category(self, title: str, body: str) -> Category | None:
        """Classify Reddit post into category."""
        text = f"{title} {body}".lower()
        
        if any(kw in text for kw in ["paper", "arxiv", "research", "study"]):
            return Category.RESEARCH_PAPER
        if any(kw in text for kw in ["model", "gpt", "claude", "llama", "gemini"]):
            return Category.MODEL_RELEASE
        if any(kw in text for kw in ["tool", "lib", "package", "library"]):
            return Category.TOOL_LAUNCH
        if any(kw in text for kw in ["funding", "raise", "money"]):
            return Category.FUNDING
        return Category.DISCUSSION


class HackerNewsAdapter(BaseNewsAdapter):
    """Fetch from Hacker News via Algolia API."""
    
    name = "hackernews"
    source_type = SourceType.HACKERNEWS
    
    KEYWORDS = ["ai", "llm", "gpt", "claude", "openai", "anthropic", "machine learning", "deep learning"]
    
    async def fetch(self) -> list[dict[str, Any]]:
        """Fetch each HN keyword independently, then deduplicate surviving stories."""
        self.feed_results = {}

        async def fetch_keyword(keyword):
            # Algolia documents search_by_date as newest-first; search is relevance-ranked.
            url = f"https://hn.algolia.com/api/v1/search_by_date?query={quote(keyword)}&tags=story&hitsPerPage=10"
            result = await self._checked_fetch(url)
            data = json.loads(result.content)
            return [{
                "source": "hackernews", "title": hit.get("title", "Untitled"),
                "url": hit.get("url") or f"https://news.ycombinator.com/item?id={hit.get('objectID')}",
                "published_date": hit.get("created_at_i"), "points": hit.get("points", 0),
                "comments": hit.get("num_comments", 0), "raw": hit,
            } for hit in data["hits"][:10]]

        items = await self._collect_feeds([(f"keyword:{keyword}", lambda k=keyword: fetch_keyword(k)) for keyword in self.KEYWORDS])
        seen = set()
        unique = []
        for item in items:
            if item["url"] not in seen:
                seen.add(item["url"])
                unique.append(item)
        return unique
    
    def parse_item(self, raw: dict[str, Any]) -> NewsItem:
        """Parse raw HN item into NewsItem."""
        # Convert Unix timestamp
        pub_date = None
        if raw.get("published_date"):
            try:
                pub_date = datetime.fromtimestamp(raw["published_date"], tz=timezone.utc)
            except (ValueError, TypeError):
                pass
        
        return NewsItem(
            source=SourceType.HACKERNEWS,
            title=raw.get("title", "Untitled"),
            url=raw.get("url", ""),
            published_date=pub_date,
            category=self._classify_category(raw.get("title", "")),
            raw_data={"source": "hackernews", "points": raw.get("points", 0)}
        )
    
    def _classify_category(self, title: str) -> Category | None:
        """Classify HN story into category."""
        text = title.lower()
        
        if any(kw in text for kw in ["paper", "arxiv", "research"]):
            return Category.RESEARCH_PAPER
        if any(kw in text for kw in ["model", "gpt", "claude", "llama"]):
            return Category.MODEL_RELEASE
        if any(kw in text for kw in ["tool", "library", "api", "sdk"]):
            return Category.TOOL_LAUNCH
        return Category.DISCUSSION


class ArXivAdapter(BaseNewsAdapter):
    """Fetch new papers from arXiv."""
    
    name = "arxiv"
    source_type = SourceType.ARXIV
    
    CATEGORIES = ["cs.AI", "cs.CL", "cs.LG", "cs.CV", "cs.NE"]
    
    async def fetch(self) -> list[dict[str, Any]]:
        """Fetch each arXiv category independently."""
        self.feed_results = {}

        async def fetch_category(category):
            url = f"http://export.arxiv.org/api/query?search_query=cat:{category}&start=0&max_results=10&sortBy=submittedDate&sortOrder=descending"
            result = await self._checked_fetch(url)
            feed = self._parse_feed(result.content)
            return [{
                "source": "arxiv", "title": entry.get("title", "Untitled").replace("\\n", " "),
                "url": entry.get("link", ""), "published_date": entry.get("published_parsed"),
                "authors": entry.get("authors", []), "summary": entry.get("summary", "")[:500], "raw": entry,
            } for entry in feed.entries[:10]]

        return await self._collect_feeds([(f"category:{category}", lambda c=category: fetch_category(c)) for category in self.CATEGORIES])
    
    def parse_item(self, raw: dict[str, Any]) -> NewsItem:
        """Parse raw arXiv item into NewsItem."""
        pub_date = None
        if raw.get("published_date"):
            try:
                pub_date = datetime(*raw["published_date"][:6], tzinfo=timezone.utc)
            except (ValueError, TypeError):
                pass
        
        return NewsItem(
            source=SourceType.ARXIV,
            title=raw.get("title", "Untitled"),
            url=raw.get("url", ""),
            published_date=pub_date,
            category=Category.RESEARCH_PAPER,
            summary=raw.get("summary", "")[:300],
            raw_data={"source": "arxiv", "authors": raw.get("authors", [])}
        )


class RSSFeedAdapter(BaseNewsAdapter):
    """Fetch from arbitrary RSS feeds."""
    
    name = "rss"
    source_type = SourceType.NEWS
    
    FEEDS = [
        # AI company blogs
        ("OpenAI Blog", "https://openai.com/news/rss.xml"),
        ("Anthropic Blog", "https://www.anthropic.com/blog/rss.xml"),
        ("Google AI Blog", "https://research.google/blog/rss/"),
        ("Meta AI Blog", "https://ai.meta.com/blog/tag/research/rss/"),
        ("DeepMind Blog", "https://deepmind.google/blog/rss.xml"),
        ("Mistral Blog", "https://mistral.ai/news/rss"),
        # Tech news AI sections
        ("TechCrunch AI", "https://techcrunch.com/category/artificial-intelligence/feed/"),
        ("The Verge AI", "https://www.theverge.com/rss/index.xml"),
        ("VentureBeat AI", "https://venturebeat.com/category/ai/feed/"),
        ("Ars Technica", "https://feeds.arstechnica.com/arstechnica/technology-lab"),
        ("Wired AI", "https://www.wired.com/feed/tag/ai/latest/rss"),
        # Google News RSS (AI & LLM topics)
        ("Google News - AI", "https://news.google.com/rss/search?q=artificial+intelligence&hl=en-US&gl=US&ceid=US:en"),
        ("Google News - LLM", "https://news.google.com/rss/search?q=large+language+model&hl=en-US&gl=US&ceid=US:en"),
        ("Google News - GPT", "https://news.google.com/rss/search?q=gpt+openai&hl=en-US&gl=US&ceid=US:en"),
        ("Google News - Claude", "https://news.google.com/rss/search?q=claude+anthropic&hl=en-US&gl=US&ceid=US:en"),
        ("Google News - Gemini", "https://news.google.com/rss/search?q=gemini+google&hl=en-US&gl=US&ceid=US:en"),
        ("Google News - AI Agents", "https://news.google.com/rss/search?q=ai+agents&hl=en-US&gl=US&ceid=US:en"),
    ]
    
    def __init__(self, config: dict[str, Any] | None = None):
        super().__init__(config)
        self.feeds = config.get("feeds", self.FEEDS) if config else self.FEEDS
    
    async def fetch(self) -> list[dict[str, Any]]:
        """Fetch from all RSS feeds."""
        import feedparser
        self.feed_results = {}

        async def fetch_feed(name, url):
            result = await self._checked_fetch(url)
            feed = self._parse_feed(result.content)
            return [{
                "source": "rss", "title": entry.get("title", "Untitled"),
                "url": entry.get("link", ""), "published_date": entry.get("published_parsed"),
                "summary": entry.get("summary", "")[:300], "source_name": name, "raw": entry,
            } for entry in feed.entries[:5]]

        return await self._collect_feeds([(url, lambda n=name, u=url: fetch_feed(n, u)) for name, url in self.feeds])
    
    def parse_item(self, raw: dict[str, Any]) -> NewsItem:
        """Parse raw RSS item into NewsItem."""
        pub_date = None
        if raw.get("published_date"):
            try:
                pub_date = datetime(*raw["published_date"][:6], tzinfo=timezone.utc)
            except (ValueError, TypeError):
                pass
        
        return NewsItem(
            source=SourceType.NEWS,
            title=raw.get("title", "Untitled"),
            url=raw.get("url", ""),
            published_date=pub_date,
            category=self._classify_category(raw.get("title", ""), raw.get("summary", "")),
            summary=raw.get("summary", "")[:200],
            raw_data={"source": "rss", "source_name": raw.get("source_name")}
        )
    
    def _classify_category(self, title: str, summary: str) -> Category | None:
        """Classify RSS item into category."""
        text = f"{title} {summary}".lower()
        
        if any(kw in text for kw in ["paper", "research", "study"]):
            return Category.RESEARCH_PAPER
        if any(kw in text for kw in ["model", "release", "launch"]):
            return Category.MODEL_RELEASE
        if any(kw in text for kw in ["tool", "library", "product"]):
            return Category.TOOL_LAUNCH
        if any(kw in text for kw in ["funding", "raise", "acquisition"]):
            return Category.FUNDING
        return Category.DISCUSSION


def get_adapter(source: str, config: dict[str, Any] | None = None) -> BaseNewsAdapter:
    """Factory function to get adapter by source name."""
    adapters = {
        "github": GitHubAdapter,
        "reddit": RedditAdapter,
        "hackernews": HackerNewsAdapter,
        "arxiv": ArXivAdapter,
        "rss": RSSFeedAdapter,
        "huggingface": HuggingFaceAdapter,
    }
    
    adapter_class = adapters.get(source.lower())
    if adapter_class is None:
        raise ValueError(f"Unknown source: {source}")
    
    return adapter_class(config)


class HuggingFaceAdapter(BaseNewsAdapter):
    """Fetch trending models from Hugging Face."""

    name = "huggingface"
    source_type = SourceType.HUGGINGFACE

    async def fetch(self) -> list[dict[str, Any]]:
        """Keep model/paper successes even when the other endpoint fails."""
        import re
        self.feed_results = {}

        async def fetch_models():
            result = await self._checked_fetch("https://huggingface.co/models?sort=trending")
            links = re.findall(r'href="/([A-Za-z][A-Za-z0-9._-]+/[A-Za-z][A-Za-z0-9._-]+)"', result.content)
            seen = set()
            items = []
            for link in links[:30]:
                skip = ["join", "inference", "settings", "docs", "enterprise", "pricing", "blog", "tasks", "datasets", "spaces"]
                parts = link.split("/")
                if parts[0].lower() in skip:
                    continue
                if link not in seen and len(parts[0]) > 1:
                    seen.add(link)
                    items.append({"source": "huggingface", "title": link, "url": f"https://huggingface.co/{link}", "published_date": None, "body": f"Trending model on Hugging Face: {link}", "raw": {"model_id": link}})
            return items

        async def fetch_papers():
            result = await self._checked_fetch("https://huggingface.co/papers")
            links = re.findall(r'href="/papers/([A-Za-z0-9._-]+)"', result.content)
            seen = set()
            items = []
            for paper_id in links[:10]:
                if paper_id not in seen:
                    seen.add(paper_id)
                    title = paper_id.replace(".", " ").replace("-", " ").title()
                    items.append({"source": "huggingface_paper", "title": title, "url": f"https://huggingface.co/papers/{paper_id}", "published_date": None, "body": f"Paper on Hugging Face: {title}", "raw": {"paper_id": paper_id}})
            return items

        return await self._collect_feeds([("https://huggingface.co/models?sort=trending", fetch_models), ("https://huggingface.co/papers", fetch_papers)])

    def parse_item(self, raw: dict[str, Any]) -> NewsItem:
        """Parse HF item into NewsItem."""
        source = raw.get("source", "")
        category = Category.RESEARCH_PAPER if "paper" in source else Category.TOOL_LAUNCH

        return NewsItem(
            source=SourceType.HUGGINGFACE,
            title=raw.get("title", "Untitled"),
            url=raw.get("url", ""),
            category=category,
            raw_data={"source": source}
        )


class PapersWithCodeAdapter(BaseNewsAdapter):
    """Fetch from Papers with Code."""

    name = "paperswithcode"
    source_type = SourceType.PAPERSWITHCODE

    async def fetch(self) -> list[dict[str, Any]]:
        """Fetch trending papers from Papers with Code."""
        items = []

        try:
            url = "https://paperswithcode.com/"
            result = await self.fetcher.fetch(url)
            if result.success and result.status_code == 200:
                import re
                # Try multiple patterns for paper titles
                titles = re.findall(r'<a[^>]*class="[^"]*title[^"]*"[^>]*>([^<]+)</a>', result.content)
                if not titles:
                    titles = re.findall(r'<h2[^>]*>([^<]+)</h2>', result.content)
                if not titles:
                    titles = re.findall(r'<a[^>]*href="/paper/[^"]*"[^>]*>([^<]+)</a>', result.content)

                seen = set()
                for title in titles[:15]:
                    title_clean = title.strip()
                    if title_clean in seen or len(title_clean) < 10:
                        continue
                    seen.add(title_clean)

                    items.append({
                        "source": "paperswithcode",
                        "title": title_clean,
                        "url": "https://paperswithcode.com/",
                        "published_date": None,
                        "body": title_clean,
                        "raw": {"title": title_clean}
                    })
        except Exception as e:
            logger.warning(f"PapersWithCode fetch failed: {e}")

        return items

    def parse_item(self, raw: dict[str, Any]) -> NewsItem:
        """Parse PapersWithCode item."""
        return NewsItem(
            source=SourceType.PAPERSWITHCODE,
            title=raw.get("title", "Untitled"),
            url=raw.get("url", ""),
            category=Category.RESEARCH_PAPER,
            raw_data={"source": "paperswithcode"}
        )


class YouTubeAdapter(BaseNewsAdapter):
    """Fetch from YouTube channels via RSS feeds with retry + Data API v3 fallback."""

    name = "youtube"
    source_type = SourceType.YOUTUBE

    # Channels to track (name, channel_id)
    CHANNELS = [
        ("AI Explained", "UCNJ1Ymd5yFuUPtn21xtRbbw"),
        ("Two Minute Papers", "UCbfYPyITQ-7l4upoX8nvctg"),
        ("Yannic Kilcher", "UCZHmQk67mSJgfCCTn7xBfew"),
    ]

    async def fetch(self) -> list[dict[str, Any]]:
        """Bound every channel, including its retries and optional API fallback."""
        self.feed_results = {}
        youtube_key = self.config.get("youtube_api_key")
        return await self._collect_feeds([(f"channel:{channel_id}", lambda n=name, c=channel_id: self._fetch_channel(n, c, youtube_key)) for name, channel_id in self.CHANNELS])

    async def _fetch_channel(self, name: str, channel_id: str, youtube_key: str | None) -> list[dict[str, Any]]:
        """RSS first, optional API fallback; failures stay failures."""
        try:
            return await self._fetch_rss(name, channel_id)
        except _FeedFailure:
            if youtube_key:
                return await self._fetch_via_api(name, channel_id, youtube_key)
            raise

    async def _fetch_rss(self, name: str, channel_id: str, max_retries: int = 2) -> list[dict[str, Any]]:
        """Do not retry permanent HTTP errors or sleep on rate limits."""
        for attempt in range(max_retries + 1):
            try:
                result = await self._checked_fetch(f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}")
                feed = self._parse_feed(result.content)
                return [{
                    "source": "youtube", "title": f"[{name}] {entry.get('title', 'Untitled')}",
                    "url": entry.get("link", ""), "published_date": entry.get("published_parsed"),
                    "author": name, "raw": entry,
                } for entry in feed.entries[:5]]
            except _FeedFailure as exc:
                if str(exc) not in ("transport_failure", "http_500", "http_502", "http_503", "http_504") or attempt == max_retries:
                    raise
                await asyncio.sleep(2 ** attempt)
        return []

    async def _fetch_via_api(self, name: str, channel_id: str, api_key: str) -> list[dict[str, Any]]:
        """Data API fallback; never log or persist its credential-bearing URLs."""
        channels_url = f"https://www.googleapis.com/youtube/v3/channels?part=contentDetails&id={channel_id}"
        api_headers = {"X-Goog-Api-Key": api_key}
        result = await self._checked_fetch(channels_url, headers=api_headers)
        data = json.loads(result.content)
        channels = data["items"]
        if not channels:
            raise _FeedFailure("channel_not_found")
        uploads_id = channels[0].get("contentDetails", {}).get("relatedPlaylists", {}).get("uploads")
        if not uploads_id:
            raise _FeedFailure("uploads_playlist_missing")
        playlist_url = f"https://www.googleapis.com/youtube/v3/playlistItems?part=snippet&maxResults=5&playlistId={uploads_id}"
        result = await self._checked_fetch(playlist_url, headers=api_headers)
        data = json.loads(result.content)
        items = []
        for item in data["items"][:5]:
            snippet = item.get("snippet", {})
            video_id = snippet.get("resourceId", {}).get("videoId", "")
            if not video_id:
                raise _FeedFailure("invalid_video_item")
            items.append({
                "source": "youtube", "title": f"[{name}] {snippet.get('title', 'Untitled')}",
                "url": f"https://www.youtube.com/watch?v={video_id}", "published_date": snippet.get("publishedAt"),
                "author": name, "raw": snippet,
            })
        return items

    def parse_item(self, raw: dict[str, Any]) -> NewsItem:
        """Parse YouTube item."""
        pub_date = None
        raw_date = raw.get("published_date")

        # RSS format: time.struct_time
        if raw_date and hasattr(raw_date, "tm_year"):
            try:
                pub_date = datetime(*raw_date[:6], tzinfo=timezone.utc)
            except (ValueError, TypeError):
                pass
        # API format: ISO string "2024-01-01T00:00:00Z"
        elif raw_date and isinstance(raw_date, str):
            try:
                pub_date = datetime.fromisoformat(raw_date.replace("Z", "+00:00"))
            except ValueError:
                pass

        return NewsItem(
            source=SourceType.YOUTUBE,
            title=raw.get("title", "Untitled"),
            url=raw.get("url", ""),
            published_date=pub_date,
            category=Category.DISCUSSION,
            raw_data={"source": "youtube"}
        )


def get_all_adapters(config: dict[str, Any] | None = None) -> list[BaseNewsAdapter]:
    """Get all configured adapters, sharing any persisted per-feed circuit state."""
    if config and config.get("feed_health_state_path") and "feed_health" not in config:
        config = dict(config)
        health = SourceHealthTracker(state_path=config["feed_health_state_path"])
        health.load_state()
        config["feed_health"] = health
    return [
        GitHubAdapter(config),
        RedditAdapter(config),
        HackerNewsAdapter(config),
        ArXivAdapter(config),
        RSSFeedAdapter(config),
        HuggingFaceAdapter(config),
        YouTubeAdapter(config),
    ]
