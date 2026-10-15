"""HTML reporter using Jinja2 templates."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from jinja2 import Environment, FileSystemLoader, TemplateNotFound
from .news_health import project_news_health


def _safe_http_url(value: Any) -> str:
    """Return an absolute HTTP(S) URL unchanged, or an inert empty value."""
    if not isinstance(value, str) or not value:
        return ""
    # Reject browser/parser normalization ambiguities rather than repairing input.
    if any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in value) or "\\" in value:
        return ""
    try:
        parsed = urlsplit(value)
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
            return ""
        if parsed.username is not None or parsed.password is not None:
            return ""
        parsed.port  # Validate malformed ports without normalizing the URL.
    except ValueError:
        return ""
    return value


def _news_timestamp(value: Any, sqlite_utc: bool = False) -> str | None:
    """Format saved news time; only SQLite's fetched-date contract implies UTC."""
    from datetime import datetime, timezone

    try:
        if isinstance(value, datetime):
            parsed = value
        elif isinstance(value, str) and len(value) <= 64 and ("T" in value or " " in value):
            parsed = datetime.fromisoformat(value)
        else:
            return None
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            if not sqlite_utc:
                return None  # A publication time without an offset is ambiguous.
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc).isoformat()
    except (ValueError, TypeError, OverflowError):
        return None


class HTMLReporter:
    """Generate HTML reports from tracker data using templates."""
    
    TEMPLATE_DIR = Path(__file__).parent.parent.parent / "templates"
    if not TEMPLATE_DIR.is_dir():
        TEMPLATE_DIR = Path(__file__).parent.parent / "templates"
    STRICT_THEME = False
    
    def __init__(self, theme: str = "default"):
        self.theme = theme
        self.env = Environment(
            loader=FileSystemLoader(str(self.TEMPLATE_DIR)),
            autoescape=True
        )
        self.env.filters["safe_url"] = _safe_http_url
        self.env.filters["news_timestamp"] = _news_timestamp
    
    def render(
        self,
        api_items: list[dict[str, Any]],
        news_items: list[dict[str, Any]] | None = None,
        tool_items: list[dict[str, Any]] | None = None,
        report_date: str | None = None,
        generated_at: str | None = None,
        persona_image_url: str | None = None,
        *,
        news_saved_total: int | None = None,
        inventory_total: int | None = None,
        change_items: list[dict[str, Any]] | None = None,
        baseline: bool = False,
        action_items: list[dict[str, Any]] | None = None,
        source_items: list[dict[str, Any]] | None = None,
        news_health_items: list[dict[str, Any]] | None = None,
        runtime_source: str | None = None,
        recommendation_groups: dict[str, dict[str, Any]] | None = None,
        recommendation_coverage: dict[str, Any] | None = None,
        public_export: bool = False,
    ) -> str:
        from datetime import datetime, timezone
        
        if report_date is None:
            report_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if generated_at is None:
            generated_at = datetime.now(timezone.utc).strftime("%H:%M UTC")
        if news_items is None:
            news_items = []
        if tool_items is None:
            tool_items = []
        
        template_name = f"report_template_{self.theme}.html" if self.theme != "default" else "report_template.html"
        
        try:
            template = self.env.get_template(template_name)
        except TemplateNotFound:
            if self.STRICT_THEME:
                raise
            template = self.env.get_template("report_template.html")
        
        from .branding import avatar_data_uri, AVATAR_ALT
        return template.render(
            branding_avatar=avatar_data_uri() if self.theme == 'lain' and (public_export or not _safe_http_url(persona_image_url)) else None,
            branding_avatar_alt=AVATAR_ALT,
            report_date=report_date,
            generated_at=generated_at,
            api_items=api_items,
            news_items=news_items,
            news_saved_total=news_saved_total,
            inventory_total=inventory_total,
            tool_items=tool_items,
            persona_image_url=persona_image_url,
            news_health_items=[] if public_export else project_news_health(news_health_items),
            change_items=change_items or [],
            baseline=baseline,
            action_items=action_items or [],
            source_items=source_items or [],
            runtime_source=runtime_source,
            recommendation_groups={} if public_export else (recommendation_groups or {}),
            recommendation_coverage={} if public_export else (recommendation_coverage or {}),
            public_export=public_export,
        )
    
    def save(
        self,
        output_path: Path | str,
        api_items: list[dict[str, Any]],
        news_items: list[dict[str, Any]] | None = None,
        tool_items: list[dict[str, Any]] | None = None,
        report_date: str | None = None,
        generated_at: str | None = None,
        persona_image_url: str | None = None,
        *,
        news_saved_total: int | None = None,
        inventory_total: int | None = None,
        change_items: list[dict[str, Any]] | None = None,
        baseline: bool = False,
        action_items: list[dict[str, Any]] | None = None,
        source_items: list[dict[str, Any]] | None = None,
        news_health_items: list[dict[str, Any]] | None = None,
        runtime_source: str | None = None,
        recommendation_groups: dict[str, dict[str, Any]] | None = None,
        recommendation_coverage: dict[str, Any] | None = None,
        public_export: bool = False,
    ) -> Path:
        html = self.render(
            api_items=api_items,
            news_items=news_items,
            news_saved_total=news_saved_total,
            inventory_total=inventory_total,
            tool_items=tool_items,
            report_date=report_date,
            generated_at=generated_at,
            persona_image_url=persona_image_url,
            news_health_items=news_health_items,
            change_items=change_items or [],
            baseline=baseline,
            action_items=action_items or [],
            source_items=source_items or [],
            runtime_source=runtime_source,
            recommendation_groups={} if public_export else (recommendation_groups or {}),
            recommendation_coverage={} if public_export else (recommendation_coverage or {}),
            public_export=public_export,
        )
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(html, encoding="utf-8")
        return output_path


class LainReporter(HTMLReporter):
    # Explicitly selecting the supplied design must never silently redesign.
    STRICT_THEME = True

    def __init__(self):
        super().__init__(theme="lain")


class DefaultReporter(HTMLReporter):
    def __init__(self):
        super().__init__(theme="default")


def get_html_reporter(theme: str = "default") -> HTMLReporter:
    if theme == "lain":
        return LainReporter()
    return DefaultReporter()
