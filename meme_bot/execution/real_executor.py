from __future__ import annotations

from datetime import datetime

from meme_bot.execution.base import Executor
from meme_bot.models import TradeResult


class RealExecutor(Executor):
    """Stub for real trading. Intentionally not implemented."""

    def execute(self, symbol: str, token_address: str, side: str, size_usd: float, price_usd: float) -> TradeResult:
        # TODO(user): Implement secure transaction flow:
        # 1) Build chain-specific transaction payload.
        # 2) Sign with trade wallet private key (vault wallet MUST never trade).
        # 3) Broadcast and confirm transaction with timeout handling.
        # 4) Return exact fill details from on-chain/execution reports.
        return TradeResult(
            symbol=symbol,
            side=side,
            requested_usd=size_usd,
            filled_usd=0.0,
            avg_price_usd=price_usd,
            tx_id="TODO_REAL_TX_ID",
            status="not_implemented",
            reason="real_executor_stub",
            timestamp=datetime.utcnow(),
            metadata={"token_address": token_address},
        )
