from __future__ import annotations

from meme_bot.utils.timefmt import now_utc


class MarketDataService:
    def __init__(self, indexer):
        self.indexer = indexer
        self.last_update_ts = None

    def get_candidates(self):
        tokens = self.indexer.fetch_universe()
        self.last_update_ts = now_utc()
        return tokens
