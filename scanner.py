# -*- coding: utf-8 -*-
"""
Ana Tarayıcı Motoru + Durum Yönetimi (Spam Önleyici) + 0$ Bulut Uyumlu HTTP Sunucusu
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
from strategy import FibLevels, FibSetup, analyze_symbol, check_candle_confirmation, get_session_label
from telegram_notifier import (
    TelegramNotifier,
    fmt_price,
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

        # Kalıcı durum: { "sent_alerts": { setup_id: ["STAGE_1", "STAGE_2"] }, "flip_watch": { symbol: {"stopped_dir": "SHORT", "ts": 123} }, "last_update_id": 0 }
        self.state: Dict = self._load_state()
        self.notifier._last_update_id = int(self.state.get("last_update_id", 0))

    def _load_state(self) -> Dict:
        default_state = {"sent_alerts": {}, "flip_watch": {}, "tracked_trades": {}}
        if not STATE_PATH.exists():
            return default_state
        try:
            data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
            if "sent_alerts" not in data:
                data["sent_alerts"] = {}
            if "flip_watch" not in data:
                data["flip_watch"] = {}
            if "tracked_trades" not in data:
                data["tracked_trades"] = {}
            return data
        except Exception:
            return default_state

    def _save_state(self) -> None:
        try:
            # Eski kayıtları temizle (en fazla son 400 kurulumu tut)
            sent = self.state.get("sent_alerts", {})
            if len(sent) > 400:
                keys = list(sent.keys())[-300:]
                self.state["sent_alerts"] = {k: sent[k] for k in keys}

            now_ms = int(time.time() * 1000)
            flips = self.state.get("flip_watch", {})
            # 12 saatten eski flip kayıtlarını temizle
            self.state["flip_watch"] = {
                sym: info
                for sym, info in flips.items()
                if now_ms - int(info.get("ts", 0)) < 12 * 3600 * 1000
            }
            STATE_PATH.write_text(json.dumps(self.state, indent=2, ensure_ascii=False), encoding="utf-8")
        except Exception as exc:
            print(f"[UYARI] state.json kaydedilemedi: {exc}")

    def run_single_scan(self) -> Dict[str, int]:
        """Tüm yüksek hacimli Bybit USDT vadeli paritelerini tarar ve yeni sinyalleri gönderir."""
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

                # extreme_ts sonrasındaki mumları sırayla kontrol et
                post_candles = [c for c in sym_candles if c.timestamp > extreme_ts]
                if not post_candles:
                    continue

                if status == "WAITING_ZONE":
                    # Aşama 1'den Aşama 2'ye (Altın Bölgeye) geçiş kontrolü
                    entered_zone = False
                    zone_ts = 0
                    deepest_ret = 0.0
                    touched_618 = False
                    invalidated = False

                    for c in post_candles:
                        # SL (1.03) kırıldıysa iptal
                        init_sl = float(tr["initial_sl"])
                        if (direction == "LONG" and c.low <= init_sl) or (
                            direction == "SHORT" and c.high >= init_sl
                        ):
                            invalidated = True
                            break

                        # Altın bölgeye değmeden 0.0 (TP1) seviyesini yukarı/aşağı kırıp gittiyse eski pusu iptal (Video 2 [04:40])
                        if not entered_zone:
                            if (direction == "LONG" and c.close > tp1_val) or (
                                direction == "SHORT" and c.close < tp1_val
                            ):
                                if not self.dry_run and self.notifier.is_configured:
                                    self.notifier.send_message(format_tp_hit_message(tr, "MISSED_NO_PULLBACK", c.close))
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
                            conf = check_candle_confirmation(sym_candles[:-1], max(1, len(sym_candles) - 8), fib_obj, direction)
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
                            )
                            msg = format_setup_message(s2_setup, "STAGE_2")
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

                    # Stop kontrolü (TP1 sonrası Başabaş, TP2 sonrası TP1 İz Süren Stop dahil)
                    if (direction == "LONG" and live_c.low <= active_sl) or (
                        direction == "SHORT" and live_c.high >= active_sl
                    ):
                        if "TP1" not in hits:
                            flip_watch[sym] = {"stopped_dir": direction, "ts": int(time.time() * 1000)}
                            if not self.dry_run and self.notifier.is_configured:
                                self.notifier.send_message(format_tp_hit_message(tr, "STOP_LOSS", live_c.close))
                        elif "TP2" not in hits:
                            if not self.dry_run and self.notifier.is_configured:
                                self.notifier.send_message(format_tp_hit_message(tr, "BREAKEVEN_STOP", live_c.close))
                        else:
                            if not self.dry_run and self.notifier.is_configured:
                                self.notifier.send_message(format_tp_hit_message(tr, "TRAILING_TP1_STOP", live_c.close))
                        del tracked_trades[tid]
                        continue

                    # TP1 (0.0 seviyesi) kontrolü
                    if "TP1" not in hits and (
                        (direction == "LONG" and bar_high >= tp1_val)
                        or (direction == "SHORT" and bar_low <= tp1_val)
                    ):
                        hits.append("TP1")
                        tr["current_sl"] = level_0618  # Stop'u girişe (Başabaş) çek!
                        if not self.dry_run and self.notifier.is_configured:
                            self.notifier.send_message(format_tp_hit_message(tr, "TP1", live_c.close))
                            print(f"[TELEGRAM GÖNDERİLDİ] TP1 Vuruldu -> {sym} ({direction})")

                    # TP2 (-0.5 uzatma seviyesi) kontrolü
                    if "TP1" in hits and "TP2" not in hits and (
                        (direction == "LONG" and bar_high >= tp2_val)
                        or (direction == "SHORT" and bar_low <= tp2_val)
                    ):
                        hits.append("TP2")
                        tr["current_sl"] = tp1_val  # Stop'u TP1'e taşı!
                        if not self.dry_run and self.notifier.is_configured:
                            self.notifier.send_message(format_tp_hit_message(tr, "TP2", live_c.close))
                            print(f"[TELEGRAM GÖNDERİLDİ] TP2 Vuruldu -> {sym} ({direction})")

                    # TP3 (-1.0 tam uzatma seviyesi) kontrolü
                    if "TP2" in hits and "TP3" not in hits and (
                        (direction == "LONG" and bar_high >= tp3_val)
                        or (direction == "SHORT" and bar_low <= tp3_val)
                    ):
                        hits.append("TP3")
                        if not self.dry_run and self.notifier.is_configured:
                            self.notifier.send_message(format_tp_hit_message(tr, "TP3", live_c.close))
                            print(f"[TELEGRAM GÖNDERİLDİ] TP3 Vuruldu -> {sym} ({direction})")
                        del tracked_trades[tid]

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
                    # Video 2 [05:00-05:58]: Stop olan yönü kaydet -> Ters yönde kurulum gelirse "Trend Dönüşü" olarak işaretle!
                    flip_watch[symbol] = {
                        "stopped_dir": setup.direction,
                        "ts": int(time.time() * 1000),
                    }
                    continue

                current_active[symbol] = setup
                already_sent = sent_alerts.get(setup.setup_id, [])

                if setup.stage == "STAGE_1_LIMIT_READY" and "STAGE_1" not in already_sent and "STAGE_2" not in already_sent:
                    msg = format_setup_message(setup, "STAGE_1")
                    if self.dry_run or not self.notifier.is_configured:
                        print(f"\n[SİNYAL - AŞAMA 1] {setup.symbol} ({setup.direction}) | Giriş(0.618): {fmt_price(setup.fib.level_0618)} | TP(0): {fmt_price(setup.fib.level_0)} | SL: {fmt_price(setup.fib.stop_loss)}")
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
                                "hits": [],
                                "ts": int(time.time() * 1000),
                            }
                            stage1_sent += 1
                            print(f"[TELEGRAM GÖNDERİLDİ] Aşama 1 -> {setup.symbol} ({setup.direction})")

                elif setup.stage == "STAGE_2_GOLDEN_ZONE" and "STAGE_2" not in already_sent:
                    msg = format_setup_message(setup, "STAGE_2")
                    if self.dry_run or not self.notifier.is_configured:
                        print(f"\n[SİNYAL - AŞAMA 2 ALTIN BÖLGE] {setup.symbol} ({setup.direction}) | Anlık: {fmt_price(setup.current_price)} | 0.618: {fmt_price(setup.fib.level_0618)} | Onay: {setup.candle_confirmation or 'Yok'}")
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
                                "hits": [],
                                "ts": int(time.time() * 1000),
                            }
                            stage2_sent += 1
                            print(f"[TELEGRAM GÖNDERİLDİ] Aşama 2 -> {setup.symbol} ({setup.direction})")

            self.active_setups = current_active
            self.scan_count += 1
            self.last_scanned_symbols_count = len(klines_map)
            now_tr = datetime.now(TR_TZ).strftime("%d.%m.%Y %H:%M:%S")
            self.last_scan_time_str = now_tr
            elapsed = time.time() - start_t

            if not self.dry_run:
                self._save_state()

            s1_total = sum(1 for s in current_active.values() if s.stage == "STAGE_1_LIMIT_READY")
            s2_total = sum(1 for s in current_active.values() if s.stage == "STAGE_2_GOLDEN_ZONE")
            print(
                f"[{now_tr}] Tarama #{self.scan_count} tamamlandı ({elapsed:.1f}s) | "
                f"Parite: {len(klines_map)} | Aktif Pusu (Aşama 1): {s1_total} | "
                f"Altın Bölgede (Aşama 2): {s2_total} | Yeni Bildirim: {stage1_sent + stage2_sent}"
            )
            return {
                "scanned": len(klines_map),
                "active_stage1": s1_total,
                "active_stage2": s2_total,
                "sent_stage1": stage1_sent,
                "sent_stage2": stage2_sent,
            }

    def format_status_text(self) -> str:
        s1 = sum(1 for s in self.active_setups.values() if s.stage == "STAGE_1_LIMIT_READY")
        s2 = sum(1 for s in self.active_setups.values() if s.stage == "STAGE_2_GOLDEN_ZONE")
        return (
            f"📊 <b>TARAYICI DURUM RAPORU</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"✅ <b>Durum:</b> Aktif Çalışıyor (15dk Mumlar)\n"
            f"🏦 <b>Borsa:</b> Bybit USDT Vadeli (Perpetual)\n"
            f"🔢 <b>Taranan Parite:</b> {self.last_scanned_symbols_count} yüksek hacimli coin\n"
            f"🔄 <b>Toplam Tarama:</b> {self.scan_count}\n"
            f"🕒 <b>Son Tarama (TSİ):</b> <code>{self.last_scan_time_str}</code>\n"
            f"🎯 <b>Pusudaki Kurulumlar (Aşama 1):</b> {s1}\n"
            f"🔥 <b>Altın Bölgedekiler (Aşama 2):</b> {s2}"
        )

    def format_active_setups_text(self) -> str:
        if not self.active_setups:
            return (
                "📭 Şu anda kuralların tamamını karşılayan aktif bir Aşama 1 veya Aşama 2 "
                "Fibonacci kurulumu bulunmuyor. Tarama kesintisiz devam ediyor."
            )

        lines = ["📌 <b>AKTİF FİBONACCİ KURULUMLARI (15dk)</b>\n━━━━━━━━━━━━━━━━━━━━━━"]
        # Önce Aşama 2 (Altın Bölge), sonra Aşama 1
        sorted_setups = sorted(
            self.active_setups.values(),
            key=lambda s: (0 if s.stage == "STAGE_2_GOLDEN_ZONE" else 1, -s.turnover_24h),
        )
        for s in sorted_setups[:20]:
            badge = "🔥 AŞAMA 2 (ALTIN BÖLGE)" if s.stage == "STAGE_2_GOLDEN_ZONE" else "🎯 AŞAMA 1 (LİMİT PUSU)"
            d_icon = "🟢 LONG" if s.direction == "LONG" else "🔴 SHORT"
            lines.append(
                f"{badge} | <b>#{s.symbol}</b> ({d_icon})\n"
                f"   Fiyat: <code>{fmt_price(s.current_price)}</code> | "
                f"0.618: <code>{fmt_price(s.fib.level_0618)}</code> | "
                f"TP: <code>{fmt_price(s.fib.level_0)}</code> | "
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
            f"• Yeni Gönderilen Sinyal: <b>{stats['sent_stage1'] + stats['sent_stage2']}</b>\n\n"
            f"Detaylı liste için /aktif yazabilirsin."
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
                    },
                    ensure_ascii=False,
                ).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.end_headers()
                self.wfile.write(body)

        def log_message(self, format, *args):
            # Konsolu gereksiz HTTP erişim loglarıyla kirletme
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
    print("  BYBIT 15DK FİBONACCİ GOLDEN ZONE TARAYICI (THE MOVING AVERAGE BİREBİR)")
    print("=" * 72)

    engine = ScannerEngine(dry_run=args.dry_run)

    if args.once:
        if not args.dry_run and engine.notifier.is_configured:
            engine.notifier.register_bot_commands()
            if not engine.state.get("sent_alerts"):
                engine.notifier.send_message(
                    "🟢 <b>Bybit 15dk Fibonacci Golden Zone Tarayıcı Bağlandı!</b>\n"
                    f"🆔 Chat ID: <code>{engine.notifier.chat_id}</code>\n"
                    f"En yüksek hacimli <b>{Config.TOP_SYMBOLS_COUNT}</b> USDT vadeli paritesi videodaki "
                    "6 özel Fibonacci seviyesi ve BoS kurallarıyla taranıyor..."
                )
        stats = engine.run_single_scan()
        if not args.dry_run and engine.notifier.is_configured:
            engine.notifier.poll_commands(
                get_status_cb=engine.format_status_text,
                get_active_cb=engine.format_active_setups_text,
                trigger_scan_cb=engine.trigger_manual_scan_text,
            )
            engine.state["last_update_id"] = engine.notifier._last_update_id
            engine._save_state()
        print("\n" + engine.format_active_setups_text().replace("<b>", "").replace("</b>", "").replace("<code>", "").replace("</code>", ""))
        return

    if not engine.notifier.token:
        print(
            "\n[BİLGİ] Henüz TELEGRAM_BOT_TOKEN ayarlanmamış!\n"
            "Lütfen '1_KURULUM_SIHIRBAZI.bat' dosyasını çalıştırarak 1 dakikada botunu bağla.\n"
            "(Şimdilik konsol izleme modunda tarama başlıyor...)\n"
        )
    elif not engine.notifier.chat_id:
        engine.notifier.register_bot_commands()
        print(
            "\n[BİLGİ] Bot Token yüklü! Telegram'dan botuna '/start' (veya herhangi bir mesaj) yazdığın an "
            "Chat ID'n otomatik algılanacak ve bildirimler başlayacak!\n"
        )
    else:
        engine.notifier.register_bot_commands()
        engine.notifier.send_message(
            "🟢 <b>Bybit 15dk Fibonacci Tarayıcı Başlatıldı!</b>\n"
            f"En yüksek hacimli <b>{Config.TOP_SYMBOLS_COUNT}</b> USDT vadeli paritesi videodaki "
            "kurallarla 7/24 taranıyor. Komutlar için /yardim yazabilirsin."
        )

    start_health_http_server(engine, Config.PORT)

    last_scan_ts = 0.0
    while True:
        try:
            # 1) Önce tarama süresi geldiyse (veya ilk açılışsa) pariteleri tara ki /aktif ve /durum dolu gelsin
            now = time.time()
            if now - last_scan_ts >= Config.SCAN_INTERVAL_SECONDS:
                engine.run_single_scan()
                last_scan_ts = time.time()

            # 2) Her 2 saniyede bir Telegram komutlarını kontrol et (/start, /durum, /aktif, /tara)
            prev_upd_id = engine.notifier._last_update_id
            engine.notifier.poll_commands(
                get_status_cb=engine.format_status_text,
                get_active_cb=engine.format_active_setups_text,
                trigger_scan_cb=engine.trigger_manual_scan_text,
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
