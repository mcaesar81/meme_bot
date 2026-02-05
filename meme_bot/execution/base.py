from __future__ import annotations

from abc import ABC, abstractmethod

from meme_bot.models import TradeResult


class Executor(ABC):
    @abstractmethod
    def execute(self, symbol: str, token_address: str, side: str, size_usd: float, price_usd: float) -> TradeResult:
        raise NotImplementedError
