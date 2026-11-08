# -*- coding: utf-8 -*-
"""
Bybit V5 Public Market Data İstemcisi (0$ - API Anahtarı Gerektirmez)
"""

import json
import ssl
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from config import Config


@dataclass
class Candle:
    timestamp: int      # Açılış zamanı (ms)
    open: float
    high: float
    low: float
    close: float
    volume: float
    turnover: float
    is_closed: bool = True


@dataclass
class TickerInfo:
    symbol: str
    last_price: float
    turnover_24h: float
    price_24h_pcnt: float


EXCLUDED_SYMBOLS = {
    "USDCUSDT", "FDUSDUSDT", "USDEUSDT", "DAIUSDT", "TUSDUSDT",
    "PYUSDUSDT", "USDDUSDT", "EURUSDT", "GBPUSDT", "CLUSDT",
    "BZUSDT", "XAUUSDT", "XAGUSDT", "PAXGUSDT", "XAUTUSDT",
    "SOXLUSDT", "SOXSUSDT", "NVDAUSDT", "TSLAUSDT", "AAPLUSDT",
    "COINUSDT", "MSTRUSDT",
}


class BybitClient:
    BASE_URLS = [
        "https://api.bybit.com",
        "https://api.bytick.com",
    ]

    def __init__(self, timeout: int = 10):
        self.timeout = timeout
        self._active_base_Idx = 0
        self._ssl_ctx = ssl.create_default_context()
        self._headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            ),
            "Accept": "application/json",
        }

    def _get_json(self, path: str, params: Dict[str, str]) -> Optional[dict]:
        query = urllib.parse.urlencode(params)
        for attempt in range(len(self.BASE_URLS) * 2):
            base_url = self.BASE_URLS[(self._active_base_Idx + attempt) % len(self.BASE_URLS)]
            url = f"{base_url}{path}?{query}"
            req = urllib.request.Request(url, headers=self._headers, method="GET")
            try:
                with urllib.request.urlopen(req, timeout=self.timeout, context=self._ssl_ctx) as resp:
                    raw = resp.read().decode("utf-8")
                    data = json.loads(raw)
                    if data.get("retCode") == 0:
                        self._active_base_Idx = (self._active_base_Idx + attempt) % len(self.BASE_URLS)
                        return data
            except Exception:
                time.sleep(0.15)
        return None

    def get_top_usdt_perpetuals(
        self,
        top_n: int = Config.TOP_SYMBOLS_COUNT,
        min_turnover: float = Config.MIN_24H_TURNOVER_USD,
    ) -> List[TickerInfo]:
        """Bybit USDT Vadeli (Linear Perpetual) piyasasındaki en yüksek hacimli pariteleri döner."""
        data = self._get_json("/v5/market/tickers", {"category": "linear"})
        if not data or "result" not in data or "list" not in data["result"]:
            return []

        tickers: List[TickerInfo] = []
        for item in data["result"]["list"]:
            symbol = str(item.get("symbol", ""))
            if not symbol.endswith("USDT") or "-" in symbol or symbol in EXCLUDED_SYMBOLS:
                continue
            try:
                last_price = float(item.get("lastPrice") or 0.0)
                turnover_24h = float(item.get("turnover24h") or 0.0)
                price_24h_pcnt = float(item.get("price24hPcnt") or 0.0) * 100.0
            except (ValueError, TypeError):
                continue

            if last_price <= 0 or turnover_24h < min_turnover:
                continue

            tickers.append(
                TickerInfo(
                    symbol=symbol,
                    last_price=last_price,
                    turnover_24h=turnover_24h,
                    price_24h_pcnt=price_24h_pcnt,
                )
            )

        tickers.sort(key=lambda t: t.turnover_24h, reverse=True)
        return tickers[:top_n]

    def get_klines(
        self,
        symbol: str,
        interval: str = Config.TIMEFRAME,
        limit: int = Config.KLINE_LIMIT,
    ) -> List[Candle]:
        data = self._get_json(
            "/v5/market/kline",
            {
                "category": "linear",
                "symbol": symbol,
                "interval": str(interval),
                "limit": str(limit),
            },
        )
        if not data or "result" not in data or "list" not in data["result"]:
            return []

        raw_list = data["result"]["list"]
        if not raw_list:
            return []

        raw_list_chrono = list(reversed(raw_list))
        candles: List[Candle] = []
        total = len(raw_list_chrono)
        for idx, row in enumerate(raw_list_chrono):
            try:
                candles.append(
                    Candle(
                        timestamp=int(row[0]),
                        open=float(row[1]),
                        high=float(row[2]),
                        low=float(row[3]),
                        close=float(row[4]),
                        volume=float(row[5]),
                        turnover=float(row[6]),
                        is_closed=(idx < total - 1),
                    )
                )
            except (ValueError, IndexError):
                continue
        return candles

    def fetch_all_klines_parallel(
        self,
        tickers: List[TickerInfo],
        interval: str = Config.TIMEFRAME,
        limit: int = Config.KLINE_LIMIT,
        max_workers: int = 8,
    ) -> Dict[str, Tuple[TickerInfo, List[Candle]]]:
        results: Dict[str, Tuple[TickerInfo, List[Candle]]] = {}
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_map = {
                executor.submit(self.get_klines, t.symbol, interval, limit): t
                for t in tickers
            }
            for fut in as_completed(future_map):
                ticker = future_map[fut]
                try:
                    candles = fut.result()
                    if len(candles) >= 60:
                        results[ticker.symbol] = (ticker, candles)
                except Exception:
                    continue
        return results
