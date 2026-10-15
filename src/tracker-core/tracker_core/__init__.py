"""Tracker core — shared library for API tracker and news tracker."""

from .schema import (
    Provider,
    ProviderList,
    NewsItem,
    NewsBatch,
    SourceType,
    Category,
    VerificationStatus,
    validate_provider,
    validate_news_item,
    validate_provider_list,
    validate_news_batch,
)

from .adapters.base import BaseAdapter, ProviderAdapter, NewsAdapter, RawData
from .pipeline.fetcher import HTTPFetcher, FetchResult
from .pipeline.stages import (
    PipelineStage,
    BaseStage,
    FetchStage,
    ParseStage,
    DedupeStage,
    FilterStage,
    StoreStage,
    ReporterStage,
    NotifierStage,
)
from .scheduler import TrackerScheduler, create_scheduler
from .cache import FileCache
from .reporter import BaseReporter, ProviderReport, NewsReport, ReportGenerator
from .notifier import BaseNotifier, FileNotifier, TelegramNotifier, EmailNotifier, NullNotifier, get_notifier
from .store import BaseStorage, JsonStorage, ProviderStorage, NewsStorage
from .config import load_schedule, ScheduleConfig, TrackerSchedule

__version__ = "0.1.0"
__all__ = [
    # Schema
    "Provider",
    "ProviderList",
    "NewsItem",
    "NewsBatch",
    "SourceType",
    "Category",
    "VerificationStatus",
    "validate_provider",
    "validate_news_item",
    "validate_provider_list",
    "validate_news_batch",
    # Adapters
    "BaseAdapter",
    "ProviderAdapter",
    "NewsAdapter",
    "RawData",
    # Pipeline
    "HTTPFetcher",
    "FetchResult",
    "PipelineStage",
    "BaseStage",
    "FetchStage",
    "ParseStage",
    "DedupeStage",
    "FilterStage",
    "StoreStage",
    "ReporterStage",
    "NotifierStage",
    # Scheduler
    "TrackerScheduler",
    "create_scheduler",
    # Cache
    "FileCache",
    # Reporter
    "BaseReporter",
    "ProviderReport",
    "NewsReport",
    "ReportGenerator",
    # Notifier
    "BaseNotifier",
    "FileNotifier",
    "TelegramNotifier",
    "EmailNotifier",
    "NullNotifier",
    "get_notifier",
    # Storage
    "BaseStorage",
    "JsonStorage",
    "ProviderStorage",
    "NewsStorage",
    # Config
    "load_schedule",
    "ScheduleConfig",
    "TrackerSchedule",
]
