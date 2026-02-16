from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Dict, Optional


class BotMode(str, Enum):
    RUN = "RUN"
    PAUSE = "PAUSE"
    SAFE = "SAFE"


@dataclass
class CandidateToken:
    symbol: str
    name: str
    address: str
    pair_address: str
    dex_id: str
    chain_id: str
    price_usd: float
    liquidity_usd: float
    volume_5m_usd: float
    momentum_score: float
    last_update_ts: datetime
    price_change_m5: float = 0.0
    price_change_m15: float = 0.0
    price_change_h1: float = 0.0


@dataclass
class Position:
    symbol: str
    token_address: str
    size_usd: float
    entry_price_usd: float
    opened_at: datetime
    token_name: str = ""
    pair_address: str = ""
    dex_id: str = ""
    chain_id: str = ""
    highest_unrealized_usd: float = 0.0
    quick_profit_taken: bool = False
    last_scale_in_at: Optional[datetime] = None
    last_fill_price_usd: float = 0.0
    max_favorable_usd: float = 0.0
    max_adverse_usd: float = 0.0
    fees_paid_usd: float = 0.0
    realized_net_usd: float = 0.0
    last_unrealized_usd: float = 0.0
    fee_blocked_count: int = 0
    quick_partial_at: Optional[datetime] = None
    add_count: int = 0
    last_high_price_usd: float = 0.0
    last_high_after_partial_usd: float = 0.0
    range_high_price_usd: float = 0.0
    range_low_price_usd: float = 0.0
    range_window_start: Optional[datetime] = None
    max_hold_extend_until: Optional[datetime] = None
    max_hold_extend_count: int = 0
    awaiting_post_add_confirm: bool = False
    post_add_confirm_until: Optional[datetime] = None
    adds_blocked: bool = False
    flat_adds_used: int = 0
    exit_in_flight: bool = False
    pnl_accounted_usd: float = 0.0
    stop_trigger_first_detected_at: Optional[datetime] = None
    stop_trigger_last_detected_at: Optional[datetime] = None
    stop_triggered_price_usd: float = 0.0
    stop_order_submitted_at: Optional[datetime] = None
    stop_price_at_detection: float = 0.0
    stop_price_at_order: float = 0.0


@dataclass
class TradeResult:
    symbol: str
    side: str
    requested_usd: float
    filled_usd: float
    avg_price_usd: float
    tx_id: str
    status: str
    reason: str
    timestamp: datetime
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class RuntimeState:
    mode: BotMode = BotMode.RUN
    pause_until_iso: Optional[str] = None
    pause_reason: Optional[str] = None
    daily_pnl_usd: float = 0.0
    hourly_pnl_usd: float = 0.0
    trades_today: int = 0
    last_trade_ts_iso: Optional[str] = None
    active_position: Optional[Dict[str, Any]] = None
    last_price_ts_iso: Optional[str] = None
    safe_reason: Optional[str] = None
    pnl_day_realized_usd: float = 0.0
    pnl_hour_realized_usd: float = 0.0
    pnl_total_realized_usd: float = 0.0
    pnl_day_bucket_local: Optional[str] = None
    pnl_hour_bucket_local: Optional[str] = None
    open_unrealized_usd: float = 0.0
    equity_usd: float = 0.0
