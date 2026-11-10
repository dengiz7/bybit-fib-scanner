# -*- coding: utf-8 -*-
"""
Hacim Patlaması & Zirve Kırılımı (Breakout Sniper) Ana Tarayıcı Motoru
- 15 Dakikalık Mumlar Üzerinde 20-Mum Kırılımlarını ve Anlık Hacim Patlamalarını (RVOL >= 1.8x) Tarar.
- Bybit Demo Hesabında 15x İzole Kaldıraçla Otomatik Pozisyon Açar.
- TP1'de (%1.6x ATR) %75 Kârı Kilitler, Stop'u Girişe Çeker; TP2'de (%2.8x ATR) Kalanı Kapatır.
- 0$ Bulut HTTP Sunucusu (Render) + Telegram Bot Komutları (/rapor, /demo, /kasa 100, /durum, /tara).
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
from bybit_trader import BybitDemoTrader
from config import STATE_PATH, Config
from strategy import BreakoutSetup, analyze_symbol, get_session_label
from telegram_notifier import (
    TelegramNotifier,
    fmt_price,
    fmt_usd_signed,
    format_hourly_report_message,
    format_setup_message,
    format_tp_hit_message,
)

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
        self.demo_trader = BybitDemoTrader()
        self.notifier = TelegramNotifier()
        self._lock = threading.Lock()

        self.scan_count: int = 0
        self.last_scan_time_str: str = "Henüz taranmadı"
        self.last_scanned_symbols_count: int = 0
        self.active_setups: Dict[str, BreakoutSetup] = {}

        self.state: Dict = self._load_state()
        self.notifier._last_update_id = int(self.state.get("last_update_id", 0))

    def _load_state(self) -> Dict:
        now_tr = datetime.now(TR_TZ)
        today_key = now_tr.strftime("%Y-%m-%d")
        hour_key = now_tr.strftime("%Y-%m-%d_%H")

        default_state = {
            "sent_alerts": {},
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

            STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
            STATE_PATH.write_text(json.dumps(self.state, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as exc:
            print(f"[UYARI] State kaydedilemedi: {exc}")

    def sync_real_bybit_balance_and_pnl(self) -> None:
        """Bybit Demo hesabındaki gerçek cüzdan bakiyesini ve gerçekleşen PnL kayıtlarını senkronize eder."""
        if not self.demo_trader.is_configured:
            return

        acc_summary = self.demo_trader.get_demo_account_summary()
        if acc_summary is not None:
            self.state["wallet_balance"] = acc_summary["wallet_balance"]
            self.state["total_equity"] = acc_summary["total_equity"]
            self.state["unrealised_pnl"] = acc_summary["unrealised_pnl"]
            self.state["current_bankroll"] = acc_summary["total_equity"]

        now_tr = datetime.now(TR_TZ)
        today_key = now_tr.strftime("%Y-%m-%d")
        daily_events: List[Dict] = self.state.setdefault("daily_events", [])
        synced_ids = set(self.state.setdefault("synced_bybit_order_ids", []))

        closed_pnl_records = self.demo_trader.get_closed_pnl_records(limit=30)
        for rec in closed_pnl_records:
            oid = str(rec.get("orderId") or rec.get("execId") or "")
            if not oid or oid in synced_ids:
                continue
            synced_ids.add(oid)

            sym = rec.get("symbol", "")
            side = rec.get("side", "")
            direction = "LONG" if side == "Buy" else "SHORT"
            try:
                real_pnl = round(float(rec.get("closedPnl") or 0.0), 2)
                upd_ms = int(rec.get("updatedTime") or time.time() * 1000)
            except Exception:
                continue

            ev_dt = datetime.fromtimestamp(upd_ms / 1000.0, tz=TR_TZ)
            if ev_dt.strftime("%Y-%m-%d") != today_key:
                continue

            matched = False
            for ev in reversed(daily_events):
                if ev.get("symbol") == sym and abs(int(ev.get("ts", 0)) - upd_ms) < 300_000 and not ev.get("bybit_synced"):
                    ev["pnl_usd"] = real_pnl
                    ev["bybit_synced"] = True
                    matched = True
                    break

            if not matched:
                etype = "TP1" if real_pnl >= 0 else "STOP_LOSS"
                daily_events.append(
                    {
                        "setup_id": f"{sym}_{direction}_{oid[:8]}",
                        "symbol": sym,
                        "direction": direction,
                        "grade": "A+",
                        "margin_usd": 20.0,
                        "event_type": etype,
                        "pnl_usd": real_pnl,
                        "trade_total_usd": real_pnl,
                        "ts": upd_ms,
                        "hour_key": ev_dt.strftime("%Y-%m-%d_%H"),
                        "day_key": ev_dt.strftime("%Y-%m-%d"),
                        "time_str": ev_dt.strftime("%H:%M"),
                        "bybit_synced": True,
                    }
                )

        if len(synced_ids) > 300:
            self.state["synced_bybit_order_ids"] = list(synced_ids)[-200:]

    def _record_trade_event(
        self,
        setup_id: str,
        tr: Dict,
        event_type: str,
        pnl_usd: float,
    ) -> float:
        now_tr = datetime.now(TR_TZ)
        if not self.dry_run and self.demo_trader.is_configured:
            acc = self.demo_trader.get_demo_account_summary()
            if acc is not None:
                self.state["wallet_balance"] = acc["wallet_balance"]
                self.state["unrealised_pnl"] = acc["unrealised_pnl"]
                self.state["current_bankroll"] = acc["total_equity"]
            else:
                cur_bank = float(self.state.get("current_bankroll", Config.DEFAULT_BANKROLL_USD))
                self.state["current_bankroll"] = round(cur_bank + pnl_usd, 2)
        else:
            cur_bank = float(self.state.get("current_bankroll", Config.DEFAULT_BANKROLL_USD))
            self.state["current_bankroll"] = round(cur_bank + pnl_usd, 2)

        tr["realized_usd"] = round(float(tr.get("realized_usd", 0.0)) + pnl_usd, 2)

        ev = {
            "setup_id": setup_id,
            "symbol": tr.get("symbol", "?"),
            "direction": tr.get("direction", "LONG"),
            "grade": tr.get("quality_grade", "A+"),
            "margin_usd": float(tr.get("margin_usd", 20.0)),
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
        """Her saat başı durum ve bilanço raporunu Telegram'a gönderir."""
        if self.dry_run or not self.notifier.is_configured:
            return

        now_tr = datetime.now(TR_TZ)
        today_key = now_tr.strftime("%Y-%m-%d")
        hour_key = now_tr.strftime("%Y-%m-%d_%H")

        saved_day_key = self.state.get("current_day_key", today_key)
        saved_hour_key = self.state.get("last_hourly_report_key", hour_key)

        # Gün değişimi (00:00 TSİ)
        if today_key != saved_day_key:
            day_end_bank = float(self.state.get("current_bankroll", Config.DEFAULT_BANKROLL_USD))
            self.state["daily_events"] = []
            self.state["day_start_bankroll"] = day_end_bank
            self.state["current_day_key"] = today_key
            self.state["last_hourly_report_key"] = hour_key
            self._save_state()
            return

        # Saat başı raporu
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

            hourly_msg = format_hourly_report_message(
                window_label=window_label,
                day_label=day_label,
                hour_events=hour_events,
                daily_events=daily_events,
                active_trades_count=in_trade_cnt,
                day_start_bankroll=float(self.state.get("day_start_bankroll", Config.DEFAULT_BANKROLL_USD)),
                current_bankroll=float(self.state.get("current_bankroll", Config.DEFAULT_BANKROLL_USD)),
                is_manual=False,
            )
            if self.notifier.send_message(hourly_msg):
                print(f"[TELEGRAM GÖNDERİLDİ] Saat Başı Raporu ({window_label})")

            self.state["last_hourly_report_key"] = hour_key
            self._save_state()

    def run_single_scan(self) -> Dict[str, int]:
        """Yüksek hacimli pariteleri tarar, açık işlemleri yönetir ve yeni kırılımlarda pozisyon açar."""
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

            current_active: Dict[str, BreakoutSetup] = {}
            breakout_sent = 0

            sent_alerts: Dict[str, List[str]] = self.state["sent_alerts"]
            tracked_trades: Dict[str, Dict] = self.state.setdefault("tracked_trades", {})

            # 0) Demo bakiye ve pozisyon koruma senkronizasyonu
            if not self.dry_run:
                self.sync_real_bybit_balance_and_pnl()
                self.demo_trader.sync_and_protect_open_positions(tracked_trades)

            # BTC Piyasa Rejimi / Korelasyon Kontrolü (1 saatlik yön)
            btc_regime = "NEUTRAL"
            if "BTCUSDT" in klines_map:
                btc_c_list = klines_map["BTCUSDT"][1]
                if len(btc_c_list) >= 5:
                    btc_1h_pct = (btc_c_list[-1].close - btc_c_list[-5].close) / btc_c_list[-5].close * 100.0
                    if btc_1h_pct < -0.80:
                        btc_regime = "BTC_DUMP"
                    elif btc_1h_pct > 0.80:
                        btc_regime = "BTC_PUMP"

            # 1) Açık takip edilen pozisyonların TP1, TP2 ve Stop-Loss kontrolleri
            for tid, tr in list(tracked_trades.items()):
                sym = tr.get("symbol")
                if sym not in klines_map:
                    continue
                ticker_obj, sym_candles = klines_map[sym]
                if not sym_candles:
                    continue

                direction = tr["direction"]
                entry_ts = int(tr.get("entry_ts", 0))
                post_candles = [c for c in sym_candles if c.timestamp > entry_ts]
                if not post_candles:
                    continue

                bar_high = max(c.high for c in post_candles)
                bar_low = min(c.low for c in post_candles)
                live_c = sym_candles[-1]

                tp1_p = float(tr.get("tp1_price") or tr.get("tp1", 0.0))
                tp2_p = float(tr.get("tp2_price") or tr.get("tp2", 0.0))
                active_sl = float(tr.get("current_sl") or tr.get("initial_sl", 0.0))
                entry_p = float(tr.get("entry_price") or tr.get("entry_0618", 0.0))

                margin_usd = float(tr.get("margin_usd", 10.0))
                trade_lev = float(tr.get("leverage") or 15)
                pos_usd = margin_usd * trade_lev
                hits: List[str] = tr.setdefault("hits", [])
                cur_bank = float(self.state.get("current_bankroll", Config.DEFAULT_BANKROLL_USD))

                # --- OYALANMA / STAGNATION KONTROLÜ (2.5 saat girişte oyalanırsa başabaş kapat) ---
                candles_in_trade = len(post_candles)
                pct_from_entry = abs(live_c.close - entry_p) / entry_p * 100.0 if entry_p > 0 else 0.0
                if candles_in_trade >= Config.STAGNATION_MAX_CANDLES and pct_from_entry < Config.STAGNATION_THRESHOLD_PCT and "TP1" not in hits:
                    tot_usd = self._record_trade_event(tid, tr, "STAGNATION_EXIT", 0.0)
                    demo_note = self.demo_trader.handle_trade_lifecycle_event(tr, "BREAKEVEN_STOP") if not self.dry_run else ""
                    if not self.dry_run and self.notifier.is_configured:
                        m_txt = (
                            f"⏱ <b>OYALANMA ÇIKIŞI (STAGNATION EXIT) — #{sym}</b>\n"
                            f"━━━━━━━━━━━━━━━━━━━━━━\n"
                            f"📌 İşlem <b>{candles_in_trade} mumdur (2.5 saat)</b> kâra gidemeyip giriş seviyesinde sıkıştı.\n"
                            f"🛡 Teminatı serbest bırakmak ve riski sıfırlamak için <b>Giriş Seviyesinde kapatıldı</b>.\n"
                            f"💼 <b>Güncel Kasa:</b> <code>{cur_bank:.2f}$</code>"
                        )
                        if demo_note:
                            m_txt += f"\n\n{demo_note}"
                        self.notifier.send_message(m_txt)
                        print(f"[TELEGRAM GÖNDERİLDİ] Oyalanma Çıkışı -> {sym}")
                    del tracked_trades[tid]
                    continue

                # --- STOP KONTROLÜ ---
                hit_sl = (direction == "LONG" and live_c.low <= active_sl) or (
                    direction == "SHORT" and live_c.high >= active_sl
                )

                if hit_sl:
                    if "TP1" in hits:
                        # Başabaş Stop (+0 zarar)
                        tot_usd = self._record_trade_event(tid, tr, "BREAKEVEN_STOP", 0.0)
                        demo_note = self.demo_trader.handle_trade_lifecycle_event(tr, "BREAKEVEN_STOP") if not self.dry_run else ""
                        if not self.dry_run and self.notifier.is_configured:
                            m_txt = format_tp_hit_message(
                                tr,
                                "BREAKEVEN_STOP",
                                live_c.close,
                                event_pnl_usd=0.0,
                                total_trade_pnl_usd=tot_usd,
                                current_bankroll=cur_bank,
                            )
                            if demo_note:
                                m_txt += f"\n\n{demo_note}"
                            self.notifier.send_message(m_txt)
                    else:
                        # Normal Stop-Loss
                        sl_ratio = abs(entry_p - active_sl) / entry_p if entry_p > 0 else 0.015
                        loss_usd = -1.0 * pos_usd * sl_ratio
                        tot_usd = self._record_trade_event(tid, tr, "STOP_LOSS", loss_usd)
                        demo_note = self.demo_trader.handle_trade_lifecycle_event(tr, "STOP_LOSS") if not self.dry_run else ""
                        
                        # Peş peşe stop sayacı ve dalgalanma koruma molası (Config'de açıksa)
                        if Config.COOLDOWN_AFTER_STOPS > 0:
                            consec = int(self.state.get("consecutive_stops", 0)) + 1
                            self.state["consecutive_stops"] = consec
                            if consec >= Config.COOLDOWN_AFTER_STOPS:
                                cooldown_ms = int(time.time() * 1000) + (Config.COOLDOWN_MINUTES * 60 * 1000)
                                self.state["cooldown_until_ts"] = cooldown_ms
                                self.state["consecutive_stops"] = 0
                                if not self.dry_run and self.notifier.is_configured:
                                    m_cool = (
                                        f"🛡️ <b>PİYASA DALGALANMA KORUMASI DEVREYE GİRDİ</b>\n"
                                        f"━━━━━━━━━━━━━━━━━━━━━━\n"
                                        f"⚠️ Peş peşe {Config.COOLDOWN_AFTER_STOPS} işlem stop-loss oldu!\n"
                                        f"🛑 Piyasa genelindeki anlık dump/sweep dalgalanmalarından kasayı korumak için <b>{Config.COOLDOWN_MINUTES} dakika yeni işlem açma durduruldu</b>.\n"
                                        f"⏱️ Süre bitiminde sistem piyasa durulmuş olarak taramalara otomatik devam edecektir."
                                    )
                                    self.notifier.send_message(m_cool)
                                    print(f"[KORUMA] Peş peşe {Config.COOLDOWN_AFTER_STOPS} stop! {Config.COOLDOWN_MINUTES} dk mola.")

                        if not self.dry_run and self.notifier.is_configured:
                            m_txt = format_tp_hit_message(
                                tr,
                                "STOP_LOSS",
                                live_c.close,
                                event_pnl_usd=loss_usd,
                                total_trade_pnl_usd=tot_usd,
                                current_bankroll=cur_bank,
                            )
                            if demo_note:
                                m_txt += f"\n\n{demo_note}"
                            self.notifier.send_message(m_txt)

                    del tracked_trades[tid]
                    continue

                # --- TP2 KONTROLÜ (Nihai Hedef) ---
                hit_tp2 = (direction == "LONG" and bar_high >= tp2_p) or (
                    direction == "SHORT" and bar_low <= tp2_p
                )
                if hit_tp2 and "TP2" not in hits:
                    hits.append("TP2")
                    self.state["consecutive_stops"] = 0
                    tp2_ratio = abs(tp2_p - entry_p) / entry_p if entry_p > 0 else 0.03
                    rem_gain_usd = (1.0 - Config.TP1_CLOSE_PCT) * pos_usd * tp2_ratio
                    tot_usd = self._record_trade_event(tid, tr, "TP2", rem_gain_usd)
                    demo_note = self.demo_trader.handle_trade_lifecycle_event(tr, "TP2") if not self.dry_run else ""
                    if not self.dry_run and self.notifier.is_configured:
                        m_txt = format_tp_hit_message(
                            tr,
                            "TP2",
                            live_c.close,
                            event_pnl_usd=rem_gain_usd,
                            total_trade_pnl_usd=tot_usd,
                            current_bankroll=cur_bank,
                        )
                        if demo_note:
                            m_txt += f"\n\n{demo_note}"
                        self.notifier.send_message(m_txt)
                        print(f"[TELEGRAM GÖNDERİLDİ] TP2 Vuruldu -> {sym} ({direction}) (+{rem_gain_usd:.2f}$)")
                    del tracked_trades[tid]
                    continue

                # --- TP1 KONTROLÜ (%75 Kâr Kilidi + SL Girişe Çekilir) ---
                hit_tp1 = (direction == "LONG" and bar_high >= tp1_p) or (
                    direction == "SHORT" and bar_low <= tp1_p
                )
                if hit_tp1 and "TP1" not in hits:
                    hits.append("TP1")
                    self.state["consecutive_stops"] = 0
                    tr["current_sl"] = entry_p  # Stop'u başabaş (giriş) seviyesine çek
                    tp1_ratio = abs(tp1_p - entry_p) / entry_p if entry_p > 0 else 0.016
                    gain_tp1_usd = Config.TP1_CLOSE_PCT * pos_usd * tp1_ratio
                    tot_usd = self._record_trade_event(tid, tr, "TP1", gain_tp1_usd)
                    demo_note = self.demo_trader.handle_trade_lifecycle_event(tr, "TP1") if not self.dry_run else ""
                    if not self.dry_run and self.notifier.is_configured:
                        m_txt = format_tp_hit_message(
                            tr,
                            "TP1",
                            live_c.close,
                            event_pnl_usd=gain_tp1_usd,
                            total_trade_pnl_usd=tot_usd,
                            current_bankroll=cur_bank,
                        )
                        if demo_note:
                            m_txt += f"\n\n{demo_note}"
                        self.notifier.send_message(m_txt)
                        print(f"[TELEGRAM GÖNDERİLDİ] TP1 Vuruldu -> {sym} ({direction}) (+{gain_tp1_usd:.2f}$)")

                    if Config.TP1_CLOSE_PCT >= 0.99:
                        del tracked_trades[tid]
                        continue

                # --- DİNAMİK KÂR KİLİDİ (TP1'e %85 yaklaşıp dönüş fitili verirse erkenden kapat) ---
                tp1_dist = abs(tp1_p - entry_p)
                if direction == "LONG":
                    progress = (bar_high - entry_p) / tp1_dist if tp1_dist > 0 else 0.0
                    is_pulling_back = live_c.close < bar_high - (0.35 * (bar_high - entry_p))
                else:
                    progress = (entry_p - bar_low) / tp1_dist if tp1_dist > 0 else 0.0
                    is_pulling_back = live_c.close > bar_low + (0.35 * (entry_p - bar_low))

                if "TP1" not in hits and progress >= Config.NEAR_TP_TRIGGER_PCT and is_pulling_back:
                    hits.append("TP1")
                    tr["current_sl"] = entry_p
                    gain_tp1_usd = Config.TP1_CLOSE_PCT * pos_usd * (abs(live_c.close - entry_p) / entry_p)
                    tot_usd = self._record_trade_event(tid, tr, "TP1", gain_tp1_usd)
                    demo_note = self.demo_trader.handle_trade_lifecycle_event(tr, "TP1") if not self.dry_run else ""
                    if not self.dry_run and self.notifier.is_configured:
                        m_txt = (
                            f"⚡ <b>DİNAMİK KÂR KİLİDİ (TP1'DEN DÖNÜŞ) — #{sym}</b>\n"
                            f"━━━━━━━━━━━━━━━━━━━━━━\n"
                            f"🎯 Fiyat TP1 hedefine <b>%{progress*100:.0f}</b> oranında ulaştı ve dönüş fitili verdi!\n"
                            f"💵 Kârı kaçırmamak için <b>pozisyonun %75'i erkenden kapatıldı (+{gain_tp1_usd:.2f}$)</b>.\n"
                            f"🛡 Kalan %25 için Stop-Loss <b>Giriş Fiyatına (Başabaş)</b> çekildi!"
                        )
                        if demo_note:
                            m_txt += f"\n\n{demo_note}"
                        self.notifier.send_message(m_txt)
                        print(f"[TELEGRAM GÖNDERİLDİ] Dinamik TP1 Kâr Kilidi -> {sym}")

                    if Config.TP1_CLOSE_PCT >= 0.99:
                        del tracked_trades[tid]
                        continue

            # 2) Yeni Hacimli Kırılımları Tara
            cur_bank = float(self.state.get("current_bankroll", Config.DEFAULT_BANKROLL_USD))
            cooldown_until = int(self.state.get("cooldown_until_ts", 0))
            now_ms = int(time.time() * 1000)
            in_cooldown = (Config.COOLDOWN_AFTER_STOPS > 0) and (now_ms < cooldown_until)
            if in_cooldown:
                rem_m = max(1, int((cooldown_until - now_ms) / 60000))
                print(f"[MOLA] Dalgalanma koruma molası devrede ({rem_m} dk kaldı). Yeni pozisyon açılmıyor.")

            for symbol, (ticker, candles) in klines_map.items():
                setup = analyze_symbol(
                    symbol=symbol,
                    candles=candles,
                    ticker=ticker,
                    bankroll_usd=cur_bank,
                    btc_regime=btc_regime,
                )
                if setup is None:
                    continue

                current_active[symbol] = setup
                already_sent = sent_alerts.get(setup.setup_id, [])

                if "BREAKOUT" not in already_sent:
                    if in_cooldown:
                        continue  # Mola süresince yeni pozisyon açma
                    msg = format_setup_message(setup, current_bankroll=cur_bank)
                    if not self.dry_run:
                        _, demo_note = self.demo_trader.place_setup_order(setup, "BREAKOUT", cur_bank)
                        if demo_note:
                            msg += f"\n\n{demo_note}"

                    if self.dry_run or not self.notifier.is_configured:
                        print(
                            f"\n[SİNYAL - BREAKOUT] {setup.symbol} ({setup.direction} | {setup.quality_grade}) "
                            f"| Giriş: {fmt_price(setup.entry_price)} | RVOL: {setup.rvol}x | TP1: {fmt_price(setup.tp1_price)}"
                        )

                    if not self.dry_run and self.notifier.is_configured:
                        if self.notifier.send_message(msg):
                            already_sent.append("BREAKOUT")
                            sent_alerts[setup.setup_id] = already_sent
                            tracked_trades[setup.setup_id] = {
                                "symbol": setup.symbol,
                                "direction": setup.direction,
                                "status": "IN_TRADE",
                                "entry_price": setup.entry_price,
                                "breakout_level": setup.breakout_level,
                                "tp1_price": setup.tp1_price,
                                "tp2_price": setup.tp2_price,
                                "initial_sl": setup.sl_price,
                                "current_sl": setup.sl_price,
                                "tp1_pct": setup.tp1_pct,
                                "tp2_pct": setup.tp2_pct,
                                "sl_pct": setup.sl_pct,
                                "rrr_tp1": setup.rrr_tp1,
                                "rrr_tp2": setup.rrr_tp2,
                                "rvol": setup.rvol,
                                "strength_score": setup.strength_score,
                                "quality_grade": setup.quality_grade,
                                "margin_usd": setup.recommended_margin_usd,
                                "leverage": getattr(setup, "leverage", Config.DEFAULT_LEVERAGE),
                                "entry_ts": candles[-2].timestamp if len(candles) >= 2 else int(time.time() * 1000),
                                "hits": [],
                                "realized_usd": 0.0,
                                "notes": setup.notes,
                                "ts": int(time.time() * 1000),
                            }
                            breakout_sent += 1
                            print(f"[TELEGRAM GÖNDERİLDİ] Hacim Kırılımı -> {setup.symbol} ({setup.direction} | {setup.rvol}x RVOL)")

            self.active_setups = current_active
            self.scan_count += 1
            self.last_scanned_symbols_count = len(klines_map)
            now_tr_str = datetime.now(TR_TZ).strftime("%d.%m.%Y %H:%M:%S")
            self.last_scan_time_str = now_tr_str
            elapsed = time.time() - start_t

            if not self.dry_run:
                self.sync_real_bybit_balance_and_pnl()
            self.check_and_send_scheduled_reports()

            if not self.dry_run:
                self._save_state()

            open_count = len(tracked_trades)
            print(
                f"[{now_tr_str}] Tarama #{self.scan_count} tamamlandı ({elapsed:.1f}s) | "
                f"Parite: {len(klines_map)} | Aktif Kırılım: {len(current_active)} | "
                f"Açık İşlem: {open_count} | Gerçek Kasa: {self.state.get('current_bankroll', 100.0):.2f}$"
            )
            return {
                "scanned": len(klines_map),
                "active_breakouts": len(current_active),
                "sent_breakouts": breakout_sent,
                "open_trades": open_count,
                "current_bankroll": float(self.state.get("current_bankroll", Config.DEFAULT_BANKROLL_USD)),
            }

    def format_status_text(self) -> str:
        if not self.dry_run:
            self.sync_real_bybit_balance_and_pnl()
        cur_bank = float(self.state.get("current_bankroll", Config.DEFAULT_BANKROLL_USD))
        wb_val = float(self.state.get("wallet_balance", cur_bank))
        upnl_val = float(self.state.get("unrealised_pnl", 0.0))
        start_bank = float(self.state.get("day_start_bankroll", Config.DEFAULT_BANKROLL_USD))
        diff = cur_bank - start_bank
        tracked = self.state.get("tracked_trades", {})
        open_cnt = len(tracked)

        return (
            f"📊 <b>TARAYICI VE GERÇEK KASA DURUM RAPORU</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🚀 <b>Strateji:</b> Hacim Patlaması & Kırılım (Breakout Sniper | 15dk)\n"
            f"🏦 <b>Borsa:</b> Bybit USDT Vadeli (Demo Canlı Senkronize)\n"
            f"🔢 <b>Taranan Parite:</b> {self.last_scanned_symbols_count} yüksek hacimli coin\n"
            f"🔄 <b>Toplam Tarama:</b> {self.scan_count}\n"
            f"🕒 <b>Son Tarama (TSİ):</b> <code>{self.last_scan_time_str}</code>\n"
            f"📈 <b>Açık / Takip Edilen İşlem:</b> <b>{open_cnt} adet</b>\n\n"
            f"💰 <b>Bybit Net Kasa (Equity):</b> <b><code>{cur_bank:.2f}$</code></b> (Bugün: <code>{fmt_usd_signed(diff)}</code>)\n"
            f"🏦 <b>Cüzdan Bakiyesi:</b> <code>{wb_val:.2f}$</code> | <b>Açık PnL:</b> <code>{fmt_usd_signed(upnl_val)}</code>\n"
            f"📌 <i>Detaylı bilanço için /rapor veya /demo yazabilirsin.</i>"
        )

    def format_live_report_text(self) -> str:
        if not self.dry_run:
            self.sync_real_bybit_balance_and_pnl()
        now_tr = datetime.now(TR_TZ)
        now_ms = int(time.time() * 1000)
        one_hour_ago_ms = now_ms - 3600 * 1000
        daily_events = self.state.get("daily_events", [])
        hour_events = [e for e in daily_events if int(e.get("ts", 0)) >= one_hour_ago_ms]

        tracked = self.state.get("tracked_trades", {})
        in_trade_cnt = len(tracked)

        return format_hourly_report_message(
            window_label=f"Son 1 Saat — {now_tr.strftime('%H:%M')}",
            day_label=now_tr.strftime("%d.%m.%Y"),
            hour_events=hour_events,
            daily_events=daily_events,
            active_trades_count=in_trade_cnt,
            day_start_bankroll=float(self.state.get("day_start_bankroll", Config.DEFAULT_BANKROLL_USD)),
            current_bankroll=float(self.state.get("current_bankroll", Config.DEFAULT_BANKROLL_USD)),
            is_manual=True,
        )

    def format_active_setups_text(self) -> str:
        if not self.active_setups:
            return (
                "📭 Şu anda kuralların tamamını (20-Mum Kırılımı + Güçlü Gövde + 1.8x RVOL) karşılayan yeni bir "
                "kırılım sinyali bulunmuyor. Tarama kesintisiz devam ediyor."
            )

        lines = ["🚀 <b>GÜNCEL HACİMLİ KIRILIM SİNYALLERİ (15dk)</b>\n━━━━━━━━━━━━━━━━━━━━━━"]
        for s in list(self.active_setups.values())[:15]:
            d_icon = "🟢 LONG" if s.direction == "LONG" else "🔴 SHORT"
            lines.append(
                f"• <b>#{s.symbol}</b> ({d_icon}) — <b>{s.rvol}x RVOL ({s.quality_grade})</b>\n"
                f"  Kırılan: <code>{fmt_price(s.breakout_level)}</code> | Giriş: <code>{fmt_price(s.entry_price)}</code>\n"
                f"  TP1: <code>{fmt_price(s.tp1_price)}</code> | TP2: <code>{fmt_price(s.tp2_price)}</code> | SL: <code>{fmt_price(s.sl_price)}</code>"
            )
        return "\n\n".join(lines)

    def trigger_manual_scan_text(self) -> str:
        stats = self.run_single_scan()
        return (
            f"✅ <b>Tarama Tamamlandı!</b>\n"
            f"• İncelenen Parite: <b>{stats['scanned']}</b>\n"
            f"• Aktif Kırılım: <b>{stats['active_breakouts']}</b>\n"
            f"• Açık İşlem Sayısı: <b>{stats['open_trades']}</b>\n"
            f"• Yeni Açılan İşlem: <b>{stats['sent_breakouts']}</b>\n"
            f"• Güncel Kasa: <b><code>{stats['current_bankroll']:.2f}$</code></b>\n\n"
            f"Detaylar için /demo veya /rapor yazabilirsin."
        )

    def format_demo_text(self) -> str:
        cur_bank = float(self.state.get("current_bankroll", Config.DEFAULT_BANKROLL_USD))
        return self.demo_trader.format_demo_status_text(cur_bank)

    def set_bankroll_text(self, new_bankroll: float) -> str:
        val = max(10.0, round(float(new_bankroll), 2))
        closed_pos = 0
        cancelled_ords = 0
        real_wb = val
        if not self.dry_run and self.demo_trader.is_configured:
            closed_pos, cancelled_ords, real_wb = self.demo_trader.reset_demo_account(val)
        self.state["current_bankroll"] = real_wb
        self.state["wallet_balance"] = real_wb
        self.state["unrealised_pnl"] = 0.0
        self.state["day_start_bankroll"] = real_wb
        self.state["tracked_trades"] = {}
        self.state["daily_events"] = []
        self._save_state()
        return (
            f"🔄 <b>SİSTEM VE BYBIT DEMO KASA SIFIRDAN BAŞLATILDI!</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"• 🧹 <b>Kapatılan Açık Pozisyon:</b> <code>{closed_pos} adet</code>\n"
            f"• 🗑 <b>İptal Edilen Bekleyen Emir:</b> <code>{cancelled_ords} adet</code>\n"
            f"• 🏦 <b>Yeni Gerçek Bybit Demo Bakiyesi:</b> <b><code>{real_wb:.2f}$</code></b>\n"
            f"• ⚡ <b>Kaldıraç & Marjin:</b> 30x İzole (İşlem başı %10 = ~10$ marjin)\n"
            f"• 🎯 <b>TP1 (%75 Kâr Kilidi):</b> TP1 vurulduğunda %75 kâr cebe kilitlenir, SL Girişe çekilir!"
        )

    def toggle_demo_auto_text(self, enable: bool) -> str:
        self.demo_trader.enabled = enable
        if enable:
            return "🟢 <b>Bybit Demo Otomatik Al-Sat AKTİF edildi!</b> Hacimli kırılımlarda otomatik işlem açılacak."
        return "⏸ <b>Bybit Demo Otomatik Al-Sat DURAKLATILDI!</b>"


def start_health_http_server(engine: ScannerEngine, port: int) -> None:
    """Render ve cron-job.org için 7/24 uyanık tutma ve sağlık sunucusu."""
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
                        "service": "bybit-15m-breakout-sniper",
                        "scan_count": engine.scan_count,
                        "last_scan": engine.last_scan_time_str,
                        "open_trades": len(engine.state.get("tracked_trades", {})),
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
    print(f"[SUNUCU] 7/24 Bulut Sağlık Sunucusu aktif -> Port {port} (/health, /scan)")


def main() -> None:
    parser = argparse.ArgumentParser(description="Bybit 15m Breakout Sniper Scanner")
    parser.add_argument("--once", action="store_true", help="Sadece 1 kez tarama yap ve çık")
    parser.add_argument("--dry-run", action="store_true", help="Telegram göndermeden test et")
    args = parser.parse_args()

    print("=" * 72)
    print("  BYBIT 15DK HACİM PATLAMASI & ZİRVE KIRILIMI (BREAKOUT SNIPER)")
    print("=" * 72)

    engine = ScannerEngine(dry_run=args.dry_run)

    if args.once:
        if not args.dry_run and engine.notifier.is_configured:
            engine.notifier.register_bot_commands()
        stats = engine.run_single_scan()
        if not args.dry_run and engine.notifier.is_configured:
            engine.notifier.poll_commands(
                get_status_cb=engine.format_status_text,
                trigger_scan_cb=engine.trigger_manual_scan_text,
                get_report_cb=engine.format_live_report_text,
                get_demo_cb=engine.format_demo_text,
                set_bankroll_cb=engine.set_bankroll_text,
                toggle_auto_cb=engine.toggle_demo_auto_text,
            )
            engine.state["last_update_id"] = engine.notifier._last_update_id
            engine._save_state()
        return

    if engine.notifier.token and engine.notifier.chat_id:
        engine.notifier.register_bot_commands()
        cleared = engine.demo_trader.cancel_all_open_orders() if not args.dry_run else 0
        engine.notifier.send_message(
            "🚀 <b>Hacim Patlaması & Zirve Kırılımı (Breakout Sniper) Botu Aktif!</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "✨ <b>Yeni Strateji Özellikleri:</b>\n"
            "  • ⚡ <b>Zaman Dilimi:</b> <code>15 Dakika (Donchian 20-Mum Kırılımı)</code>\n"
            "  • 💥 <b>Filtre:</b> En az <code>1.8x RVOL</code> kurumsal hacim patlaması + Güçlü mum gövdesi\n"
            "  • 💼 <b>İşlem Başı Marjin:</b> Kasanın <code>%10'u</code> (100$ kasa için <code>10$</code> marjin)\n"
            "  • ⚡ <b>Kaldıraç:</b> <code>30x İzole</code> (Daha fazla eşzamanlı işlem kapasitesi)\n"
            "  • 🎯 <b>TP1 (1.6x ATR):</b> %75 pozisyon kârla kapanır, Stop Giriş Seviyesine (Başabaş) çekilir!\n"
            "  • 🚀 <b>TP2 (2.8x ATR):</b> Kalan %25 pozisyon tam trend kazancıyla kapatılır!\n"
            "  • 🛡 <b>Açık İşlem Sınırı:</b> Fırsat geldikçe bakiye yettiğince eşzamanlı işlem açabilir.\n"
            f"  • 🧹 Eski bekleyen limit emirler temizlendi (<code>{cleared} adet</code>).\n"
            "  • 📊 Durum için: /durum, /demo veya /rapor yazabilirsin."
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
                trigger_scan_cb=engine.trigger_manual_scan_text,
                get_report_cb=engine.format_live_report_text,
                get_demo_cb=engine.format_demo_text,
                set_bankroll_cb=engine.set_bankroll_text,
                toggle_auto_cb=engine.toggle_demo_auto_text,
            )
            if engine.notifier._last_update_id != prev_upd_id:
                engine.state["last_update_id"] = engine.notifier._last_update_id
                engine._save_state()

            time.sleep(2)
        except KeyboardInterrupt:
            print("\n[ÇIKIŞ] Tarayıcı durduruldu.")
            break
        except Exception as exc:
            print(f"[HATA] Döngü hatası (5 sn sonra devam edecek): {exc}")
            time.sleep(5)


if __name__ == "__main__":
    main()
