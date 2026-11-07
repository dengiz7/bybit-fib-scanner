# -*- coding: utf-8 -*-
"""
The Moving Average (Arty) - 15 Dakikalık Fibonacci Retracement Stratejisi Konfigürasyonu
Videodaki tüm kuralların birebir (harfi harfine) parametrik karşılıkları.
"""

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
ENV_PATH = BASE_DIR / ".env"
STATE_PATH = BASE_DIR / "state.json"


def load_env_file(env_path: Path = ENV_PATH) -> None:
    """Harici kütüphane gerektirmeden .env dosyasını okur ve ortam değişkenlerine yükler."""
    if not env_path.exists():
        return
    try:
        for raw_line in env_path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, val = line.split("=", 1)
            key = key.strip()
            val = val.strip().strip('"').strip("'")
            if key and (key not in os.environ or not os.environ[key]):
                os.environ[key] = val
    except Exception as exc:
        print(f"[UYARI] .env okunurken hata: {exc}")


def save_env_value(key: str, value: str, env_path: Path = ENV_PATH) -> None:
    """Belirtilen anahtar-değer çiftini .env dosyasına kaydeder veya günceller."""
    os.environ[key] = str(value)
    lines = []
    found = False
    if env_path.exists():
        for raw_line in env_path.read_text(encoding="utf-8").splitlines():
            stripped = raw_line.strip()
            if stripped and not stripped.startswith("#") and "=" in stripped:
                k, _ = stripped.split("=", 1)
                if k.strip() == key:
                    lines.append(f"{key}={value}")
                    found = True
                    continue
            lines.append(raw_line)
    if not found:
        lines.append(f"{key}={value}")
    env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


load_env_file()


class Config:
    # --- Telegram Ayarları ---
    TELEGRAM_BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    TELEGRAM_CHAT_ID: str = os.getenv("TELEGRAM_CHAT_ID", "").strip()

    # --- Borsa ve Tarama Ayarları (Bybit USDT Perpetual) ---
    TIMEFRAME: str = os.getenv("TIMEFRAME", "15")  # 15 dakikalık mumlar
    TOP_SYMBOLS_COUNT: int = int(os.getenv("TOP_SYMBOLS_COUNT", "75"))  # En yüksek hacimli ilk 75 USDT paritesi
    MIN_24H_TURNOVER_USD: float = float(os.getenv("MIN_24H_TURNOVER_USD", "5000000"))  # Min 5M$ 24s hacim
    KLINE_LIMIT: int = 200  # Yapı ve ATR hesabı için çekilecek mum sayısı
    SCAN_INTERVAL_SECONDS: int = int(os.getenv("SCAN_INTERVAL_SECONDS", "30"))  # Tarama döngü süresi (sn)

    # --- Piyasa Yapısı (Market Structure) & BoS Ayarları ---
    # Video 1 [03:14]: Fitiller (wicks) piyasa yapısına dahildir.
    PIVOT_LEFT_BARS: int = int(os.getenv("PIVOT_LEFT_BARS", "3"))
    PIVOT_RIGHT_BARS: int = int(os.getenv("PIVOT_RIGHT_BARS", "2"))
    MAX_BOS_AGE_CANDLES: int = int(os.getenv("MAX_BOS_AGE_CANDLES", "14"))  # Son 14 mum (3.5 saat) içindeki taze BoS'lar

    # --- Kasa Büyütme: Minimum Kâr Mesafesi Filtresi (0.618 -> 0.0 TP1 arası saf fiyat yüzdesi) ---
    # Cılız %0.2-%0.4 dalgaları eler; TP1'de en az %0.85 (TP2'de ~%1.55, TP3'te ~%2.25+) potansiyel şart koşar.
    MIN_TP1_PCT: float = float(os.getenv("MIN_TP1_PCT", "0.85"))
    MIN_TP1_PCT_MAJORS: float = float(os.getenv("MIN_TP1_PCT_MAJORS", "0.55"))  # BTC, ETH, SOL, XRP, BNB için alt sınır

    # --- Kasa Yönetimi ve Pozisyon Büyüklüğü (100$ Baz Kasa & 10x İzole Kaldıraç) ---
    DEFAULT_BANKROLL_USD: float = float(os.getenv("DEFAULT_BANKROLL_USD", "100.0"))
    DEFAULT_LEVERAGE: int = int(os.getenv("DEFAULT_LEVERAGE", "10"))

    # --- Bybit V5 Demo Trading Otomatik Al-Sat Ayarları (%100 Ücretsiz Sanal Bakiye) ---
    BYBIT_DEMO_API_KEY: str = os.getenv("BYBIT_DEMO_API_KEY", "").strip()
    BYBIT_DEMO_API_SECRET: str = os.getenv("BYBIT_DEMO_API_SECRET", "").strip()
    BYBIT_DEMO_BASE_URL: str = os.getenv("BYBIT_DEMO_BASE_URL", "https://api-demo.bybit.com").strip()
    DEMO_AUTO_TRADE_ENABLED: bool = os.getenv("DEMO_AUTO_TRADE_ENABLED", "true").lower() in ("1", "true", "yes", "on")
    MAX_CONCURRENT_DEMO_TRADES: int = int(os.getenv("MAX_CONCURRENT_DEMO_TRADES", "3"))

    # --- Videodaki 6 Özel Fibonacci Seviyesi (Video 1 [03:52] & Video 2 [06:01]) ---
    FIB_LEVEL_1: float = 1.000       # İtkisel hareketin başlangıcı (Stop-Loss referansı)
    FIB_LEVEL_0618: float = 0.618    # Altın Cep (Golden Pocket) - Ana Limit Emir Giriş Seviyesi
    FIB_LEVEL_0500: float = 0.500    # Altın Bölge (Golden Zone) Sınırı
    FIB_LEVEL_0: float = 0.000       # İtkisel hareketin sonu - Ana Güvenli Take-Profit (Önceki Tepe/Dip)
    FIB_LEVEL_NEG_05: float = -0.500 # 1. Uzatma (Extension) - Trailing SL hedefi
    FIB_LEVEL_NEG_10: float = -1.000 # 2. Uzatma (Extension) - Trailing SL hedefi

    # --- Stop-Loss Buffer ("just above/below the 1 level" - Video 1 [04:38, 05:45]) ---
    # 0.618 giriş, 0.0 TP ve 1.03 SL ile tam 0.618 / (1.03 - 0.618) = 1.50 RRR verir!
    SL_BUFFER_RATIO: float = float(os.getenv("SL_BUFFER_RATIO", "0.03"))

    # --- Geri Çekilme & Aşama Eşikleri (Video 1 [03:35]) ---
    # "watch this move play out until the price starts retracing heavily"
    MIN_RETRACEMENT_STAGE1: float = float(os.getenv("MIN_RETRACEMENT_STAGE1", "0.20"))  # Aşama 1 bildirimi için min %20 geri çekilme
    GOLDEN_ZONE_MIN: float = 0.500   # Aşama 2 Altın Bölge başlangıcı
    GOLDEN_ZONE_MAX: float = 0.786   # 0.618 etrafındaki aktif giriş bandı üst sınırı (1.0 SL'ye kadar geçerli)

    # --- Fibonacci'nin Başarısız Olduğu 3 Senaryo İçin Zorunlu Filtreler (Video 1 [01:43-02:46]) ---
    # 1) Yatay / Testere Piyasa (Choppy Range) Filtresi:
    ATR_PERIOD: int = 20
    MIN_IMPULSE_ATR_MULT: float = float(os.getenv("MIN_IMPULSE_ATR_MULT", "1.60"))  # İtkisel dalga boyu >= 1.6x ATR olmalı
    MIN_IMPULSE_EFFICIENCY: float = float(os.getenv("MIN_IMPULSE_EFFICIENCY", "0.38"))  # Net mesafe / toplam mum boyları >= 0.38

    # 2) Haber / Anormal Devasa Mum (Monster Spike Candle) Filtresi (Video 1 [02:15, 05:03]):
    MAX_SINGLE_CANDLE_ATR_MULT: float = float(os.getenv("MAX_SINGLE_CANDLE_ATR_MULT", "4.0"))  # Tek mum > 4x ATR ise haber/manipülasyon mumu say ve ele

    # 3) Düşük Hacim (Low Volume) Filtresi (Video 1 [02:25]):
    VOLUME_SMA_PERIOD: int = 20
    MIN_IMPULSE_MAX_RVOL: float = float(os.getenv("MIN_IMPULSE_MAX_RVOL", "1.05"))  # İtkisel dalgadaki en hacimli mum >= 1.05x ort. hacim
    MIN_IMPULSE_AVG_RVOL: float = float(os.getenv("MIN_IMPULSE_AVG_RVOL", "0.85"))  # Veya dalga ortalaması >= 0.85x

    # --- Bulut (Render / Cron) HTTP Sunucu Ayarı ---
    PORT: int = int(os.getenv("PORT", "8080"))

    @classmethod
    def reload(cls) -> None:
        load_env_file()
        cls.TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        cls.TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
        cls.BYBIT_DEMO_API_KEY = os.getenv("BYBIT_DEMO_API_KEY", "").strip()
        cls.BYBIT_DEMO_API_SECRET = os.getenv("BYBIT_DEMO_API_SECRET", "").strip()
