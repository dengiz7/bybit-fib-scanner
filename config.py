# -*- coding: utf-8 -*-
"""
Hacim Patlaması ve Zirve Kırılımı (RVOL Breakout Sniper) Konfigürasyonu
"""

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
ENV_PATH = BASE_DIR / ".env"
STATE_PATH = BASE_DIR / "state.json"


def load_env_file(env_path: Path = ENV_PATH) -> None:
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

    # --- Borsa ve Tarama Ayarları (Bybit USDT Linear Perpetual) ---
    TIMEFRAME: str = os.getenv("TIMEFRAME", "15")  # 15 dakikalık mumlar (Hızlı hacim kırılımları)
    TOP_SYMBOLS_COUNT: int = int(os.getenv("TOP_SYMBOLS_COUNT", "40"))  # En yüksek hacimli ilk 40 ana coin (Gereksiz çöp altcoinleri eler!)
    MIN_24H_TURNOVER_USD: float = float(os.getenv("MIN_24H_TURNOVER_USD", "25000000"))  # Min 25M$ 24s hacim (Likidite şartı)
    KLINE_LIMIT: int = 200  # Çekilecek mum sayısı
    SCAN_INTERVAL_SECONDS: int = int(os.getenv("SCAN_INTERVAL_SECONDS", "30"))  # Tarama döngü süresi (sn)

    # --- Hacim Patlaması & Kırılım (Breakout Sniper) Parametreleri ---
    BREAKOUT_LOOKBACK: int = int(os.getenv("BREAKOUT_LOOKBACK", "16"))  # Son 16 mumun (4 saat) tavanı / tabanı (Daha çevik tespit)
    MIN_RVOL: float = float(os.getenv("MIN_RVOL", "1.80"))  # Kırılım mumu en az 1.8x ortalama hacimde olmalı
    MIN_BODY_RATIO: float = float(os.getenv("MIN_BODY_RATIO", "0.45"))  # Kırılım mumunun gövdesi mum boyunun en az %45'i olmalı
    MIN_TP1_MOVE_PCT: float = float(os.getenv("MIN_TP1_MOVE_PCT", "1.40"))  # Komisyonu ezen min %1.40 saf fiyat hareketi
    TP1_ATR_MULT: float = float(os.getenv("TP1_ATR_MULT", "1.60"))  # TP1: 1.6x ATR
    TP2_ATR_MULT: float = float(os.getenv("TP2_ATR_MULT", "2.80"))  # TP2: 2.8x ATR
    SL_ATR_MULT: float = float(os.getenv("SL_ATR_MULT", "1.15"))   # SL: Kırılan seviyenin 1.15x ATR arkası (Dinamik piyasa koşullarına göre)
    TP1_CLOSE_PCT: float = float(os.getenv("TP1_CLOSE_PCT", "1.00"))  # TP1'de pozisyonun %100'ünü kapatıp tam kârı al (Sniper Modu)!

    # --- Akıllı Risk Filtresi & Erken Çıkış Parametreleri ---
    MAX_BREAKOUT_ATR_DIST: float = float(os.getenv("MAX_BREAKOUT_ATR_DIST", "0.65"))  # Kırılım seviyesinden max 0.65x ATR uzaklaşma (Tepeden alımı engeller, geç sinyalleri eler!)
    MAX_CHASE_ATR_RATIO: float = float(os.getenv("MAX_CHASE_ATR_RATIO", "0.65"))  # Kırılım seviyesinden max 0.65x ATR kovalamaca sınırı
    MAX_REJECTION_WICK_RATIO: float = float(os.getenv("MAX_REJECTION_WICK_RATIO", "0.32"))  # Kırılım mumunda ters yöndeki fitil max %32 (Tepeden ret yemiş fitilleri reddet)
    STAGNATION_MAX_CANDLES: int = int(os.getenv("STAGNATION_MAX_CANDLES", "10"))  # 10 mum (2.5 saat) boyunca girişte oyalanırsa başabaş kapat
    STAGNATION_THRESHOLD_PCT: float = float(os.getenv("STAGNATION_THRESHOLD_PCT", "0.40"))  # Girişin %0.4 civarındaysa oyalanma say
    NEAR_TP_TRIGGER_PCT: float = float(os.getenv("NEAR_TP_TRIGGER_PCT", "0.85"))  # TP1'e %85 yaklaşınca dönüş fitilinde erken kâr kilidi

    # --- Kasa Yönetimi, Sabit Dolar Riski ve Pozisyon Büyüklüğü ---
    DEFAULT_BANKROLL_USD: float = float(os.getenv("DEFAULT_BANKROLL_USD", "100.0"))
    DEFAULT_LEVERAGE: int = int(os.getenv("DEFAULT_LEVERAGE", "30"))
    MARGIN_PER_TRADE_PCT: float = float(os.getenv("MARGIN_PER_TRADE_PCT", "10.0"))  # Her işleme sabit kasanın %10'u marjinle gir
    MAX_RISK_USD: float = float(os.getenv("MAX_RISK_USD", "4.00"))  # Sabit Dolar Riski: Stop olursa kasadan MAX 4.00$ gidebilir!
    MAX_CONCURRENT_DEMO_TRADES: int = int(os.getenv("MAX_CONCURRENT_DEMO_TRADES", "0"))  # 0 = Sınırsız (Bakiye el verdikçe fırsatlara girer)
    COOLDOWN_AFTER_STOPS: int = int(os.getenv("COOLDOWN_AFTER_STOPS", "0"))  # 0 = Devre dışı (bekleme molası yok, fırsat gördükçe girer)
    COOLDOWN_MINUTES: int = int(os.getenv("COOLDOWN_MINUTES", "0"))  # Mola süresi (0 dakika)

    # --- Bybit V5 Demo Trading Otomatik Al-Sat Ayarları ---
    BYBIT_DEMO_API_KEY: str = os.getenv("BYBIT_DEMO_API_KEY", "").strip()
    BYBIT_DEMO_API_SECRET: str = os.getenv("BYBIT_DEMO_API_SECRET", "").strip()
    BYBIT_DEMO_BASE_URL: str = os.getenv("BYBIT_DEMO_BASE_URL", "https://api-demo.bybit.com").strip()
    DEMO_AUTO_TRADE_ENABLED: bool = os.getenv("DEMO_AUTO_TRADE_ENABLED", "true").lower() in ("1", "true", "yes", "on")

    # --- Bulut (Render / Cron) HTTP Sunucu Ayarı ---
    PORT: int = int(os.getenv("PORT", "8080"))

    @classmethod
    def reload(cls) -> None:
        load_env_file()
        cls.TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        cls.TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
        cls.BYBIT_DEMO_API_KEY = os.getenv("BYBIT_DEMO_API_KEY", "").strip()
        cls.BYBIT_DEMO_API_SECRET = os.getenv("BYBIT_DEMO_API_SECRET", "").strip()
