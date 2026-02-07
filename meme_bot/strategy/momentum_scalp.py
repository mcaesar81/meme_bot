from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional

from meme_bot.models import CandidateToken, Position


class MomentumScalpStrategy:
    def __init__(self, cfg: dict):
        self.cfg = cfg

    def pick_entry(self, candidates: list[CandidateToken]) -> Optional[CandidateToken]:
        if not candidates:
            return None
        return sorted(candidates, key=lambda c: c.momentum_score, reverse=True)[0]

    def initial_entry_size(self) -> float:
        return float(self.cfg["test_entry_usd"])

    def maybe_scale_in_size(self, position: Position, now: datetime, unrealized_usd: float) -> float:
        if not self.cfg.get("allow_scale_in", False):
            return 0.0
        if unrealized_usd < 0:
            return 0.0
        if position.size_usd >= float(self.cfg["max_position_usd"]):
            return 0.0

        if position.last_scale_in_at and (now - position.last_scale_in_at) < timedelta(seconds=20):
            return 0.0

        step = float(self.cfg["scale_in_step_usd"])
        remaining = float(self.cfg["max_position_usd"]) - position.size_usd
        return max(0.0, min(step, remaining))

    def should_exit(
        self,
        position: Position,
        now: datetime,
        unrealized_usd: float,
        stall_override_sec: Optional[float] = None,
    ) -> tuple[bool, str]:
        if unrealized_usd <= -float(self.cfg["stop_loss_usd"]):
            return True, "stop_loss"

        hold_sec = (now - position.opened_at).total_seconds()
        if hold_sec >= float(self.cfg["max_hold_sec"]):
            return True, "max_hold"

        stall_limit = float(self.cfg["stall_exit_sec"])
        if position.quick_profit_taken:
            stall_limit = float(self.cfg["tighten_stall_after_quick_profit_sec"])
        if stall_override_sec is not None:
            stall_limit = min(stall_limit, stall_override_sec)

        if position.last_scale_in_at and (now - position.last_scale_in_at).total_seconds() >= stall_limit:
            return True, "stall_exit"

        return False, "hold"

    def quick_profit_partial_size(self, position: Position, unrealized_usd: float) -> float:
        if position.quick_profit_taken:
            return 0.0
        if unrealized_usd < float(self.cfg["quick_profit_usd"]):
            return 0.0
        return round(position.size_usd * float(self.cfg["quick_profit_partial_ratio"]), 4)
