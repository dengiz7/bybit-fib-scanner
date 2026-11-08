# -*- coding: utf-8 -*-
"""
Bybit V5 Demo Trading Otomatik Al-Sat Motoru (%100 Ücretsiz & Sıfır Harici Kütüphane)
Resmi Bybit V5 Demo API: https://api-demo.bybit.com
- 100$ Sanal Kasa Bütçesiyle Kaliteye Göre (A+ -> %20, A -> %15, B -> %8) Otomatik Emir Açma
- Otomatik 10x İzole Kaldıraç Ayarı
- Aşama 1'de 0.618 Limit Emir + Otomatik Stop-Loss
- Aşama 2'de Altın Bölge Aktif Giriş + Otomatik Stop-Loss
- TP1'de %50 Kâr Al + Stop-Loss'u Başabaş (Giriş) Seviyesine Otomatik Çekme
- TP2'de %25 Kâr Al + Stop-Loss'u TP1 Seviyesine Otomatik Taşıma
- TP3'te Kalan Pozisyonu Tamamen Kapatma
- Altın Bölgeye Dönmeden Giden (MISSED_NO_PULLBACK) Limit Emirleri Otomatik İptal Etme
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
from strategy import FibSetup


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

        if method.upper() == "GET":
            query_str = urllib.parse.urlencode(params or {})
            if query_str:
                url = f"{url}?{query_str}"
            payload_str = query_str
            data_bytes = None
        else:
            payload_str = json.dumps(params or {}, separators=(",", ":"))
            data_bytes = payload_str.encode("utf-8")
            headers["Content-Type"] = "application/json"

        if signed:
            if not self.is_configured:
                return {"retCode": -1, "retMsg": "Demo API Key/Secret ayarlanmamış"}
            sig = self._sign(ts, recv_window, payload_str)
            headers.update(
                {
                    "X-BAPI-API-KEY": self.api_key,
                    "X-BAPI-TIMESTAMP": ts,
                    "X-BAPI-RECV-WINDOW": recv_window,
                    "X-BAPI-SIGN": sig,
                }
            )

        try:
            req = urllib.request.Request(url, data=data_bytes, headers=headers, method=method.upper())
            with urllib.request.urlopen(req, timeout=timeout, context=self._ssl_ctx) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as exc:
            return {"retCode": -1, "retMsg": str(exc)}

    def get_instrument_spec(self, symbol: str) -> Dict[str, str]:
        """Paritenin lot adımı (qtyStep), min miktar (minOrderQty) ve fiyat adımı (tickSize) kurallarını çeker."""
        if symbol in self._inst_cache:
            return self._inst_cache[symbol]

        res = self._request(
            "GET",
            "/v5/market/instruments-info",
            {"category": "linear", "symbol": symbol},
            signed=False,
        )
        items = (res.get("result") or {}).get("list") or []
        if items:
            item = items[0]
            lot = item.get("lotSizeFilter") or {}
            pf = item.get("priceFilter") or {}
            lf = item.get("leverageFilter") or {}
            spec = {
                "qtyStep": str(lot.get("qtyStep", "0.001")),
                "minOrderQty": str(lot.get("minOrderQty", "0.001")),
                "tickSize": str(pf.get("tickSize", "0.0001")),
                "maxLeverage": str(lf.get("maxLeverage", "25")),
            }
            self._inst_cache[symbol] = spec
            return spec

        return {"qtyStep": "0.01", "minOrderQty": "0.01", "tickSize": "0.0001", "maxLeverage": "25"}

    @staticmethod
    def round_step(value: float, step_str: str, round_down: bool = True) -> str:
        """Bybit lot/tick kurallarına göre ondalık yuvarlama yapar."""
        try:
            d_val = Decimal(str(value))
            d_step = Decimal(str(step_str))
            if d_step <= 0:
                return str(value)
            rounding = ROUND_DOWN if round_down else ROUND_HALF_UP
            units = (d_val / d_step).to_integral_value(rounding=rounding)
            res = units * d_step
            return format(res.normalize(), "f")
        except Exception:
            return str(round(value, 4))

    def get_demo_account_summary(self) -> Optional[Dict[str, float]]:
        """
        Bybit Demo UNIFIED hesabındaki gerçek Cüzdan Bakiyesini (walletBalance),
        Açık İşlemler Dahil Net Varlığı (equity) ve Anlık Açık PnL'i (unrealisedPnl) döner.
        """
        if not self.is_configured:
            return None
        res = self._request(
            "GET",
            "/v5/account/wallet-balance",
            {"accountType": "UNIFIED", "coin": "USDT"},
            signed=True,
        )
        if res.get("retCode") != 0:
            return None
        acc_list = (res.get("result") or {}).get("list") or []
        if not acc_list:
            return None

        acc = acc_list[0]
        for coin_info in acc.get("coin", []):
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
        """Paritede kaldıracı 25x (veya paritenin izin verdiği maksimum kaldıraç) olarak ayarlar."""
        spec = self.get_instrument_spec(symbol)
        try:
            max_lev = int(float(spec.get("maxLeverage", "25")))
        except Exception:
            max_lev = 25
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

    def cancel_all_symbol_orders(self, symbol: str) -> bool:
        if not self.is_configured:
            return False
        res = self._request(
            "POST",
            "/v5/order/cancel-all",
            {"category": "linear", "symbol": symbol},
            signed=True,
        )
        return res.get("retCode") == 0

    def cancel_all_open_orders(self) -> int:
        """Tahtada bekleyen ve teminatı kilitleyen tüm açık limit emirleri temizler."""
        if not self.is_configured:
            return 0
        open_ords = self.get_open_orders()
        count = len(open_ords)
        if count > 0:
            self._request(
                "POST",
                "/v5/order/cancel-all",
                {"category": "linear", "settleCoin": "USDT"},
                signed=True,
            )
        return count

    def close_all_open_positions(self) -> int:
        """Bybit Demo hesabındaki tüm açık pozisyonları Market emriyle anında kapatır."""
        if not self.is_configured:
            return 0
        positions = self.get_open_positions()
        closed_cnt = 0
        for p in positions:
            sym = str(p.get("symbol") or "")
            side = str(p.get("side") or "Buy")
            sz = str(p.get("size") or "0")
            if not sym or float(sz) <= 0:
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
        Dönüş: (kapatılan_pozisyon_sayısı, iptal_edilen_emir_sayısı, yeni_gerçek_bakiye)
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
        setup: FibSetup,
        alert_type: str,
        virtual_bankroll_usd: float,
    ) -> Tuple[bool, str]:
        """
        Teminatı uzaktaki Aşama 1 limit emirleriyle kilitlememek için, Bybit Demo üzerinde
        sadece fiyat Aşama 2 (0.580 - 0.618 Gerçek Altın Cep) içine girdiğinde 25x kaldıraç ve
        Sabit Risk Koruması (Stop olursa maks. kasanın %2'si = ~2$ zarar) ile doğrudan işlem açar!
        """
        if not self.is_configured or not self.enabled:
            return False, ""

        lev = Config.DEFAULT_LEVERAGE
        alloc_pct = setup.recommended_bankroll_pct
        base_margin_usd = max(4.0, virtual_bankroll_usd * (alloc_pct / 100.0))
        base_notional_usd = base_margin_usd * lev

        # Sabit Risk (Risk-Cap) Hesabı: Stop-Loss mesafesi genişse pozisyon büyüklüğünü otomatik kıs,
        # böylece tek bir stop asla kasanın %2'sinden (~2.00$) fazla kaybettirmesin!
        entry_ref = setup.current_price if setup.current_price > 0 else setup.fib.level_0618
        sl_dist_ratio = abs(entry_ref - setup.fib.stop_loss) / entry_ref if entry_ref > 0 else 0.015
        max_risk_usd = max(1.50, virtual_bankroll_usd * (Config.MAX_RISK_PCT_PER_TRADE / 100.0))
        risk_capped_notional = (max_risk_usd / sl_dist_ratio) if sl_dist_ratio > 0.002 else base_notional_usd
        notional_usd = min(base_notional_usd, risk_capped_notional)
        margin_usd = max(2.0, notional_usd / lev)

        # Aşama 1'de (fiyat henüz %20 geri çekilmişken) tahtaya emir dizip 100$ teminatı kilitleme!
        if alert_type == "STAGE_1":
            return (
                False,
                f"🛡 <b>Bybit Demo Kasa & RRR Koruması:</b> Erken (0.50) giriş yapıp Risk/Ödül oranını bozmamak için "
                f"fiyat <code>0.580 – 0.618</code> <b>Altın Cep (Golden Pocket)</b> bölgesine girdiği an "
                f"<b><code>{margin_usd:.2f}$</code> Marjin ({lev}x ➡️ {notional_usd:.0f}$ Büyüklük | Maks. Stop Riski: -{max_risk_usd:.2f}$)</b> ile otomatik açılacak!",
            )

        symbol = setup.symbol
        # 1) Bu paritede zaten açık pozisyon varsa tekrar açma
        existing_pos = self.get_open_positions(symbol=symbol)
        if existing_pos:
            return False, f"ℹ️ <b>Bybit Demo:</b> #{symbol} paritesinde zaten açık pozisyon mevcut."

        # 2) Eşzamanlı açık pozisyon sınırı (0 = Sınırsız, bakiye el verdiğince açar)
        if Config.MAX_CONCURRENT_DEMO_TRADES > 0:
            all_pos = self.get_open_positions()
            if len(all_pos) >= Config.MAX_CONCURRENT_DEMO_TRADES:
                return (
                    False,
                    f"⚠️ <b>Bybit Demo Kasa Koruması:</b> Maksimum {Config.MAX_CONCURRENT_DEMO_TRADES} açık pozisyon sınırına ulaşıldığı için #{symbol} emri pas geçildi.",
                )

        # 3) Kaldıracı 25x (veya parite üst limiti) ayarla ve lot kurallarını çek
        eff_lev = self.ensure_leverage(symbol, lev)
        spec = self.get_instrument_spec(symbol)
        notional_usd = min(base_margin_usd * eff_lev, risk_capped_notional)
        margin_usd = max(2.0, notional_usd / eff_lev)

        if entry_ref <= 0:
            return False, ""

        raw_qty = notional_usd / entry_ref
        qty_str = self.round_step(raw_qty, spec["qtyStep"], round_down=True)
        min_qty = float(spec["minOrderQty"])
        if float(qty_str) < min_qty:
            qty_str = spec["minOrderQty"]

        side = "Buy" if setup.direction == "LONG" else "Sell"
        sl_str = self.round_step(setup.fib.stop_loss, spec["tickSize"], round_down=(setup.direction == "LONG"))
        tp1_str = self.round_step(setup.fib.level_0, spec["tickSize"], round_down=(setup.direction == "SHORT"))
        tp2_str = self.round_step(setup.fib.level_neg_05, spec["tickSize"], round_down=(setup.direction == "SHORT"))
        tp3_str = self.round_step(setup.fib.level_neg_10, spec["tickSize"], round_down=(setup.direction == "SHORT"))

        # STAGE_2 (Fiyat 0.580 - 0.618 Altın Cebe Girdi!) -> Anında aktif pozisyon aç + Otomatik TP ve SL koy
        self.cancel_all_symbol_orders(symbol)
        order_params = {
            "category": "linear",
            "symbol": symbol,
            "side": side,
            "orderType": "Market",
            "qty": qty_str,
            "takeProfit": tp3_str,
            "stopLoss": sl_str,
            "tpTriggerBy": "LastPrice",
            "slTriggerBy": "LastPrice",
            "positionIdx": 0,
        }
        res = self._request("POST", "/v5/order/create", order_params, signed=True)
        if res.get("retCode") == 0:
            est_sl_usd = notional_usd * sl_dist_ratio
            type_lbl = "Kuantum Sniper Girişi" if Config.STRATEGY_MODE == "SNIPER" else "0.618 Altın Cep Girişi"
            return (
                True,
                f"🤖 <b>BYBIT DEMO OTOMATİK POZİSYON + TP/SL AÇILDI!</b>\n"
                f"  • <b>Tür:</b> <code>{type_lbl} ({side})</code> | <b>Miktar:</b> <code>{qty_str}</code>\n"
                f"  • <b>Kademeli TP1 (%80) / TP2 / TP3:</b> <code>{tp1_str}</code> / <code>{tp2_str}</code> / <code>{tp3_str}</code>\n"
                f"  • <b>Otomatik Stop-Loss:</b> <code>{sl_str}</code> <i>(Maks. Risk: -{est_sl_usd:.2f}$)</i>\n"
                f"  • <b>Kullanılan Marjin:</b> <b><code>{margin_usd:.2f}$</code></b> ({eff_lev}x ➡️ <code>{notional_usd:.0f}$</code> Büyüklük)",
            )
        return False, f"⚠️ <b>Bybit Demo Giriş Hatası:</b> <code>{res.get('retMsg')}</code>"

    def handle_trade_lifecycle_event(self, trade: Dict, event_type: str) -> str:
        """
        TP1, TP2, TP3, Stop veya Pusu İptali olduğunda Bybit Demo hesabındaki pozisyonu
        otomatik olarak kısmi kapatır ve Stop-Loss'u Başabaş / TP1 noktasına taşır!
        """
        if not self.is_configured or not self.enabled:
            return ""

        symbol = trade["symbol"]
        direction = trade["direction"]
        close_side = "Sell" if direction == "LONG" else "Buy"
        spec = self.get_instrument_spec(symbol)

        if event_type == "MISSED_NO_PULLBACK":
            self.cancel_all_symbol_orders(symbol)
            return f"🤖 <b>Bybit Demo:</b> #{symbol} bekleyen 0.618 Limit Emri otomatik iptal edildi."

        open_pos = self.get_open_positions(symbol=symbol)
        if not open_pos:
            self.cancel_all_symbol_orders(symbol)
            return ""

        pos = open_pos[0]
        cur_size = float(pos.get("size") or 0.0)
        avg_entry = float(pos.get("avgPrice") or trade.get("entry_0618") or 0.0)
        if cur_size <= 0:
            return ""

        if event_type == "TP1":
            # 1) Pozisyonun %80'ini (Config.TP1_CLOSE_PCT) Market Reduce-Only ile kapatıp ana kârı cebe kilitle!
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
            # 2) Kalan %20 pozisyonun Stop-Loss'unu Giriş Fiyatına (Başabaş) çek!
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

        elif event_type == "TP2":
            # Kalan %20 pozisyonun yarısını (toplamın %10'unu) kapat ve SL'yi TP1'e taşı
            close_qty = self.round_step(cur_size * 0.50, spec["qtyStep"], round_down=True)
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
            tp1_sl = self.round_step(float(trade["tp1"]), spec["tickSize"], round_down=(direction == "LONG"))
            self._request(
                "POST",
                "/v5/position/trading-stop",
                {
                    "category": "linear",
                    "symbol": symbol,
                    "stopLoss": tp1_sl,
                    "slTriggerBy": "LastPrice",
                    "positionIdx": 0,
                },
                signed=True,
            )
            return (
                f"🤖 <b>Bybit Demo Otomatik İşlem:</b> #{symbol} kalan pozisyonun yarısı (<code>{close_qty}</code>) daha kapatıldı "
                f"ve İz Süren Stop <b>TP1 (<code>{tp1_sl}</code>)</b> seviyesine taşındı!"
            )

        elif event_type in ("TP3", "STOP_LOSS", "BREAKEVEN_STOP", "TRAILING_TP1_STOP"):
            # Kalan tüm pozisyonu kapat ve bekleyen emirleri temizle
            close_qty = self.round_step(cur_size, spec["qtyStep"], round_down=True)
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
            return f"🤖 <b>Bybit Demo Otomatik İşlem:</b> #{symbol} kalan pozisyon (<code>{close_qty}</code>) tamamen kapatıldı."

        return ""

    def sync_and_protect_open_positions(self, tracked_trades: Dict[str, Dict]) -> None:
        """
        Bybit Demo hesabındaki açık pozisyonları her taramada kontrol eder:
        - Eğer bir pozisyonda TP veya SL boşsa (ör. SOLUSDT veya yeniden başlatma sonrası kalan pozisyonlar),
          otomatik olarak TP ve SL değerlerini Bybit üzerine yazar ve takip listesine alır!
        """
        if not self.is_configured or not self.enabled:
            return

        open_positions = self.get_open_positions()
        if not open_positions:
            return

        # Sembol bazında mevcut takip edilen işlemleri eşle
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
                target_tp = float(tr.get("tp3") or tr.get("tp1") or 0.0)
                target_sl = float(tr.get("current_sl") or tr.get("initial_sl") or 0.0)
            else:
                # Takip listesinde olmayan (eskiden kalmış, ör. SOLUSDT) pozisyon için otomatik koruma (1:1.5 -> 1:3 RRR)
                sl_dist = avg_price * 0.011  # %1.1 Stop mesafesi
                tp1_dist = avg_price * 0.0165  # %1.65 TP1 (1.5R)
                tp2_dist = avg_price * 0.028   # %2.80 TP2 (2.5R)
                tp3_dist = avg_price * 0.040   # %4.00 TP3 (3.6R)
                if direction == "LONG":
                    target_sl = avg_price - sl_dist
                    tp1_v, tp2_v, target_tp = avg_price + tp1_dist, avg_price + tp2_dist, avg_price + tp3_dist
                else:
                    target_sl = avg_price + sl_dist
                    tp1_v, tp2_v, target_tp = avg_price - tp1_dist, avg_price - tp2_dist, avg_price - tp3_dist

                tid = f"{sym}_{direction}_SYNCED"
                tracked_trades[tid] = {
                    "symbol": sym,
                    "direction": direction,
                    "status": "IN_TRADE",
                    "origin_ts": int(time.time() * 1000) - 900_000,
                    "bos_ts": int(time.time() * 1000) - 600_000,
                    "extreme_ts": int(time.time() * 1000) - 300_000,
                    "zone_entry_ts": int(time.time() * 1000) - 300_000,
                    "broken_price": tp1_v,
                    "level_1": target_sl,
                    "entry_0618": avg_price,
                    "level_0500": avg_price,
                    "initial_sl": target_sl,
                    "current_sl": target_sl,
                    "tp1": tp1_v,
                    "tp2": tp2_v,
                    "tp3": target_tp,
                    "rrr_tp1": 1.50,
                    "rrr_tp2": 2.55,
                    "rrr_tp3": 3.60,
                    "rvol": 1.3,
                    "atr_mult": 1.8,
                    "strength_score": 75,
                    "quality_grade": "A",
                    "bankroll_pct": 8,
                    "margin_usd": 8.0,
                    "confluence_notes": ["✅ Otomatik Koruma Senkronizasyonu"],
                    "realized_usd": 0.0,
                    "hits": [],
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
                "⚠️ <b>Durum:</b> Henüz Demo API Anahtarı bağlanmadı (Şu an Sinyal + Sanal 100$ Takip Modunda).\n\n"
                "📌 <b>Bağlamak İçin:</b>\n"
                "Bybit Demo Trading ekranından aldığın API Key ve Secret değerlerini Render'daki "
                "<code>Environment</code> kısmına <code>BYBIT_DEMO_API_KEY</code> ve <code>BYBIT_DEMO_API_SECRET</code> "
                "olarak eklemen yeterli!"
            )

        acc_sum = self.get_demo_account_summary()
        open_pos = self.get_open_positions()
        open_orders = self.get_open_orders()

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
        mode_str = "🟢 AKTİF (Otomatik Emir Açıyor)" if self.enabled else "⏸ DURAKLATILDI (/oto_ac ile başlat)"

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

        ord_lines = []
        for o in open_orders[:15]:
            sym = o.get("symbol")
            side = "🟢 Alış" if o.get("side") == "Buy" else "🔴 Satış"
            pr = o.get("price")
            qt = o.get("qty")
            ord_lines.append(f"  • <b>#{sym}</b> ({side} Limit) | Fiyat: <code>{pr}</code> | Miktar: <code>{qt}</code>")
        if not ord_lines:
            ord_lines.append("  • <i>Şu an bekleyen Limit emir bulunmuyor.</i>")

        limit_str = (
            str(Config.MAX_CONCURRENT_DEMO_TRADES)
            if Config.MAX_CONCURRENT_DEMO_TRADES > 0
            else "Sınırsız (Bakiye El Verdiğince Açar)"
        )

        return (
            f"🤖 <b>BYBIT DEMO HESABI VE OTOMATİK İŞLEM RAPORU</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"⚙️ <b>Otomatik Al-Sat:</b> {mode_str}\n"
            f"💼 <b>Botun Kullandığı Kasa Bütçesi:</b> <b><code>{virtual_bankroll_usd:.2f}$</code></b> <i>(İstediğin an <code>/kasa 100</code> yazarak sıfırlayabilirsin)</i>\n"
            f"🏦 <b>Bybit Demo Cüzdan Bakiyesi:</b> <code>{wallet_str}</code>\n"
            f"🛡 <b>Eşzamanlı İşlem Sınırı:</b> <code>{limit_str}</code>\n\n"
            f"📈 <b>AÇIK DEMO POZİSYONLAR ({len(open_pos)}):</b>\n"
            + "\n".join(pos_lines)
            + f"\n\n⏳ <b>BEKLEYEN LİMİT EMİRLER ({len(open_orders)}):</b>\n"
            + "\n".join(ord_lines)
        )
