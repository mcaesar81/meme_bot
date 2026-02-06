from __future__ import annotations

import json
import sys
import traceback
from dataclasses import asdict, is_dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from meme_bot.utils import FileLock


def _json_default(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


class JsonLineLogger:
    def __init__(self, events_path: str, trades_path: str):
        self.events_path = Path(events_path)
        self.trades_path = Path(trades_path)

    def event(self, event_type: str, payload: dict[str, Any]) -> None:
        try:
            self._append(
                self.events_path,
                {
                    "ts": datetime.utcnow().isoformat(),
                    "event_type": event_type,
                    "payload": payload,
                },
            )
        except Exception as exc:  # noqa: BLE001
            self._write_fallback_error("event", exc)

    def trade(self, trade_obj: Any) -> None:
        if is_dataclass(trade_obj):
            payload = asdict(trade_obj)
        elif isinstance(trade_obj, dict):
            payload = trade_obj
        else:
            payload = {"raw": str(trade_obj)}

        try:
            self._append(self.trades_path, payload)
        except Exception as exc:  # noqa: BLE001
            self._write_fallback_error("trade", exc)

    @staticmethod
    def _append(path: Path, record: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with FileLock(str(path)):
            with path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, default=_json_default) + "\n")

    def _write_fallback_error(self, kind: str, exc: Exception) -> None:
        trace = traceback.format_exc()
        try:
            sys.stderr.write(
                f"[JsonLineLogger:{kind}] write failed path={self.events_path if kind == 'event' else self.trades_path}: {exc}\n{trace}\n"
            )
            sys.stderr.flush()
        except Exception:
            pass
