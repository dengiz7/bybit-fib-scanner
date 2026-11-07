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

    @property
    def is_configured(self) -> bool:
        return bool(self.api_key and self.api_secret)

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
        ts = str(int(time.time() * 1000))
        headers = {
            "User-Agent": "Mozilla/5.0 (BybitFib15mScanner/2.0)",
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
            spec = {
                "qtyStep": str(lot.get("qtyStep", "0.001")),
                "minOrderQty": str(lot.get("minOrderQty", "0.001")),
                "tickSize": str(pf.get("tickSize", "0.0001")),
            }
            self._inst_cache[symbol] = spec
            return spec

        return {"qtyStep": "0.01", "minOrderQty": "0.01", "tickSize": "0.0001"}

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

    def get_demo_wallet_balance(self) -> Optional[float]:
        """Bybit Demo UNIFIED hesabındaki USDT toplam bakiyesini döner."""
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
        for coin_info in acc_list[0].get("coin", []):
            if coin_info.get("coin") == "USDT":
                try:
                    return float(coin_info.get("walletBalance") or coin_info.get("equity") or 0.0)
                except Exception:
                    return 0.0
        try:
            return float(acc_list[0].get("totalEquity") or 0.0)
        except Exception:
            return None

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

    def ensure_leverage(self, symbol: str, leverage: int = Config.DEFAULT_LEVERAGE) -> None:
        """Paritede kaldıracı 10x ayarlar (Zaten 10x ise hata vermez)."""
        lev_str = str(leverage)
        # İzole marjin + 10x kaldıraç geçiş denemesi
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

    def place_setup_order(
        self,
        setup: FibSetup,
        alert_type: str,
        virtual_bankroll_usd: float,
    ) -> Tuple[bool, str]:
        """
        Aşama 1 veya Aşama 2 sinyali geldiğinde Bybit Demo hesabında işlemi otomatik kurar.
        - Aşama 1: 0.618 seviyesine Limit Emir + 1.03 Stop-Loss
        - Aşama 2: Eğer henüz açık pozisyon yoksa Altın Bölgeden Market (veya 0.618 Limit) giriş + 1.03 Stop-Loss
        Dönüş: (başarılı_mı, açıklama_metni)
        """
        if not self.is_configured or not self.enabled:
            return False, ""

        symbol = setup.symbol
        # 1) Bu paritede zaten açık pozisyon varsa tekrar açma
        existing_pos = self.get_open_positions(symbol=symbol)
        if existing_pos:
            return False, f"ℹ️ <b>Bybit Demo:</b> #{symbol} paritesinde zaten açık pozisyon mevcut."

        # 2) Toplam eşzamanlı açık pozisyon sınırı kontrolü (Kasa koruması)
        all_pos = self.get_open_positions()
        if len(all_pos) >= Config.MAX_CONCURRENT_DEMO_TRADES:
            return (
                False,
                f"⚠️ <b>Bybit Demo Kasa Koruması:</b> Aynı anda maksimum {Config.MAX_CONCURRENT_DEMO_TRADES} açık pozisyon sınırına ulaşıldığı için #{symbol} emri pas geçildi.",
            )

        # 3) Kaldıracı 10x ayarla ve lot kurallarını çek
        lev = Config.DEFAULT_LEVERAGE
        self.ensure_leverage(symbol, lev)
        spec = self.get_instrument_spec(symbol)

        alloc_pct = setup.recommended_bankroll_pct
        margin_usd = max(5.0, virtual_bankroll_usd * (alloc_pct / 100.0))
        notional_usd = margin_usd * lev

        entry_ref = setup.fib.level_0618 if alert_type == "STAGE_1" else setup.current_price
        if entry_ref <= 0:
            return False, ""

        raw_qty = notional_usd / entry_ref
        qty_str = self.round_step(raw_qty, spec["qtyStep"], round_down=True)
        min_qty = float(spec["minOrderQty"])
        if float(qty_str) < min_qty:
            qty_str = spec["minOrderQty"]

        side = "Buy" if setup.direction == "LONG" else "Sell"
        sl_str = self.round_step(setup.fib.stop_loss, spec["tickSize"], round_down=(setup.direction == "LONG"))

        if alert_type == "STAGE_1":
            # Önce eski bekleyen emir varsa temizle, sonra 0.618 Limit Emir koy
            self.cancel_all_symbol_orders(symbol)
            price_str = self.round_step(setup.fib.level_0618, spec["tickSize"], round_down=False)
            order_params = {
                "category": "linear",
                "symbol": symbol,
                "side": side,
                "orderType": "Limit",
                "qty": qty_str,
                "price": price_str,
                "timeInForce": "GTC",
                "stopLoss": sl_str,
                "slTriggerBy": "LastPrice",
                "positionIdx": 0,
            }
            res = self._request("POST", "/v5/order/create", order_params, signed=True)
            if res.get("retCode") == 0:
                return (
                    True,
                    f"🤖 <b>BYBIT DEMO OTOMATİK EMİR GİRİLDİ!</b>\n"
                    f"  • <b>Tür:</b> <code>0.618 Limit Emir ({side})</code> | <b>Miktar:</b> <code>{qty_str}</code>\n"
                    f"  • <b>Giriş Fiyatı:</b> <code>{price_str}</code> | <b>Otomatik SL:</b> <code>{sl_str}</code>\n"
                    f"  • <b>Kullanılan Marjin:</b> <code>{margin_usd:.2f}$</code> (100$ Kasanın %{alloc_pct}'i | {lev}x)",
                )
            return False, f"⚠️ <b>Bybit Demo Emir Hatası:</b> <code>{res.get('retMsg')}</code>"

        else:
            # STAGE_2 (Fiyat 0.500 - 0.618 Altın Bölgeye Girdi!)
            # Bekleyen doldurulmamış limit emri varsa iptal et ve Altın Bölgeden aktif giriş yap
            self.cancel_all_symbol_orders(symbol)
            order_params = {
                "category": "linear",
                "symbol": symbol,
                "side": side,
                "orderType": "Market",
                "qty": qty_str,
                "stopLoss": sl_str,
                "slTriggerBy": "LastPrice",
                "positionIdx": 0,
            }
            res = self._request("POST", "/v5/order/create", order_params, signed=True)
            if res.get("retCode") == 0:
                return (
                    True,
                    f"🤖 <b>BYBIT DEMO OTOMATİK POZİSYON AÇILDI!</b>\n"
                    f"  • <b>Tür:</b> <code>Altın Bölge Aktif Giriş ({side})</code> | <b>Miktar:</b> <code>{qty_str}</code>\n"
                    f"  • <b>Otomatik Stop-Loss:</b> <code>{sl_str}</code>\n"
                    f"  • <b>Kullanılan Marjin:</b> <code>{margin_usd:.2f}$</code> (100$ Kasanın %{alloc_pct}'i | {lev}x)",
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
            # 1) Pozisyonun %50'sini Market Reduce-Only ile kapat
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
            # 2) Kalan %50 pozisyonun Stop-Loss'unu Giriş Fiyatına (Başabaş) çek!
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
                f"🤖 <b>Bybit Demo Otomatik İşlem:</b> #{symbol} pozisyonunun <b>%50'si ({close_qty}) kârla kapatıldı</b> "
                f"ve kalan kısmın Stop-Loss'u <b>Giriş Fiyatına (<code>{be_price}</code> Başabaş)</b> çekildi!"
            )

        elif event_type == "TP2":
            # Kalan pozisyonun yarısını (toplamın %25'ini) kapat ve SL'yi TP1'e taşı
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
                f"🤖 <b>Bybit Demo Otomatik İşlem:</b> #{symbol} pozisyonunun <b>%25'i ({close_qty}) daha kapatıldı</b> "
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

        wallet_usdt = self.get_demo_wallet_balance()
        open_pos = self.get_open_positions()
        open_orders = self.get_open_orders()

        wallet_str = f"{wallet_usdt:,.2f} USDT" if wallet_usdt is not None else "Bağlantı kontrol ediliyor..."
        mode_str = "🟢 AKTİF (Otomatik Emir Açıyor)" if self.enabled else "⏸ DURAKLATILDI (/oto_ac ile başlat)"

        pos_lines = []
        for p in open_pos[:10]:
            sym = p.get("symbol")
            side = "🟢 LONG" if p.get("side") == "Buy" else "🔴 SHORT"
            sz = p.get("size")
            avg_p = p.get("avgPrice")
            upnl = float(p.get("unrealisedPnl") or 0.0)
            sl_p = p.get("stopLoss") or "Yok"
            pos_lines.append(
                f"  • <b>#{sym}</b> ({side}) | Miktar: <code>{sz}</code> | Giriş: <code>{avg_p}</code> | "
                f"SL: <code>{sl_p}</code> | Anlık PnL: <b><code>{upnl:+.2f}$</code></b>"
            )
        if not pos_lines:
            pos_lines.append("  • <i>Şu an açık Demo pozisyon bulunmuyor.</i>")

        ord_lines = []
        for o in open_orders[:10]:
            sym = o.get("symbol")
            side = "🟢 Alış" if o.get("side") == "Buy" else "🔴 Satış"
            pr = o.get("price")
            qt = o.get("qty")
            ord_lines.append(f"  • <b>#{sym}</b> ({side} Limit) | Fiyat: <code>{pr}</code> | Miktar: <code>{qt}</code>")
        if not ord_lines:
            ord_lines.append("  • <i>Şu an bekleyen Limit emir bulunmuyor.</i>")

        return (
            f"🤖 <b>BYBIT DEMO HESABI VE OTOMATİK İŞLEM RAPORU</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"⚙️ <b>Otomatik Al-Sat:</b> {mode_str}\n"
            f"💼 <b>Botun Kullandığı Kasa Bütçesi:</b> <b><code>{virtual_bankroll_usd:.2f}$</code></b> <i>(İstediğin an <code>/kasa 100</code> yazarak sıfırlayabilirsin)</i>\n"
            f"🏦 <b>Bybit Demo Cüzdan Bakiyesi:</b> <code>{wallet_str}</code>\n"
            f"🛡 <b>Maks. Eşzamanlı İşlem Sınırı:</b> <code>{Config.MAX_CONCURRENT_DEMO_TRADES}</code>\n\n"
            f"📈 <b>AÇIK DEMO POZİSYONLAR ({len(open_pos)}):</b>\n"
            + "\n".join(pos_lines)
            + f"\n\n⏳ <b>BEKLEYEN 0.618 LİMİT EMİRLER ({len(open_orders)}):</b>\n"
            + "\n".join(ord_lines)
        )
