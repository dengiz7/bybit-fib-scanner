# -*- coding: utf-8 -*-
"""
Hacim Patlaması ve Zirve Kırılımı (RVOL Breakout Sniper) + Akıllı Risk Filtresi Motoru
- 15 Dakikalık Mumlar Üzerinde Donchian (20 Mum) Tavan / Taban Kırılımlarını Tarar
- Şart 1: Fiyat son 20 mumun (5 saat) zirvesini yukarı (LONG) veya tabanını aşağı (SHORT) kırmalıdır.
- Şart 2: Kırılım mumu güçlü olmalı (Gövde boyu >= %45, sadece fitil değil, net kapanış!).
- Şart 3: Kırılım mumu 20 mumluk ortalama hacmin en az 1.8x katı olmalı (RVOL >= 1.8).
- Şart 4: FİLTRE — Kırılım mumunda ters yönde uzun fitil (rejection / tepeden/dipten ret) olmamalı!
- Şart 5: FİLTRE — Fiyat kırılım seviyesinden ATR'nin 1.4 katından fazla uzaklaşmamış olmalı (Geç kalınmış sinyalleri reddet).
- Şart 6: FİLTRE — 1 Saatlik Genel Trend (48-Mum EMA) yönünde olmalı (Trend tersi zayıf tepkileri reddet).
- Şart 7: FİLTRE — Son mumlarda tükenme (Exhaustion / RSI 74+ veya 26-) olmamalı.
- Şart 8: FİLTRE — BTC ani ters yönde hareket etmiyor olmalı.
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
    recommended_margin_usd: float   # Önerilen Marjin Tutarı ($ - Kasanın %10'u)
    notes: List[str]                # Kırılım Notları
    leverage: int = 30              # Dinamik Stop Riski İçin Hesaplanmış Kaldıraç


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


def compute_rsi(candles: List[Candle], period: int = 14) -> float:
    if len(candles) < period + 1:
        return 50.0
    gains = []
    losses = []
    for i in range(len(candles) - period, len(candles)):
        chg = candles[i].close - candles[i - 1].close
        if chg > 0:
            gains.append(chg)
            losses.append(0.0)
        else:
            gains.append(0.0)
            losses.append(abs(chg))
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def compute_ema(candles: List[Candle], period: int = 48) -> float:
    if len(candles) < period:
        return 0.0
    k = 2.0 / (period + 1.0)
    ema = candles[0].close
    for c in candles[1:]:
        ema = (c.close * k) + (ema * (1.0 - k))
    return ema


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
    btc_regime: Optional[str] = None,
) -> Optional[BreakoutSetup]:
    """15 dakikalık mumlarda Hacim Patlamalı 20-Mum Zirve/Taban Kırılımını ve Akıllı Risk Filtrelerini analiz eder."""
    lookback = Config.BREAKOUT_LOOKBACK
    if len(candles) < lookback + 15:
        return None

    closed = candles[:-1]
    live_c = candles[-1]
    c = closed[-1]       # Yeni kapanan kırılım adayı mumu

    atr = compute_atr(closed, 14)
    if atr <= 0:
        return None

    # Devasa anormal manipülasyon mumu varsa (tek mum > 4.2x ATR) ele
    candle_range = max(1e-12, c.high - c.low)
    if candle_range > 4.2 * atr:
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

    # ------------------------------------------------------------------
    # 🛡️ AKILLI RİSK FİLTRESİ (RISK FILTER RULES - ŞÜPHEDEYSEN REDDET)
    # ------------------------------------------------------------------

    # Kural 1: Ters Yöndeki Fitil Uzunsa (Rejection / Tepeden/Tabandan Ret Yemiş Mum) -> REDDET!
    if direction == "LONG":
        upper_wick = c.high - c.close
        if upper_wick > Config.MAX_REJECTION_WICK_RATIO * candle_range:
            return None  # Tepeden güçlü ret fitili var, sahte kırılım, REDDET!
    elif direction == "SHORT":
        lower_wick = c.close - c.low
        if lower_wick > Config.MAX_REJECTION_WICK_RATIO * candle_range:
            return None  # Tabandan güçlü ret fitili var, sahte kırılım, REDDET!

    # Kural 2: Fiyat Kırılım Seviyesinden Zaten Çok Uzaklaşmışsa (Chase / Geç Kalınmış Sinyal) -> REDDET!
    dist_from_breakout = abs(c.close - breakout_level)
    max_chase_dist = Config.MAX_CHASE_ATR_RATIO * atr
    if dist_from_breakout > max_chase_dist:
        return None  # Fiyat çoktan fırlamış, tepeden/dipten kovalamak (FOMO) yasak, REDDET!

    # Kural 3: Canlı Mumda Sahte Kırılım veya Aşırı Uzaklaşma Kontrolü
    if direction == "LONG":
        if live_c.close < breakout_level:
            return None  # Sahte kırılım, canlı mum seviyenin altına çöktü!
        if live_c.close > c.close + (0.50 * atr):
            return None  # Canlı mumda fiyat daha da fırladı, geç kalındı!
    elif direction == "SHORT":
        if live_c.close > breakout_level:
            return None  # Sahte kırılım, canlı mum seviyenin üstüne çöktü!
        if live_c.close < c.close - (0.50 * atr):
            return None  # Canlı mumda fiyat daha da fırladı, geç kalındı!

    # Kural 4: 1 Saatlik Genel Trend Uyumu (48 Mum = 12 Saatlik EMA Trend Filtresi)
    ema_trend = compute_ema(closed, 48)
    if ema_trend > 0:
        if direction == "LONG" and c.close < ema_trend:
            return None  # 1 Saatlik genel düşüş trendinin tersine cılız kırılım, REDDET!
        elif direction == "SHORT" and c.close > ema_trend:
            return None  # 1 Saatlik genel yükseliş trendinin tersine cılız kırılım, REDDET!

    # Kural 5: Tükenme (Exhaustion / RSI Aşırı Şişme) Filtresi
    rsi_14 = compute_rsi(closed, 14)
    if direction == "LONG" and rsi_14 > 74.0:
        return None  # Aşırı alım tükeniş bölgesi, REDDET!
    elif direction == "SHORT" and rsi_14 < 26.0:
        return None  # Aşırı satım tükeniş bölgesi, REDDET!

    # Kural 6: BTC Piyasa Rejimi / Korelasyon Filtresi
    if btc_regime == "BTC_DUMP" and direction == "LONG":
        return None  # BTC sert çakılırken altcoin LONG'u REDDET!
    elif btc_regime == "BTC_PUMP" and direction == "SHORT":
        return None  # BTC sert fırlarken altcoin SHORT'u REDDET!

    # ------------------------------------------------------------------
    # FİYAT HEDEFLERİ & KÂR/ZARAR PLANI
    # ------------------------------------------------------------------
    entry_p = c.close
    tp1_dist = max(Config.TP1_ATR_MULT * atr, entry_p * (Config.MIN_TP1_MOVE_PCT / 100.0))
    tp2_dist = max(Config.TP2_ATR_MULT * atr, tp1_dist * 1.75)

    # Stop-Loss: Kırılan yapının (destek/direnç) ve kırılım mumu fitilinin arkasında güvenli bölge
    # Not: Stop'u entry'den değil, kırılan yapıdan geriye koyuyoruz ki doğal retest stop ettirmesin!
    if direction == "LONG":
        tp1_p = entry_p + tp1_dist
        tp2_p = entry_p + tp2_dist
        struct_sl = breakout_level - (Config.SL_ATR_MULT * atr)
        candle_sl = c.low - (0.20 * atr)
        sl_p = min(struct_sl, candle_sl, entry_p - (0.80 * atr))
    else:
        tp1_p = entry_p - tp1_dist
        tp2_p = entry_p - tp2_dist
        struct_sl = breakout_level + (Config.SL_ATR_MULT * atr)
        candle_sl = c.high + (0.20 * atr)
        sl_p = max(struct_sl, candle_sl, entry_p + (0.80 * atr))

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

    # Sabit %10 Marjin & Dinamik Kaldıraç Hesabı (Maks 4.00$ Sabit Dolar Riski)
    # Her işleme kasanın %10'u marjinle girilir; kaldıraç stop mesafesine göre otomatik ayarlanır!
    margin_usd = max(2.50, round(bankroll_usd * (Config.MARGIN_PER_TRADE_PCT / 100.0), 2))
    sl_ratio = (sl_pct / 100.0) if sl_pct > 0 else 0.015
    calc_lev = int(round(Config.MAX_RISK_USD / (margin_usd * sl_ratio)))
    dyn_lev = max(2, min(Config.DEFAULT_LEVERAGE, calc_lev))
    notes.append(f"⚡ Dinamik Kaldıraç: {dyn_lev}x ({margin_usd:.1f}$ Marjin ➡️ Maks. {Config.MAX_RISK_USD:.2f}$ Stop Riski)")

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
        leverage=dyn_lev,
        notes=notes,
    )


def analyze_symbol(
    symbol: str,
    candles: List[Candle],
    ticker: TickerInfo,
    bankroll_usd: float = Config.DEFAULT_BANKROLL_USD,
    btc_regime: Optional[str] = None,
) -> Optional[BreakoutSetup]:
    """Ana Tarama Arayüzü: Hacim Patlamalı Kırılımı ve Risk Filtrelerini analiz eder."""
    return analyze_breakout(symbol, candles, ticker, bankroll_usd, btc_regime)
