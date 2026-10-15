"""Versioned schema for tracker data.

All models live here. New fields are optional with defaults.
Breaking changes create a new version class, not a rename.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field, HttpUrl


class SourceType(str, Enum):
    """Types of data sources."""
    PROVIDER = "provider"        # LLM API provider
    GITHUB = "github"            # GitHub repo/release
    REDDIT = "reddit"            # Reddit post
    HACKERNEWS = "hackernews"    # HN item
    ARXIV = "arxiv"              # Research paper
    NEWS = "news"                # Tech news site
    BLOG = "blog"                # Company blog
    HUGGINGFACE = "huggingface"  # HF model/paper
    PAPERSWITHCODE = "paperswithcode"  # Papers with Code
    YOUTUBE = "youtube"          # YouTube video


class Category(str, Enum):
    """Categories for news items."""
    MODEL_RELEASE = "model_release"
    RESEARCH_PAPER = "research_paper"
    TOOL_LAUNCH = "tool_launch"
    FUNDING = "funding"
    DISCUSSION = "discussion"
    POLICY = "policy"
    PROVIDER_UPDATE = "provider_update"
    FREE_TIER_CHANGE = "free_tier_change"


class VerificationStatus(str, Enum):
    """Status of provider verification."""
    UNVERIFIED = "unverified"
    PENDING_REVIEW = "pending_review"
    VERIFIED = "verified"
    DEPRECATED = "deprecated"


# ---------------------------------------------------------------------------
# Provider Schema (Free API Tracker)
# ---------------------------------------------------------------------------

class Provider(BaseModel):
    """Single provider record in the free API tracker."""
    
    name: str
    base_url: HttpUrl
    docs_url: Optional[HttpUrl] = None
    pricing_url: Optional[HttpUrl] = None
    
    # Free tier details
    free_tier_available: bool = True
    free_tier_details: Optional[dict[str, Any]] = Field(
        default=None,
        description="Rate limits, token limits, model list, etc."
    )
    
    # Verification state
    status: VerificationStatus = VerificationStatus.UNVERIFIED
    needs_review: bool = False
    last_verified: Optional[datetime] = None
    last_checked: Optional[datetime] = None
    
    # Metadata
    region: Optional[str] = None  # "western" or "chinese" or None
    tags: list[str] = Field(default_factory=list)
    
    # Content hash for change detection
    content_hash: Optional[str] = None
    
    # Timestamps
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)
    
    model_config = ConfigDict(json_schema_extra={
        "example": {
            "name": "Google AI Studio",
            "base_url": "https://ai.google.dev/",
            "docs_url": "https://ai.google.dev/docs",
            "free_tier_available": True,
            "free_tier_details": {
                "rate_limit": "60 requests per minute",
                "models": ["gemini-pro", "gemini-pro-vision"]
            },
            "status": "verified",
            "region": "western"
        }
    })


class ProviderList(BaseModel):
    """Container for all providers."""
    providers: list[Provider] = Field(default_factory=list)
    last_updated: Optional[datetime] = None
    version: str = "1.0"


# ---------------------------------------------------------------------------
# News Item Schema (News Tracker)
# ---------------------------------------------------------------------------

class NewsItem(BaseModel):
    """Single news item from any source."""
    
    source: SourceType
    title: str
    url: HttpUrl
    published_date: Optional[datetime] = None
    fetched_date: datetime = Field(default_factory=datetime.utcnow)
    
    # Category classification
    category: Optional[Category] = None
    
    # Raw content for reference
    raw_data: Optional[dict[str, Any]] = Field(
        default=None,
        description="Original fetch response for debugging"
    )
    
    # Relevance
    relevance_score: float = 0.0
    is_relevant: bool = False
    
    # Deduplication
    dedup_key: Optional[str] = None
    
    # Summarization (filled later)
    summary: Optional[str] = None
    
    # Sources that reported this story (for cross-verification)
    source_count: int = 1
    
    model_config = ConfigDict(json_schema_extra={
        "example": {
            "source": "github",
            "title": "OpenAI releases GPT-4o",
            "url": "https://github.com/openai/openai-python/releases/tag/v1.0.0",
            "published_date": "2024-05-13T00:00:00Z",
            "category": "model_release",
            "relevance_score": 0.95,
            "is_relevant": True,
            "summary": "OpenAI released GPT-4o, their new multimodal model."
        }
    })


class NewsBatch(BaseModel):
    """Daily batch of news items."""
    date: datetime
    items: list[NewsItem] = Field(default_factory=list)
    total_count: int = 0
    relevant_count: int = 0
    
    model_config = ConfigDict(json_schema_extra={
        "example": {
            "date": "2024-05-13T00:00:00Z",
            "items": [],
            "total_count": 0,
            "relevant_count": 0
        }
    })


# ---------------------------------------------------------------------------
# Schema Validation Helpers
# ---------------------------------------------------------------------------

def validate_provider(data: dict[str, Any]) -> Provider:
    """Validate and parse provider data."""
    return Provider(**data)


def validate_news_item(data: dict[str, Any]) -> NewsItem:
    """Validate and parse news item data."""
    return NewsItem(**data)


def validate_provider_list(data: dict[str, Any]) -> ProviderList:
    """Validate provider list data."""
    return ProviderList(**data)


def validate_news_batch(data: dict[str, Any]) -> NewsBatch:
    """Validate news batch data."""
    return NewsBatch(**data)
