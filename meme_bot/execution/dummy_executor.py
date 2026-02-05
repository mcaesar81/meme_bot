from __future__ import annotations

from datetime import datetime
from uuid import uuid4

from meme_bot.execution.base import Executor
from meme_bot.models import TradeResult


class DummyExecutor(Executor):
    """Paper trading executor with deterministic fill behavior."""

    def execute(self, symbol: str, token_address: str, side: str, size_usd: float, price_usd: float) -> TradeResult:
        return TradeResult(
            symbol=symbol,
            side=side,
            requested_usd=size_usd,
            filled_usd=size_usd,
            avg_price_usd=price_usd,
            tx_id=f"paper-{uuid4()}",
            status="filled",
            reason="paper_fill",
            timestamp=datetime.utcnow(),
            metadata={"token_address": token_address},
        )
