from __future__ import annotations

from datetime import datetime

from meme_bot.models import CandidateToken


class MarketDataService:
    def __init__(self, indexer):
        self.indexer = indexer
        self.last_update_ts: datetime | None = None

    def get_candidates(self) -> list[CandidateToken]:
        tokens = self.indexer.fetch_universe()
        self.last_update_ts = datetime.utcnow()
        return tokens
