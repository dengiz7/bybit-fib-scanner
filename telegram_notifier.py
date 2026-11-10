# -*- coding: utf-8 -*-
"""
Telegram Bildirim ve Komut Modülü (Hacim Patlaması & Kırılım Sniper)
"""

import json
import ssl
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Callable, Dict, List, Optional

from config import Config
from strategy import BreakoutSetup

TR_TZ = timezone(timedelta(hours=3))


def fmt_price(price: float) -> str:
    if price >= 100:
        return f"{price:,.2f}"
    if price >= 1:
        return f"{price:,.4f}"
    if price >= 0.001:
        return f"{price:,.6f}"
    return f"{price:,.8f}"


def fmt_usd_compact(amount: float) -> str:
    if amount >= 1_000_000_000:
        return f"{amount / 1_000_000_000:.2f} Milyar $"
    if amount >= 1_000_000:
        return f"{amount / 1_000_000:.1f} Milyon $"
    if amount >= 1_000:
        return f"{amount / 1_000:.1f} Bin $"
    return f"{amount:.2f} $"


def fmt_usd_signed(val: float) -> str:
    return f"+{val:.2f}$" if val >= 0 else f"{val:.2f}$"


def format_setup_message(
    setup: BreakoutSetup,
    current_bankroll: float = Config.DEFAULT_BANKROLL_USD,
) -> str:
    is_long = setup.direction == "LONG"
    dir_emoji = "🟢" if is_long else "🔴"
    dir_text = "LONG (Yukarı Hacimli Kırılım)" if is_long else "SHORT (Aşağı Hacimli Kırılım)"
    level_label = "🔺 20-Mum Zirvesi" if is_long else "🔻 20-Mum Tabanı"

    bankroll = max(10.0, current_bankroll)
    margin_usd = max(2.50, round(bankroll * (Config.MARGIN_PER_TRADE_PCT / 100.0), 2))
    sl_ratio = (setup.sl_pct / 100.0) if setup.sl_pct > 0 else 0.015
    calc_lev = int(round(Config.MAX_RISK_USD / (margin_usd * sl_ratio)))
    lev = getattr(setup, "leverage", max(2, min(Config.DEFAULT_LEVERAGE, calc_lev)))
    pos_usd = margin_usd * lev

    tp1_lev = setup.tp1_pct * lev
    tp2_lev = setup.tp2_pct * lev
    sl_lev = setup.sl_pct * lev

    sl_loss_usd = min(Config.MAX_RISK_USD * 1.05, pos_usd * sl_ratio)
    tp1_win_usd = (pos_usd * (setup.tp1_pct / 100.0)) * Config.TP1_CLOSE_PCT
    tp2_win_usd = (pos_usd * (setup.tp2_pct / 100.0)) * (1.0 - Config.TP1_CLOSE_PCT)
    total_pot_win = tp1_win_usd + tp2_win_usd

    notes_str = "\n".join(f"  • {n}" for n in setup.notes)
    tv_url = f"https://www.tradingview.com/chart/?symbol=BYBIT:{setup.symbol}.P&interval=15"
    bybit_url = f"https://www.bybit.com/trade/usdt/{setup.symbol}"

    if Config.TP1_CLOSE_PCT >= 0.99:
        tp_section = (
            f"🎯 <b>KÂR ALMA HEDEFİ (TEK HEDEFLE %100 KAPANIŞ):</b>\n"
            f"  👉 <code>{fmt_price(setup.tp1_price)}</code> (Spot: <code>+%{setup.tp1_pct:.2f}</code> | {lev}x: <b>+%{tp1_lev:.1f}</b>)\n"
            f"  <i>📌 Eylem: Hedefe ulaşıldığında pozisyonun <b>%100'ü kapatılır (+{tp1_win_usd:.2f}$)</b> ve tam kâr kasaya eklenir!</i>\n\n"
        )
    else:
        tp_section = (
            f"🎯 <b>KÂR ALMA (TP) HEDEFLERİ:</b>\n"
            f"  1️⃣ <b>TP1 (1.6x ATR Hedefi — 1:{setup.rrr_tp1:.2f} RRR):</b>\n"
            f"     👉 <code>{fmt_price(setup.tp1_price)}</code> (Spot: <code>+%{setup.tp1_pct:.2f}</code> | {lev}x: <b>+%{tp1_lev:.1f}</b>)\n"
            f"     <i>📌 Eylem: Pozisyonun %{int(Config.TP1_CLOSE_PCT*100)}'ini kapatıp kârı cebe kilitle (+{tp1_win_usd:.2f}$), SL'yi Girişe çek!</i>\n"
            f"  2️⃣ <b>TP2 (2.8x ATR Trend Uzatması — 1:{setup.rrr_tp2:.2f} RRR):</b>\n"
            f"     👉 <code>{fmt_price(setup.tp2_price)}</code> (Spot: <code>+%{setup.tp2_pct:.2f}</code> | {lev}x: <b>+%{tp2_lev:.1f}</b>)\n"
            f"     <i>📌 Eylem: Kalan %{int((1-Config.TP1_CLOSE_PCT)*100)} pozisyonu kapat (+{tp2_win_usd:.2f}$) ve işlemi bitir!</i>\n\n"
        )

    msg = (
        f"🚀 <b>HACİM PATLAMASI & ZİRVE KIRILIMI (BREAKOUT SNIPER)</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"{dir_emoji} <b>Parite:</b> #{setup.symbol} | <b>15dk</b> | <b>{dir_text}</b>\n"
        f"💥 <b>Kırılan Seviye ({level_label}):</b> <code>{fmt_price(setup.breakout_level)}</code>\n"
        f"⚡ <b>Hacim Gücü:</b> <b>{setup.rvol}x RVOL</b> (24s Hacim: <code>{fmt_usd_compact(setup.turnover_24h)}</code>)\n\n"
        f"🧠 <b>KIRILIM ONAYLARI ({setup.strength_score}/100 — {setup.quality_grade}):</b>\n"
        f"{notes_str}\n\n"
        f"💼 <b>KASA & POZİSYON PLANI:</b>\n"
        f"  • 💵 <b>Kullanılan Marjin:</b> <b><code>{margin_usd:.2f}$</code></b> (Kasanın %{Config.MARGIN_PER_TRADE_PCT:.0f}'i Sabit Marjin)\n"
        f"  • ⚡ <b>Dinamik Kaldıraç:</b> <b>{lev}x İzole</b> (Pozisyon Büyüklüğü: <code>{pos_usd:.0f}$</code>)\n"
        f"  • 🛡 <b>Stop Olursa Maks. Risk:</b> <code>-{sl_loss_usd:.2f}$</code> (Sabit Risk Limiti: {Config.MAX_RISK_USD:.2f}$)\n"
        f"  • 🎯 <b>Hedef Kâr Kazancı:</b> <b><code>+{total_pot_win:.2f}$</code></b>\n\n"
        f"💰 <b>Onaylı Giriş Fiyatı:</b> <code>{fmt_price(setup.entry_price)}</code>\n"
        f"🛑 <b>Koruma Stop-Loss:</b> <code>{fmt_price(setup.sl_price)}</code> "
        f"(Spot: <code>-%{setup.sl_pct:.2f}</code> | {lev}x: <code>-%{sl_lev:.1f}</code>)\n\n"
        f"{tp_section}"
        f"🔗 <a href=\"{tv_url}\">TradingView Grafiği</a>  |  <a href=\"{bybit_url}\">Bybit Vadeli Tahtası</a>"
    )
    return msg


def format_tp_hit_message(
    trade: Dict,
    event_type: str,
    current_price: float,
    event_pnl_usd: float = 0.0,
    total_trade_pnl_usd: float = 0.0,
    current_bankroll: float = Config.DEFAULT_BANKROLL_USD,
) -> str:
    symbol = trade["symbol"]
    direction = trade["direction"]
    entry = float(trade["entry_price"])
    tp1 = float(trade["tp1_price"])
    tp2 = float(trade["tp2_price"])
    lev = float(trade.get("leverage") or Config.DEFAULT_LEVERAGE)
    dir_emoji = "🟢" if direction == "LONG" else "🔴"

    if event_type == "TP1":
        pct = abs(tp1 - entry) / entry * 100.0
        if Config.TP1_CLOSE_PCT >= 0.99:
            return (
                f"🎯 <b>TP HEDEFİ VURULDU (%100 KAPANIŞ)! — #{symbol}</b> ({dir_emoji} {direction})\n"
                f"━━━━━━━━━━━━━━━━━━━━━━\n"
                f"🚀 <b>Giriş:</b> <code>{fmt_price(entry)}</code> ➡️ <b>Çıkış:</b> <code>{fmt_price(tp1)}</code>\n"
                f"📈 <b>Fiyat Hareketi:</b> Spot <code>+%{pct:.2f}</code> | {lev:.0f}x: <b>+%{pct * lev:.1f}</b>\n"
                f"💵 <b>Cebe Kilitlenen Net Kâr (%100):</b> <b><code>{fmt_usd_signed(event_pnl_usd)}</code></b>\n"
                f"💼 <b>Güncel Kasa:</b> <b><code>{current_bankroll:.2f}$</code></b>\n\n"
                f"🎉 Pozisyonun %100'ü hedefe ulaştı, kâr nakde dönüştü ve işlem tamamlandı!"
            )
        return (
            f"✅ <b>TP1 HEDEFİ VURULDU! — #{symbol}</b> ({dir_emoji} {direction})\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🎯 <b>Giriş:</b> <code>{fmt_price(entry)}</code> ➡️ <b>TP1:</b> <code>{fmt_price(tp1)}</code>\n"
            f"📈 <b>Fiyat Hareketi:</b> Spot <code>+%{pct:.2f}</code> | {lev:.0f}x Kaldıraç: <b>+%{pct * lev:.1f}</b>\n"
            f"💵 <b>Cebe Kilitlenen Net Kâr (%75):</b> <b><code>{fmt_usd_signed(event_pnl_usd)}</code></b>\n"
            f"💼 <b>Güncel Kasa:</b> <b><code>{current_bankroll:.2f}$</code></b>\n\n"
            f"🛡 <b>KORUMA DEVREDE:</b>\n"
            f"• Pozisyonun <b>%75'i kapatıldı</b>.\n"
            f"• Kalan %25 için Stop-Loss <b>Giriş Fiyatına (Başabaş / Sıfır Risk)</b> çekildi!\n"
            f"• Sıradaki hedef: <b>TP2:</b> <code>{fmt_price(tp2)}</code>"
        )
    elif event_type == "TP2":
        pct = abs(tp2 - entry) / entry * 100.0
        return (
            f"🚀 <b>TP2 TAM TREND HEDEFİ VURULDU! — #{symbol}</b> ({dir_emoji} {direction})\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🎯 <b>Giriş:</b> <code>{fmt_price(entry)}</code> ➡️ <b>TP2:</b> <code>{fmt_price(tp2)}</code>\n"
            f"📈 <b>Toplam Hareket:</b> Spot <code>+%{pct:.2f}</code> | {lev}x: <b>+%{pct * lev:.1f}</b>\n"
            f"💵 <b>Bu İşlemin Toplam Net Kârı:</b> <b><code>{fmt_usd_signed(total_trade_pnl_usd)}</code></b>\n"
            f"💼 <b>Güncel Kasa:</b> <b><code>{current_bankroll:.2f}$</code></b>\n\n"
            f"🎉 <b>Tebrikler!</b> Kırılım dalgası hedefine ulaştı ve işlem tamamlandı."
        )
    elif event_type == "BREAKEVEN_STOP":
        return (
            f"🛡 <b>BAŞABAŞ (GİRİŞ SEVİYESİ) STOP — #{symbol}</b> ({dir_emoji} {direction})\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"📌 TP1'de <b>%75 kârı cebimize kilitlemiştik</b>. "
            f"Kalan %25'lik kısım giriş fiyatında sıfır zararla kapandı.\n"
            f"💵 <b>İşlemden Net Kazanç:</b> <b><code>{fmt_usd_signed(total_trade_pnl_usd)}</code></b> | "
            f"<b>Güncel Kasa:</b> <code>{current_bankroll:.2f}$</code>"
        )
    elif event_type == "STOP_LOSS":
        return (
            f"🛑 <b>STOP-LOSS ÇALIŞTI — #{symbol}</b> ({dir_emoji} {direction})\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"📌 Fiyat kırılım yönünde devam etmeyip koruma stop seviyesine geriledi.\n"
            f"💵 <b>Net Zarar:</b> <code>{fmt_usd_signed(event_pnl_usd)}</code>\n"
            f"💼 <b>Güncel Kasa:</b> <code>{current_bankroll:.2f}$</code>"
        )
    return ""


def format_hourly_report_message(
    window_label: str,
    day_label: str,
    hour_events: List[Dict],
    daily_events: List[Dict],
    active_trades_count: int,
    day_start_bankroll: float,
    current_bankroll: float,
    is_manual: bool = False,
) -> str:
    title_icon = "🔔" if not is_manual else "📊"
    title_prefix = "SAAT BAŞI DURUM VE BİLANÇO RAPORU" if not is_manual else "ANLIK İŞLEM VE KASA BİLANÇOSU"

    day_diff = current_bankroll - day_start_bankroll
    day_pct = (day_diff / day_start_bankroll) * 100.0 if day_start_bankroll > 0 else 0.0

    hour_pnl = sum(float(e.get("pnl_usd", 0.0)) for e in hour_events)
    hour_tp_cnt = sum(1 for e in hour_events if e.get("event_type") in ("TP1", "TP2"))
    hour_sl_cnt = sum(1 for e in hour_events if e.get("event_type") == "STOP_LOSS")

    daily_tp_cnt = sum(1 for e in daily_events if e.get("event_type") in ("TP1", "TP2"))
    daily_sl_cnt = sum(1 for e in daily_events if e.get("event_type") == "STOP_LOSS")

    tot_trades = daily_tp_cnt + daily_sl_cnt
    wr = (daily_tp_cnt / tot_trades * 100.0) if tot_trades > 0 else 0.0

    hour_lines = []
    for ev in hour_events[-10:]:
        sym = ev.get("symbol", "?")
        d = ev.get("direction", "LONG")
        d_icon = "🟢" if d == "LONG" else "🔴"
        pnl = float(ev.get("pnl_usd", 0.0))
        etype = ev.get("event_type", "TP1")
        hour_lines.append(f"  • [{ev.get('time_str', '--:--')}] #{sym} ({d_icon}) ➡️ <b>{etype}: {fmt_usd_signed(pnl)}</b>")

    hour_text = "\n".join(hour_lines) if hour_lines else "  • <i>Son 1 saat içinde kapanan işlem olmadı.</i>"

    return (
        f"{title_icon} <b>{title_prefix}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🕒 <b>Zaman Aralığı:</b> <code>{window_label}</code> ({day_label})\n"
        f"📈 <b>Şu An Açık İşlemler:</b> <b>{active_trades_count} adet</b>\n\n"
        f"⏱ <b>SON 1 SAATTEKİ İŞLEMLER ({hour_tp_cnt} TP / {hour_sl_cnt} SL):</b>\n"
        f"{hour_text}\n"
        f"👉 <b>Son 1 Saat Net PnL:</b> <b><code>{fmt_usd_signed(hour_pnl)}</code></b>\n\n"
        f"📅 <b>BUGÜNÜN TOPLAM PERFORMANSI ({day_label}):</b>\n"
        f"  • 🎯 <b>Hedef Kâr (TP):</b> <b>{daily_tp_cnt} adet</b>\n"
        f"  • 🛑 <b>Stop-Loss:</b> <b>{daily_sl_cnt} adet</b>\n"
        f"  • 📊 <b>Kazanma Oranı (Win Rate):</b> <b>%{wr:.1f}</b>\n"
        f"  • 🏦 <b>Gün Başı Kasa:</b> <code>{day_start_bankroll:.2f}$</code>\n"
        f"  • 💰 <b>Güncel Net Kasa:</b> <b><code>{current_bankroll:.2f}$</code></b>\n"
        f"  • 🚀 <b>Günün Toplam Net Kârı:</b> <b><code>{fmt_usd_signed(day_diff)} (%{day_pct:+.2f})</code></b>"
    )


class TelegramNotifier:
    def __init__(self, token: Optional[str] = None, chat_id: Optional[str] = None):
        Config.reload()
        self.token = (token or Config.TELEGRAM_BOT_TOKEN).strip()
        self.chat_id = (chat_id or Config.TELEGRAM_CHAT_ID).strip()
        self._last_update_id: int = 0
        self._ssl_ctx = ssl.create_default_context()

    @property
    def is_configured(self) -> bool:
        return bool(self.token and self.chat_id)

    def send_message(self, text: str, chat_id: Optional[str] = None) -> bool:
        tgt_chat = chat_id or self.chat_id
        if not self.token or not tgt_chat:
            return False
        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        payload = {
            "chat_id": tgt_chat,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        data = urllib.parse.urlencode(payload).encode("utf-8")
        req = urllib.request.Request(url, data=data, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=10, context=self._ssl_ctx) as resp:
                res = json.loads(resp.read().decode("utf-8"))
                return bool(res.get("ok"))
        except Exception as exc:
            print(f"[UYARI] Telegram mesajı gönderilemedi: {exc}")
            return False

    def register_bot_commands(self) -> bool:
        if not self.token:
            return False
        url = f"https://api.telegram.org/bot{self.token}/setMyCommands"
        commands = [
            {"command": "rapor", "description": "Saatlik ve günlük TP/SL kâr-zarar raporu"},
            {"command": "demo", "description": "Bybit Demo açık pozisyonlar ve bakiye"},
            {"command": "kasa", "description": "Kasa bütçesini 100$ yap veya sıfırla"},
            {"command": "oto_ac", "description": "Otomatik Bybit Demo işlemlerini başlat"},
            {"command": "oto_kapat", "description": "Otomatik işlemleri duraklat"},
            {"command": "durum", "description": "Tarayıcı durumu ve güncel kasa"},
            {"command": "tara", "description": "Hemen anlık Bybit parite taraması yap"},
            {"command": "yardim", "description": "Komut listesi ve strateji bilgisi"},
        ]
        data = json.dumps({"commands": commands}).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=8, context=self._ssl_ctx) as resp:
                res = json.loads(resp.read().decode("utf-8"))
                return bool(res.get("ok"))
        except Exception:
            return False

    def poll_commands(
        self,
        get_status_cb: Callable[[], str],
        trigger_scan_cb: Callable[[], str],
        get_report_cb: Optional[Callable[[], str]] = None,
        get_demo_cb: Optional[Callable[[], str]] = None,
        set_bankroll_cb: Optional[Callable[[float], str]] = None,
        toggle_auto_cb: Optional[Callable[[bool], str]] = None,
    ) -> None:
        if not self.token:
            return
        url = f"https://api.telegram.org/bot{self.token}/getUpdates?offset={self._last_update_id + 1}&timeout=1"
        req = urllib.request.Request(url, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=5, context=self._ssl_ctx) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except Exception:
            return

        if not data.get("ok"):
            return

        handled_cmds = set()
        for upd in data.get("result", []):
            upd_id = upd.get("update_id", 0)
            if upd_id > self._last_update_id:
                self._last_update_id = upd_id

            msg = upd.get("message") or upd.get("edited_message") or {}
            chat = msg.get("chat") or {}
            text = str(msg.get("text") or "").strip()
            sender_id = str(chat.get("id") or "").strip()

            if not sender_id or not text:
                continue

            if not self.chat_id:
                self.chat_id = sender_id
                from config import save_env_value
                save_env_value("TELEGRAM_CHAT_ID", sender_id)
                print(f"[TELEGRAM] Chat ID kaydedildi: {sender_id}")

            if sender_id != self.chat_id:
                continue

            parts = text.split()
            cmd = parts[0].lower().split("@")[0]
            if cmd in handled_cmds:
                continue
            handled_cmds.add(cmd)

            if cmd in ("/start", "/yardim", "/help"):
                welcome = (
                    "👋 <b>Hacim Patlaması & Zirve Kırılımı (Breakout Sniper) Botuna Hoş Geldin!</b>\n\n"
                    "🚀 <b>Strateji Mantığı:</b> 15 dakikalık mumlarda 20-mum zirvesini/tabanını <b>en az 1.8x - 2.5x hacim patlamasıyla</b> "
                    "kıran pariteleri yakalar; sahte kırılımları eler ve Bybit Demo hesabında otomatik emir açar.\n\n"
                    "📌 <b>Kullanabileceğin Komutlar:</b>\n"
                    "  • /rapor — Saatlik ve günlük TP/SL kâr-zarar bilançosu\n"
                    "  • /demo — Bybit Demo açık pozisyonlar ve bakiye\n"
                    "  • /kasa 100 — Kasa bütçesini 100$ yap ve pozisyonları sıfırla\n"
                    "  • /oto_ac ve /oto_kapat — Otomatik al-satı aç/kapat\n"
                    "  • /durum — Tarayıcı durumu ve güncel kasa\n"
                    "  • /tara — Anlık Bybit taraması başlat"
                )
                self.send_message(welcome, sender_id)
            elif cmd in ("/rapor", "/gunluk", "/bakiye"):
                if get_report_cb:
                    self.send_message(get_report_cb(), sender_id)
            elif cmd in ("/demo", "/cuzdan"):
                if get_demo_cb:
                    self.send_message(get_demo_cb(), sender_id)
            elif cmd in ("/kasa", "/sifirla", "/reset"):
                if set_bankroll_cb:
                    val = 100.0
                    if len(parts) > 1:
                        try:
                            val = float(parts[1].replace(",", ".").replace("$", ""))
                        except Exception:
                            val = 100.0
                    self.send_message(set_bankroll_cb(val), sender_id)
            elif cmd == "/oto_ac":
                if toggle_auto_cb:
                    self.send_message(toggle_auto_cb(True), sender_id)
            elif cmd == "/oto_kapat":
                if toggle_auto_cb:
                    self.send_message(toggle_auto_cb(False), sender_id)
            elif cmd == "/durum":
                self.send_message(get_status_cb(), sender_id)
            elif cmd == "/tara":
                self.send_message("🔎 Hacim patlaması ve kırılım taraması yapılıyor...", sender_id)
                summary = trigger_scan_cb()
                self.send_message(summary, sender_id)
