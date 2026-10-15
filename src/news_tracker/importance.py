"""Urgency / importance scoring — Phase 1: deterministic rule-based.

Score boosts for:
- New free-tier LLM/API providers (feeds FreeLLMAPI router)
- Generous/changed rate limits
- Open-weight or locally-runnable models
- Python-ecosystem data tooling
- Open-source releases over closed ones
"""

from __future__ import annotations

from tracker_core.schema import NewsItem, Category


# ── Scoring criteria ─────────────────────────────────────────────────────

# Weight for each criterion (sum of all possible = ~1.0)
WEIGHTS = {
    "new_provider": 0.25,        # New free-tier LLM/API provider
    "rate_limit_change": 0.15,   # Generous or changed rate limits
    "open_weight": 0.15,         # Open-weight / locally runnable
    "open_source": 0.10,         # Open-source release (general)
    "python_ecosystem": 0.10,    # Pandas/Polars/SQL/ETL tooling
    "category_bonus": 0.10,      # Category-specific bonus
    "cross_source": 0.15,        # Already counted in scoring.py but tracked
}

# Patterns for each criterion
PATTERNS = {
    "new_provider": [
        r"free\s+(tier|api|model)", r"new\s+(llm|model|api)\s+(provider|released)",
        r"open\s*weight\s+model", r"llm\s+api\s+(free|released|launched)",
        r"api\s+key\s+(free|available)", r"no\s+(credit\s+card|signup)\s+required",
        r"provider\s+(launched|released|available)",
    ],
    "rate_limit": [
        r"rate\s+limit\s+(increased|changed|generous|higher)",
        r"requests?\s+per\s+minute\s+\d{3,}", r"rpm\s+\d{3,}",
        r"token\s+limit\s+(increased|higher)",
        r"quota\s+(increased|changed|free)",
    ],
    "open_weight": [
        r"open.?weight", r"locally\s+runnable", r"self.?hosted",
        r"runs?\s+on\s+(consumer|local|your\s+hardware)",
        r"gguf", r"ggml", r"quantized", r"4.?bit", r"8.?bit",
        r"llama\.cpp", r"ollama", r"local\s+llm",
    ],
    "open_source": [
        r"open.?source", r"apache\s*2\.0", r"mit\s+license",
        r"gpl", r"bsd", r"publicly\s+available",
        r"source\s+code\s+(released|available|open)",
    ],
    "python_ecosystem": [
        r"pandas", r"polars", r"pyarrow", r"duckdb", r"sql\s+engine",
        r"sqlalchemy", r"airflow", r"dagster", r"prefect", r"etl",
        r"data\s+pipeline", r"orchestration", r"dbt",
        r"pip\s+install", r"conda\s+install", r"pypi",
    ],
}

# Category bonuses
CATEGORY_BONUS = {
    Category.MODEL_RELEASE: 0.10,
    Category.TOOL_LAUNCH: 0.08,
    Category.FUNDING: 0.05,
    Category.RESEARCH_PAPER: 0.05,
    Category.POLICY: 0.03,
    Category.DISCUSSION: 0.00,
    Category.PROVIDER_UPDATE: 0.08,
    Category.FREE_TIER_CHANGE: 0.10,
}


def score_importance(item: NewsItem) -> float:
    """
    Calculate importance score for an item (0.0 to 1.0).
    Deterministic, explainable — no LLM call.
    """
    text = f"{item.title} {item.summary or ''}".lower()
    score = 0.0

    # New provider bonus
    if _matches_any(text, PATTERNS["new_provider"]):
        score += WEIGHTS["new_provider"]

    # Rate limit change
    if _matches_any(text, PATTERNS["rate_limit"]):
        score += WEIGHTS["rate_limit"]

    # Open-weight / locally runnable
    if _matches_any(text, PATTERNS["open_weight"]):
        score += WEIGHTS["open_weight"]

    # Open source
    if _matches_any(text, PATTERNS["open_source"]):
        score += WEIGHTS["open_source"]

    # Python ecosystem
    if _matches_any(text, PATTERNS["python_ecosystem"]):
        score += WEIGHTS["python_ecosystem"]

    # Category bonus
    if item.category in CATEGORY_BONUS:
        score += CATEGORY_BONUS[item.category]

    return min(score, 1.0)


def _matches_any(text: str, patterns: list[str]) -> bool:
    """Check if text matches any pattern."""
    import re
    for pattern in patterns:
        if re.search(pattern, text, re.IGNORECASE):
            return True
    return False
