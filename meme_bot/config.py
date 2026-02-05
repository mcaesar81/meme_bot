from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict

import yaml

from meme_bot.utils import FileLock


@dataclass
class Config:
    raw: Dict[str, Any]

    @classmethod
    def load(cls, path: str | Path = "config.yaml") -> "Config":
        target = Path(path)
        with FileLock(str(target)):
            data = yaml.safe_load(target.read_text(encoding="utf-8"))
        return cls(raw=data)

    def get(self, *keys: str, default: Any = None) -> Any:
        current: Any = self.raw
        for key in keys:
            if not isinstance(current, dict):
                return default
            current = current.get(key)
            if current is None:
                return default
        return current
