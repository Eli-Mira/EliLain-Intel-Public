"""Cache module for storing fetched data locally.

Prevents re-fetching on failures and enables change detection via content hashing.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


class FileCache:
    """Simple file-based cache with content hashing."""
    
    def __init__(self, cache_dir: Path | str = ".cache"):
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
    
    def get(self, key: str) -> dict[str, Any] | None:
        """Get cached data by key."""
        path = self._get_path(key)
        if not path.exists():
            return None
        
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return None
    
    def set(self, key: str, data: dict[str, Any]) -> None:
        """Store data in cache."""
        path = self._get_path(key)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, default=str)
    
    def has_changed(self, key: str, new_content: str | bytes) -> bool:
        """Check if content has changed since last cache."""
        current_hash = self._hash_content(new_content)
        cached = self.get(key)
        
        if cached is None:
            return True
        
        return cached.get("content_hash") != current_hash
    
    def invalidate(self, key: str) -> bool:
        """Remove cached data."""
        path = self._get_path(key)
        if path.exists():
            path.unlink()
            return True
        return False
    
    def clear(self) -> int:
        """Clear all cached data. Returns count of files removed."""
        count = 0
        for path in self.cache_dir.glob("*.json"):
            path.unlink()
            count += 1
        return count
    
    def _get_path(self, key: str) -> Path:
        """Get cache file path for key."""
        # Sanitize key to safe filename
        safe_key = hashlib.md5(key.encode()).hexdigest()[:16]
        return self.cache_dir / f"{safe_key}.json"
    
    @staticmethod
    def _hash_content(content: str | bytes) -> str:
        """Create content hash for change detection."""
        if isinstance(content, str):
            content = content.encode("utf-8")
        return hashlib.sha256(content).hexdigest()
    
    def save_with_hash(self, key: str, content: str | bytes, data: dict[str, Any]) -> None:
        """Save data along with content hash."""
        hash_value = self._hash_content(content)
        data["content_hash"] = hash_value
        data["cached_at"] = str(Path(__file__).parent.parent.parent)  # Placeholder for timestamp
        self.set(key, data)
    
    def get_hash(self, key: str) -> str | None:
        """Get stored hash for a key."""
        cached = self.get(key)
        return cached.get("content_hash") if cached else None
