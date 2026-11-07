# -*- coding: utf-8 -*-
"""
The Moving Average (Arty) - Fibonacci Retracement Strateji Motoru (15m Mumlar)
İki videodaki tüm kuralların harfi harfine matematiksel ve nedensel (causal) uygulaması:
  1. Kısa vadeli trend tespiti: Yükselişte Higher Lows (HL) + HH, Düşüşte Lower Highs (LH) + LL
  2. Fitiller (wicks) piyasa yapısına dahildir (Video 1 [03:16])
  3. Yapı Kırılımı (BoS) için önceki yapı fitil seviyesinin ötesinde MUM KAPANIŞI şarttır (Video 1 [03:24])
  4. Yatay/testere piyasa (choppy range), devasa haber/manipülasyon mumu (ATR spike) ve düşük hacim elenir
  5. İtkisel hareket bitip belirgin geri çekilme başladığında 6 seviyeli Fib çizilir: 1, 0.618, 0.5, 0, -0.5, -1
  6. Aşama 1: 0.618 Limit Emir pususu | Aşama 2: 0.5 - 0.618 Altın Bölge girişi (+ mum onayı kontrolü)
  7. Stop-Loss (1 seviyesinin hemen dışı) kırılırsa ters yönde trend dönüş izlemesi (Flip Watch) başlar (Video 2 [05:00])
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, List, Optional

from bybit_client import Candle, TickerInfo
from config import Config

MAJOR_SYMBOLS = {"BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT"}


@dataclass
class SwingPoint:
    idx: int
    timestamp: int
    price: float
    kind: str  # "HIGH" veya "LOW"


@dataclass
class FibLevels:
    level_1: float       # 1.000 - Başlangıç (Origin)
    level_0618: float    # 0.618 - Altın Cep (Ana Limit Giriş)
    level_0500: float    # 0.500 - Altın Bölge Sınırı
    level_0: float       # 0.000 - Bitiş / Ana Güvenli Hedef (TP1)
    level_neg_05: float  # -0.500 - 1. Uzatma (TP2 / Trailing)
    level_neg_10: float  # -1.000 - 2. Uzatma (TP3 / Trailing)
    stop_loss: float     # 1.000 seviyesinin hemen dışı (%3 leg buffer)


@dataclass
class FibSetup:
    setup_id: str
    symbol: str
    direction: str                  # "LONG" veya "SHORT"
    stage: str                      # "STAGE_1_LIMIT_READY" | "STAGE_2_GOLDEN_ZONE" | "STOPPED_OUT"
    origin_timestamp: int
    bos_timestamp: int
    extreme_timestamp: int
    broken_structure_price: float
    current_price: float
    current_retracement: float      # 0.0 = hedefte, 0.5 = %50 geri çekilme, 0.618 = altın cep, 1.0 = başlangıçta
    deepest_retracement: float
    touched_0618: bool
    candle_confirmation: Optional[str]  # Bölge içi ret fitili / yutan mum onayı varsa açıklaması
    fib: FibLevels
    rrr_at_0618: float
    rrr_tp2: float = 2.71
    rrr_tp3: float = 3.93
    rvol: float = 1.0
    impulse_atr_mult: float = 1.0
    turnover_24h: float = 0.0
    session_label: str = ""
    is_trend_flip: bool = False
    strength_score: int = 65
    quality_grade: str = "A"
    recommended_bankroll_pct: int = 15
    confluence_notes: Optional[List[str]] = None


def compute_atr(candles: List[Candle], end_idx: int, period: int = Config.ATR_PERIOD) -> float:
    """Belirtilen end_idx (dahil değil) öncesindeki mumlar üzerinden ortalama gerçek aralığı (ATR) hesaplar."""
    start = max(1, end_idx - period)
    if end_idx <= start:
        rngs = [c.high - c.low for c in candles[: max(1, end_idx)]]
        return sum(rngs) / len(rngs) if rngs else 0.0

    trs: List[float] = []
    for i in range(start, end_idx):
        high = candles[i].high
        low = candles[i].low
        prev_close = candles[i - 1].close
        tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        trs.append(tr)
    return sum(trs) / len(trs) if trs else 0.0


def compute_avg_volume(candles: List[Candle], end_idx: int, period: int = Config.VOLUME_SMA_PERIOD) -> float:
    start = max(0, end_idx - period)
    segment = candles[start:end_idx]
    if not segment:
        return 0.0
    return sum(c.volume for c in segment) / len(segment)


def compute_ema(candles: List[Candle], period: int) -> float:
    """Kapanış fiyatları üzerinden Üstel Hareketli Ortalama (EMA) hesaplar."""
    if not candles:
        return 0.0
    if len(candles) <= period:
        return sum(c.close for c in candles) / len(candles)
    k = 2.0 / (period + 1.0)
    ema = sum(c.close for c in candles[:period]) / period
    for c in candles[period:]:
        ema = c.close * k + ema * (1.0 - k)
    return ema


def evaluate_confluence_score(
    closed_candles: List[Candle],
    origin_idx: int,
    peak_idx: int,
    fib: FibLevels,
    direction: str,
    rvol: float,
    impulse_atr_mult: float,
    efficiency: float,
    candle_confirmation: Optional[str],
    is_trend_flip: bool,
) -> tuple[int, str, int, List[str]]:
    """
    Ana Fibonacci stratejisini çorbaya çevirmeden, kurumsal SMC / ICT ve Trend kesişimlerini
    puanlar (40 - 100 Puan) ve kasanın yüzde kaçıyla girilmesi gerektiğini belirler.
    Dönüş: (score, grade, bankroll_pct, notes_list)
    """
    score = 45  # Temel Arty Fibonacci + BoS + ATR + Verimlilik + Kâr Filtresi taban puanı
    notes: List[str] = ["✅ Temel Fib + Mum Kapanışlı BoS"]

    leg = abs(fib.level_0 - fib.level_1)
    gz_low = min(fib.level_0500, fib.level_0618) - 0.10 * leg
    gz_high = max(fib.level_0500, fib.level_0618) + 0.10 * leg

    # 1) ICT Fair Value Gap (FVG / İmbalans Boşluğu) Kesişimi (+20 Puan)
    has_fvg_in_gz = False
    has_any_fvg = False
    for i in range(max(2, origin_idx + 1), min(len(closed_candles), peak_idx + 1)):
        c_prev2 = closed_candles[i - 2]
        c_curr = closed_candles[i]
        if direction == "LONG" and c_curr.low > c_prev2.high:
            has_any_fvg = True
            fvg_bot, fvg_top = c_prev2.high, c_curr.low
            if max(fvg_bot, gz_low) <= min(fvg_top, gz_high):
                has_fvg_in_gz = True
                break
        elif direction == "SHORT" and c_curr.high < c_prev2.low:
            has_any_fvg = True
            fvg_bot, fvg_top = c_curr.high, c_prev2.low
            if max(fvg_bot, gz_low) <= min(fvg_top, gz_high):
                has_fvg_in_gz = True
                break

    if has_fvg_in_gz:
        score += 20
        notes.append("✅ Altın Bölgede FVG (İmbalans Mıknatısı)")
    elif has_any_fvg:
        score += 10
        notes.append("✅ İtkisel Dalgada FVG Boşluğu")

    # 2) SMC Order Block (OB / Kurumsal Emir Bloğu Kalkanı) (+15 Puan)
    has_ob_shield = False
    ob_start = max(0, origin_idx - 2)
    ob_end = min(len(closed_candles) - 1, origin_idx + 2)
    for i in range(ob_start, ob_end + 1):
        c = closed_candles[i]
        if direction == "LONG" and c.close < c.open:
            # Son satıcılı mumun gövde/fitil tavanı 0.618 - 1.000 arasını koruyor mu?
            if c.high >= fib.level_1 and c.low <= fib.level_0618:
                has_ob_shield = True
                break
        elif direction == "SHORT" and c.close > c.open:
            if c.low <= fib.level_1 and c.high >= fib.level_0618:
                has_ob_shield = True
                break

    if has_ob_shield:
        score += 15
        notes.append("✅ 0.618 Arkasında Order Block (OB) Kalkanı")

    # 3) Ana Trend (EMA 50 & EMA 200) Uyumu (+10 Puan)
    ema50 = compute_ema(closed_candles, 50)
    ema200 = compute_ema(closed_candles, 200)
    last_close = closed_candles[-1].close
    if direction == "LONG":
        if last_close >= ema50 and ema50 >= ema200:
            score += 10
            notes.append("✅ EMA 50/200 Ana Yükseliş Trendi Uyumu")
        elif last_close >= ema50:
            score += 6
            notes.append("✅ EMA 50 Trend Uyumu")
        else:
            notes.append("⚠️ Ana Trende Karşı (Tepki/Dönüş İşlemi)")
    else:
        if last_close <= ema50 and ema50 <= ema200:
            score += 10
            notes.append("✅ EMA 50/200 Ana Düşüş Trendi Uyumu")
        elif last_close <= ema50:
            score += 6
            notes.append("✅ EMA 50 Trend Uyumu")
        else:
            notes.append("⚠️ Ana Trende Karşı (Tepki/Dönüş İşlemi)")

    # 4) Hacim Patlaması (RVOL) ve İtkisel Momentum (+10 Puan)
    if rvol >= 1.50 or (impulse_atr_mult >= 2.2 and efficiency >= 0.55):
        score += 10
        notes.append(f"✅ Kurumsal Hacim & Momentum ({rvol:.1f}x RVOL)")
    elif rvol >= 1.25:
        score += 5

    # 5) Altın Bölge Mum Onayı (Pin Bar / Engulfing) veya Trend Flip (+10 Puan)
    if candle_confirmation:
        score += 10
        notes.append("✅ Altın Bölge Mum Onaylı (Pin Bar/Engulfing)")
    elif is_trend_flip:
        score += 5
        notes.append("✅ Stop Sonrası Taze Trend Dönüşü (Flip)")

    score = min(100, max(40, score))

    if score >= 80:
        grade = "A+"
        bankroll_pct = 20  # 100$ kasada 20$ Marjin (10x İzole)
    elif score >= 65:
        grade = "A"
        bankroll_pct = 15  # 100$ kasada 15$ Marjin (10x İzole)
    else:
        grade = "B"
        bankroll_pct = 8   # 100$ kasada 8$ Marjin (10x İzole)

    return score, grade, bankroll_pct, notes


def get_session_label(now_utc: Optional[datetime] = None) -> str:
    """Londra (07:00-16:00 UTC) ve New York (12:00-21:00 UTC) seans durumunu etiketler."""
    if now_utc is None:
        now_utc = datetime.now(timezone.utc)
    h = now_utc.hour
    if 12 <= h < 16:
        return "🔥 Londra + New York Kesişimi (En Yüksek Hacim)"
    if 7 <= h < 12:
        return "🇬🇧 Londra Seansı Aktif"
    if 16 <= h < 21:
        return "🇺🇸 New York Seansı Aktif"
    return "🌙 Asya / Gece Seansı (Algoritmik Hacim Onaylı)"


def extract_market_structure(
    closed_candles: List[Candle],
    left: int = Config.PIVOT_LEFT_BARS,
    right: int = Config.PIVOT_RIGHT_BARS,
) -> List[SwingPoint]:
    """
    Kapanmış mumların FİTİL UÇLARINI (high/low) kullanarak fraktal Swing High ve Swing Low noktalarını bulur
    ve ardışık (alternating) ZigZag piyasa yapısına dönüştürür.
    """
    n = len(closed_candles)
    if n < left + right + 1:
        return []

    raw_pivots: List[SwingPoint] = []
    for i in range(left, n - right):
        c = closed_candles[i]
        is_high = all(c.high >= closed_candles[i - k].high for k in range(1, left + 1)) and all(
            c.high > closed_candles[i + k].high for k in range(1, right + 1)
        )
        is_low = all(c.low <= closed_candles[i - k].low for k in range(1, left + 1)) and all(
            c.low < closed_candles[i + k].low for k in range(1, right + 1)
        )

        if is_high and not is_low:
            raw_pivots.append(SwingPoint(idx=i, timestamp=c.timestamp, price=c.high, kind="HIGH"))
        elif is_low and not is_high:
            raw_pivots.append(SwingPoint(idx=i, timestamp=c.timestamp, price=c.low, kind="LOW"))
        elif is_high and is_low:
            # Çok nadir dıştan yutan mum durumu: mum yönüne göre ata
            if c.close >= c.open:
                raw_pivots.append(SwingPoint(idx=i, timestamp=c.timestamp, price=c.high, kind="HIGH"))
            else:
                raw_pivots.append(SwingPoint(idx=i, timestamp=c.timestamp, price=c.low, kind="LOW"))

    # Ardışık aynı tip pivotları temizle (iki HIGH üst üste gelirse daha yüksek olanı, iki LOW gelirse daha düşük olanı tut)
    cleaned: List[SwingPoint] = []
    for p in raw_pivots:
        if not cleaned:
            cleaned.append(p)
            continue
        last = cleaned[-1]
        if last.kind == p.kind:
            if (p.kind == "HIGH" and p.price >= last.price) or (p.kind == "LOW" and p.price <= last.price):
                cleaned[-1] = p
        else:
            cleaned.append(p)

    return cleaned


def build_fib_levels(origin: float, extreme: float, direction: str) -> FibLevels:
    """Videodaki 6 özel Fibonacci seviyesini (1, 0.618, 0.5, 0, -0.5, -1) ve SL'yi hesaplar."""
    leg = abs(extreme - origin)
    buf = Config.SL_BUFFER_RATIO * leg

    if direction == "LONG":
        # origin = Swing Low (1.0), extreme = Swing High (0.0)
        return FibLevels(
            level_1=origin,
            level_0618=extreme - 0.618 * leg,
            level_0500=extreme - 0.500 * leg,
            level_0=extreme,
            level_neg_05=extreme + 0.500 * leg,
            level_neg_10=extreme + 1.000 * leg,
            stop_loss=origin - buf,
        )
    else:
        # SHORT: origin = Swing High (1.0), extreme = Swing Low (0.0)
        return FibLevels(
            level_1=origin,
            level_0618=extreme + 0.618 * leg,
            level_0500=extreme + 0.500 * leg,
            level_0=extreme,
            level_neg_05=extreme - 0.500 * leg,
            level_neg_10=extreme - 1.000 * leg,
            stop_loss=origin + buf,
        )


def check_candle_confirmation(
    closed_candles: List[Candle],
    from_idx: int,
    fib: FibLevels,
    direction: str,
) -> Optional[str]:
    """
    Video 1 [01:33-01:40]: Altın Bölge (0.5 - 0.618) içinde kapanmış mum formasyonu onayı var mı kontrol eder:
      - Continuation / Rejection Wick (Ret Fitili / Pin Bar)
      - Bullish / Bearish Engulfing (Yutan Mum)
    """
    n = len(closed_candles)
    for i in range(max(1, from_idx), n):
        c = closed_candles[i]
        prev = closed_candles[i - 1]
        rng = c.high - c.low
        if rng <= 0:
            continue
        body = abs(c.close - c.open)

        if direction == "LONG":
            # Mum altın bölgeye değmiş ve 0.618/0.5 üzerinde güçlü kapanmış mı?
            if c.low <= fib.level_0500 and c.low > fib.stop_loss:
                lower_wick = min(c.open, c.close) - c.low
                # 1) Güçlü alt ret fitili (Pinbar / Rejection Wick)
                if lower_wick >= 0.45 * rng and c.close >= c.open and c.close >= fib.level_0618:
                    return "Altın Bölgede Alıcı Ret Fitili (Pin Bar Onaylı)"
                # 2) Bullish Engulfing (Boğa Yutan Mum)
                if (
                    c.close > c.open
                    and prev.close < prev.open
                    and c.close >= prev.open
                    and c.open <= prev.close
                ):
                    return "Altın Bölgede Boğa Yutan Mum (Bullish Engulfing Onaylı)"
        else:
            # SHORT
            if c.high >= fib.level_0500 and c.high < fib.stop_loss:
                upper_wick = c.high - max(c.open, c.close)
                # 1) Güçlü üst ret fitili
                if upper_wick >= 0.45 * rng and c.close <= c.open and c.close <= fib.level_0618:
                    return "Altın Bölgede Satıcı Ret Fitili (Pin Bar Onaylı)"
                # 2) Bearish Engulfing (Ayı Yutan Mum)
                if (
                    c.close < c.open
                    and prev.close > prev.open
                    and c.close <= prev.open
                    and c.open >= prev.close
                ):
                    return "Altın Bölgede Ayı Yutan Mum (Bearish Engulfing Onaylı)"
    return None


def _evaluate_candidate(
    symbol: str,
    direction: str,
    origin_sw: SwingPoint,
    prev_target_sw: SwingPoint,
    prev_same_sw: SwingPoint,
    older_target_sw: Optional[SwingPoint],
    older_same_sw: Optional[SwingPoint],
    closed_candles: List[Candle],
    live_candle: Candle,
    ticker: TickerInfo,
    flipped_from_direction: Optional[str] = None,
) -> Optional[FibSetup]:
    """Tek bir salınım adayını videodaki tüm filtreler ve nedensel mum simülasyonuyla değerlendirir."""
    n_closed = len(closed_candles)

    # 1) KISA VADELİ TREND ŞARTI (Video 1 [06:02] & Video 2 [01:51])
    if direction == "LONG":
        # Yükseliş trendi: Higher Low (HL) oluşmalı
        if origin_sw.price <= prev_same_sw.price:
            return None
        # Testere (choppy) filtresi: Ya önceki tepeler de yükseliyor olmalı (HH+HL),
        # ya ardışık 2 Higher Low olmalı, ya da stop sonrası yeni trend dönüşü olmalı
        has_hh = older_target_sw is None or (prev_target_sw.price > older_target_sw.price)
        has_2hl = older_same_sw is not None and (prev_same_sw.price > older_same_sw.price)
        if not (has_hh or has_2hl or flipped_from_direction == "SHORT"):
            return None
    else:
        # Düşüş trendi: Lower High (LH) oluşmalı
        if origin_sw.price >= prev_same_sw.price:
            return None
        has_ll = older_target_sw is None or (prev_target_sw.price < older_target_sw.price)
        has_2lh = older_same_sw is not None and (prev_same_sw.price < older_same_sw.price)
        if not (has_ll or has_2lh or flipped_from_direction == "LONG"):
            return None

    # Origin son ~40 kapanmış mum içinde olmalı
    if n_closed - 1 - origin_sw.idx > 40:
        return None

    # 2) YAPI KIRILIMI (BoS) - MUM KAPANIŞI ZORUNLULUĞU (Video 1 [03:24-03:33])
    bos_idx: Optional[int] = None
    for i in range(origin_sw.idx + 1, n_closed):
        c = closed_candles[i]
        # İtkisel dalga sırasında origin fitili kırıldıysa bu salınım geçersizdir
        if direction == "LONG" and c.low < origin_sw.price:
            return None
        if direction == "SHORT" and c.high > origin_sw.price:
            return None

        # Önceki yapı fitil seviyesinin ötesinde NET MUM KAPANIŞI
        if direction == "LONG" and c.close > prev_target_sw.price:
            bos_idx = i
            break
        if direction == "SHORT" and c.close < prev_target_sw.price:
            bos_idx = i
            break

    if bos_idx is None:
        return None

    # BoS taze olmalı (çok eski kurulumları bildirme)
    if (n_closed - 1 - bos_idx) > Config.MAX_BOS_AGE_CANDLES:
        return None

    # 3) NEDENSEL (CAUSAL) SAĞ KENAR SİMÜLASYONU:
    # İtkisel dalganın bittiği uç noktayı (0.0 seviyesi) ve geri çekilme geçmişini adım adım izle
    peak_idx = origin_sw.idx + 1
    peak_price = (
        closed_candles[peak_idx].high if direction == "LONG" else closed_candles[peak_idx].low
    )
    for i in range(origin_sw.idx + 1, bos_idx + 1):
        if direction == "LONG" and closed_candles[i].high >= peak_price:
            peak_price = closed_candles[i].high
            peak_idx = i
        elif direction == "SHORT" and closed_candles[i].low <= peak_price:
            peak_price = closed_candles[i].low
            peak_idx = i

    in_golden_zone = False
    golden_zone_idx = -1
    touched_0618 = False
    deepest_retrace = 0.0

    for i in range(bos_idx + 1, n_closed):
        c = closed_candles[i]
        leg = abs(peak_price - origin_sw.price)
        if leg <= 0:
            return None

        if not in_golden_zone:
            # Henüz 0.50 Altın Bölgesine değmeden yeni zirve/dip yapıyorsa itkisel hareket devam ediyordur
            if direction == "LONG" and c.high >= peak_price:
                peak_price = c.high
                peak_idx = i
                deepest_retrace = 0.0
                continue
            if direction == "SHORT" and c.low <= peak_price:
                peak_price = c.low
                peak_idx = i
                deepest_retrace = 0.0
                continue

            fib_now = build_fib_levels(origin_sw.price, peak_price, direction)
            if (direction == "LONG" and c.low <= fib_now.stop_loss) or (
                direction == "SHORT" and c.high >= fib_now.stop_loss
            ):
                return None

            retrace = (
                (peak_price - c.low) / leg
                if direction == "LONG"
                else (c.high - peak_price) / leg
            )
            deepest_retrace = max(deepest_retrace, retrace)
            if retrace >= Config.GOLDEN_ZONE_MIN:
                in_golden_zone = True
                golden_zone_idx = i
                if retrace >= Config.FIB_LEVEL_0618:
                    touched_0618 = True
        else:
            # Altın Bölgeye zaten girmiş olan kurulumun sonraki mumlardaki sonucu:
            fib_now = build_fib_levels(origin_sw.price, peak_price, direction)
            # 1) Stop-Loss patladı mı? -> Video 2 [05:00] Trend Dönüş Sinyali!
            if (direction == "LONG" and c.low <= fib_now.stop_loss) or (
                direction == "SHORT" and c.high >= fib_now.stop_loss
            ):
                return FibSetup(
                    setup_id=f"{symbol}_{direction}_{origin_sw.timestamp}",
                    symbol=symbol,
                    direction=direction,
                    stage="STOPPED_OUT",
                    origin_timestamp=origin_sw.timestamp,
                    bos_timestamp=closed_candles[bos_idx].timestamp,
                    extreme_timestamp=closed_candles[peak_idx].timestamp,
                    broken_structure_price=prev_target_sw.price,
                    current_price=live_candle.close,
                    current_retracement=1.05,
                    deepest_retracement=deepest_retrace,
                    touched_0618=touched_0618,
                    candle_confirmation=None,
                    fib=fib_now,
                    rrr_at_0618=1.50,
                    rvol=0.0,
                    impulse_atr_mult=0.0,
                    turnover_24h=ticker.turnover_24h,
                    session_label="",
                )

            # 2) Take-Profit (0.0 önceki tepe/dip) zaten vuruldu mu? -> Tamamlanmış işlemi bildirme!
            if (direction == "LONG" and c.high >= fib_now.level_0) or (
                direction == "SHORT" and c.low <= fib_now.level_0
            ):
                return None

            retrace = (
                (peak_price - c.low) / leg
                if direction == "LONG"
                else (c.high - peak_price) / leg
            )
            deepest_retrace = max(deepest_retrace, retrace)
            if retrace >= Config.FIB_LEVEL_0618:
                touched_0618 = True

    # İtkisel dalganın bitip geri çekilmenin başladığını doğrulamak için peak'ten sonra en az 1 mum kapanmış olmalı
    if peak_idx >= n_closed - 1 and not in_golden_zone:
        return None

    # 4) FİBONACCİ'NİN BAŞARISIZ OLDUĞU 3 SENARYO FİLTRELERİ (Video 1 [01:43-02:46, 05:03-05:19])
    baseline_atr = compute_atr(closed_candles, origin_sw.idx, Config.ATR_PERIOD)
    if baseline_atr <= 0:
        return None

    impulse_candles = closed_candles[origin_sw.idx + 1 : peak_idx + 1]
    if not impulse_candles or len(impulse_candles) > 18:
        return None

    leg = abs(peak_price - origin_sw.price)
    impulse_atr_mult = leg / baseline_atr

    # Filtre A: Minimum İtkisel Dalga Boyu (Testere / Gürültü Elemesi)
    if impulse_atr_mult < Config.MIN_IMPULSE_ATR_MULT:
        return None

    # Filtre B: İtkisel Verimlilik Oranı (Yatayda zikzak yapan mumları ele)
    sum_ranges = sum(max(1e-12, ic.high - ic.low) for ic in impulse_candles)
    efficiency = leg / sum_ranges
    if efficiency < Config.MIN_IMPULSE_EFFICIENCY:
        return None

    # Filtre C: Devasa Haber / Manipülasyon Mumu Elemesi ("100 pip monstrous candle" - Video 1 [05:03])
    max_single_range = max(ic.high - ic.low for ic in impulse_candles)
    if (max_single_range / baseline_atr) > Config.MAX_SINGLE_CANDLE_ATR_MULT:
        return None

    # Filtre D: Algoritmik Hacim Filtresi (Ölü saatlerdeki hacimsiz fakeout'ları ele)
    avg_vol_20 = compute_avg_volume(closed_candles, origin_sw.idx, Config.VOLUME_SMA_PERIOD)
    if avg_vol_20 > 0:
        max_imp_vol = max(ic.volume for ic in impulse_candles)
        avg_imp_vol = sum(ic.volume for ic in impulse_candles) / len(impulse_candles)
        rvol = max_imp_vol / avg_vol_20
        avg_rvol = avg_imp_vol / avg_vol_20
        if rvol < Config.MIN_IMPULSE_MAX_RVOL and avg_rvol < Config.MIN_IMPULSE_AVG_RVOL:
            return None
    else:
        rvol = 1.0

    # 5) CANLI (FORMING) MUM KONTROLÜ VE AŞAMA KARARI
    if not in_golden_zone:
        # Canlı mumda yeni zirve/dip yapılıyorsa itkisel hareket hala sürüyordur, bekle
        if (direction == "LONG" and live_candle.high > peak_price) or (
            direction == "SHORT" and live_candle.low < peak_price
        ):
            return None

    fib = build_fib_levels(origin_sw.price, peak_price, direction)

    # Filtre E: Kasa Büyütme - Minimum Kâr Yüzdesi Şartı (Cılız dalgaları ele)
    if fib.level_0618 > 0:
        tp1_move_pct = abs(fib.level_0 - fib.level_0618) / fib.level_0618 * 100.0
        min_req_pct = Config.MIN_TP1_PCT_MAJORS if symbol in MAJOR_SYMBOLS else Config.MIN_TP1_PCT
        if tp1_move_pct < min_req_pct:
            return None

    # Canlı mumda SL patladı mı?
    if (direction == "LONG" and live_candle.low <= fib.stop_loss) or (
        direction == "SHORT" and live_candle.high >= fib.stop_loss
    ):
        if in_golden_zone:
            return FibSetup(
                setup_id=f"{symbol}_{direction}_{origin_sw.timestamp}",
                symbol=symbol,
                direction=direction,
                stage="STOPPED_OUT",
                origin_timestamp=origin_sw.timestamp,
                bos_timestamp=closed_candles[bos_idx].timestamp,
                extreme_timestamp=closed_candles[peak_idx].timestamp,
                broken_structure_price=prev_target_sw.price,
                current_price=live_candle.close,
                current_retracement=1.05,
                deepest_retracement=deepest_retrace,
                touched_0618=touched_0618,
                candle_confirmation=None,
                fib=fib,
                rrr_at_0618=1.50,
                rvol=rvol,
                impulse_atr_mult=impulse_atr_mult,
                turnover_24h=ticker.turnover_24h,
                session_label="",
            )
        return None

    # Altın bölgeye daha önce girdiyse ve canlı mumda TP (0.0) vurulduysa işlem bitti
    if in_golden_zone and (
        (direction == "LONG" and live_candle.high >= fib.level_0)
        or (direction == "SHORT" and live_candle.low <= fib.level_0)
    ):
        return None

    live_extreme_retrace = (
        (peak_price - live_candle.low) / leg
        if direction == "LONG"
        else (live_candle.high - peak_price) / leg
    )
    current_retrace = (
        (peak_price - live_candle.close) / leg
        if direction == "LONG"
        else (live_candle.close - peak_price) / leg
    )

    deepest_retrace = max(deepest_retrace, live_extreme_retrace)
    if deepest_retrace >= Config.FIB_LEVEL_0618:
        touched_0618 = True

    # Risk/Ödül hesabı (0.618 Limit Giriş -> TP1: 0.0, TP2: -0.5, TP3: -1.0)
    risk = abs(fib.level_0618 - fib.stop_loss)
    reward_tp1 = abs(fib.level_0 - fib.level_0618)
    reward_tp2 = abs(fib.level_neg_05 - fib.level_0618)
    reward_tp3 = abs(fib.level_neg_10 - fib.level_0618)
    rrr = round(reward_tp1 / risk, 2) if risk > 0 else 1.50
    rrr_tp2 = round(reward_tp2 / risk, 2) if risk > 0 else 2.71
    rrr_tp3 = round(reward_tp3 / risk, 2) if risk > 0 else 3.93

    if deepest_retrace >= Config.GOLDEN_ZONE_MIN:
        # Fiyat Altın Bölgeye değip çoktan TP'ye %75+ yaklaştıysa geç kalınmış sinyal üretme
        if current_retrace < 0.28:
            return None
        conf_start = golden_zone_idx if golden_zone_idx >= 0 else peak_idx + 1
        confirmation = check_candle_confirmation(closed_candles, conf_start, fib, direction)
        stage = "STAGE_2_GOLDEN_ZONE"
    elif current_retrace >= Config.MIN_RETRACEMENT_STAGE1:
        confirmation = None
        stage = "STAGE_1_LIMIT_READY"
    else:
        return None

    is_flip = (
        (direction == "LONG" and flipped_from_direction == "SHORT")
        or (direction == "SHORT" and flipped_from_direction == "LONG")
    )

    strength_score, quality_grade, bankroll_pct, conf_notes = evaluate_confluence_score(
        closed_candles=closed_candles,
        origin_idx=origin_sw.idx,
        peak_idx=peak_idx,
        fib=fib,
        direction=direction,
        rvol=rvol,
        impulse_atr_mult=impulse_atr_mult,
        efficiency=efficiency,
        candle_confirmation=confirmation,
        is_trend_flip=is_flip,
    )

    return FibSetup(
        setup_id=f"{symbol}_{direction}_{origin_sw.timestamp}",
        symbol=symbol,
        direction=direction,
        stage=stage,
        origin_timestamp=origin_sw.timestamp,
        bos_timestamp=closed_candles[bos_idx].timestamp,
        extreme_timestamp=closed_candles[peak_idx].timestamp,
        broken_structure_price=prev_target_sw.price,
        current_price=live_candle.close,
        current_retracement=round(current_retrace, 3),
        deepest_retracement=round(deepest_retrace, 3),
        touched_0618=touched_0618,
        candle_confirmation=confirmation,
        fib=fib,
        rrr_at_0618=rrr,
        rrr_tp2=rrr_tp2,
        rrr_tp3=rrr_tp3,
        rvol=round(rvol, 2),
        impulse_atr_mult=round(impulse_atr_mult, 2),
        turnover_24h=ticker.turnover_24h,
        session_label=get_session_label(),
        is_trend_flip=is_flip,
        strength_score=strength_score,
        quality_grade=quality_grade,
        recommended_bankroll_pct=bankroll_pct,
        confluence_notes=conf_notes,
    )


def analyze_symbol(
    symbol: str,
    candles: List[Candle],
    ticker: TickerInfo,
    flipped_from_direction: Optional[str] = None,
) -> Optional[FibSetup]:
    """
    Bir paritenin 15 dakikalık mumlarını analiz eder ve aktif (Aşama 1 / Aşama 2 / Stop-Flip)
    Fibonacci kurulumu varsa döner.
    """
    if len(candles) < 60:
        return None

    closed_candles = candles[:-1]
    live_candle = candles[-1]

    swings = extract_market_structure(closed_candles)
    if len(swings) < 4:
        return None

    # En güncel salınım adaylarından geriye doğru son 3 pivot noktasını kontrol et
    max_check = min(len(swings) - 1, 2)
    for offset in range(max_check + 1):
        idx_s = len(swings) - 1 - offset
        if idx_s < 2:
            break

        origin_sw = swings[idx_s]
        prev_target_sw = swings[idx_s - 1]
        prev_same_sw = swings[idx_s - 2]
        older_target_sw = swings[idx_s - 3] if idx_s >= 3 else None
        older_same_sw = swings[idx_s - 4] if idx_s >= 4 else None

        if origin_sw.kind == "LOW" and prev_target_sw.kind == "HIGH" and prev_same_sw.kind == "LOW":
            setup = _evaluate_candidate(
                symbol=symbol,
                direction="LONG",
                origin_sw=origin_sw,
                prev_target_sw=prev_target_sw,
                prev_same_sw=prev_same_sw,
                older_target_sw=older_target_sw,
                older_same_sw=older_same_sw,
                closed_candles=closed_candles,
                live_candle=live_candle,
                ticker=ticker,
                flipped_from_direction=flipped_from_direction,
            )
            if setup is not None:
                return setup

        elif origin_sw.kind == "HIGH" and prev_target_sw.kind == "LOW" and prev_same_sw.kind == "HIGH":
            setup = _evaluate_candidate(
                symbol=symbol,
                direction="SHORT",
                origin_sw=origin_sw,
                prev_target_sw=prev_target_sw,
                prev_same_sw=prev_same_sw,
                older_target_sw=older_target_sw,
                older_same_sw=older_same_sw,
                closed_candles=closed_candles,
                live_candle=live_candle,
                ticker=ticker,
                flipped_from_direction=flipped_from_direction,
            )
            if setup is not None:
                return setup

    return None
