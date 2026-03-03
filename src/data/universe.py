"""
AI Trader US - Universe Manager

Manages stock universe (S&P 500, S&P 400 MidCap, etc.)
"""

from typing import List, Dict, Optional
from loguru import logger

from src.data.providers.yfinance import YFinanceProvider


class UniverseManager:
    """Stock universe management"""

    def __init__(self, provider: YFinanceProvider = None, config: dict = None):
        self._provider = provider or YFinanceProvider()
        self._config = config or {}
        self._cache: Dict[str, List[str]] = {}

    def get_universe(self, pools: List[str] = None) -> List[str]:
        """Get combined universe from specified pools"""
        if pools is None:
            pools = self._config.get("pools", ["sp500"])

        all_tickers = []
        for pool in pools:
            if pool not in self._cache:
                self._cache[pool] = self._provider.get_universe(pool)
            all_tickers.extend(self._cache[pool])

        # Deduplicate
        unique = sorted(set(all_tickers))
        logger.info(f"Universe: {len(unique)} tickers from {pools}")
        return unique

    def get_sp500(self) -> List[str]:
        return self.get_universe(["sp500"])

    def get_full_universe(self) -> List[str]:
        return self.get_universe(["sp500", "sp400"])
