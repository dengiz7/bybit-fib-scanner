# -*- coding: utf-8 -*-
"""
Hacim Patlaması ve Zirve Kırılımı (RVOL Breakout Sniper) Strateji Motoru
- 15 Dakikalık Mumlar Üzerinde Donchian (20 Mum) Tavan / Taban Kırılımlarını Tarar
- Şart 1: Fiyat son 20 mumun (5 saat) zirvesini yukarı (LONG) veya tabanını aşağı (SHORT) kırmalıdır.
- Şart 2: Kırılım mumu güçlü olmalı (Gövde boyu >= %45, sadece fitil değil, net kapanış!).
- Şart 3: Kırılım mumu 20 mumluk ortalama hacmin en az 1.8x - 2.0x katı olmalı (RVOL >= 1.8).
- Şart 4: Komisyonu ezen net kâr hedefi: TP1 en az %1.40 saf fiyat hareketi (15x ile +%21+ kâr).
- Kâr Alma & Koruma: TP1'de %75 kâr cebe kilitlenir, Stop girişe (Başabaş) çekilir, kalan %25 TP2'ye koşar.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, List, Optional

from bybit_client import Candle, TickerInfo
from config import Config


@dataclass
class BreakoutSetup:
    setup_id: str
    symbol: str
    direction: str                  # "LONG" veya "SHORT"
    breakout_level: float           # Kırılan 20-mum zirvesi veya tabanı
    entry_price: float              # Kırılım mumu kapanış fiyatı
    sl_price: float                 # Kırılan seviyenin arkası (1.15x ATR)
    tp1_price: float                # Ana Kâr Hedefi (%75 Kapanış + Başabaş Stop)
    tp2_price: float                # İkinci Uzatma Hedefi (Kalan %25)
    tp1_pct: float                  # TP1 fiyat yüzdesi (Spot %)
    tp2_pct: float                  # TP2 fiyat yüzdesi (Spot %)
    sl_pct: float                   # SL fiyat yüzdesi (Spot %)
    rrr_tp1: float                  # TP1 Risk/Ödül oranı
    rrr_tp2: float                  # TP2 Risk/Ödül oranı
    rvol: float                     # Bağıl Hacim Patlama Katsayısı (örn. 2.8x)
    turnover_24h: float             # 24s Dolar Hacmi
    session_label: str              # Aktif Seans
    strength_score: int             # Sinyal Güç Skoru (70 - 100)
    quality_grade: str              # "A+" veya "A"
    recommended_margin_usd: float   # Önerilen Marjin Tutarı ($)
    notes: List[str]                # Kırılım Notları


def compute_atr(candles: List[Candle], period: int = 14) -> float:
    n = len(candles)
    if n < 2:
        return 0.0
    start = max(1, n - period)
    trs = []
    for i in range(start, n):
        h = candles[i].high
        l = candles[i].low
        pc = candles[i - 1].close
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    return sum(trs) / len(trs) if trs else 0.0


def compute_volume_sma(candles: List[Candle], period: int = 20) -> float:
    n = len(candles)
    if n < 1:
        return 0.0
    start = max(0, n - period)
    seg = candles[start:n]
    return sum(c.volume for c in seg) / len(seg) if seg else 0.0


def get_session_label(now_utc: Optional[datetime] = None) -> str:
    if now_utc is None:
        now_utc = datetime.now(timezone.utc)
    h = now_utc.hour
    if 12 <= h < 16:
        return "🔥 Londra + New York Kesişimi (En Yüksek Hacim)"
    if 7 <= h < 12:
        return "🇬🇧 Londra Seansı Aktif"
    if 16 <= h < 21:
        return "🇺🇸 New York Seansı Aktif"
    return "🌙 Asya / Gece Seansı"


def analyze_breakout(
    symbol: str,
    candles: List[Candle],
    ticker: TickerInfo,
    bankroll_usd: float = Config.DEFAULT_BANKROLL_USD,
) -> Optional[BreakoutSetup]:
    """15 dakikalık mumlarda Hacim Patlamalı 20-Mum Zirve/Taban Kırılımını analiz eder."""
    lookback = Config.BREAKOUT_LOOKBACK
    if len(candles) < lookback + 15:
        return None

    closed = candles[:-1]
    live_c = candles[-1]
    c = closed[-1]       # Yeni kapanan kırılım adayı mumu
    prev = closed[-2]

    atr = compute_atr(closed, 14)
    if atr <= 0:
        return None

    # Devasa anormal manipülasyon mumu varsa (tek mum > 4.5x ATR) ele
    candle_range = max(1e-12, c.high - c.low)
    if candle_range > 4.5 * atr:
        return None

    # Hacim Hesabı
    v_sma = compute_volume_sma(closed[:-1], 20)
    if v_sma <= 0:
        return None
    rvol = c.volume / v_sma

    if rvol < Config.MIN_RVOL:
        return None

    # Son 'lookback' mumun tavan ve taban seviyeleri (kırılım mumu hariç)
    window = closed[-(lookback + 1): -1]
    high_level = max(x.high for x in window)
    low_level = min(x.low for x in window)

    body_size = abs(c.close - c.open)
    body_ratio = body_size / candle_range
    if body_ratio < Config.MIN_BODY_RATIO:
        return None

    direction = None
    breakout_level = 0.0
    notes: List[str] = []

    # 1) YUKARI KIRILIM (BULLISH BREAKOUT)
    if c.close > high_level and c.close > c.open:
        direction = "LONG"
        breakout_level = high_level
        notes.append(f"✅ {lookback} Mumluk Zirve Yukarı Kırıldı ({high_level:.4f})")
        notes.append(f"⚡ Güçlü Hacim Patlaması: {rvol:.1f}x RVOL")
        notes.append("📈 Güçlü Boğa Mum Gövdesi Onaylandı")

    # 2) AŞAĞI KIRILIM (BEARISH BREAKDOWN)
    elif c.close < low_level and c.close < c.open:
        direction = "SHORT"
        breakout_level = low_level
        notes.append(f"✅ {lookback} Mumluk Taban Aşağı Kırıldı ({low_level:.4f})")
        notes.append(f"⚡ Güçlü Hacim Patlaması: {rvol:.1f}x RVOL")
        notes.append("📉 Güçlü Ayı Mum Gövdesi Onaylandı")

    if direction is None:
        return None

    entry_p = c.close
    tp1_dist = max(Config.TP1_ATR_MULT * atr, entry_p * (Config.MIN_TP1_MOVE_PCT / 100.0))
    tp2_dist = max(Config.TP2_ATR_MULT * atr, tp1_dist * 1.75)
    sl_dist = max(Config.SL_ATR_MULT * atr, entry_p * 0.008)

    if direction == "LONG":
        tp1_p = entry_p + tp1_dist
        tp2_p = entry_p + tp2_dist
        sl_p = entry_p - sl_dist
    else:
        tp1_p = entry_p - tp1_dist
        tp2_p = entry_p - tp2_dist
        sl_p = entry_p + sl_dist

    # Canlı mumda fiyat çoktan TP1'e ulaştıysa veya SL'yi vurduysa geç kalmış sinyali alma
    if direction == "LONG" and (live_c.high >= tp1_p or live_c.low <= sl_p):
        return None
    if direction == "SHORT" and (live_c.low <= tp1_p or live_c.high >= sl_p):
        return None

    tp1_pct = abs(tp1_p - entry_p) / entry_p * 100.0
    tp2_pct = abs(tp2_p - entry_p) / entry_p * 100.0
    sl_pct = abs(entry_p - sl_p) / entry_p * 100.0

    if tp1_pct < Config.MIN_TP1_MOVE_PCT or sl_pct < 0.35:
        return None

    rrr1 = round(tp1_pct / sl_pct, 2) if sl_pct > 0 else 1.50
    rrr2 = round(tp2_pct / sl_pct, 2) if sl_pct > 0 else 2.60

    # Puanlama
    score = 75
    if rvol >= 2.5:
        score += 15
        notes.append(f"🔥 Devasa Kurumsal Hacim ({rvol:.1f}x)")
    elif rvol >= 2.0:
        score += 10
    if body_ratio >= 0.65:
        score += 10
        notes.append("🚀 Kusursuz Momentum Mum Kapanışı")

    score = min(99, score)
    grade = "A+" if score >= 85 else "A"

    # Marjin Hesabı (Kasanın %20'si)
    margin_usd = max(4.0, round(bankroll_usd * (Config.MARGIN_PER_TRADE_PCT / 100.0), 2))

    return BreakoutSetup(
        setup_id=f"{symbol}_{direction}_BRK_{c.timestamp}",
        symbol=symbol,
        direction=direction,
        breakout_level=breakout_level,
        entry_price=entry_p,
        sl_price=sl_p,
        tp1_price=tp1_p,
        tp2_price=tp2_p,
        tp1_pct=round(tp1_pct, 2),
        tp2_pct=round(tp2_pct, 2),
        sl_pct=round(sl_pct, 2),
        rrr_tp1=rrr1,
        rrr_tp2=rrr2,
        rvol=round(rvol, 2),
        turnover_24h=ticker.turnover_24h,
        session_label=get_session_label(),
        strength_score=score,
        quality_grade=grade,
        recommended_margin_usd=margin_usd,
        notes=notes,
    )


def analyze_symbol(
    symbol: str,
    candles: List[Candle],
    ticker: TickerInfo,
    bankroll_usd: float = Config.DEFAULT_BANKROLL_USD,
) -> Optional[BreakoutSetup]:
    """Ana Tarama Arayüzü: Hacim Patlamalı Kırılımı analiz eder."""
    return analyze_breakout(symbol, candles, ticker, bankroll_usd)
