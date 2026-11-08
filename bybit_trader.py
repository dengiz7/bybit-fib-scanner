# -*- coding: utf-8 -*-
"""
Bybit V5 Demo Trading Otomatik Al-Sat Motoru (%100 Ücretsiz & Sıfır Harici Kütüphane)
Resmi Bybit V5 Demo API: https://api-demo.bybit.com
- Hacim Patlaması & Zirve Kırılımı (Breakout Sniper) Stratejisine Göre Otomatik Emir Açma
- Otomatik 15x İzole Kaldıraç Ayarı (Parite sınırına göre ayarlanır)
- TP1'de (%1.6x ATR) Pozisyonun %75'ini Kârla Kapatıp Stop-Loss'u Başabaş (Giriş) Seviyesine Otomatik Çekme
- TP2'de (%2.8x ATR) Kalan %25 Pozisyonu Tamamen Kapatma
- Otomatik Risk Koruması & Acil Durum Senkronizasyonu
"""

import hashlib
import hmac
import json
import math
import ssl
import time
import urllib.parse
import urllib.request
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP
from typing import Dict, List, Optional, Tuple

from config import Config
from strategy import BreakoutSetup


class BybitDemoTrader:
    def __init__(
        self,
        api_key: Optional[str] = None,
        api_secret: Optional[str] = None,
        base_url: Optional[str] = None,
    ):
        Config.reload()
        self.api_key = (api_key if api_key is not None else Config.BYBIT_DEMO_API_KEY).strip()
        self.api_secret = (api_secret if api_secret is not None else Config.BYBIT_DEMO_API_SECRET).strip()
        self.base_url = (base_url if base_url is not None else Config.BYBIT_DEMO_BASE_URL).rstrip("/")
        self.enabled: bool = Config.DEMO_AUTO_TRADE_ENABLED
        self._ssl_ctx = ssl.create_default_context()
        self._inst_cache: Dict[str, Dict[str, str]] = {}
        self._time_offset_ms: Optional[int] = None

    @property
    def is_configured(self) -> bool:
        return bool(self.api_key and self.api_secret)

    def _get_timestamp_ms(self) -> str:
        """Bybit sunucu saati ile milisaniye bazında senkronize zaman damgası üretir."""
        local_ms = int(time.time() * 1000)
        if self._time_offset_ms is None:
            try:
                req = urllib.request.Request(
                    f"{self.base_url}/v5/market/time",
                    headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"},
                )
                with urllib.request.urlopen(req, timeout=5, context=self._ssl_ctx) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                    srv_nano = (data.get("result") or {}).get("timeNano")
                    srv_sec = (data.get("result") or {}).get("timeSecond")
                    if srv_nano:
                        srv_ms = int(int(srv_nano) / 1_000_000)
                        self._time_offset_ms = srv_ms - local_ms
                    elif srv_sec:
                        srv_ms = int(srv_sec) * 1000
                        self._time_offset_ms = srv_ms - local_ms
                    else:
                        self._time_offset_ms = 0
            except Exception:
                self._time_offset_ms = 0
        return str(local_ms + (self._time_offset_ms or 0))

    def _sign(self, timestamp: str, recv_window: str, payload_str: str) -> str:
        raw = f"{timestamp}{self.api_key}{recv_window}{payload_str}"
        return hmac.new(
            self.api_secret.encode("utf-8"),
            raw.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    def _request(
        self,
        method: str,
        path: str,
        params: Optional[Dict] = None,
        signed: bool = True,
        timeout: int = 10,
    ) -> Dict:
        url = f"{self.base_url}{path}"
        recv_window = "20000"
        ts = self._get_timestamp_ms()
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            ),
            "Accept": "application/json",
        }

        body_str = ""
        query_str = ""

        if method.upper() == "GET":
            if params:
                query_str = urllib.parse.urlencode(params)
                url = f"{url}?{query_str}"
            sign_payload = query_str
        else:
            headers["Content-Type"] = "application/json"
            if params:
                body_str = json.dumps(params)
            sign_payload = body_str

        if signed:
            if not self.is_configured:
                return {"retCode": -1, "retMsg": "API Key veya Secret eksik"}
            headers["X-BAPI-API-KEY"] = self.api_key
            headers["X-BAPI-TIMESTAMP"] = ts
            headers["X-BAPI-RECV-WINDOW"] = recv_window
            headers["X-BAPI-SIGN"] = self._sign(ts, recv_window, sign_payload)

        req_data = body_str.encode("utf-8") if (method.upper() != "GET" and body_str) else None
        req = urllib.request.Request(url, data=req_data, headers=headers, method=method.upper())

        try:
            with urllib.request.urlopen(req, timeout=timeout, context=self._ssl_ctx) as resp:
                raw_bytes = resp.read()
                return json.loads(raw_bytes.decode("utf-8"))
        except urllib.error.HTTPError as he:
            err_body = he.read().decode("utf-8", errors="replace")
            try:
                return json.loads(err_body)
            except Exception:
                return {"retCode": he.code, "retMsg": f"HTTP {he.code}: {err_body}"}
        except Exception as exc:
            return {"retCode": -999, "retMsg": str(exc)}

    def get_instrument_spec(self, symbol: str) -> Dict[str, str]:
        """Sembolün lot büyüklüğü ve fiyat basamak kurallarını çeker."""
        if symbol in self._inst_cache:
            return self._inst_cache[symbol]

        res = self._request(
            "GET",
            "/v5/market/instruments-info",
            {"category": "linear", "symbol": symbol},
            signed=False,
        )
        spec = {
            "tickSize": "0.0001",
            "qtyStep": "0.01",
            "minOrderQty": "0.01",
            "maxLeverage": "25",
        }
        if res.get("retCode") == 0:
            lst = (res.get("result") or {}).get("list") or []
            if lst:
                inst = lst[0]
                pf = inst.get("priceFilter") or {}
                lf = inst.get("lotSizeFilter") or {}
                lv = inst.get("leverageFilter") or {}
                spec["tickSize"] = str(pf.get("tickSize") or "0.0001")
                spec["qtyStep"] = str(lf.get("qtyStep") or "0.01")
                spec["minOrderQty"] = str(lf.get("minOrderQty") or "0.01")
                spec["maxLeverage"] = str(lv.get("maxLeverage") or "25")
                self._inst_cache[symbol] = spec
        return spec

    @staticmethod
    def round_step(val: float, step_str: str, round_down: bool = True) -> str:
        try:
            d_val = Decimal(str(val))
            d_step = Decimal(step_str)
            mode = ROUND_DOWN if round_down else ROUND_HALF_UP
            rounded = (d_val / d_step).quantize(Decimal("1"), rounding=mode) * d_step
            norm = rounded.normalize()
            if abs(norm) < Decimal("1e-4"):
                return f"{rounded:.8f}".rstrip("0").rstrip(".")
            return f"{norm:f}"
        except Exception:
            return f"{val:.4f}"

    def get_demo_account_summary(self) -> Optional[Dict[str, float]]:
        """Bybit Demo UNIFIED hesabındaki cüzdan bakiyesi ve açık PnL bilgisini döner."""
        if not self.is_configured:
            return None
        res = self._request("GET", "/v5/account/wallet-balance", {"accountType": "UNIFIED"}, signed=True)
        if res.get("retCode") != 0:
            return None
        lst = (res.get("result") or {}).get("list") or []
        if not lst:
            return None
        acc = lst[0]
        for coin_info in acc.get("coin") or []:
            if coin_info.get("coin") == "USDT":
                try:
                    wb = float(coin_info.get("walletBalance") or 0.0)
                    eq = float(coin_info.get("equity") or wb)
                    upnl = float(coin_info.get("unrealisedPnl") or (eq - wb))
                    return {
                        "wallet_balance": round(wb, 2),
                        "total_equity": round(eq, 2),
                        "unrealised_pnl": round(upnl, 2),
                    }
                except Exception:
                    break
        try:
            wb = float(acc.get("totalWalletBalance") or 0.0)
            eq = float(acc.get("totalEquity") or wb)
            upnl = float(acc.get("totalPerpUPL") or (eq - wb))
            return {
                "wallet_balance": round(wb, 2),
                "total_equity": round(eq, 2),
                "unrealised_pnl": round(upnl, 2),
            }
        except Exception:
            return None

    def get_demo_wallet_balance(self) -> Optional[float]:
        """Bybit Demo UNIFIED hesabındaki gerçek Net Kasa (Equity) değerini döner."""
        summary = self.get_demo_account_summary()
        if summary is None:
            return None
        return summary["total_equity"]

    def get_closed_pnl_records(self, start_time_ms: Optional[int] = None, limit: int = 50) -> List[Dict]:
        """Bybit üzerindeki gerçek kapanmış işlemlerin (komisyon düşülmüş net PnL dahil) listesini çeker."""
        if not self.is_configured:
            return []
        params: Dict[str, str] = {"category": "linear", "limit": str(limit)}
        if start_time_ms is not None and start_time_ms > 0:
            params["startTime"] = str(start_time_ms)
        res = self._request("GET", "/v5/position/closed-pnl", params, signed=True)
        if res.get("retCode") != 0:
            return []
        return (res.get("result") or {}).get("list") or []

    def get_open_positions(self, symbol: Optional[str] = None) -> List[Dict]:
        if not self.is_configured:
            return []
        params: Dict[str, str] = {"category": "linear", "settleCoin": "USDT"}
        if symbol:
            params["symbol"] = symbol
        res = self._request("GET", "/v5/position/list", params, signed=True)
        if res.get("retCode") != 0:
            return []
        raw_list = (res.get("result") or {}).get("list") or []
        open_pos = []
        for p in raw_list:
            try:
                sz = float(p.get("size") or 0.0)
                if sz > 0:
                    open_pos.append(p)
            except Exception:
                continue
        return open_pos

    def get_open_orders(self, symbol: Optional[str] = None) -> List[Dict]:
        if not self.is_configured:
            return []
        params: Dict[str, str] = {"category": "linear", "settleCoin": "USDT"}
        if symbol:
            params["symbol"] = symbol
        res = self._request("GET", "/v5/order/realtime", params, signed=True)
        if res.get("retCode") != 0:
            return []
        return (res.get("result") or {}).get("list") or []

    def ensure_leverage(self, symbol: str, leverage: int = Config.DEFAULT_LEVERAGE) -> int:
        """Paritede kaldıracı ayarlar (İzole modda, parite üst limitine göre)."""
        spec = self.get_instrument_spec(symbol)
        try:
            max_lev = int(float(spec.get("maxLeverage", "30")))
        except Exception:
            max_lev = 30
        eff_lev = min(leverage, max(5, max_lev))
        lev_str = str(eff_lev)

        self._request(
            "POST",
            "/v5/position/switch-isolated",
            {
                "category": "linear",
                "symbol": symbol,
                "tradeMode": 1,  # 1: Isolated, 0: Cross
                "buyLeverage": lev_str,
                "sellLeverage": lev_str,
            },
            signed=True,
        )
        self._request(
            "POST",
            "/v5/position/set-leverage",
            {
                "category": "linear",
                "symbol": symbol,
                "buyLeverage": lev_str,
                "sellLeverage": lev_str,
            },
            signed=True,
        )
        return eff_lev

    def cancel_all_symbol_orders(self, symbol: str) -> None:
        if not self.is_configured:
            return
        self._request(
            "POST",
            "/v5/order/cancel-all",
            {"category": "linear", "symbol": symbol},
            signed=True,
        )

    def cancel_all_open_orders(self) -> int:
        if not self.is_configured:
            return 0
        open_ords = self.get_open_orders()
        cancelled_cnt = 0
        symbols = set(o.get("symbol") for o in open_ords if o.get("symbol"))
        for sym in symbols:
            res = self._request(
                "POST",
                "/v5/order/cancel-all",
                {"category": "linear", "symbol": sym},
                signed=True,
            )
            if res.get("retCode") == 0:
                cancelled_cnt += 1
        return cancelled_cnt

    def close_all_open_positions(self) -> int:
        if not self.is_configured:
            return 0
        open_pos = self.get_open_positions()
        closed_cnt = 0
        for p in open_pos:
            sym = p.get("symbol")
            side = p.get("side")
            sz = p.get("size")
            if not sym or not sz or float(sz) <= 0:
                continue
            close_side = "Sell" if side == "Buy" else "Buy"
            res = self._request(
                "POST",
                "/v5/order/create",
                {
                    "category": "linear",
                    "symbol": sym,
                    "side": close_side,
                    "orderType": "Market",
                    "qty": sz,
                    "reduceOnly": True,
                    "positionIdx": 0,
                },
                signed=True,
            )
            if res.get("retCode") == 0:
                closed_cnt += 1
        return closed_cnt

    def reset_demo_account(self, target_usd: float = 100.0) -> Tuple[int, int, float]:
        """
        1) Tüm açık Demo pozisyonları kapatır,
        2) Tüm bekleyen emirleri iptal eder,
        3) Bybit V5 '/v5/account/demo-apply-money' ile gerçek Demo USDT bakiyesini hedef tutara (ör. 100$) eşitler!
        """
        if not self.is_configured:
            return 0, 0, target_usd
        closed_pos = self.close_all_open_positions()
        cancelled_ords = self.cancel_all_open_orders()
        time.sleep(0.8)
        summary = self.get_demo_account_summary()
        cur_wb = summary["wallet_balance"] if summary else target_usd
        diff = int(round(target_usd - cur_wb))
        if abs(diff) >= 1:
            adj_type = 0 if diff > 0 else 1
            self._request(
                "POST",
                "/v5/account/demo-apply-money",
                {
                    "adjustType": adj_type,
                    "utaDemoApplyMoney": [
                        {
                            "coin": "USDT",
                            "amountStr": str(abs(diff)),
                        }
                    ],
                },
                signed=True,
            )
            time.sleep(0.6)
            summary = self.get_demo_account_summary()
            cur_wb = summary["wallet_balance"] if summary else target_usd
        return closed_pos, cancelled_ords, round(cur_wb, 2)

    def place_setup_order(
        self,
        setup: BreakoutSetup,
        alert_type: str = "BREAKOUT",
        virtual_bankroll_usd: float = Config.DEFAULT_BANKROLL_USD,
    ) -> Tuple[bool, str]:
        """
        Hacim Patlamalı Zirve Kırılımı gerçekleştiğinde Bybit Demo üzerinde
        30x (veya parite limitinde) izole kaldıraçla anında Market emir açar ve TP2 / SL seviyelerini yerleştirir!
        """
        if not self.is_configured or not self.enabled:
            return False, ""

        symbol = setup.symbol
        direction = setup.direction
        side = "Buy" if direction == "LONG" else "Sell"
        lev = Config.DEFAULT_LEVERAGE

        # 1) Bu paritede zaten açık pozisyon varsa tekrar açma
        existing_pos = self.get_open_positions(symbol=symbol)
        if existing_pos:
            return False, f"ℹ️ <b>Bybit Demo:</b> #{symbol} paritesinde zaten açık pozisyon mevcut."

        # 2) Eşzamanlı açık pozisyon sınırı (0 = Sınırsız, bakiye yettiğince açar)
        if Config.MAX_CONCURRENT_DEMO_TRADES > 0:
            all_pos = self.get_open_positions()
            if len(all_pos) >= Config.MAX_CONCURRENT_DEMO_TRADES:
                return (
                    False,
                    f"⚠️ <b>Bybit Demo Kasa Koruması:</b> Maksimum {Config.MAX_CONCURRENT_DEMO_TRADES} açık pozisyon sınırına ulaşıldığı için #{symbol} emri pas geçildi.",
                )

        eff_lev = self.ensure_leverage(symbol, lev)
        spec = self.get_instrument_spec(symbol)

        margin_usd = max(4.0, setup.recommended_margin_usd)
        notional_usd = margin_usd * eff_lev
        entry_ref = setup.entry_price
        if entry_ref <= 0:
            return False, ""

        raw_qty = notional_usd / entry_ref
        qty_str = self.round_step(raw_qty, spec["qtyStep"], round_down=True)
        min_qty = float(spec["minOrderQty"])
        if float(qty_str) < min_qty:
            qty_str = spec["minOrderQty"]

        sl_str = self.round_step(setup.sl_price, spec["tickSize"], round_down=(direction == "LONG"))
        tp1_str = self.round_step(setup.tp1_price, spec["tickSize"], round_down=(direction == "SHORT"))
        tp2_str = self.round_step(setup.tp2_price, spec["tickSize"], round_down=(direction == "SHORT"))

        self.cancel_all_symbol_orders(symbol)
        order_params = {
            "category": "linear",
            "symbol": symbol,
            "side": side,
            "orderType": "Market",
            "qty": qty_str,
            "takeProfit": tp2_str,
            "stopLoss": sl_str,
            "tpTriggerBy": "LastPrice",
            "slTriggerBy": "LastPrice",
            "positionIdx": 0,
        }
        res = self._request("POST", "/v5/order/create", order_params, signed=True)
        if res.get("retCode") == 0:
            est_sl_usd = notional_usd * (setup.sl_pct / 100.0)
            return (
                True,
                f"🤖 <b>BYBIT DEMO OTOMATİK İŞLEM AÇILDI!</b>\n"
                f"  • <b>Tür:</b> <code>Hacim Patlaması & Kırılım ({side})</code> | <b>Miktar:</b> <code>{qty_str}</code>\n"
                f"  • <b>Kırılan Seviye:</b> <code>{setup.breakout_level}</code> | <b>Hacim:</b> <code>{setup.rvol:.1f}x RVOL</code>\n"
                f"  • <b>Kademeli Hedefler:</b> TP1 (%75): <code>{tp1_str}</code> (+%{setup.tp1_pct:.2f}) | TP2: <code>{tp2_str}</code> (+%{setup.tp2_pct:.2f})\n"
                f"  • <b>Otomatik Stop-Loss:</b> <code>{sl_str}</code> <i>(Maks. Risk: -{est_sl_usd:.2f}$)</i>\n"
                f"  • <b>Kullanılan Marjin:</b> <b><code>{margin_usd:.2f}$</code></b> ({eff_lev}x ➡️ <code>{notional_usd:.0f}$</code> Büyüklük)",
            )
        return False, f"⚠️ <b>Bybit Demo Giriş Hatası:</b> <code>{res.get('retMsg')}</code>"

    def handle_trade_lifecycle_event(self, trade: Dict, event_type: str) -> str:
        """
        TP1, TP2, Stop veya Başabaş olduğunda Bybit Demo hesabındaki pozisyonu
        otomatik olarak kısmi kapatır ve Stop-Loss'u Giriş (Başabaş) seviyesine taşır!
        """
        if not self.is_configured or not self.enabled:
            return ""

        symbol = trade["symbol"]
        direction = trade["direction"]
        close_side = "Sell" if direction == "LONG" else "Buy"
        spec = self.get_instrument_spec(symbol)

        open_pos = self.get_open_positions(symbol=symbol)
        if not open_pos:
            self.cancel_all_symbol_orders(symbol)
            return ""

        pos = open_pos[0]
        cur_size = float(pos.get("size") or 0.0)
        avg_entry = float(pos.get("avgPrice") or trade.get("entry_price") or trade.get("entry_0618") or 0.0)
        if cur_size <= 0:
            return ""

        if event_type == "TP1":
            # 1) Pozisyonun %75'ini (Config.TP1_CLOSE_PCT) Market Reduce-Only ile kapatıp ana kârı cebe kilitle!
            tp1_pct_close = Config.TP1_CLOSE_PCT
            close_qty = self.round_step(cur_size * tp1_pct_close, spec["qtyStep"], round_down=True)
            if float(close_qty) < float(spec["minOrderQty"]):
                close_qty = self.round_step(cur_size, spec["qtyStep"], round_down=True)
            if float(close_qty) >= float(spec["minOrderQty"]):
                self._request(
                    "POST",
                    "/v5/order/create",
                    {
                        "category": "linear",
                        "symbol": symbol,
                        "side": close_side,
                        "orderType": "Market",
                        "qty": close_qty,
                        "reduceOnly": True,
                        "positionIdx": 0,
                    },
                    signed=True,
                )
            # 2) Kalan %25 pozisyonun Stop-Loss'unu Giriş Fiyatına (Başabaş) çek!
            be_price = self.round_step(avg_entry, spec["tickSize"], round_down=(direction == "LONG"))
            self._request(
                "POST",
                "/v5/position/trading-stop",
                {
                    "category": "linear",
                    "symbol": symbol,
                    "stopLoss": be_price,
                    "slTriggerBy": "LastPrice",
                    "positionIdx": 0,
                },
                signed=True,
            )
            return (
                f"🤖 <b>Bybit Demo Otomatik İşlem:</b> #{symbol} pozisyonunun <b>%{int(tp1_pct_close*100)}'i ({close_qty}) kârla kapatılıp cebe kilitlendi</b> "
                f"ve kalan %{int((1.0-tp1_pct_close)*100)} kısmın Stop-Loss'u <b>Giriş Fiyatına (<code>{be_price}</code> Başabaş)</b> çekildi!"
            )

        elif event_type in ("TP2", "TP3", "STOP_LOSS", "BREAKEVEN_STOP"):
            # Kalan tüm pozisyonu kapat ve bekleyen emirleri temizle
            close_qty = self.round_step(cur_size, spec["qtyStep"], round_down=True)
            if float(close_qty) >= float(spec["minOrderQty"]):
                self._request(
                    "POST",
                    "/v5/order/create",
                    {
                        "category": "linear",
                        "symbol": symbol,
                        "side": close_side,
                        "orderType": "Market",
                        "qty": close_qty,
                        "reduceOnly": True,
                        "positionIdx": 0,
                    },
                    signed=True,
                )
            self.cancel_all_symbol_orders(symbol)
            lbl = "kalan pozisyon kârla kapatıldı" if event_type in ("TP2", "TP3") else "pozisyon kapatıldı"
            return f"🤖 <b>Bybit Demo Otomatik İşlem:</b> #{symbol} {lbl} (<code>{close_qty}</code>)."

        return ""

    def sync_and_protect_open_positions(self, tracked_trades: Dict[str, Dict]) -> None:
        """
        Bybit Demo hesabındaki açık pozisyonları kontrol eder ve eksik TP/SL varsa koruma ekler.
        """
        if not self.is_configured or not self.enabled:
            return

        open_positions = self.get_open_positions()
        if not open_positions:
            return

        tracked_by_sym: Dict[str, Dict] = {
            tr.get("symbol", ""): tr for tr in tracked_trades.values() if tr.get("symbol")
        }

        for pos in open_positions:
            sym = str(pos.get("symbol") or "")
            if not sym:
                continue
            side = str(pos.get("side") or "Buy")
            direction = "LONG" if side == "Buy" else "SHORT"
            avg_price = float(pos.get("avgPrice") or 0.0)
            if avg_price <= 0:
                continue

            cur_tp = str(pos.get("takeProfit") or "").strip()
            cur_sl = str(pos.get("stopLoss") or "").strip()
            has_tp = bool(cur_tp and float(cur_tp) > 0)
            has_sl = bool(cur_sl and float(cur_sl) > 0)

            spec = self.get_instrument_spec(sym)
            tr = tracked_by_sym.get(sym)

            if tr is not None:
                target_tp = float(tr.get("tp2_price") or tr.get("tp2") or tr.get("tp1_price") or 0.0)
                target_sl = float(tr.get("current_sl") or tr.get("initial_sl") or 0.0)
            else:
                sl_dist = avg_price * 0.012
                tp1_dist = avg_price * 0.018
                tp2_dist = avg_price * 0.032
                if direction == "LONG":
                    target_sl = avg_price - sl_dist
                    tp1_v, target_tp = avg_price + tp1_dist, avg_price + tp2_dist
                else:
                    target_sl = avg_price + sl_dist
                    tp1_v, target_tp = avg_price - tp1_dist, avg_price - tp2_dist

                tid = f"{sym}_{direction}_SYNCED"
                tracked_trades[tid] = {
                    "symbol": sym,
                    "direction": direction,
                    "status": "IN_TRADE",
                    "entry_price": avg_price,
                    "breakout_level": avg_price,
                    "initial_sl": target_sl,
                    "current_sl": target_sl,
                    "tp1_price": tp1_v,
                    "tp2_price": target_tp,
                    "margin_usd": 20.0,
                    "rvol": 2.0,
                    "strength_score": 80,
                    "quality_grade": "A",
                    "hits": [],
                    "realized_usd": 0.0,
                    "notes": ["✅ Otomatik Pozisyon Koruma"],
                    "ts": int(time.time() * 1000),
                }

            if (not has_tp or not has_sl) and target_tp > 0 and target_sl > 0:
                tp_str = self.round_step(target_tp, spec["tickSize"], round_down=(direction == "SHORT"))
                sl_str = self.round_step(target_sl, spec["tickSize"], round_down=(direction == "LONG"))
                payload = {
                    "category": "linear",
                    "symbol": sym,
                    "positionIdx": 0,
                }
                if not has_tp:
                    payload["takeProfit"] = tp_str
                    payload["tpTriggerBy"] = "LastPrice"
                if not has_sl:
                    payload["stopLoss"] = sl_str
                    payload["slTriggerBy"] = "LastPrice"
                self._request("POST", "/v5/position/trading-stop", payload, signed=True)

    def format_demo_status_text(self, virtual_bankroll_usd: float) -> str:
        if not self.is_configured:
            return (
                "🤖 <b>BYBIT DEMO OTOMATİK AL-SAT DURUMU</b>\n"
                "━━━━━━━━━━━━━━━━━━━━━━\n"
                "⚠️ <b>Durum:</b> Henüz Demo API Anahtarı bağlanmadı.\n\n"
                "📌 <b>Bağlamak İçin:</b>\n"
                "Bybit Demo Trading ekranından aldığın API Key ve Secret değerlerini Render'daki "
                "<code>Environment</code> kısmına <code>BYBIT_DEMO_API_KEY</code> ve <code>BYBIT_DEMO_API_SECRET</code> "
                "olarak eklemen yeterli!"
            )

        acc_sum = self.get_demo_account_summary()
        open_pos = self.get_open_positions()

        if acc_sum is not None:
            wb_val = acc_sum["wallet_balance"]
            eq_val = acc_sum["total_equity"]
            upnl_val = acc_sum["unrealised_pnl"]
            wallet_str = (
                f"<b><code>{eq_val:.2f}$</code></b> "
                f"(Cüzdan: <code>{wb_val:.2f}$</code> | Açık PnL: <code>{upnl_val:+.2f}$</code>)"
            )
        else:
            wallet_str = f"<code>{virtual_bankroll_usd:.2f}$</code>"
        mode_str = "🟢 AKTİF (Hacim Kırılımlarında Otomatik Açıyor)" if self.enabled else "⏸ DURAKLATILDI (/oto_ac ile başlat)"

        pos_lines = []
        for p in open_pos[:25]:
            sym = p.get("symbol")
            side = "🟢 LONG" if p.get("side") == "Buy" else "🔴 SHORT"
            sz = p.get("size")
            avg_p = p.get("avgPrice")
            upnl = float(p.get("unrealisedPnl") or 0.0)
            tp_p = p.get("takeProfit") or "Oto"
            sl_p = p.get("stopLoss") or "Oto"
            pos_lines.append(
                f"  • <b>#{sym}</b> ({side}) | Miktar: <code>{sz}</code> | Giriş: <code>{avg_p}</code> | "
                f"TP/SL: <code>{tp_p} / {sl_p}</code> | PnL: <b><code>{upnl:+.2f}$</code></b>"
            )
        if not pos_lines:
            pos_lines.append("  • <i>Şu an açık Demo pozisyon bulunmuyor.</i>")

        limit_str = (
            str(Config.MAX_CONCURRENT_DEMO_TRADES)
            if Config.MAX_CONCURRENT_DEMO_TRADES > 0
            else "Sınırsız (Bakiye El Verdiğince Açılır)"
        )

        return (
            f"🤖 <b>BYBIT DEMO HESABI VE OTOMATİK İŞLEM DURUMU</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"⚙️ <b>Otomatik Al-Sat:</b> {mode_str}\n"
            f"💼 <b>Kullanılan Kasa Bütçesi:</b> <b><code>{virtual_bankroll_usd:.2f}$</code></b> <i>(İstediğin an <code>/kasa 100</code> yazarak sıfırlayabilirsin)</i>\n"
            f"🏦 <b>Bybit Demo Cüzdan Bakiyesi:</b> <code>{wallet_str}</code>\n"
            f"🛡 <b>Eşzamanlı İşlem Sınırı:</b> <code>{limit_str}</code>\n\n"
            f"📈 <b>AÇIK DEMO POZİSYONLAR ({len(open_pos)}):</b>\n"
            + "\n".join(pos_lines)
        )
