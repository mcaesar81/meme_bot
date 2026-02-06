from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timedelta
from threading import Event, Thread
from typing import Any

from meme_bot.app import run_loop
from meme_bot.config import Config
from meme_bot.state_machine import StateMachine
from meme_bot.state_store import RuntimeStateStore


class BotRunner:
    def __init__(self, cfg_path: str = "config.yaml"):
        self.cfg_path = cfg_path
        self._stop_event = Event()
        self._thread: Thread | None = None

    def start(self) -> bool:
        if self.is_alive:
            return False

        self._stop_event = Event()
        self._thread = Thread(target=run_loop, args=(self._stop_event, self.cfg_path), daemon=True, name="meme_bot_runner")
        self._thread.start()
        return True

    def stop(self, timeout_sec: float = 10.0) -> bool:
        if not self._thread:
            return False

        self._stop_event.set()
        self._thread.join(timeout=timeout_sec)
        return True

    def restart(self) -> None:
        self.stop()
        self.start()

    @property
    def is_alive(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def _load_state_ctx(self) -> tuple[Config, RuntimeStateStore, Any, StateMachine]:
        cfg = Config.load(self.cfg_path)
        store = RuntimeStateStore(cfg.get("state_store", "runtime_state_path"))
        state = store.load()
        machine = StateMachine(state)
        return cfg, store, state, machine

    def run(self, reason: str = "manual_control") -> dict[str, Any]:
        _, store, state, machine = self._load_state_ctx()
        machine.set_run()
        store.save(state)
        return {"ok": True, "mode": state.mode, "reason": reason}

    def pause(self, reason: str = "manual_control") -> dict[str, Any]:
        cfg, store, state, machine = self._load_state_ctx()
        pause_sec = int(cfg.get("risk", "pause_duration_sec", default=120))
        machine.set_pause(datetime.utcnow() + timedelta(seconds=pause_sec), reason)
        store.save(state)
        return {"ok": True, "mode": state.mode, "reason": reason}

    def safe(self, reason: str = "manual_control") -> dict[str, Any]:
        _, store, state, machine = self._load_state_ctx()
        machine.set_safe(reason)
        store.save(state)
        return {"ok": True, "mode": state.mode, "reason": reason}

    def status(self) -> dict[str, Any]:
        cfg = Config.load(self.cfg_path)
        store = RuntimeStateStore(cfg.get("state_store", "runtime_state_path"))
        state = store.load()
        return {
            "runner_started": self._thread is not None,
            "runner_alive": self.is_alive,
            "mode": state.mode,
            "pause_until_iso": state.pause_until_iso,
            "pause_reason": state.pause_reason,
            "safe_reason": state.safe_reason,
            "trade_count_today": state.trades_today,
            "daily_pnl_usd": state.daily_pnl_usd,
            "last_trade_ts_iso": state.last_trade_ts_iso,
            "last_price_ts_iso": state.last_price_ts_iso,
            "active_position": state.active_position,
        }
