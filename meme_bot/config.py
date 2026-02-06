from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict

import yaml

from meme_bot.utils import FileLock


@dataclass
class Config:
    raw: Dict[str, Any]
    source_path: Path

    @classmethod
    def load(cls, path: str | Path = "config.yaml") -> "Config":
        target = Path(path).expanduser().resolve()
        with FileLock(str(target)):
            data = yaml.safe_load(target.read_text(encoding="utf-8"))
        return cls(raw=data, source_path=target)

    @property
    def base_dir(self) -> Path:
        return self.source_path.parent

    def get(self, *keys: str, default: Any = None) -> Any:
        current: Any = self.raw
        for key in keys:
            if not isinstance(current, dict):
                return default
            current = current.get(key)
            if current is None:
                return default
        return current

    def resolve_path(self, *keys: str, default: str) -> str:
        raw_path = self.get(*keys, default=default)
        target = Path(str(raw_path)).expanduser()
        if not target.is_absolute():
            target = (self.base_dir / target).resolve()
        return str(target)
