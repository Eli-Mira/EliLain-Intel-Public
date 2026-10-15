"""Storage module for persisting tracker data."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any


class BaseStorage:
    """Base class for storage backends."""
    
    def __init__(self, base_path: Path | str = ".data"):
        self.base_path = Path(base_path)
        self.base_path.mkdir(parents=True, exist_ok=True)
    
    async def save(self, items: list[Any]) -> list[Any]:
        """Save items to storage."""
        raise NotImplementedError
    
    async def load(self, date: datetime | None = None) -> list[Any]:
        """Load items from storage."""
        raise NotImplementedError
    
    async def find_by_id(self, item_id: str) -> Any | None:
        """Find item by ID."""
        raise NotImplementedError


class JsonStorage(BaseStorage):
    """JSON file-based storage."""
    
    def __init__(self, base_path: Path | str = ".data", pretty: bool = True):
        super().__init__(base_path)
        self.pretty = pretty
    
    async def save(self, items: list[Any]) -> list[Any]:
        """Save items to JSON file."""
        data = [item.model_dump() if hasattr(item, "model_dump") else item for item in items]
        
        filename = self.base_path / "items.json"
        with open(filename, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2 if self.pretty else None, default=str)
        
        return items
    
    async def load(self, date: datetime | None = None) -> list[Any]:
        """Load all items from JSON file."""
        filename = self.base_path / "items.json"
        
        if not filename.exists():
            return []
        
        with open(filename, "r", encoding="utf-8") as f:
            data = json.load(f)
        
        return data
    
    async def find_by_id(self, item_id: str) -> Any | None:
        """Find item by ID."""
        items = await self.load()
        for item in items:
            if hasattr(item, "id") and item.id == item_id:
                return item
            if isinstance(item, dict) and item.get("id") == item_id:
                return item
        return None
    
    async def append(self, items: list[Any]) -> list[Any]:
        """Append items to existing file."""
        existing = await self.load()
        existing.extend(items)
        return await self.save(existing)


class ProviderStorage(JsonStorage):
    """Storage for provider data."""
    
    def __init__(self, base_path: Path | str = ".data"):
        super().__init__(base_path)
        self._file = self.base_path / "providers.json"
    
    async def save(self, providers) -> list[Any]:
        """Save providers to providers.json."""
        from tracker_core.schema import ProviderList
        
        data = ProviderList(providers=providers).model_dump()
        self._file.write_text(json.dumps(data, indent=2, default=str))
        return providers
    
    async def load(self, date: datetime | None = None) -> list[Any]:
        """Load all providers."""
        if not self._file.exists():
            return []
        
        with open(self._file, "r", encoding="utf-8") as f:
            data = json.load(f)
        
        from tracker_core.schema import Provider
        return [Provider(**p) for p in data.get("providers", [])]


class NewsStorage(JsonStorage):
    """Storage for news items."""
    
    def __init__(self, base_path: Path | str = ".data"):
        super().__init__(base_path)
    
    async def save_daily(self, date: datetime, items: list[Any]) -> None:
        """Save daily batch to dated file."""
        filename = self.base_path / f"news_{date.strftime('%Y%m%d')}.json"
        data = [item.model_dump() if hasattr(item, "model_dump") else item for item in items]
        filename.write_text(json.dumps(data, indent=2, default=str))
    
    async def load_daily(self, date: datetime) -> list[Any]:
        """Load daily batch."""
        filename = self.base_path / f"news_{date.strftime('%Y%m%d')}.json"
        
        if not filename.exists():
            return []
        
        with open(filename, "r", encoding="utf-8") as f:
            data = json.load(f)
        
        return data
