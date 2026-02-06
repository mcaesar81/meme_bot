from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from fastapi import WebSocket

from meme_bot.utils import FileLock


def _safe_json_line(line: str) -> dict[str, Any] | None:
    text = line.strip()
    if not text:
        return None
    try:
        item = json.loads(text)
    except json.JSONDecodeError:
        return None
    if isinstance(item, dict):
        return item
    return {"value": item}


def touch_file(path: str | Path) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.exists():
        with FileLock(str(target)):
            target.touch()
    return target


def tail_lines(path: str | Path, n: int = 200) -> list[dict[str, Any]]:
    target = touch_file(path)
    with FileLock(str(target)):
        lines = target.read_text(encoding="utf-8").splitlines()[-max(1, n) :]

    out: list[dict[str, Any]] = []
    for line in lines:
        parsed = _safe_json_line(line)
        if parsed is not None:
            out.append(parsed)
    return out


async def follow_file(path: str | Path, websocket: WebSocket, poll_interval_sec: float = 0.5) -> None:
    target = touch_file(path)
    cursor = target.stat().st_size

    while True:
        await asyncio.sleep(poll_interval_sec)

        try:
            size = target.stat().st_size
        except FileNotFoundError:
            target = touch_file(target)
            cursor = 0
            continue

        if size < cursor:
            cursor = 0

        if size == cursor:
            continue

        with FileLock(str(target)):
            with target.open("r", encoding="utf-8") as handle:
                handle.seek(cursor)
                chunk = handle.read()
                cursor = handle.tell()

        for line in chunk.splitlines():
            parsed = _safe_json_line(line)
            if parsed is not None:
                await websocket.send_json(parsed)
