# app/scanner.py

import os
import time
import logging
import asyncio
from datetime import datetime, timedelta
from typing import List, Dict

import requests
import pandas as pd
from telegram import Bot

from app.strategy import mtf_strategy
from app.signals import create_base_signal, telegram_signal_blocked
from app.plans import PLAN_FREE, PLAN_PLUS, PLAN_PREMIUM
from app.notifier import notify_new_signal_alert
from app.database import signals_collection

# ======================================================
# LOGGING
# ======================================================
logger = logging.getLogger(__name__)

# ======================================================
# CONFIGURACIÓN GENERAL
# ======================================================
BINANCE_FUTURES_API = "https://fapi.binance.com"

SCAN_INTERVAL_SECONDS = int(os.getenv("SCAN_INTERVAL_SECONDS", "300"))  # 5 minutos
MIN_QUOTE_VOLUME = int(os.getenv("MIN_QUOTE_VOLUME", "20000000"))  # 20M USDT
DEDUP_MINUTES = int(os.getenv("DEDUP_MINUTES", "10"))
REQUEST_DELAY = float(os.getenv("REQUEST_DELAY", "0.2"))
REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "15"))

# ======================================================
# RATE LIMITER
# ======================================================
class RateLimiter:
    def __init__(self, delay: float):
        self.delay = delay
        self.last_request = 0.0

    def wait(self):
        elapsed = time.time() - self.last_request
        if elapsed < self.delay:
            time.sleep(self.delay - elapsed)
        self.last_request = time.time()

rate_limiter = RateLimiter(REQUEST_DELAY)

# ======================================================
# DATA FETCH
# ======================================================
def get_klines(symbol: str, interval: str, limit: int = 220) -> pd.DataFrame:
    rate_limiter.wait()
    url = f"{BINANCE_FUTURES_API}/fapi/v1/klines"
    params = {"symbol": symbol, "interval": interval, "limit": limit}
    response = requests.get(url, params=params, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    df = pd.DataFrame(
        response.json(),
        columns=[
            "open_time", "open", "high", "low", "close", "volume",
            "close_time", "quote_volume", "trades",
            "taker_buy_base", "taker_buy_quote", "ignore"
        ],
    )
    return df[["open", "high", "low", "close", "volume"]].astype(float)

def get_active_futures_symbols() -> List[str]:
    rate_limiter.wait()
    url = f"{BINANCE_FUTURES_API}/fapi/v1/ticker/24hr"
    response = requests.get(url, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    symbols = [
        item["symbol"]
        for item in response.json()
        if item["symbol"].endswith("USDT")
        and float(item["quoteVolume"]) >= MIN_QUOTE_VOLUME
    ]
    logger.info(f"📊 {len(symbols)} símbolos activos con volumen suficiente")
    return symbols

# ======================================================
# ANTI DUPLICADOS (BASE SIGNAL)
# ======================================================
def recent_duplicate_exists(symbol: str, direction: str, visibility: str) -> bool:
    since = datetime.utcnow() - timedelta(minutes=DEDUP_MINUTES)
    exists = signals_collection().find_one(
        {
            "symbol": symbol,
            "direction": direction,
            "visibility": visibility,
            "created_at": {"$gte": since},
        }
    ) is not None

    if exists:
        logger.info(f"♻️ Duplicado reciente detectado: {symbol} {direction} ({visibility})")
    return exists

# ======================================================
# RANKING INTELIGENTE
# ======================================================
def _safe_ratio(num: float, den: float) -> float:
    try:
        den = float(den)
        if den == 0:
            return 0.0
        return float(num) / den
    except Exception:
        return 0.0


def _entry_quality(df_5m: pd.DataFrame, direction: str) -> float:
    """
    Bonus suave por entrada menos tardía.
    10 = entrada fresca
    0 = entrada demasiado perseguida
    """
    try:
        last = df_5m.iloc[-1]
        close = float(last["close"])
        high = float(last["high"])
        low = float(last["low"])

        candle_range = max(high - low, 1e-9)

        if direction == "LONG":
            progress = _safe_ratio(close - low, candle_range)
        else:
            progress = _safe_ratio(high - close, candle_range)

        freshness = max(0.0, min(1.0, 1.0 - progress))
        return round(freshness * 10.0, 2)

    except Exception:
        return 0.0


def _volume_quality(df_5m: pd.DataFrame) -> float:
    """
    Bonus suave de volumen entre 0 y 5.
    """
    try:
        last = df_5m.iloc[-1]
        volume = float(last["volume"])
        vol_ma = float(df_5m["volume"].tail(20).mean())

        if vol_ma <= 0:
            return 0.0

        ratio = volume / vol_ma

        if ratio >= 1.8:
            return 5.0
        if ratio >= 1.5:
            return 4.0
        if ratio >= 1.3:
            return 3.0
        if ratio >= 1.15:
            return 2.0
        if ratio >= 1.0:
            return 1.0
        return 0.0

    except Exception:
        return 0.0

# ======================================================
# SCANNER PRINCIPAL (CONTROLADO POR TELEGRAM)
# ======================================================
async def scan_market_async(bot: Bot):
    logger.info("📡 Scanner iniciado — señales SOLO por calidad")

    while True:
        try:
            # 🔒 BLOQUEO GLOBAL POR SEÑALES VIGENTES EN TELEGRAM
            if telegram_signal_blocked():
                logger.info("⏳ Señales aún vigentes en Telegram. Escaneo pausado.")
                await asyncio.sleep(SCAN_INTERVAL_SECONDS)
                continue

            symbols = get_active_futures_symbols()
            candidates: List[Dict] = []

            for symbol in symbols:
                try:
                    df_1h = get_klines(symbol, "1h")
                    df_15m = get_klines(symbol, "15m")
                    df_5m = get_klines(symbol, "5m")

                    result = mtf_strategy(df_1h, df_15m, df_5m)
                    if result:
                        direction = result["direction"]

                        entry_quality = _entry_quality(df_5m, direction)
                        volume_quality = _volume_quality(df_5m)

                        final_score = round(
                            float(result.get("score", 0))
                            + (entry_quality * 0.6)
                            + volume_quality,
                            2,
                        )

                        result["symbol"] = symbol
                        result["entry_quality"] = entry_quality
                        result["volume_quality"] = volume_quality
                        result["final_score"] = final_score

                        candidates.append(result)

                    await asyncio.sleep(0.05)

                except Exception as e:
                    logger.debug(f"⚠️ Error procesando {symbol}: {e}")

            if not candidates:
                logger.info("📭 No hay oportunidades fuertes en este ciclo")
                await asyncio.sleep(SCAN_INTERVAL_SECONDS)
                continue

            # ==================================================
            # RANKING GLOBAL — ENVÍA LO MEJOR DISPONIBLE
            # ==================================================
            candidates.sort(
                key=lambda x: (
                    x.get("final_score", x.get("score", 0)),
                    x.get("entry_quality", 0),
                    x.get("volume_quality", 0),
                    x.get("score", 0),
                ),
                reverse=True,
            )

            top_n = candidates[: min(3, len(candidates))]

            plan_map = [
                (PLAN_PREMIUM, "🥇 ORO"),
                (PLAN_PLUS, "🥈 PLATA"),
                (PLAN_FREE, "🥉 BRONCE"),
            ]

            for idx, signal in enumerate(top_n):
                visibility, medal = plan_map[idx]
                symbol = signal["symbol"]
                direction = signal["direction"]
                entry_price = float(signal["entry_price"])
                score = signal.get("final_score", signal.get("score", 0))

                if recent_duplicate_exists(symbol, direction, visibility):
                    continue

                # Alineado con signals.py actual:
                # TP1 más cerca para medir efectividad y SL un poco más amplio.
                if direction == "LONG":
                    stop_loss = round(entry_price * 0.988, 4)  # -1.2%
                    take_profits = [
                        round(entry_price * 1.01, 4),  # +1.0%
                        round(entry_price * 1.02, 4),  # +2.0%
                    ]
                else:
                    stop_loss = round(entry_price * 1.012, 4)  # +1.2%
                    take_profits = [
                        round(entry_price * 0.99, 4),  # -1.0%
                        round(entry_price * 0.98, 4),  # -2.0%
                    ]

                base_signal = create_base_signal(
                    symbol=symbol,
                    direction=direction,
                    entry_price=entry_price,
                    stop_loss=stop_loss,
                    take_profits=take_profits,
                    timeframes=["5M", "15M", "1H"],
                    visibility=visibility,
                    score=score,
                    components=signal.get("components", [])
                )

                if not base_signal:
                    continue

                try:
                    await notify_new_signal_alert(
                        bot,
                        visibility,
                        base_signal=base_signal
                    )
                except Exception as e:
                    logger.error(f"⚠️ Error notificando señal: {e}")

                logger.info(
                    "✅ %s | %s %s | base_score=%s | final_score=%s | entry_q=%s | vol_q=%s | plan=%s",
                    medal,
                    symbol,
                    direction,
                    signal.get("score", 0),
                    signal.get("final_score", signal.get("score", 0)),
                    signal.get("entry_quality", 0),
                    signal.get("volume_quality", 0),
                    visibility,
                )

            await asyncio.sleep(SCAN_INTERVAL_SECONDS)

        except Exception:
            logger.error("❌ Error crítico en scanner", exc_info=True)
            await asyncio.sleep(60)

def scan_market(bot: Bot):
    logger.info("🚀 Iniciando scanner en thread separado")
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(scan_market_async(bot))
