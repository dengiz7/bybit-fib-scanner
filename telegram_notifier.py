# -*- coding: utf-8 -*-
"""
Telegram Bildirim ve Komut Yöneticisi (%100 Ücretsiz Telegram Bot API)
- Aşama 1 (BoS + Geri Çekilme -> 0.618 Limit Emir Pususu)
- Aşama 2 (0.5 - 0.618 Altın Bölgeye Giriş + Mum Onayı Durumu)
- Otomatik Chat ID yakalama (/start komutuyla) ve canlı komutlar (/durum, /aktif, /tara, /yardim)
"""

import json
import ssl
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Callable, Dict, List, Optional

from config import Config, save_env_value
from strategy import FibSetup


TR_TZ = timezone(timedelta(hours=3))


def fmt_price(price: float) -> str:
    """Fiyat büyüklüğüne göre dinamik ondalık basamak formatlar (BTC'den 0.00001'lik altcoinlere kadar)."""
    p = abs(price)
    if p == 0:
        return "0"
    if p >= 1000:
        return f"{price:,.2f}"
    if p >= 10:
        return f"{price:.4f}".rstrip("0").rstrip(".")
    if p >= 0.1:
        return f"{price:.5f}".rstrip("0").rstrip(".")
    if p >= 0.001:
        return f"{price:.6f}".rstrip("0").rstrip(".")
    return f"{price:.8f}".rstrip("0").rstrip(".")


def fmt_usd_compact(val: float) -> str:
    if val >= 1_000_000_000:
        return f"{val / 1_000_000_000:.2f} Milyar $"
    if val >= 1_000_000:
        return f"{val / 1_000_000:.1f} Milyon $"
    return f"{val:,.0f} $"


def fmt_ts_tr(ms: int) -> str:
    dt = datetime.fromtimestamp(ms / 1000.0, tz=TR_TZ)
    return dt.strftime("%H:%M")


def calc_pct_diff(entry: float, target: float) -> float:
    if entry == 0:
        return 0.0
    return abs(target - entry) / entry * 100.0


def format_setup_message(setup: FibSetup, alert_type: str) -> str:
    """
    alert_type:
      - "STAGE_1" -> Kurulum Hazır / 0.618 Limit Emir Pususu
      - "STAGE_2" -> Fiyat 0.5 - 0.618 Altın Bölgesine Girdi!
    """
    is_long = setup.direction == "LONG"
    dir_emoji = "🟢" if is_long else "🔴"
    dir_text = "LONG (Yükseliş Trendi)" if is_long else "SHORT (Düşüş Trendi)"
    struct_text = "Higher Highs & Higher Lows (HH + HL)" if is_long else "Lower Highs & Lower Lows (LH + LL)"

    tp1_pct = calc_pct_diff(setup.fib.level_0618, setup.fib.level_0)
    tp2_pct = calc_pct_diff(setup.fib.level_0618, setup.fib.level_neg_05)
    tp3_pct = calc_pct_diff(setup.fib.level_0618, setup.fib.level_neg_10)
    sl_pct = calc_pct_diff(setup.fib.level_0618, setup.fib.stop_loss)

    # 10x İzole Kaldıraç referans getirileri
    tp1_10x = tp1_pct * 10.0
    tp2_10x = tp2_pct * 10.0
    tp3_10x = tp3_pct * 10.0
    sl_10x = sl_pct * 10.0

    tv_url = f"https://www.tradingview.com/chart/?symbol=BYBIT:{setup.symbol}.P&interval=15"
    bybit_url = f"https://www.bybit.com/trade/usdt/{setup.symbol}"

    flip_banner = ""
    if setup.is_trend_flip:
        flip_banner = (
            "\n🔄 <b>TREND DÖNÜŞÜ (FLIP) KURULUMU:</b> Önceki ters yönlü kurulum stop oldu ve "
            "videodaki kural gereği yeni trend yönünde BoS onaylandı!\n"
        )

    if alert_type == "STAGE_1":
        header = f"🎯 <b>AŞAMA 1 — FİBONACCİ KURULUMU HAZIR (LİMİT EMİR PUSUSU)</b>"
        status_line = (
            f"⏳ <b>Durum:</b> BoS mum kapanışıyla onaylandı, geri çekilme başladı "
            f"(Şu an: <code>%{setup.current_retracement * 100:.1f}</code> geri çekildi).\n"
            f"📌 <b>Eylem:</b> <code>0.618</code> seviyesine Limit Emir kurabilir veya Altın Bölge bildirimini bekleyebilirsin."
        )
    else:
        header = f"🔥 <b>AŞAMA 2 — FİYAT ALTIN BÖLGEDE (0.500 – 0.618)!</b>"
        touch_note = (
            "✅ <b>0.618 Altın Cep Seviyesine Değdi!</b>"
            if setup.touched_0618
            else "⚡ <b>0.500 – 0.618 Altın Bölge İçinde!</b>"
        )
        conf_note = (
            f"\n🕯 <b>Mum Onayı:</b> {setup.candle_confirmation}"
            if setup.candle_confirmation
            else "\n🕯 <b>Mum Onayı:</b> Henüz kapanmış onay mumu yok (0.618 limit giriş aktif)."
        )
        status_line = (
            f"{touch_note} (En derin temas: <code>{setup.deepest_retracement:.3f}</code>)"
            f"{conf_note}"
        )

    msg = (
        f"{header}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"{dir_emoji} <b>Parite:</b> #{setup.symbol} | <b>15dk</b> | <b>{dir_text}</b>\n"
        f"📐 <b>Piyasa Yapısı:</b> {struct_text}\n"
        f"💥 <b>Kırılan Yapı (BoS):</b> <code>{fmt_price(setup.broken_structure_price)}</code> "
        f"(Saat {fmt_ts_tr(setup.bos_timestamp)} mumu kapattı)"
        f"{flip_banner}\n"
        f"{status_line}\n\n"
        f"💰 <b>Anlık Fiyat:</b> <code>{fmt_price(setup.current_price)}</code>\n\n"
        f"📊 <b>KADEMELİ İŞLEM & KASA BÜYÜTME PLANI:</b>\n"
        f"  • 🎯 <b>İdeal Giriş (0.618 Limit):</b> <code>{fmt_price(setup.fib.level_0618)}</code>\n"
        f"  • 🟠 <b>Altın Bölge Bandı:</b> <code>{fmt_price(setup.fib.level_0500)}</code> – <code>{fmt_price(setup.fib.level_0618)}</code>\n"
        f"  • 🛑 <b>Stop-Loss (1'in Dışı):</b> <code>{fmt_price(setup.fib.stop_loss)}</code> "
        f"(Spot: <code>-%{sl_pct:.2f}</code> | 10x: <code>-%{sl_10x:.1f}</code>)\n\n"
        f"🎯 <b>KÂR ALMA (TP) & İZ SÜREN STOP HEDEFLERİ:</b>\n"
        f"  1️⃣ <b>TP1 (0.0 Güvenli Hedef — 1:{setup.rrr_at_0618:.2f} RRR):</b>\n"
        f"     👉 <code>{fmt_price(setup.fib.level_0)}</code> (Spot: <code>+%{tp1_pct:.2f}</code> | 10x: <b>+%{tp1_10x:.1f}</b>)\n"
        f"     <i>📌 Eylem: Pozisyonun %50'sini kapat, Stop-Loss'u Girişe (Başabaş) çek!</i>\n"
        f"  2️⃣ <b>TP2 (-0.5 Uzatma — 1:{setup.rrr_tp2:.2f} RRR):</b>\n"
        f"     👉 <code>{fmt_price(setup.fib.level_neg_05)}</code> (Spot: <code>+%{tp2_pct:.2f}</code> | 10x: <b>+%{tp2_10x:.1f}</b>)\n"
        f"     <i>📌 Eylem: %25 daha kâr al, Stop-Loss'u TP1 seviyesine taşı!</i>\n"
        f"  3️⃣ <b>TP3 (-1.0 Tam Uzatma — 1:{setup.rrr_tp3:.2f} RRR):</b>\n"
        f"     👉 <code>{fmt_price(setup.fib.level_neg_10)}</code> (Spot: <code>+%{tp3_pct:.2f}</code> | 10x: <b>+%{tp3_10x:.1f}</b>)\n"
        f"     <i>📌 Eylem: Kalan %25 pozisyonu tamamen kapat!</i>\n\n"
        f"🔍 <b>FİLTRE KONTROLLERİ:</b>\n"
        f"  • İtkisel Güç: <code>{setup.impulse_atr_mult}x ATR</code> | Hacim: <code>{setup.rvol}x RVOL</code>\n"
        f"  • 24s Hacim: <code>{fmt_usd_compact(setup.turnover_24h)}</code>\n"
        f"  • Seans: {setup.session_label}\n\n"
        f"🔗 <a href=\"{tv_url}\">TradingView Grafiği</a>  |  <a href=\"{bybit_url}\">Bybit Vadeli Tahtası</a>"
    )
    return msg


def format_tp_hit_message(trade: Dict, tp_stage: str, current_price: float) -> str:
    """Aktif takip edilen Aşama 2 işlemi TP1, TP2 veya TP3 vurduğunda gönderilecek bildirim."""
    symbol = trade["symbol"]
    direction = trade["direction"]
    entry = float(trade["entry_0618"])
    tp1 = float(trade["tp1"])
    tp2 = float(trade["tp2"])
    tp3 = float(trade["tp3"])
    dir_emoji = "🟢" if direction == "LONG" else "🔴"

    if tp_stage == "TP1":
        pct = calc_pct_diff(entry, tp1)
        return (
            f"✅ <b>TP1 (0.0 GÜVENLİ HEDEF) VURULDU! — #{symbol}</b> ({dir_emoji} {direction})\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🎯 <b>0.618 Giriş:</b> <code>{fmt_price(entry)}</code> ➡️ <b>TP1:</b> <code>{fmt_price(tp1)}</code>\n"
            f"💰 <b>Kazanç:</b> Spot <code>+%{pct:.2f}</code> | 10x Kaldıraç: <b>+%{pct * 10:.1f}</b> (<b>1 : 1.50 RRR</b>)\n\n"
            f"🛡 <b>ŞİMDİ NE YAPMALI? (Video Kuralı):</b>\n"
            f"1. Pozisyonun <b>%50'sini</b> kârla kapat.\n"
            f"2. Kalan %50 için Stop-Loss'u giriş seviyene (<code>{fmt_price(entry)}</code> - <b>Başabaş / Sıfır Risk</b>) çek!\n"
            f"3. Sıradaki uzatma hedefi: <b>TP2 (-0.5):</b> <code>{fmt_price(tp2)}</code>"
        )
    elif tp_stage == "TP2":
        pct = calc_pct_diff(entry, tp2)
        return (
            f"🚀 <b>TP2 (-0.5 UZATMA HEDEFİ) VURULDU! — #{symbol}</b> ({dir_emoji} {direction})\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🎯 <b>0.618 Giriş:</b> <code>{fmt_price(entry)}</code> ➡️ <b>TP2:</b> <code>{fmt_price(tp2)}</code>\n"
            f"💰 <b>Kazanç:</b> Spot <code>+%{pct:.2f}</code> | 10x Kaldıraç: <b>+%{pct * 10:.1f}</b> (<b>1 : 2.71 RRR</b>)\n\n"
            f"🛡 <b>ŞİMDİ NE YAPMALI? (Trailing Stop):</b>\n"
            f"1. Pozisyonun <b>%25'ini</b> daha kapat.\n"
            f"2. İz süren Stop-Loss'u <b>TP1 (<code>{fmt_price(tp1)}</code>)</b> seviyesine taşı!\n"
            f"3. Son hedef: <b>TP3 (-1.0):</b> <code>{fmt_price(tp3)}</code>"
        )
    elif tp_stage == "TP3":
        pct = calc_pct_diff(entry, tp3)
        return (
            f"🏆 <b>TP3 (-1.0 TAM UZATMA HEDEFİ) VURULDU! — #{symbol}</b> ({dir_emoji} {direction})\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🎯 <b>0.618 Giriş:</b> <code>{fmt_price(entry)}</code> ➡️ <b>TP3:</b> <code>{fmt_price(tp3)}</code>\n"
            f"💰 <b>Toplam Hareket:</b> Spot <code>+%{pct:.2f}</code> | 10x Kaldıraç: <b>+%{pct * 10:.1f}</b> (<b>1 : 3.93 RRR</b>)\n\n"
            f"🎉 <b>Tebrikler!</b> Videodaki tüm Fibonacci uzatma hedefleri eksiksiz tamamlandı."
        )
    elif tp_stage == "BREAKEVEN_STOP":
        return (
            f"🛡 <b>BAŞABAŞ (GİRİŞ SEVİYESİ) STOP ÇALIŞTI — #{symbol}</b> ({dir_emoji} {direction})\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"📌 TP1 (<code>{fmt_price(tp1)}</code>) hedefinde <b>%50 kâr cebimize girmişti</b>. "
            f"Kalan %50 pozisyon giriş seviyesinde (<code>{fmt_price(entry)}</code>) sıfır zararla kapandı.\n"
            f"✅ <b>Sonuç:</b> İşlem net kârla tamamlandı!"
        )
    elif tp_stage == "TRAILING_TP1_STOP":
        return (
            f"🛡 <b>İZ SÜREN STOP (TP1 SEVİYESİNDE) ÇALIŞTI — #{symbol}</b> ({dir_emoji} {direction})\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"📌 TP1 ve TP2 hedefleri vurulmuştu (%75 kâr alındı). "
            f"Son %25'lik kısım da TP1 (<code>{fmt_price(tp1)}</code>) seviyesinde kârla kapandı!\n"
            f"✅ <b>Sonuç:</b> İşlem yüksek kârla tamamlandı!"
        )
    elif tp_stage == "MISSED_NO_PULLBACK":
        return (
            f"⚠️ <b>PUSU İPTAL (ALTIN BÖLGEYE DÖNMEDEN GİTTİ) — #{symbol}</b> ({dir_emoji} {direction})\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"📌 Fiyat <code>0.500 – 0.618</code> Altın Bölgesine geri çekilmeden (limit emri almadan) "
            f"doğrudan <code>0.0</code> (<code>{fmt_price(tp1)}</code>) hedefini kırdı (Video 2 [04:40] Kuralı).\n"
            f"🗑 <b>Eylem:</b> <code>{fmt_price(entry)}</code> seviyesinde bekleyen Limit Emrin varsa <b>İPTAL ET</b>. "
            f"Yeni yapı kırılımı (BoS) beklenecek."
        )
    else:
        return (
            f"🛑 <b>STOP-LOSS OLDU (1.0 SEVİYESİ KIRILDI) — #{symbol}</b> ({dir_emoji} {direction})\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"⚠️ Fiyat <code>1.0</code> başlangıç yapısını (<code>{fmt_price(float(trade['current_sl']))}</code>) kırdı.\n"
            f"🔄 <b>Video Kuralı (Flip):</b> Mevcut mikro-trend sona erdi. Sistem artık #{symbol} paritesinde "
            f"<b>ters yönde</b> yeni yapı kırılımı (BoS) aramaya başladı!"
        )


class TelegramNotifier:
    def __init__(self, token: Optional[str] = None, chat_id: Optional[str] = None):
        Config.reload()
        self.token = (token if token is not None else Config.TELEGRAM_BOT_TOKEN).strip()
        self.chat_id = (chat_id if chat_id is not None else Config.TELEGRAM_CHAT_ID).strip()
        self._ssl_ctx = ssl.create_default_context()
        self._last_update_id: int = 0

    @property
    def is_configured(self) -> bool:
        return bool(self.token and self.chat_id)

    def _api_call(self, method: str, payload: Optional[Dict] = None, timeout: int = 10) -> Optional[dict]:
        if not self.token:
            return None
        url = f"https://api.telegram.org/bot{self.token}/{method}"
        try:
            if payload is not None:
                data = json.dumps(payload).encode("utf-8")
                req = urllib.request.Request(
                    url,
                    data=data,
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
            else:
                req = urllib.request.Request(url, method="GET")
            with urllib.request.urlopen(req, timeout=timeout, context=self._ssl_ctx) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as exc:
            print(f"[TELEGRAM UYARI] {method} hatası: {exc}")
            return None

    def get_bot_info(self) -> Optional[dict]:
        res = self._api_call("getMe")
        if res and res.get("ok"):
            return res.get("result")
        return None

    def register_bot_commands(self) -> None:
        """Telegram sohbet ekranında mavi 'Menü' butonunu otomatik oluşturur."""
        commands = [
            {"command": "aktif", "description": "Pusudaki ve Altın Bölgedeki aktif kurulumlar"},
            {"command": "durum", "description": "Tarayıcı çalışma durumu ve son tarama saati"},
            {"command": "tara", "description": "Hemen şimdi anlık Bybit taraması başlat"},
            {"command": "yardim", "description": "Komut listesini ve açıklamaları göster"},
        ]
        self._api_call("setMyCommands", {"commands": commands})

    def send_message(self, text: str, chat_id: Optional[str] = None) -> bool:
        target_chat = (chat_id or self.chat_id).strip()
        if not self.token or not target_chat:
            return False
        res = self._api_call(
            "sendMessage",
            {
                "chat_id": target_chat,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
        )
        return bool(res and res.get("ok"))

    def poll_commands(
        self,
        get_status_cb: Callable[[], str],
        get_active_cb: Callable[[], str],
        trigger_scan_cb: Callable[[], str],
    ) -> None:
        """
        Telegram'dan gelen /start, /durum, /aktif, /tara komutlarını dinler.
        Eğer CHAT_ID henüz ayarlanmamışsa /start gönderen ilk kişinin Chat ID'sini otomatik kaydeder!
        """
        if not self.token:
            return

        params: Dict = {"timeout": 0}
        if self._last_update_id > 0:
            params["offset"] = self._last_update_id + 1

        res = self._api_call("getUpdates", params, timeout=8)
        if not res or not res.get("ok"):
            print(f"[TELEGRAM UYARI] getUpdates yanit vermedi: {res}")
            return

        updates = res.get("result", [])
        if updates:
            print(f"[TELEGRAM] {len(updates)} yeni mesaj/komut alindi.")

        handled_cmds_in_batch = set()
        for upd in res.get("result", []):
            upd_id = int(upd.get("update_id", 0))
            if upd_id > self._last_update_id:
                self._last_update_id = upd_id

            msg = (
                upd.get("message")
                or upd.get("edited_message")
                or upd.get("channel_post")
                or {}
            )
            chat = msg.get("chat") or (upd.get("my_chat_member") or {}).get("chat") or {}
            text = str(msg.get("text") or "").strip()
            sender_chat_id = str(chat.get("id") or "").strip()
            first_name = str(chat.get("first_name") or chat.get("title") or "Dostum")

            if not sender_chat_id:
                continue
            if not text and self.chat_id:
                continue

            # Henüz Chat ID kayıtlı değilse otomatik yakala, .env dosyasına kaydet ve ilk taramayı gönder!
            first_time_linked = False
            if not self.chat_id:
                self.chat_id = sender_chat_id
                save_env_value("TELEGRAM_CHAT_ID", sender_chat_id)
                first_time_linked = True
                print(f"[TELEGRAM] Chat ID otomatik algılandı ve kaydedildi: {sender_chat_id}")

            # Güvenlik: Sadece kayıtlı Chat ID'nin komutlarına yanıt ver
            if sender_chat_id != self.chat_id:
                continue

            cmd = text.split()[0].lower().split("@")[0] if text else "/start"
            if cmd in handled_cmds_in_batch and not first_time_linked:
                continue
            handled_cmds_in_batch.add(cmd)

            print(f"[TELEGRAM KOMUT] Alındı: {cmd} (Gönderen: {sender_chat_id})")
            if first_time_linked or cmd in ("/start", "/yardim", "/help"):
                welcome = (
                    f"👋 Merhaba <b>{first_name}</b>!\n\n"
                    f"🚀 <b>Bybit 15dk Fibonacci Golden Zone Tarayıcı</b> aktif ve seninle eşleşti!\n"
                    f"🆔 Kayıtlı Chat ID: <code>{self.chat_id}</code>\n\n"
                    f"📌 <b>Kullanabileceğin Komutlar (Sol alttaki Menü'den de seçebilirsin):</b>\n"
                    f"  • /aktif — Şu an pusuda (Aşama 1) ve Altın Bölgede (Aşama 2) olan pariteler\n"
                    f"  • /durum — Tarayıcı çalışma istatistikleri ve son tarama bilgisi\n"
                    f"  • /tara — Hemen şimdi anlık tarama başlat\n"
                    f"  • /yardim — Bu menüyü göster"
                )
                self.send_message(welcome, sender_chat_id)
                if first_time_linked:
                    # İlk eşleşmede mevcut tüm aktif sinyalleri anında gönder!
                    summary = trigger_scan_cb()
                    self.send_message(summary, sender_chat_id)
            elif cmd == "/durum":
                self.send_message(get_status_cb(), sender_chat_id)
            elif cmd == "/aktif":
                self.send_message(get_active_cb(), sender_chat_id)
            elif cmd == "/tara":
                self.send_message("🔎 Anlık Bybit 15dk parite taraması başlatılıyor...", sender_chat_id)
                summary = trigger_scan_cb()
                self.send_message(summary, sender_chat_id)
