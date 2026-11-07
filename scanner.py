# -*- coding: utf-8 -*-
"""
Ana Tarayıcı Motoru + SMC/ICT Kasa Yönetimi + Saatlik/Günlük Bilanço + 0$ Bulut HTTP Sunucusu
Hem Windows bilgisayarda tek tıkla anlık çalışır, hem de Render (Frankfurt) + cron-job.org üzerinde 7/24 ücretsiz çalışır.
"""

import argparse
import json
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Dict, List, Optional

from bybit_client import BybitClient
from config import STATE_PATH, Config
from strategy import (
    FibLevels,
    FibSetup,
    analyze_symbol,
    check_candle_confirmation,
    get_session_label,
)
from telegram_notifier import (
    TelegramNotifier,
    calc_pct_diff,
    fmt_price,
    fmt_usd_signed,
    format_daily_end_report_message,
    format_hourly_report_message,
    format_setup_message,
    format_tp_hit_message,
)

# Windows konsolunda Türkçe ve emoji karakterlerin sorunsuz görünmesi için UTF-8 zorla
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

TR_TZ = timezone(timedelta(hours=3))


class ScannerEngine:
    def __init__(self, dry_run: bool = False):
        self.dry_run = dry_run
        self.bybit = BybitClient()
        self.notifier = TelegramNotifier()
        self._lock = threading.Lock()

        self.scan_count: int = 0
        self.last_scan_time_str: str = "Henüz taranmadı"
        self.last_scanned_symbols_count: int = 0
        self.active_setups: Dict[str, FibSetup] = {}

        self.state: Dict = self._load_state()
        self.notifier._last_update_id = int(self.state.get("last_update_id", 0))

    def _load_state(self) -> Dict:
        now_tr = datetime.now(TR_TZ)
        today_key = now_tr.strftime("%Y-%m-%d")
        hour_key = now_tr.strftime("%Y-%m-%d_%H")

        default_state = {
            "sent_alerts": {},
            "flip_watch": {},
            "tracked_trades": {},
            "daily_events": [],
            "day_start_bankroll": Config.DEFAULT_BANKROLL_USD,
            "current_bankroll": Config.DEFAULT_BANKROLL_USD,
            "current_day_key": today_key,
            "last_hourly_report_key": hour_key,
            "last_update_id": 0,
        }
        if not STATE_PATH.exists():
            return default_state
        try:
            data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
            for k, v in default_state.items():
                if k not in data:
                    data[k] = v
            return data
        except Exception:
            return default_state

    def _save_state(self) -> None:
        try:
            sent = self.state.get("sent_alerts", {})
            if len(sent) > 400:
                keys = list(sent.keys())[-300:]
                self.state["sent_alerts"] = {k: sent[k] for k in keys}

            now_ms = int(time.time() * 1000)
            flips = self.state.get("flip_watch", {})
            self.state["flip_watch"] = {
                sym: info
                for sym, info in flips.items()
                if now_ms - int(info.get("ts", 0)) < 12 * 3600 * 1000
            }
            STATE_PATH.write_text(json.dumps(self.state, indent=2, ensure_ascii=False), encoding="utf-8")
        except Exception as exc:
            print(f"[UYARI] state.json kaydedilemedi: {exc}")

    def _record_trade_event(
        self,
        setup_id: str,
        tr: Dict,
        event_type: str,
        pnl_usd: float,
    ) -> float:
        """
        Gerçekleşen TP1 / TP2 / TP3 / SL / Başabaş olayını kaydeder ve güncel kasayı günceller.
        Dönüş: Bu işlemin şu ana kadarki toplam net dolar kâr/zararı.
        """
        now_tr = datetime.now(TR_TZ)
        cur_bank = float(self.state.get("current_bankroll", Config.DEFAULT_BANKROLL_USD))
        cur_bank = round(cur_bank + pnl_usd, 2)
        self.state["current_bankroll"] = cur_bank

        tr["realized_usd"] = round(float(tr.get("realized_usd", 0.0)) + pnl_usd, 2)

        ev = {
            "setup_id": setup_id,
            "symbol": tr.get("symbol", "?"),
            "direction": tr.get("direction", "LONG"),
            "grade": tr.get("quality_grade", "A"),
            "margin_usd": float(tr.get("margin_usd", 15.0)),
            "event_type": event_type,
            "pnl_usd": round(pnl_usd, 2),
            "trade_total_usd": tr["realized_usd"],
            "ts": int(time.time() * 1000),
            "hour_key": now_tr.strftime("%Y-%m-%d_%H"),
            "day_key": now_tr.strftime("%Y-%m-%d"),
            "time_str": now_tr.strftime("%H:%M"),
        }
        self.state.setdefault("daily_events", []).append(ev)
        return tr["realized_usd"]

    def check_and_send_scheduled_reports(self) -> None:
        """
        1) Her saat başı (01:00, 02:00 ... 23:00 TSİ):
           Son 1 saat içinde olan TP/SL'leri + Günün genel durumunu gönderir.
        2) Gün bitiminde (00:00 TSİ):
           Tamamlanan günün 24 saatlik TP/SL ve Kasa Kazanç bilançosunu gönderir ve yeni günü başlatır.
        """
        if self.dry_run or not self.notifier.is_configured:
            return

        now_tr = datetime.now(TR_TZ)
        today_key = now_tr.strftime("%Y-%m-%d")
        hour_key = now_tr.strftime("%Y-%m-%d_%H")

        saved_day_key = self.state.get("current_day_key", today_key)
        saved_hour_key = self.state.get("last_hourly_report_key", hour_key)

        # A) Gün değiştiyse (Saat 00:00 TSİ) -> Önce Gün Sonu Bilançosunu gönder!
        if today_key != saved_day_key:
            daily_events = self.state.get("daily_events", [])
            day_start_bank = float(self.state.get("day_start_bankroll", Config.DEFAULT_BANKROLL_USD))
            day_end_bank = float(self.state.get("current_bankroll", Config.DEFAULT_BANKROLL_USD))

            try:
                dt_prev = datetime.strptime(saved_day_key, "%Y-%m-%d")
                day_label = dt_prev.strftime("%d.%m.%Y")
            except Exception:
                day_label = saved_day_key

            eod_msg = format_daily_end_report_message(
                day_label=day_label,
                daily_events=daily_events,
                day_start_bankroll=day_start_bank,
                day_end_bankroll=day_end_bank,
            )
            if self.notifier.send_message(eod_msg):
                print(f"[TELEGRAM GÖNDERİLDİ] Gün Sonu Bilançosu ({day_label})")

            # Yeni gün için sayaçları sıfırla, kasayı yeni güne devret
            self.state["daily_events"] = []
            self.state["day_start_bankroll"] = day_end_bank
            self.state["current_day_key"] = today_key
            self.state["last_hourly_report_key"] = hour_key
            self._save_state()
            return

        # B) Saat değiştiyse (Saat başı: 01:00, 02:00 ... 23:00 TSİ) -> Saatlik + Genel Durum Raporu gönder!
        if hour_key != saved_hour_key:
            now_ms = int(time.time() * 1000)
            one_hour_ago_ms = now_ms - 3650 * 1000
            daily_events = self.state.get("daily_events", [])
            hour_events = [
                e for e in daily_events
                if int(e.get("ts", 0)) >= one_hour_ago_ms or e.get("hour_key") == saved_hour_key
            ]

            prev_dt = now_tr - timedelta(hours=1)
            window_label = f"{prev_dt.strftime('%H:00')} – {now_tr.strftime('%H:00')}"
            day_label = now_tr.strftime("%d.%m.%Y")

            tracked = self.state.get("tracked_trades", {})
            in_trade_cnt = sum(1 for t in tracked.values() if t.get("status") == "IN_TRADE")
            waiting_cnt = sum(1 for t in tracked.values() if t.get("status") == "WAITING_ZONE")

            hourly_msg = format_hourly_report_message(
                window_label=window_label,
                day_label=day_label,
                hour_events=hour_events,
                daily_events=daily_events,
                active_in_trade_count=in_trade_cnt,
                active_waiting_count=waiting_cnt,
                day_start_bankroll=float(self.state.get("day_start_bankroll", Config.DEFAULT_BANKROLL_USD)),
                current_bankroll=float(self.state.get("current_bankroll", Config.DEFAULT_BANKROLL_USD)),
                is_manual=False,
            )
            if self.notifier.send_message(hourly_msg):
                print(f"[TELEGRAM GÖNDERİLDİ] Saat Başı Raporu ({window_label})")

            self.state["last_hourly_report_key"] = hour_key
            self._save_state()

    def run_single_scan(self) -> Dict[str, int]:
        """Tüm yüksek hacimli Bybit USDT vadeli paritelerini tarar, aktif işlemleri ve raporları yönetir."""
        with self._lock:
            start_t = time.time()
            tickers = self.bybit.get_top_usdt_perpetuals(
                top_n=Config.TOP_SYMBOLS_COUNT,
                min_turnover=Config.MIN_24H_TURNOVER_USD,
            )
            klines_map = self.bybit.fetch_all_klines_parallel(
                tickers=tickers,
                interval=Config.TIMEFRAME,
                limit=Config.KLINE_LIMIT,
            )

            current_active: Dict[str, FibSetup] = {}
            stage1_sent = 0
            stage2_sent = 0

            sent_alerts: Dict[str, List[str]] = self.state["sent_alerts"]
            flip_watch: Dict[str, Dict] = self.state["flip_watch"]
            tracked_trades: Dict[str, Dict] = self.state.setdefault("tracked_trades", {})

            # 1) Önce pusuda bekleyen (WAITING_ZONE) veya aktif işlemde olan (IN_TRADE) tüm kurulumları takip et
            for tid, tr in list(tracked_trades.items()):
                sym = tr.get("symbol")
                if sym not in klines_map:
                    continue
                ticker_obj, sym_candles = klines_map[sym]
                if not sym_candles:
                    continue

                direction = tr["direction"]
                status = tr.get("status", "IN_TRADE")
                extreme_ts = int(tr.get("extreme_ts", 0))
                level_1 = float(tr.get("level_1", tr.get("current_sl", 0)))
                level_0618 = float(tr["entry_0618"])
                level_0500 = float(tr.get("level_0500", level_0618))
                tp1_val = float(tr["tp1"])
                tp2_val = float(tr["tp2"])
                tp3_val = float(tr["tp3"])
                leg = abs(tp1_val - level_1) if abs(tp1_val - level_1) > 0 else 1e-9

                post_candles = [c for c in sym_candles if c.timestamp > extreme_ts]
                if not post_candles:
                    continue

                if status == "WAITING_ZONE":
                    entered_zone = False
                    zone_ts = 0
                    deepest_ret = 0.0
                    touched_618 = False
                    invalidated = False

                    for c in post_candles:
                        init_sl = float(tr["initial_sl"])
                        if (direction == "LONG" and c.low <= init_sl) or (
                            direction == "SHORT" and c.high >= init_sl
                        ):
                            invalidated = True
                            break

                        if not entered_zone:
                            if (direction == "LONG" and c.close > tp1_val) or (
                                direction == "SHORT" and c.close < tp1_val
                            ):
                                if not self.dry_run and self.notifier.is_configured:
                                    self.notifier.send_message(
                                        format_tp_hit_message(
                                            tr,
                                            "MISSED_NO_PULLBACK",
                                            c.close,
                                            current_bankroll=float(self.state.get("current_bankroll", 100.0)),
                                        )
                                    )
                                    print(f"[TELEGRAM GÖNDERİLDİ] Pusu İptal (Altın Bölgeye Dönmeden Gitti) -> {sym}")
                                invalidated = True
                                break

                        ret = (
                            (tp1_val - c.low) / leg
                            if direction == "LONG"
                            else (c.high - tp1_val) / leg
                        )
                        if ret > deepest_ret:
                            deepest_ret = ret
                        if ret >= 0.618:
                            touched_618 = True
                        if ret >= 0.500 and not entered_zone:
                            entered_zone = True
                            zone_ts = c.timestamp

                    if invalidated:
                        del tracked_trades[tid]
                        continue

                    if entered_zone:
                        tr["status"] = "IN_TRADE"
                        tr["zone_entry_ts"] = zone_ts
                        status = "IN_TRADE"
                        already_sent = sent_alerts.get(tid, [])
                        if "STAGE_2" not in already_sent:
                            live_c = sym_candles[-1]
                            cur_ret = (
                                (tp1_val - live_c.close) / leg
                                if direction == "LONG"
                                else (live_c.close - tp1_val) / leg
                            )
                            fib_obj = FibLevels(
                                level_1=level_1,
                                level_0618=level_0618,
                                level_0500=level_0500,
                                level_0=tp1_val,
                                level_neg_05=tp2_val,
                                level_neg_10=tp3_val,
                                stop_loss=float(tr["initial_sl"]),
                            )
                            conf = check_candle_confirmation(
                                sym_candles[:-1], max(1, len(sym_candles) - 8), fib_obj, direction
                            )
                            score = int(tr.get("strength_score", 70))
                            notes = list(tr.get("confluence_notes", ["✅ Temel Fib + BoS"]))
                            if conf and not any("Mum Onaylı" in n for n in notes):
                                score = min(100, score + 10)
                                notes.append("✅ Altın Bölge Mum Onaylı (Pin Bar/Engulfing)")
                            grade = "A+" if score >= 80 else ("A" if score >= 65 else "B")
                            bank_pct = 20 if grade == "A+" else (15 if grade == "A" else 8)
                            cur_bank = float(self.state.get("current_bankroll", Config.DEFAULT_BANKROLL_USD))
                            tr["strength_score"] = score
                            tr["quality_grade"] = grade
                            tr["bankroll_pct"] = bank_pct
                            tr["margin_usd"] = round(cur_bank * (bank_pct / 100.0), 2)
                            tr["confluence_notes"] = notes

                            s2_setup = FibSetup(
                                setup_id=tid,
                                symbol=sym,
                                direction=direction,
                                stage="STAGE_2_GOLDEN_ZONE",
                                origin_timestamp=int(tr.get("origin_ts", extreme_ts)),
                                bos_timestamp=int(tr.get("bos_ts", extreme_ts)),
                                extreme_timestamp=extreme_ts,
                                broken_structure_price=float(tr.get("broken_price", tp1_val)),
                                current_price=live_c.close,
                                current_retracement=round(cur_ret, 3),
                                deepest_retracement=round(deepest_ret, 3),
                                touched_0618=touched_618,
                                candle_confirmation=conf,
                                fib=fib_obj,
                                rrr_at_0618=float(tr.get("rrr_tp1", 1.50)),
                                rrr_tp2=float(tr.get("rrr_tp2", 2.71)),
                                rrr_tp3=float(tr.get("rrr_tp3", 3.93)),
                                rvol=float(tr.get("rvol", 1.2)),
                                impulse_atr_mult=float(tr.get("atr_mult", 1.8)),
                                turnover_24h=ticker_obj.turnover_24h,
                                session_label=get_session_label(),
                                strength_score=score,
                                quality_grade=grade,
                                recommended_bankroll_pct=bank_pct,
                                confluence_notes=notes,
                            )
                            msg = format_setup_message(s2_setup, "STAGE_2", current_bankroll=cur_bank)
                            if not self.dry_run and self.notifier.is_configured:
                                if self.notifier.send_message(msg):
                                    already_sent.append("STAGE_2")
                                    sent_alerts[tid] = already_sent
                                    stage2_sent += 1
                                    print(f"[TELEGRAM GÖNDERİLDİ] Pusu Altın Bölgeye Girdi (Aşama 2) -> {sym} ({direction})")

                if status == "IN_TRADE":
                    zone_entry_ts = int(tr.get("zone_entry_ts", extreme_ts))
                    trade_candles = [c for c in sym_candles if c.timestamp > zone_entry_ts]
                    if not trade_candles:
                        continue

                    bar_high = max(c.high for c in trade_candles)
                    bar_low = min(c.low for c in trade_candles)
                    live_c = sym_candles[-1]
                    hits: List[str] = tr.setdefault("hits", [])
                    active_sl = float(tr["current_sl"])
                    margin_usd = float(tr.get("margin_usd", 15.0))
                    pos_usd = margin_usd * Config.DEFAULT_LEVERAGE

                    # Stop kontrolü (TP1 öncesi Tam Stop, TP1 sonrası Başabaş, TP2 sonrası TP1 İz Süren Stop)
                    if (direction == "LONG" and live_c.low <= active_sl) or (
                        direction == "SHORT" and live_c.high >= active_sl
                    ):
                        if "TP1" not in hits:
                            sl_pct = calc_pct_diff(level_0618, float(tr["initial_sl"]))
                            loss_usd = -1.0 * pos_usd * (sl_pct / 100.0)
                            tot_usd = self._record_trade_event(tid, tr, "STOP_LOSS", loss_usd)
                            flip_watch[sym] = {"stopped_dir": direction, "ts": int(time.time() * 1000)}
                            if not self.dry_run and self.notifier.is_configured:
                                self.notifier.send_message(
                                    format_tp_hit_message(
                                        tr,
                                        "STOP_LOSS",
                                        live_c.close,
                                        event_pnl_usd=loss_usd,
                                        total_trade_pnl_usd=tot_usd,
                                        current_bankroll=float(self.state["current_bankroll"]),
                                    )
                                )
                        elif "TP2" not in hits:
                            tot_usd = self._record_trade_event(tid, tr, "BREAKEVEN_STOP", 0.0)
                            if not self.dry_run and self.notifier.is_configured:
                                self.notifier.send_message(
                                    format_tp_hit_message(
                                        tr,
                                        "BREAKEVEN_STOP",
                                        live_c.close,
                                        event_pnl_usd=0.0,
                                        total_trade_pnl_usd=tot_usd,
                                        current_bankroll=float(self.state["current_bankroll"]),
                                    )
                                )
                        else:
                            tp1_pct = calc_pct_diff(level_0618, tp1_val)
                            rem_gain_usd = 0.25 * pos_usd * (tp1_pct / 100.0)
                            tot_usd = self._record_trade_event(tid, tr, "TRAILING_TP1_STOP", rem_gain_usd)
                            if not self.dry_run and self.notifier.is_configured:
                                self.notifier.send_message(
                                    format_tp_hit_message(
                                        tr,
                                        "TRAILING_TP1_STOP",
                                        live_c.close,
                                        event_pnl_usd=rem_gain_usd,
                                        total_trade_pnl_usd=tot_usd,
                                        current_bankroll=float(self.state["current_bankroll"]),
                                    )
                                )
                        del tracked_trades[tid]
                        continue

                    # TP1 (0.0 seviyesi -> Pozisyonun %50'si kapanır, SL girişe çekilir)
                    if "TP1" not in hits and (
                        (direction == "LONG" and bar_high >= tp1_val)
                        or (direction == "SHORT" and bar_low <= tp1_val)
                    ):
                        hits.append("TP1")
                        tr["current_sl"] = level_0618
                        tp1_pct = calc_pct_diff(level_0618, tp1_val)
                        gain_tp1_usd = 0.50 * pos_usd * (tp1_pct / 100.0)
                        tot_usd = self._record_trade_event(tid, tr, "TP1", gain_tp1_usd)
                        if not self.dry_run and self.notifier.is_configured:
                            self.notifier.send_message(
                                format_tp_hit_message(
                                    tr,
                                    "TP1",
                                    live_c.close,
                                    event_pnl_usd=gain_tp1_usd,
                                    total_trade_pnl_usd=tot_usd,
                                    current_bankroll=float(self.state["current_bankroll"]),
                                )
                            )
                            print(f"[TELEGRAM GÖNDERİLDİ] TP1 Vuruldu -> {sym} ({direction}) (+{gain_tp1_usd:.2f}$)")

                    # TP2 (-0.5 uzatma -> Pozisyonun %25'i daha kapanır, SL TP1'e çekilir)
                    if "TP1" in hits and "TP2" not in hits and (
                        (direction == "LONG" and bar_high >= tp2_val)
                        or (direction == "SHORT" and bar_low <= tp2_val)
                    ):
                        hits.append("TP2")
                        tr["current_sl"] = tp1_val
                        tp2_pct = calc_pct_diff(level_0618, tp2_val)
                        gain_tp2_usd = 0.25 * pos_usd * (tp2_pct / 100.0)
                        tot_usd = self._record_trade_event(tid, tr, "TP2", gain_tp2_usd)
                        if not self.dry_run and self.notifier.is_configured:
                            self.notifier.send_message(
                                format_tp_hit_message(
                                    tr,
                                    "TP2",
                                    live_c.close,
                                    event_pnl_usd=gain_tp2_usd,
                                    total_trade_pnl_usd=tot_usd,
                                    current_bankroll=float(self.state["current_bankroll"]),
                                )
                            )
                            print(f"[TELEGRAM GÖNDERİLDİ] TP2 Vuruldu -> {sym} ({direction}) (+{gain_tp2_usd:.2f}$)")

                    # TP3 (-1.0 tam uzatma -> Son %25 pozisyon kapanır)
                    if "TP2" in hits and "TP3" not in hits and (
                        (direction == "LONG" and bar_high >= tp3_val)
                        or (direction == "SHORT" and bar_low <= tp3_val)
                    ):
                        hits.append("TP3")
                        tp3_pct = calc_pct_diff(level_0618, tp3_val)
                        gain_tp3_usd = 0.25 * pos_usd * (tp3_pct / 100.0)
                        tot_usd = self._record_trade_event(tid, tr, "TP3", gain_tp3_usd)
                        if not self.dry_run and self.notifier.is_configured:
                            self.notifier.send_message(
                                format_tp_hit_message(
                                    tr,
                                    "TP3",
                                    live_c.close,
                                    event_pnl_usd=gain_tp3_usd,
                                    total_trade_pnl_usd=tot_usd,
                                    current_bankroll=float(self.state["current_bankroll"]),
                                )
                            )
                            print(f"[TELEGRAM GÖNDERİLDİ] TP3 Vuruldu -> {sym} ({direction}) (+{gain_tp3_usd:.2f}$)")
                        del tracked_trades[tid]

            # 2) Yeni kurulumları tara
            for symbol, (ticker, candles) in klines_map.items():
                flip_info = flip_watch.get(symbol)
                flipped_from = flip_info.get("stopped_dir") if flip_info else None

                setup = analyze_symbol(
                    symbol=symbol,
                    candles=candles,
                    ticker=ticker,
                    flipped_from_direction=flipped_from,
                )
                if setup is None:
                    continue

                if setup.stage == "STOPPED_OUT":
                    flip_watch[symbol] = {
                        "stopped_dir": setup.direction,
                        "ts": int(time.time() * 1000),
                    }
                    continue

                current_active[symbol] = setup
                already_sent = sent_alerts.get(setup.setup_id, [])
                cur_bank = float(self.state.get("current_bankroll", Config.DEFAULT_BANKROLL_USD))
                rec_margin = round(cur_bank * (setup.recommended_bankroll_pct / 100.0), 2)

                if (
                    setup.stage == "STAGE_1_LIMIT_READY"
                    and "STAGE_1" not in already_sent
                    and "STAGE_2" not in already_sent
                ):
                    msg = format_setup_message(setup, "STAGE_1", current_bankroll=cur_bank)
                    if self.dry_run or not self.notifier.is_configured:
                        print(
                            f"\n[SİNYAL - AŞAMA 1] {setup.symbol} ({setup.direction} | {setup.quality_grade} %{setup.recommended_bankroll_pct}) "
                            f"| Giriş(0.618): {fmt_price(setup.fib.level_0618)} | TP1: {fmt_price(setup.fib.level_0)}"
                        )
                    if not self.dry_run and self.notifier.is_configured:
                        if self.notifier.send_message(msg):
                            already_sent.append("STAGE_1")
                            sent_alerts[setup.setup_id] = already_sent
                            tracked_trades[setup.setup_id] = {
                                "symbol": setup.symbol,
                                "direction": setup.direction,
                                "status": "WAITING_ZONE",
                                "origin_ts": setup.origin_timestamp,
                                "bos_ts": setup.bos_timestamp,
                                "extreme_ts": setup.extreme_timestamp,
                                "broken_price": setup.broken_structure_price,
                                "level_1": setup.fib.level_1,
                                "entry_0618": setup.fib.level_0618,
                                "level_0500": setup.fib.level_0500,
                                "initial_sl": setup.fib.stop_loss,
                                "current_sl": setup.fib.stop_loss,
                                "tp1": setup.fib.level_0,
                                "tp2": setup.fib.level_neg_05,
                                "tp3": setup.fib.level_neg_10,
                                "rrr_tp1": setup.rrr_at_0618,
                                "rrr_tp2": setup.rrr_tp2,
                                "rrr_tp3": setup.rrr_tp3,
                                "rvol": setup.rvol,
                                "atr_mult": setup.impulse_atr_mult,
                                "strength_score": setup.strength_score,
                                "quality_grade": setup.quality_grade,
                                "bankroll_pct": setup.recommended_bankroll_pct,
                                "margin_usd": rec_margin,
                                "confluence_notes": setup.confluence_notes or [],
                                "realized_usd": 0.0,
                                "hits": [],
                                "ts": int(time.time() * 1000),
                            }
                            stage1_sent += 1
                            print(f"[TELEGRAM GÖNDERİLDİ] Aşama 1 -> {setup.symbol} ({setup.direction} | {setup.quality_grade})")

                elif setup.stage == "STAGE_2_GOLDEN_ZONE" and "STAGE_2" not in already_sent:
                    msg = format_setup_message(setup, "STAGE_2", current_bankroll=cur_bank)
                    if self.dry_run or not self.notifier.is_configured:
                        print(
                            f"\n[SİNYAL - AŞAMA 2 ALTIN BÖLGE] {setup.symbol} ({setup.direction} | {setup.quality_grade} %{setup.recommended_bankroll_pct}) "
                            f"| Anlık: {fmt_price(setup.current_price)} | 0.618: {fmt_price(setup.fib.level_0618)}"
                        )
                    if not self.dry_run and self.notifier.is_configured:
                        if self.notifier.send_message(msg):
                            already_sent.append("STAGE_2")
                            sent_alerts[setup.setup_id] = already_sent
                            tracked_trades[setup.setup_id] = {
                                "symbol": setup.symbol,
                                "direction": setup.direction,
                                "status": "IN_TRADE",
                                "origin_ts": setup.origin_timestamp,
                                "bos_ts": setup.bos_timestamp,
                                "extreme_ts": setup.extreme_timestamp,
                                "zone_entry_ts": candles[-2].timestamp if len(candles) >= 2 else setup.extreme_timestamp,
                                "broken_price": setup.broken_structure_price,
                                "level_1": setup.fib.level_1,
                                "entry_0618": setup.fib.level_0618,
                                "level_0500": setup.fib.level_0500,
                                "initial_sl": setup.fib.stop_loss,
                                "current_sl": setup.fib.stop_loss,
                                "tp1": setup.fib.level_0,
                                "tp2": setup.fib.level_neg_05,
                                "tp3": setup.fib.level_neg_10,
                                "rrr_tp1": setup.rrr_at_0618,
                                "rrr_tp2": setup.rrr_tp2,
                                "rrr_tp3": setup.rrr_tp3,
                                "rvol": setup.rvol,
                                "atr_mult": setup.impulse_atr_mult,
                                "strength_score": setup.strength_score,
                                "quality_grade": setup.quality_grade,
                                "bankroll_pct": setup.recommended_bankroll_pct,
                                "margin_usd": rec_margin,
                                "confluence_notes": setup.confluence_notes or [],
                                "realized_usd": 0.0,
                                "hits": [],
                                "ts": int(time.time() * 1000),
                            }
                            stage2_sent += 1
                            print(f"[TELEGRAM GÖNDERİLDİ] Aşama 2 -> {setup.symbol} ({setup.direction} | {setup.quality_grade})")

            self.active_setups = current_active
            self.scan_count += 1
            self.last_scanned_symbols_count = len(klines_map)
            now_tr = datetime.now(TR_TZ).strftime("%d.%m.%Y %H:%M:%S")
            self.last_scan_time_str = now_tr
            elapsed = time.time() - start_t

            # Saat başı ve gün bitimi otomatik rapor kontrolü
            self.check_and_send_scheduled_reports()

            if not self.dry_run:
                self._save_state()

            s1_total = sum(1 for s in current_active.values() if s.stage == "STAGE_1_LIMIT_READY")
            s2_total = sum(1 for s in current_active.values() if s.stage == "STAGE_2_GOLDEN_ZONE")
            print(
                f"[{now_tr}] Tarama #{self.scan_count} tamamlandı ({elapsed:.1f}s) | "
                f"Parite: {len(klines_map)} | Aktif Pusu (Aşama 1): {s1_total} | "
                f"Altın Bölgede (Aşama 2): {s2_total} | Kasa: {self.state.get('current_bankroll', 100.0):.2f}$"
            )
            return {
                "scanned": len(klines_map),
                "active_stage1": s1_total,
                "active_stage2": s2_total,
                "sent_stage1": stage1_sent,
                "sent_stage2": stage2_sent,
                "current_bankroll": float(self.state.get("current_bankroll", Config.DEFAULT_BANKROLL_USD)),
            }

    def format_status_text(self) -> str:
        s1 = sum(1 for s in self.active_setups.values() if s.stage == "STAGE_1_LIMIT_READY")
        s2 = sum(1 for s in self.active_setups.values() if s.stage == "STAGE_2_GOLDEN_ZONE")
        cur_bank = float(self.state.get("current_bankroll", Config.DEFAULT_BANKROLL_USD))
        start_bank = float(self.state.get("day_start_bankroll", Config.DEFAULT_BANKROLL_USD))
        diff = cur_bank - start_bank
        return (
            f"📊 <b>TARAYICI VE KASA DURUM RAPORU</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"✅ <b>Durum:</b> Aktif Çalışıyor (15dk Mumlar + SMC/ICT Puanlama)\n"
            f"🏦 <b>Borsa:</b> Bybit USDT Vadeli (Perpetual)\n"
            f"🔢 <b>Taranan Parite:</b> {self.last_scanned_symbols_count} yüksek hacimli coin\n"
            f"🔄 <b>Toplam Tarama:</b> {self.scan_count}\n"
            f"🕒 <b>Son Tarama (TSİ):</b> <code>{self.last_scan_time_str}</code>\n"
            f"🎯 <b>Pusudaki Kurulumlar (Aşama 1):</b> {s1}\n"
            f"🔥 <b>Altın Bölgedekiler (Aşama 2):</b> {s2}\n\n"
            f"💰 <b>Güncel Kasa:</b> <b><code>{cur_bank:.2f}$</code></b> (Bugün: <code>{fmt_usd_signed(diff)}</code>)\n"
            f"📌 <i>Saatlik ve günlük detaylı TP/SL tablosu için /rapor yazabilirsin.</i>"
        )

    def format_live_report_text(self) -> str:
        now_tr = datetime.now(TR_TZ)
        now_ms = int(time.time() * 1000)
        one_hour_ago_ms = now_ms - 3600 * 1000
        daily_events = self.state.get("daily_events", [])
        hour_events = [e for e in daily_events if int(e.get("ts", 0)) >= one_hour_ago_ms]

        tracked = self.state.get("tracked_trades", {})
        in_trade_cnt = sum(1 for t in tracked.values() if t.get("status") == "IN_TRADE")
        waiting_cnt = sum(1 for t in tracked.values() if t.get("status") == "WAITING_ZONE")

        return format_hourly_report_message(
            window_label=f"Son 1 Saat — {now_tr.strftime('%H:%M')}",
            day_label=now_tr.strftime("%d.%m.%Y"),
            hour_events=hour_events,
            daily_events=daily_events,
            active_in_trade_count=in_trade_cnt,
            active_waiting_count=waiting_cnt,
            day_start_bankroll=float(self.state.get("day_start_bankroll", Config.DEFAULT_BANKROLL_USD)),
            current_bankroll=float(self.state.get("current_bankroll", Config.DEFAULT_BANKROLL_USD)),
            is_manual=True,
        )

    def format_active_setups_text(self) -> str:
        if not self.active_setups:
            return (
                "📭 Şu anda kuralların tamamını karşılayan aktif bir Aşama 1 veya Aşama 2 "
                "Fibonacci kurulumu bulunmuyor. Tarama kesintisiz devam ediyor."
            )

        lines = ["📌 <b>AKTİF FİBONACCİ + SMC KURULUMLARI (15dk)</b>\n━━━━━━━━━━━━━━━━━━━━━━"]
        sorted_setups = sorted(
            self.active_setups.values(),
            key=lambda s: (0 if s.stage == "STAGE_2_GOLDEN_ZONE" else 1, -s.strength_score, -s.turnover_24h),
        )
        for s in sorted_setups[:20]:
            badge = "🔥 AŞAMA 2 (ALTIN BÖLGE)" if s.stage == "STAGE_2_GOLDEN_ZONE" else "🎯 AŞAMA 1 (LİMİT PUSU)"
            d_icon = "🟢 LONG" if s.direction == "LONG" else "🔴 SHORT"
            lines.append(
                f"{badge} | <b>#{s.symbol}</b> ({d_icon}) — <b>{s.quality_grade} ({s.strength_score}p | Kasa %{s.recommended_bankroll_pct})</b>\n"
                f"   Fiyat: <code>{fmt_price(s.current_price)}</code> | "
                f"0.618: <code>{fmt_price(s.fib.level_0618)}</code> | "
                f"TP1: <code>{fmt_price(s.fib.level_0)}</code> | "
                f"SL: <code>{fmt_price(s.fib.stop_loss)}</code>"
            )
        return "\n\n".join(lines)

    def trigger_manual_scan_text(self) -> str:
        stats = self.run_single_scan()
        return (
            f"✅ <b>Tarama Tamamlandı!</b>\n"
            f"• İncelenen Parite: <b>{stats['scanned']}</b>\n"
            f"• Pusuda Bekleyen (Aşama 1): <b>{stats['active_stage1']}</b>\n"
            f"• Altın Bölgede Olan (Aşama 2): <b>{stats['active_stage2']}</b>\n"
            f"• Yeni Gönderilen Sinyal: <b>{stats['sent_stage1'] + stats['sent_stage2']}</b>\n"
            f"• Güncel Kasa: <b><code>{stats['current_bankroll']:.2f}$</code></b>\n\n"
            f"Detaylar için /aktif veya /rapor yazabilirsin."
        )


def start_health_http_server(engine: ScannerEngine, port: int) -> None:
    """
    Ücretsiz bulut servislerinde (Render Frankfurt + cron-job.org) botun 7/24 uyanık kalması
    ve dışarıdan /health veya /scan ile tetiklenebilmesi için ultra hafif yerleşik HTTP sunucusu.
    """

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path.startswith("/scan"):
                stats = engine.run_single_scan()
                body = json.dumps({"ok": True, "stats": stats}, ensure_ascii=False).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.end_headers()
                self.wfile.write(body)
            else:
                body = json.dumps(
                    {
                        "ok": True,
                        "service": "bybit-15m-fibonacci-scanner",
                        "scan_count": engine.scan_count,
                        "last_scan": engine.last_scan_time_str,
                        "active_setups": len(engine.active_setups),
                        "current_bankroll": float(engine.state.get("current_bankroll", Config.DEFAULT_BANKROLL_USD)),
                    },
                    ensure_ascii=False,
                ).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.end_headers()
                self.wfile.write(body)

        def log_message(self, format, *args):
            return

    server = HTTPServer(("0.0.0.0", port), Handler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    print(f"[SUNUCU] 7/24 Bulut Uyumlu Sağlık/Tetikleme Sunucusu aktif -> Port {port} (/health, /scan)")


def main() -> None:
    parser = argparse.ArgumentParser(description="Bybit 15m Fibonacci Golden Zone Scanner")
    parser.add_argument("--once", action="store_true", help="Sadece 1 kez tarama yap ve çık")
    parser.add_argument("--dry-run", action="store_true", help="Telegram mesajı göndermeden konsolda test et")
    args = parser.parse_args()

    print("=" * 72)
    print("  BYBIT 15DK FİBONACCİ + SMC/ICT KASA YÖNETİMLİ TARAYICI")
    print("=" * 72)

    engine = ScannerEngine(dry_run=args.dry_run)

    if args.once:
        if not args.dry_run and engine.notifier.is_configured:
            engine.notifier.register_bot_commands()
        stats = engine.run_single_scan()
        if not args.dry_run and engine.notifier.is_configured:
            engine.notifier.poll_commands(
                get_status_cb=engine.format_status_text,
                get_active_cb=engine.format_active_setups_text,
                trigger_scan_cb=engine.trigger_manual_scan_text,
                get_report_cb=engine.format_live_report_text,
            )
            engine.state["last_update_id"] = engine.notifier._last_update_id
            engine._save_state()
        print(
            "\n"
            + engine.format_active_setups_text()
            .replace("<b>", "")
            .replace("</b>", "")
            .replace("<code>", "")
            .replace("</code>", "")
        )
        return

    if not engine.notifier.token:
        print(
            "\n[BİLGİ] Henüz TELEGRAM_BOT_TOKEN ayarlanmamış!\n"
            "Lütfen '1_KURULUM_SIHIRBAZI.bat' dosyasını çalıştırarak 1 dakikada botunu bağla.\n"
        )
    elif not engine.notifier.chat_id:
        engine.notifier.register_bot_commands()
        print(
            "\n[BİLGİ] Bot Token yüklü! Telegram'dan botuna '/start' yazdığın an "
            "Chat ID'n otomatik algılanacak ve bildirimler başlayacak!\n"
        )
    else:
        engine.notifier.register_bot_commands()
        engine.notifier.send_message(
            "🟢 <b>Bybit 15dk Fibonacci + SMC/ICT Kasa Yönetimli Tarayıcı Güncellendi!</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "✨ <b>Yeni Özellikler Aktif:</b>\n"
            "  • 🧠 <b>SMC/ICT Güç Skoru (0–100 Puan):</b> FVG, Order Block, EMA 50/200 ve RVOL kesişimi\n"
            "  • 💼 <b>Otomatik Kasa Yüzdesi:</b> Kaliteye göre (<code>A+ -> %20</code>, <code>A -> %15</code>, <code>B -> %8</code>) giriş ve net $ kâr/risk hesabı\n"
            "  • ⏰ <b>Saat Başı Rapor:</b> Her saat başı o saat içindeki TP/SL'ler + Günün genel durumu\n"
            "  • 🏁 <b>Gün Sonu Bilançosu:</b> Her gece 00:00'da toplam TP, SL ve net $ kasa kazancı\n"
            "  • 📊 İstediğin an canlı tablo için: /rapor"
        )

    start_health_http_server(engine, Config.PORT)

    last_scan_ts = 0.0
    while True:
        try:
            now = time.time()
            if now - last_scan_ts >= Config.SCAN_INTERVAL_SECONDS:
                engine.run_single_scan()
                last_scan_ts = time.time()

            prev_upd_id = engine.notifier._last_update_id
            engine.notifier.poll_commands(
                get_status_cb=engine.format_status_text,
                get_active_cb=engine.format_active_setups_text,
                trigger_scan_cb=engine.trigger_manual_scan_text,
                get_report_cb=engine.format_live_report_text,
            )
            if engine.notifier._last_update_id != prev_upd_id:
                engine.state["last_update_id"] = engine.notifier._last_update_id
                engine._save_state()

            time.sleep(2)
        except KeyboardInterrupt:
            print("\n[ÇIKIŞ] Tarayıcı kullanıcı tarafından durduruldu.")
            break
        except Exception as exc:
            print(f"[HATA] Döngü hatası (5 sn sonra devam edecek): {exc}")
            time.sleep(5)


if __name__ == "__main__":
    main()
