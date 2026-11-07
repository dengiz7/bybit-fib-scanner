# -*- coding: utf-8 -*-
"""
Telegram Bildirim, Kasa Yönetimi ve Saatlik/Günlük Bilanço Yöneticisi (%100 Ücretsiz Telegram Bot API)
- Aşama 1 (BoS + Geri Çekilme -> 0.618 Limit Emir Pususu + SMC/ICT Güç Skoru + Kasa % Önerisi)
- Aşama 2 (0.5 - 0.618 Altın Bölgeye Giriş + Mum Onayı + Kasa % Önerisi)
- Canlı TP1 / TP2 / TP3 / Başabaş Stop / SL Bildirimleri ($ Kâr/Zarar Hesaplı)
- Her Saat Başı (Saatlik + Genel Durum) ve Gün Bitimi (24s Gün Sonu Bilançosu) Otomatik Raporları
- Canlı Komutlar: /aktif, /durum, /rapor, /tara, /yardim
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


def fmt_usd_signed(val: float) -> str:
    if val > 0:
        return f"+{val:.2f}$"
    if val < 0:
        return f"-{abs(val):.2f}$"
    return "0.00$"


def fmt_ts_tr(ms: int) -> str:
    dt = datetime.fromtimestamp(ms / 1000.0, tz=TR_TZ)
    return dt.strftime("%H:%M")


def calc_pct_diff(entry: float, target: float) -> float:
    if entry == 0:
        return 0.0
    return abs(target - entry) / entry * 100.0


def get_grade_badge(grade: str) -> str:
    if grade == "A+":
        return "🔥 A+ KALİTE (⭐⭐⭐⭐⭐ — Kurumsal Kesişim)"
    if grade == "A":
        return "⚡ A KALİTE (⭐⭐⭐⭐ — Güçlü Kurulum)"
    return "⚖️ B KALİTE (⭐⭐⭐ — Standart / Temkinli)"


def format_setup_message(
    setup: FibSetup,
    alert_type: str,
    current_bankroll: float = Config.DEFAULT_BANKROLL_USD,
) -> str:
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
    lev = Config.DEFAULT_LEVERAGE
    tp1_10x = tp1_pct * lev
    tp2_10x = tp2_pct * lev
    tp3_10x = tp3_pct * lev
    sl_10x = sl_pct * lev

    # Kasa Yönetimi ve Dolar ($) Hesaplaması
    bankroll = max(10.0, current_bankroll)
    alloc_pct = setup.recommended_bankroll_pct
    margin_usd = bankroll * (alloc_pct / 100.0)
    pos_usd = margin_usd * lev

    sl_loss_usd = pos_usd * (sl_pct / 100.0)
    # Kademeli çıkış (%50 TP1, %25 TP2, %25 TP3) ve tam hedef karşılıkları
    tp1_full_usd = pos_usd * (tp1_pct / 100.0)
    tp2_full_usd = pos_usd * (tp2_pct / 100.0)
    tp3_full_usd = pos_usd * (tp3_pct / 100.0)

    grade_badge = get_grade_badge(setup.quality_grade)
    conf_list = setup.confluence_notes or ["✅ Temel Fib + BoS"]
    conf_str = " | ".join(conf_list)

    tv_url = f"https://www.tradingview.com/chart/?symbol=BYBIT:{setup.symbol}.P&interval=15"
    bybit_url = f"https://www.bybit.com/trade/usdt/{setup.symbol}"

    flip_banner = ""
    if setup.is_trend_flip:
        flip_banner = (
            "\n🔄 <b>TREND DÖNÜŞÜ (FLIP) KURULUMU:</b> Önceki ters yönlü kurulum stop oldu ve "
            "videodaki kural gereği yeni trend yönünde BoS onaylandı!\n"
        )

    if alert_type == "STAGE_1":
        header = "🎯 <b>AŞAMA 1 — FİBONACCİ KURULUMU HAZIR (LİMİT EMİR PUSUSU)</b>"
        status_line = (
            f"⏳ <b>Durum:</b> BoS mum kapanışıyla onaylandı, geri çekilme başladı "
            f"(Şu an: <code>%{setup.current_retracement * 100:.1f}</code> geri çekildi).\n"
            f"📌 <b>Eylem:</b> <code>0.618</code> seviyesine Limit Emir kurabilir veya Altın Bölge bildirimini bekleyebilirsin."
        )
    else:
        header = "🔥 <b>AŞAMA 2 — FİYAT ALTIN BÖLGEDE (0.500 – 0.618)!</b>"
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
        f"🧠 <b>SİNYAL GÜCÜ & SMC / ICT ANALİZİ:</b>\n"
        f"  • <b>Güç Skoru:</b> <code>{setup.strength_score} / 100</code> — <b>{grade_badge}</b>\n"
        f"  • <b>Kesişimler:</b> {conf_str}\n\n"
        f"💼 <b>KASA YÖNETİMİ ÖNERİSİ (Kasa: {bankroll:.2f}$ | {lev}x İzole):</b>\n"
        f"  • 💵 <b>Önerilen Giriş:</b> Kasanın <b>%{alloc_pct}'i</b> (<b><code>{margin_usd:.2f}$</code> Marjin</b> ➡️ <code>{pos_usd:.0f}$</code> Pozisyon)\n"
        f"  • 🛡 <b>Stop Olursa Riskin:</b> <code>-{sl_loss_usd:.2f}$</code> <i>(Kasanın sadece %{(sl_loss_usd / bankroll) * 100:.1f}'i)</i>\n"
        f"  • 🎯 <b>Tam Kâr Potansiyeli:</b> TP1: <b>+{tp1_full_usd:.2f}$</b> | TP2: <b>+{tp2_full_usd:.2f}$</b> | TP3: <b>+{tp3_full_usd:.2f}$</b>\n\n"
        f"💰 <b>Anlık Fiyat:</b> <code>{fmt_price(setup.current_price)}</code>\n"
        f"  • 🎯 <b>İdeal Giriş (0.618 Limit):</b> <code>{fmt_price(setup.fib.level_0618)}</code>\n"
        f"  • 🟠 <b>Altın Bölge Bandı:</b> <code>{fmt_price(setup.fib.level_0500)}</code> – <code>{fmt_price(setup.fib.level_0618)}</code>\n"
        f"  • 🛑 <b>Stop-Loss (1'in Dışı):</b> <code>{fmt_price(setup.fib.stop_loss)}</code> "
        f"(Spot: <code>-%{sl_pct:.2f}</code> | {lev}x: <code>-%{sl_10x:.1f}</code>)\n\n"
        f"🎯 <b>KÂR ALMA (TP) & İZ SÜREN STOP HEDEFLERİ:</b>\n"
        f"  1️⃣ <b>TP1 (0.0 Güvenli Hedef — 1:{setup.rrr_at_0618:.2f} RRR):</b>\n"
        f"     👉 <code>{fmt_price(setup.fib.level_0)}</code> (Spot: <code>+%{tp1_pct:.2f}</code> | {lev}x: <b>+%{tp1_10x:.1f}</b>)\n"
        f"     <i>📌 Eylem: Pozisyonun %50'sini kapat (+{tp1_full_usd * 0.50:.2f}$ cebe), SL'yi Girişe (Başabaş) çek!</i>\n"
        f"  2️⃣ <b>TP2 (-0.5 Uzatma — 1:{setup.rrr_tp2:.2f} RRR):</b>\n"
        f"     👉 <code>{fmt_price(setup.fib.level_neg_05)}</code> (Spot: <code>+%{tp2_pct:.2f}</code> | {lev}x: <b>+%{tp2_10x:.1f}</b>)\n"
        f"     <i>📌 Eylem: %25 daha kâr al (+{tp2_full_usd * 0.25:.2f}$ ek kâr), SL'yi TP1 seviyesine taşı!</i>\n"
        f"  3️⃣ <b>TP3 (-1.0 Tam Uzatma — 1:{setup.rrr_tp3:.2f} RRR):</b>\n"
        f"     👉 <code>{fmt_price(setup.fib.level_neg_10)}</code> (Spot: <code>+%{tp3_pct:.2f}</code> | {lev}x: <b>+%{tp3_10x:.1f}</b>)\n"
        f"     <i>📌 Eylem: Kalan %25 pozisyonu tamamen kapat (+{tp3_full_usd * 0.25:.2f}$ ek kâr)!</i>\n\n"
        f"🔍 <b>FİLTRE KONTROLLERİ:</b>\n"
        f"  • İtkisel Güç: <code>{setup.impulse_atr_mult}x ATR</code> | Hacim: <code>{setup.rvol}x RVOL</code>\n"
        f"  • 24s Hacim: <code>{fmt_usd_compact(setup.turnover_24h)}</code>\n"
        f"  • Seans: {setup.session_label}\n\n"
        f"🔗 <a href=\"{tv_url}\">TradingView Grafiği</a>  |  <a href=\"{bybit_url}\">Bybit Vadeli Tahtası</a>"
    )
    return msg


def format_tp_hit_message(
    trade: Dict,
    tp_stage: str,
    current_price: float,
    event_pnl_usd: float = 0.0,
    total_trade_pnl_usd: float = 0.0,
    current_bankroll: float = Config.DEFAULT_BANKROLL_USD,
) -> str:
    """Aktif takip edilen işlem TP1, TP2, TP3 veya SL vurduğunda gönderilecek bildirim."""
    symbol = trade["symbol"]
    direction = trade["direction"]
    entry = float(trade["entry_0618"])
    tp1 = float(trade["tp1"])
    tp2 = float(trade["tp2"])
    tp3 = float(trade["tp3"])
    grade = trade.get("quality_grade", "A")
    alloc_pct = int(trade.get("bankroll_pct", 6))
    margin_usd = float(trade.get("margin_usd", 6.0))
    lev = Config.DEFAULT_LEVERAGE
    dir_emoji = "🟢" if direction == "LONG" else "🔴"

    if tp_stage == "TP1":
        pct = calc_pct_diff(entry, tp1)
        return (
            f"✅ <b>TP1 (0.0 GÜVENLİ HEDEF) VURULDU! — #{symbol}</b> ({dir_emoji} {direction} | {grade} Kalite)\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🎯 <b>0.618 Giriş:</b> <code>{fmt_price(entry)}</code> ➡️ <b>TP1:</b> <code>{fmt_price(tp1)}</code>\n"
            f"📈 <b>Hareket:</b> Spot <code>+%{pct:.2f}</code> | {lev}x Kaldıraç: <b>+%{pct * lev:.1f}</b> (<b>1 : 1.50 RRR</b>)\n"
            f"💵 <b>Kasa Etkisi (%{alloc_pct} / {margin_usd:.1f}$ Girişle):</b>\n"
            f"  • Cebe Giren (%50 Kapanış): <b><code>{fmt_usd_signed(event_pnl_usd)}</code></b>\n"
            f"  • Güncel Kasa: <b><code>{current_bankroll:.2f}$</code></b>\n\n"
            f"🛡 <b>ŞİMDİ NE YAPMALI? (Video Kuralı):</b>\n"
            f"1. Pozisyonun <b>%50'sini</b> kârla kapat.\n"
            f"2. Kalan %50 için Stop-Loss'u giriş seviyene (<code>{fmt_price(entry)}</code> - <b>Başabaş / Sıfır Risk</b>) çek!\n"
            f"3. Sıradaki uzatma hedefi: <b>TP2 (-0.5):</b> <code>{fmt_price(tp2)}</code>"
        )
    elif tp_stage == "TP2":
        pct = calc_pct_diff(entry, tp2)
        return (
            f"🚀 <b>TP2 (-0.5 UZATMA HEDEFİ) VURULDU! — #{symbol}</b> ({dir_emoji} {direction} | {grade} Kalite)\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🎯 <b>0.618 Giriş:</b> <code>{fmt_price(entry)}</code> ➡️ <b>TP2:</b> <code>{fmt_price(tp2)}</code>\n"
            f"📈 <b>Hareket:</b> Spot <code>+%{pct:.2f}</code> | {lev}x Kaldıraç: <b>+%{pct * lev:.1f}</b> (<b>1 : 2.71 RRR</b>)\n"
            f"💵 <b>Kasa Etkisi:</b>\n"
            f"  • Bu Kademeden Eklenen (%25): <b><code>{fmt_usd_signed(event_pnl_usd)}</code></b>\n"
            f"  • Bu İşlemden Toplam Kâr: <b><code>{fmt_usd_signed(total_trade_pnl_usd)}</code></b>\n"
            f"  • Güncel Kasa: <b><code>{current_bankroll:.2f}$</code></b>\n\n"
            f"🛡 <b>ŞİMDİ NE YAPMALI? (Trailing Stop):</b>\n"
            f"1. Pozisyonun <b>%25'ini</b> daha kapat.\n"
            f"2. İz süren Stop-Loss'u <b>TP1 (<code>{fmt_price(tp1)}</code>)</b> seviyesine taşı!\n"
            f"3. Son hedef: <b>TP3 (-1.0):</b> <code>{fmt_price(tp3)}</code>"
        )
    elif tp_stage == "TP3":
        pct = calc_pct_diff(entry, tp3)
        return (
            f"🏆 <b>TP3 (-1.0 TAM UZATMA HEDEFİ) VURULDU! — #{symbol}</b> ({dir_emoji} {direction} | {grade} Kalite)\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🎯 <b>0.618 Giriş:</b> <code>{fmt_price(entry)}</code> ➡️ <b>TP3:</b> <code>{fmt_price(tp3)}</code>\n"
            f"📈 <b>Toplam Hareket:</b> Spot <code>+%{pct:.2f}</code> | {lev}x Kaldıraç: <b>+%{pct * lev:.1f}</b> (<b>1 : 3.93 RRR</b>)\n"
            f"💵 <b>Kasa Etkisi:</b>\n"
            f"  • Son Kademe (%25): <b><code>{fmt_usd_signed(event_pnl_usd)}</code></b>\n"
            f"  • İşlem Toplam Net Kârı: <b><code>{fmt_usd_signed(total_trade_pnl_usd)}</code></b>\n"
            f"  • Güncel Kasa: <b><code>{current_bankroll:.2f}$</code></b>\n\n"
            f"🎉 <b>Tebrikler!</b> Videodaki tüm Fibonacci uzatma hedefleri eksiksiz tamamlandı."
        )
    elif tp_stage == "BREAKEVEN_STOP":
        return (
            f"🛡 <b>BAŞABAŞ (GİRİŞ SEVİYESİ) STOP ÇALIŞTI — #{symbol}</b> ({dir_emoji} {direction} | {grade} Kalite)\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"📌 TP1 (<code>{fmt_price(tp1)}</code>) hedefinde <b>%50 kâr cebimize girmişti</b>. "
            f"Kalan %50 pozisyon giriş seviyesinde (<code>{fmt_price(entry)}</code>) sıfır zararla kapandı.\n"
            f"💵 <b>Bu İşlemden Net Kazanç:</b> <b><code>{fmt_usd_signed(total_trade_pnl_usd)}</code></b> | "
            f"<b>Güncel Kasa:</b> <code>{current_bankroll:.2f}$</code>"
        )
    elif tp_stage == "TRAILING_TP1_STOP":
        return (
            f"🛡 <b>İZ SÜREN STOP (TP1 SEVİYESİNDE) ÇALIŞTI — #{symbol}</b> ({dir_emoji} {direction} | {grade} Kalite)\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"📌 TP1 ve TP2 hedefleri vurulmuştu (%75 kâr alındı). "
            f"Son %25'lik kısım da TP1 (<code>{fmt_price(tp1)}</code>) seviyesinde kârla kapandı ({fmt_usd_signed(event_pnl_usd)})!\n"
            f"💵 <b>Bu İşlemden Toplam Net Kazanç:</b> <b><code>{fmt_usd_signed(total_trade_pnl_usd)}</code></b> | "
            f"<b>Güncel Kasa:</b> <code>{current_bankroll:.2f}$</code>"
        )
    elif tp_stage == "MISSED_NO_PULLBACK":
        return (
            f"⚠️ <b>PUSU İPTAL (ALTIN BÖLGEYE DÖNMEDEN GİTTİ) — #{symbol}</b> ({dir_emoji} {direction})\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"📌 Fiyat <code>0.500 – 0.618</code> Altın Bölgesine geri çekilmeden (limit emri almadan) "
            f"doğrudan <code>0.0</code> (<code>{fmt_price(tp1)}</code>) hedefini kırdı (Video 2 [04:40] Kuralı).\n"
            f"🗑 <b>Eylem:</b> <code>{fmt_price(entry)}</code> seviyesinde bekleyen Limit Emrin varsa <b>İPTAL ET</b> (Kasa etkisi: 0.00$)."
        )
    else:
        return (
            f"🛑 <b>STOP-LOSS OLDU (1.0 SEVİYESİ KIRILDI) — #{symbol}</b> ({dir_emoji} {direction} | {grade} Kalite)\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"⚠️ Fiyat <code>1.0</code> başlangıç yapısını (<code>{fmt_price(float(trade['current_sl']))}</code>) kırdı.\n"
            f"💵 <b>Kasa Etkisi (%{alloc_pct} / {margin_usd:.1f}$ Girişle):</b> <b><code>{fmt_usd_signed(event_pnl_usd)}</code></b> | "
            f"<b>Güncel Kasa:</b> <code>{current_bankroll:.2f}$</code>\n"
            f"🔄 <b>Video Kuralı (Flip):</b> Sistem artık #{symbol} paritesinde <b>ters yönde</b> yeni yapı kırılımı (BoS) arıyor!"
        )


def _summarize_trade_outcomes(events: List[Dict]) -> Dict:
    """
    Olay listesini (TP1, TP2, TP3, STOP_LOSS, BREAKEVEN_STOP, TRAILING_TP1_STOP)
    benzersiz işlem (setup_id) bazında ve kademe bazında özetler.
    """
    by_trade: Dict[str, Dict] = {}
    tp1_count = 0
    tp2_count = 0
    tp3_count = 0
    sl_count = 0
    net_usd = 0.0

    for ev in events:
        etype = ev.get("event_type", "")
        pnl = float(ev.get("pnl_usd", 0.0))
        net_usd += pnl
        tid = ev.get("setup_id", f"{ev.get('symbol')}_{ev.get('ts', 0)}")

        if etype == "TP1":
            tp1_count += 1
        elif etype == "TP2":
            tp2_count += 1
        elif etype == "TP3":
            tp3_count += 1
        elif etype == "STOP_LOSS":
            sl_count += 1

        info = by_trade.setdefault(
            tid,
            {
                "symbol": ev.get("symbol", "?"),
                "direction": ev.get("direction", "LONG"),
                "grade": ev.get("grade", "A"),
                "margin_usd": float(ev.get("margin_usd", 15.0)),
                "events": [],
                "net_usd": 0.0,
                "last_time": ev.get("time_str", ""),
            },
        )
        info["events"].append(etype)
        info["net_usd"] += pnl
        info["last_time"] = ev.get("time_str", info["last_time"])

    # Benzersiz işlem bazında kaç tanesi TP (kazançlı), kaç tanesi SL (zararlı)
    unique_tp_trades = sum(1 for t in by_trade.values() if "TP1" in t["events"] or t["net_usd"] > 0)
    unique_sl_trades = sum(1 for t in by_trade.values() if "STOP_LOSS" in t["events"] and "TP1" not in t["events"])
    total_decided = unique_tp_trades + unique_sl_trades
    win_rate = (unique_tp_trades / total_decided * 100.0) if total_decided > 0 else 0.0

    return {
        "by_trade": by_trade,
        "unique_tp": unique_tp_trades,
        "unique_sl": unique_sl_trades,
        "total_decided": total_decided,
        "win_rate": win_rate,
        "tp1_count": tp1_count,
        "tp2_count": tp2_count,
        "tp3_count": tp3_count,
        "sl_count": sl_count,
        "net_usd": net_usd,
    }


def format_hourly_report_message(
    window_label: str,
    day_label: str,
    hour_events: List[Dict],
    daily_events: List[Dict],
    active_in_trade_count: int,
    active_waiting_count: int,
    day_start_bankroll: float,
    current_bankroll: float,
    is_manual: bool = False,
) -> str:
    """
    Her saat başı otomatik (veya /rapor komutuyla anlık) gönderilen:
    1) Son 1 saat içinde TP ve SL olan işlemler
    2) Günün genel durumu (toplam TP, toplam SL, kazanma oranı ve $ kasa bilançosu)
    """
    h_sum = _summarize_trade_outcomes(hour_events)
    d_sum = _summarize_trade_outcomes(daily_events)

    title = (
        f"📊 <b>ANLIK PERFORMANS VE GENEL DURUM RAPORU ({window_label})</b>"
        if is_manual
        else f"⏰ <b>SAAT BAŞI PERFORMANS & GENEL DURUM RAPORU ({window_label})</b>"
    )

    # 1. Bölüm: Son 1 saatte olanlar
    hour_lines: List[str] = []
    if not h_sum["by_trade"]:
        hour_lines.append("  • <i>Son 1 saat içinde yeni TP veya SL olan işlem olmadı; açık kurulumlar takip ediliyor.</i>")
    else:
        for t in h_sum["by_trade"].values():
            d_icon = "🟢" if t["direction"] == "LONG" else "🔴"
            ev_badges = " ➡️ ".join(t["events"])
            res_icon = "✅" if t["net_usd"] >= 0 else "🛑"
            hour_lines.append(
                f"  {res_icon} <b>#{t['symbol']}</b> ({d_icon} {t['direction']} | {t['grade']}) — "
                f"<b>{ev_badges}</b> (<code>{fmt_usd_signed(t['net_usd'])}</code>)"
            )

    day_net_usd = current_bankroll - day_start_bankroll
    day_net_pct = (day_net_usd / day_start_bankroll * 100.0) if day_start_bankroll > 0 else 0.0

    msg = (
        f"{title}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🕒 <b>1) SON 1 SAAT İÇİNDE OLANLAR:</b>\n"
        f"  • Saatlik Özet: <b>✅ {h_sum['unique_tp']} Kazanç (TP)</b> | <b>🛑 {h_sum['unique_sl']} Stop (SL)</b> | "
        f"Net: <b><code>{fmt_usd_signed(h_sum['net_usd'])}</code></b>\n"
        + "\n".join(hour_lines)
        + "\n\n"
        f"📈 <b>2) GÜNÜN GENEL DURUMU ({day_label} — 00:00'dan Beri):</b>\n"
        f"  • <b>Toplam Sonuçlanan İşlem:</b> <b>{d_sum['total_decided']}</b> "
        f"(<b>✅ {d_sum['unique_tp']} TP</b>  |  <b>🛑 {d_sum['unique_sl']} SL</b>)\n"
        f"  • <b>Başarı Oranı (Win Rate):</b> <code>%{d_sum['win_rate']:.1f}</code>\n"
        f"  • <b>Vurulan Kademeler:</b> TP1: <b>{d_sum['tp1_count']}</b> | TP2: <b>{d_sum['tp2_count']}</b> | TP3: <b>{d_sum['tp3_count']}</b>\n"
        f"  • <b>Şu An Takip Edilen:</b> 🔥 <b>{active_in_trade_count}</b> Altın Bölgede (İşlemde) | 🎯 <b>{active_waiting_count}</b> Pusuda\n\n"
        f"💰 <b>KASA VE KAZANÇ BİLANÇOSU:</b>\n"
        f"  • <b>Gün Başı Kasa:</b> <code>{day_start_bankroll:.2f}$</code>\n"
        f"  • <b>Bugünkü Net Kazanç:</b> <b><code>{fmt_usd_signed(day_net_usd)}</code></b> (<b><code>%{day_net_pct:+.2f}</code></b>)\n"
        f"  • 🏦 <b>Güncel Kasa Bakiyesi:</b> <b><code>{current_bankroll:.2f}$</code></b>"
    )
    return msg


def format_daily_end_report_message(
    day_label: str,
    daily_events: List[Dict],
    day_start_bankroll: float,
    day_end_bankroll: float,
) -> str:
    """Her gün bitiminde (00:00 TSİ) gönderilen detaylı 24 saatlik Gün Sonu Bilançosu."""
    d_sum = _summarize_trade_outcomes(daily_events)
    day_net_usd = day_end_bankroll - day_start_bankroll
    day_net_pct = (day_net_usd / day_start_bankroll * 100.0) if day_start_bankroll > 0 else 0.0

    trade_lines: List[str] = []
    if not d_sum["by_trade"]:
        trade_lines.append("  • <i>Bugün sonuçlanan işlem kaydı bulunmuyor.</i>")
    else:
        for t in list(d_sum["by_trade"].values())[:25]:
            d_icon = "🟢" if t["direction"] == "LONG" else "🔴"
            ev_badges = "+".join(t["events"])
            res_icon = "✅" if t["net_usd"] >= 0 else "🛑"
            trade_lines.append(
                f"  {res_icon} <b>#{t['symbol']}</b> ({d_icon} {t['direction']} | {t['grade']}) ➡️ "
                f"{ev_badges}: <b><code>{fmt_usd_signed(t['net_usd'])}</code></b>"
            )

    return (
        f"🏁 <b>GÜN SONU BİLANÇOSU VE KAZANÇ RAPORU ({day_label})</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📊 <b>24 SAATLİK İŞLEM İSTATİSTİKLERİ:</b>\n"
        f"  • <b>Toplam Sonuçlanan İşlem:</b> <b>{d_sum['total_decided']}</b>\n"
        f"  • ✅ <b>Toplam TP (Kazançlı İşlem):</b> <b>{d_sum['unique_tp']}</b> "
        f"<i>(TP1: {d_sum['tp1_count']} | TP2: {d_sum['tp2_count']} | TP3: {d_sum['tp3_count']})</i>\n"
        f"  • 🛑 <b>Toplam SL (Stop Olan İşlem):</b> <b>{d_sum['unique_sl']}</b>\n"
        f"  • 🏆 <b>Günlük Başarı Oranı (Win Rate):</b> <b><code>%{d_sum['win_rate']:.1f}</code></b>\n\n"
        f"💰 <b>GÜN SONU KASA & NET KAZANÇ TABLOSU:</b>\n"
        f"  • <b>Gün Başlangıç Kasası:</b> <code>{day_start_bankroll:.2f}$</code>\n"
        f"  • <b>Günlük Net Kâr/Zarar:</b> <b><code>{fmt_usd_signed(day_net_usd)}</code></b> (<b><code>%{day_net_pct:+.2f}</code></b>)\n"
        f"  • 🏦 <b>Yeni Güne Devreden Kasa:</b> <b><code>{day_end_bankroll:.2f}$</code></b>\n\n"
        f"📜 <b>GÜNÜN SONUÇLANAN İŞLEMLERİ:</b>\n"
        + "\n".join(trade_lines)
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
            {"command": "rapor", "description": "Saatlik ve günlük TP/SL + Kasa Kazanç bilançosu"},
            {"command": "demo", "description": "Bybit Demo açık pozisyonlar ve bekleyen emirler"},
            {"command": "aktif", "description": "Pusudaki ve Altın Bölgedeki aktif kurulumlar"},
            {"command": "durum", "description": "Tarayıcı çalışma durumu ve güncel 100$ kasa bilgisi"},
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
        get_report_cb: Optional[Callable[[], str]] = None,
        get_demo_cb: Optional[Callable[[], str]] = None,
        set_bankroll_cb: Optional[Callable[[float], str]] = None,
        toggle_auto_cb: Optional[Callable[[bool], str]] = None,
    ) -> None:
        """
        Telegram'dan gelen /start, /durum, /aktif, /rapor, /demo, /kasa, /oto_ac, /oto_kapat, /tara komutlarını dinler.
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
        for upd in updates:
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

            first_time_linked = False
            if not self.chat_id:
                self.chat_id = sender_chat_id
                save_env_value("TELEGRAM_CHAT_ID", sender_chat_id)
                first_time_linked = True
                print(f"[TELEGRAM] Chat ID otomatik algılandı ve kaydedildi: {sender_chat_id}")

            if sender_chat_id != self.chat_id:
                continue

            parts = text.split() if text else ["/start"]
            cmd = parts[0].lower().split("@")[0]
            if cmd in handled_cmds_in_batch and not first_time_linked:
                continue
            handled_cmds_in_batch.add(cmd)

            print(f"[TELEGRAM KOMUT] Alındı: {cmd} (Gönderen: {sender_chat_id})")
            if first_time_linked or cmd in ("/start", "/yardim", "/help"):
                welcome = (
                    f"👋 Merhaba <b>{first_name}</b>!\n\n"
                    f"🚀 <b>Bybit 15dk Fibonacci + SMC/ICT + Demo Otomatik İşlem Botu</b> aktif!\n"
                    f"🆔 Kayıtlı Chat ID: <code>{self.chat_id}</code>\n\n"
                    f"📌 <b>Kullanabileceğin Komutlar (Sol alttaki Menü'den de seçebilirsin):</b>\n"
                    f"  • /rapor — Saatlik ve günlük TP/SL sayısı + Kasa Kazanç bilançosu\n"
                    f"  • /demo — Bybit Demo açık pozisyonlar, bekleyen emirler ve cüzdan\n"
                    f"  • /kasa 100 — Kasa bütçesini 100$ (veya istediğin tutar) olarak ayarla/sıfırla\n"
                    f"  • /oto_ac ve /oto_kapat — Bybit Demo otomatik emir açmayı aç/kapat\n"
                    f"  • /aktif — Şu an pusuda (Aşama 1) ve Altın Bölgede (Aşama 2) olan pariteler\n"
                    f"  • /durum — Tarayıcı çalışma istatistikleri ve güncel kasa bilgisi\n"
                    f"  • /tara — Hemen şimdi anlık tarama başlat"
                )
                self.send_message(welcome, sender_chat_id)
                if first_time_linked:
                    summary = trigger_scan_cb()
                    self.send_message(summary, sender_chat_id)
            elif cmd in ("/rapor", "/gunluk", "/bakiye"):
                if get_report_cb is not None:
                    self.send_message(get_report_cb(), sender_chat_id)
            elif cmd in ("/demo", "/cuzdan"):
                if get_demo_cb is not None:
                    self.send_message(get_demo_cb(), sender_chat_id)
            elif cmd == "/kasa":
                if set_bankroll_cb is not None:
                    val = 100.0
                    if len(parts) > 1:
                        try:
                            val = float(parts[1].replace(",", ".").replace("$", ""))
                        except Exception:
                            val = 100.0
                    self.send_message(set_bankroll_cb(val), sender_chat_id)
            elif cmd == "/oto_ac":
                if toggle_auto_cb is not None:
                    self.send_message(toggle_auto_cb(True), sender_chat_id)
            elif cmd == "/oto_kapat":
                if toggle_auto_cb is not None:
                    self.send_message(toggle_auto_cb(False), sender_chat_id)
            elif cmd == "/durum":
                self.send_message(get_status_cb(), sender_chat_id)
            elif cmd == "/aktif":
                self.send_message(get_active_cb(), sender_chat_id)
            elif cmd == "/tara":
                self.send_message("🔎 Anlık Bybit 15dk parite taraması başlatılıyor...", sender_chat_id)
                summary = trigger_scan_cb()
                self.send_message(summary, sender_chat_id)
