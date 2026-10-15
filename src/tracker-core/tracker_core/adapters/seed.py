"""Seed provider list for Free LLM API Tracker.

Western providers:
- Google AI Studio (Gemini)
- OpenRouter (aggregator)
- NVIDIA NIM
- Mistral La Plateforme
- Groq
- Cerebras
- Together AI

Chinese providers:
- DeepSeek
- Alibaba Qwen (DashScope)
- Zhipu AI (GLM)
- Moonshot AI (Kimi)
- MiniMax
- 01.AI (Yi)
"""

from datetime import datetime

SEED_PROVIDERS = [
    # Western
    {
        "name": "Google AI Studio",
        "base_url": "https://ai.google.dev/",
        "docs_url": "https://ai.google.dev/docs",
        "pricing_url": "https://ai.google.dev/pricing",
        "free_tier_available": True,
        "free_tier_details": {
            "rate_limit": "60 requests per minute",
            "models": ["gemini-pro", "gemini-pro-vision", "gemini-1.5-pro"]
        },
        "status": "unverified",
        "region": "western",
        "tags": ["google", "gemini", "multimodal"]
    },
    {
        "name": "OpenRouter",
        "base_url": "https://openrouter.ai/",
        "docs_url": "https://openrouter.ai/docs",
        "pricing_url": "https://openrouter.ai/models",
        "free_tier_available": True,
        "free_tier_details": {
            "description": "Aggregator with many free models from various providers",
            "models": ["openai/gpt-3.5-turbo", "google/gemini-pro", "meta-llama/llama-3"]
        },
        "status": "unverified",
        "region": "western",
        "tags": ["aggregator", "openai", "meta-llama"]
    },
    {
        "name": "NVIDIA NIM",
        "base_url": "https://ai.nvidia.com/",
        "docs_url": "https://docs.api.nvidia.com/nim",
        "pricing_url": "https://build.nvidia.com/explore/chatgpt",
        "free_tier_available": True,
        "free_tier_details": {
            "rate_limit": "Varies by model",
            "models": ["meta/llama-3-8b-instruct", "mistralai/mistral-7b-instruct"]
        },
        "status": "unverified",
        "region": "western",
        "tags": ["nvidia", "llama", "mistral"]
    },
    {
        "name": "Mistral La Plateforme",
        "base_url": "https://mistral.ai/",
        "docs_url": "https://docs.mistral.ai/",
        "pricing_url": "https://mistral.ai/technology/#pricing",
        "free_tier_available": True,
        "free_tier_details": {
            "rate_limit": "Free tier: 50 RPM",
            "models": ["mistral-small", "mistral-medium", "mistral-large"]
        },
        "status": "unverified",
        "region": "western",
        "tags": ["mistral", "french", "open-weight"]
    },
    {
        "name": "Groq",
        "base_url": "https://groq.com/",
        "docs_url": "https://console.groq.com/docs",
        "pricing_url": "https://groq.com/pricing",
        "free_tier_available": True,
        "free_tier_details": {
            "rate_limit": "5 requests per minute (free tier)",
            "models": ["llama-3.1-8b-instant", "llama-3.1-70b-versatile", "llama-3.3-70b-versatile"]
        },
        "status": "unverified",
        "region": "western",
        "tags": ["groq", "llama", "fast-inference"]
    },
    {
        "name": "Cerebras",
        "base_url": "https://www.cerebras.ai/",
        "docs_url": "https://inference-docs.cerebras.ai/",
        "pricing_url": "https://www.cerebras.ai/pricing",
        "free_tier_available": True,
        "free_tier_details": {
            "rate_limit": "Free sandbox available",
            "models": ["wafer-2-samples", "lama-3.1-70b"]
        },
        "status": "unverified",
        "region": "western",
        "tags": ["cerebras", "wafer-scale", "llama"]
    },
    {
        "name": "Together AI",
        "base_url": "https://www.together.ai/",
        "docs_url": "https://docs.together.ai/",
        "pricing_url": "https://www.together.ai/pricing",
        "free_tier_available": True,
        "free_tier_details": {
            "rate_limit": "Free tier: 10 RPM",
            "models": ["meta-llama/Llama-3-8b-chat-hf", "mistralai/Mistral-7B-Instruct-v0.2"]
        },
        "status": "unverified",
        "region": "western",
        "tags": ["together", "open-source", "fine-tuning"]
    },
    # Chinese
    {
        "name": "DeepSeek",
        "base_url": "https://www.deepseek.com/",
        "docs_url": "https://platform.deepseek.com/api-docs/",
        "pricing_url": "https://www.deepseek.com/pricing",
        "free_tier_available": True,
        "free_tier_details": {
            "rate_limit": "Free tier available",
            "models": ["deepseek-chat", "deepseek-coder"]
        },
        "status": "unverified",
        "region": "chinese",
        "tags": ["deepseek", "chinese", "coder"]
    },
    {
        "name": "Alibaba Qwen (DashScope)",
        "base_url": "https://tongyi.aliyun.com/",
        "docs_url": "https://help.aliyun.com/zh/dashscope/",
        "pricing_url": "https://help.aliyun.com/zh/dashscope/product/pricing",
        "free_tier_available": True,
        "free_tier_details": {
            "rate_limit": "Free tier with quota",
            "models": ["qwen-turbo", "qwen-plus", "qwen-max"]
        },
        "status": "unverified",
        "region": "chinese",
        "tags": ["alibaba", "qwen", "chinese"]
    },
    {
        "name": "Zhipu AI (GLM)",
        "base_url": "https://open.bigmodel.cn/",
        "docs_url": "https://open.bigmodel.cn/dev/api",
        "pricing_url": "https://open.bigmodel.cn/pricing",
        "free_tier_available": True,
        "free_tier_details": {
            "rate_limit": "Free tier: 25 RPM",
            "models": ["glm-4", "glm-4-plus", "glm-4-air"]
        },
        "status": "unverified",
        "region": "chinese",
        "tags": ["zhipu", "glm", "chinese"]
    },
    {
        "name": "Moonshot AI (Kimi)",
        "base_url": "https://www.moonshot.cn/",
        "docs_url": "https://platform.moonshot.cn/docs",
        "pricing_url": "https://platform.moonshot.cn/pricing",
        "free_tier_available": True,
        "free_tier_details": {
            "rate_limit": "Free tier available",
            "models": ["kimi-k2", "kimi-k1.5"]
        },
        "status": "unverified",
        "region": "chinese",
        "tags": ["moonshot", "kimi", "chinese", "long-context"]
    },
    {
        "name": "MiniMax",
        "base_url": "https://www.minimaxi.com/",
        "docs_url": "https://www.minimaxi.com/document/guide",
        "pricing_url": "https://www.minimaxi.com/price",
        "free_tier_available": True,
        "free_tier_details": {
            "rate_limit": "Free tier with quota",
            "models": ["abab6.5", "abab5.5s"]
        },
        "status": "unverified",
        "region": "chinese",
        "tags": ["minimax", "chinese", "abab"]
    },
    {
        "name": "01.AI (Yi)",
        "base_url": "https://www.lingyiwanwu.com/",
        "docs_url": "https://platform.01.ai/docs",
        "pricing_url": "https://platform.01.ai/pricing",
        "free_tier_available": True,
        "free_tier_details": {
            "rate_limit": "Free tier: 60 RPM",
            "models": ["yi-large", "yi-large-turbo", "yi-34b"]
        },
        "status": "unverified",
        "region": "chinese",
        "tags": ["01-ai", "yi", "chinese"]
    }
]


def create_seed_providers() -> list[dict]:
    """Create seed provider list with metadata."""
    providers = []
    for data in SEED_PROVIDERS:
        provider = {
            **data,
            "last_verified": None,
            "last_checked": None,
            "needs_review": True,
            "content_hash": None,
            "created_at": datetime.utcnow().isoformat() + "Z",
            "updated_at": datetime.utcnow().isoformat() + "Z"
        }
        providers.append(provider)
    return providers


def get_seed_json_path() -> str:
    """Get path to seed JSON file."""
    import os
    return os.path.join(os.path.dirname(__file__), "seed_providers.json")
