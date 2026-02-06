from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from datetime import datetime, date

from meme_bot.models import RuntimeState
from meme_bot.utils import FileLock


def _json_default(o):
    if isinstance(o, (datetime, date)):
        return o.isoformat()
    return str(o)


class RuntimeStateStore:
    def __init__(self, path: str):
        self.path = Path(path)

    def load(self) -> RuntimeState:
        with FileLock(str(self.path)):
            if not self.path.exists():
                return RuntimeState()
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        return RuntimeState(**raw)

    def save(self, state: RuntimeState) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with FileLock(str(self.path)):
            temp = self.path.with_suffix(".tmp")
            temp.write_text(
                json.dumps(asdict(state), indent=2, default=_json_default),
                encoding="utf-8",
            )
            temp.replace(self.path)

