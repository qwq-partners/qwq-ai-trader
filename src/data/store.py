"""
AI Trader US - Data Store

Parquet-based local data cache with incremental download.
"""

from datetime import date, timedelta
from pathlib import Path
from typing import Optional
import pandas as pd
from loguru import logger


class DataStore:
    """Local Parquet file cache for price data"""

    def __init__(self, data_dir: str = None):
        if data_dir is None:
            data_dir = Path(__file__).parent.parent.parent / "data" / "prices"
        self._dir = Path(data_dir)
        self._dir.mkdir(parents=True, exist_ok=True)

    def _path(self, symbol: str, interval: str = "daily") -> Path:
        safe_symbol = symbol.replace('/', '_').replace('.', '_')
        return self._dir / f"{safe_symbol}_{interval}.parquet"

    def load(self, symbol: str, interval: str = "daily") -> Optional[pd.DataFrame]:
        """Load cached data"""
        path = self._path(symbol, interval)
        if not path.exists():
            return None
        try:
            df = pd.read_parquet(path)
            return df
        except Exception as e:
            logger.warning(f"Failed to load {path}: {e}")
            return None

    def save(self, symbol: str, df: pd.DataFrame, interval: str = "daily"):
        """Save data to parquet"""
        if df.empty:
            return
        path = self._path(symbol, interval)
        try:
            df.to_parquet(path, engine='pyarrow')
            logger.debug(f"Saved {symbol} ({interval}): {len(df)} bars -> {path}")
        except Exception as e:
            logger.error(f"Failed to save {path}: {e}")

    def get_last_date(self, symbol: str, interval: str = "daily") -> Optional[date]:
        """Get the last date in cached data"""
        df = self.load(symbol, interval)
        if df is None or df.empty:
            return None
        return df.index[-1].date() if hasattr(df.index[-1], 'date') else df.index[-1]

    def update(self, symbol: str, new_data: pd.DataFrame, interval: str = "daily"):
        """Incrementally update cached data"""
        existing = self.load(symbol, interval)
        if existing is not None and not existing.empty:
            combined = pd.concat([existing, new_data])
            combined = combined[~combined.index.duplicated(keep='last')]
            combined = combined.sort_index()
        else:
            combined = new_data
        self.save(symbol, combined, interval)

    def list_symbols(self, interval: str = "daily") -> list:
        """List all cached symbols"""
        suffix = f"_{interval}.parquet"
        return [
            f.stem.replace(suffix.replace('.parquet', ''), '').rstrip('_')
            for f in self._dir.glob(f"*{suffix}")
        ]
