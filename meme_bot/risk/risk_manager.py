from __future__ import annotations

from datetime import datetime, timedelta

from meme_bot.models import CandidateToken, RuntimeState


class RiskManager:
    def __init__(self, cfg: dict):
        self.cfg = cfg

    def filter_candidates(self, candidates: list[CandidateToken]) -> list[CandidateToken]:
        min_liq = float(self.cfg["min_liquidity_usd"])
        min_vol = float(self.cfg["min_volume_5m_usd"])
        return [c for c in candidates if c.liquidity_usd >= min_liq and c.volume_5m_usd >= min_vol]

    def can_open_trade(self, state: RuntimeState, now: datetime) -> tuple[bool, str]:
        if state.trades_today >= int(self.cfg["max_trades_per_day"]):
            return False, "max_trades_per_day"
        if state.daily_pnl_usd <= -float(self.cfg["max_loss_per_day_usd"]):
            return False, "max_loss_per_day"
        if state.hourly_pnl_usd <= -float(self.cfg["max_loss_per_hour_usd"]):
            return False, "max_loss_per_hour"

        if state.last_trade_ts_iso:
            last = datetime.fromisoformat(state.last_trade_ts_iso)
            if now - last < timedelta(seconds=int(self.cfg["trade_cooldown_sec"])):
                return False, "trade_cooldown"

        return True, "ok"

    def slippage_ok(self, quote_price: float, index_price: float) -> bool:
        if index_price <= 0:
            return False
        slippage = abs(quote_price - index_price) / index_price * 10_000
        return slippage <= float(self.cfg["slippage_limit_bps"])
