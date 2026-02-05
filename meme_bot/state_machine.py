from __future__ import annotations

from datetime import datetime, timezone

from meme_bot.models import BotMode, RuntimeState


class StateMachine:
    def __init__(self, state: RuntimeState):
        self.state = state

    def set_run(self) -> None:
        self.state.mode = BotMode.RUN
        self.state.pause_until_iso = None
        self.state.pause_reason = None
        self.state.safe_reason = None

    def set_pause(self, until: datetime, reason: str) -> None:
        self.state.mode = BotMode.PAUSE
        self.state.pause_until_iso = until.astimezone(timezone.utc).isoformat()
        self.state.pause_reason = reason

    def set_safe(self, reason: str) -> None:
        self.state.mode = BotMode.SAFE
        self.state.safe_reason = reason

    def is_pause_over(self, now: datetime) -> bool:
        if self.state.mode != BotMode.PAUSE or not self.state.pause_until_iso:
            return False
        return now.astimezone(timezone.utc) >= datetime.fromisoformat(self.state.pause_until_iso)
