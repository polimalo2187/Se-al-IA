# app/scanner.py

import os
import time
import logging
import asyncio
from datetime import datetime, timedelta
from typing import List, Dict

import requests
import pandas as pd
import ta
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

# Ranking inteligente
RANKING_ADX_CAP = float(os.getenv("RANKING_ADX_CAP", "35"))
RANKING_ADX_WEIGHT = float(os.getenv("RANKING_ADX_WEIGHT", "0.35"))
RANKING_VOLUME_CAP = float(os.getenv("RANKING_VOLUME_CAP", "2.0"))
RANKING_VOLUME_WEIGHT = float(os.getenv("RANKING_VOLUME_WEIGHT", "8.0"))
RANKING_LATE_ENTRY_WEIGHT = float(os.getenv("RANKING_LATE_ENTRY_WEIGHT", "1200"))

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
# RANKING PROFESIONAL
# ======================================================
def _safe_float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


def _compute_adx_1h(df_1h: pd.DataFrame) -> float:
    try:
        adx_series = ta.trend.adx(
            df_1h["high"],
            df_1h["low"],
            df_1h["close"],
            window=14,
        )
        if adx_series.empty:
            return 0.0
        return _safe_float(adx_series.iloc[-1])
    except Exception:
        return 0.0


def _compute_volume_ratio_5m(df_5m: pd.DataFrame) -> float:
    try:
        vol_ma = _safe_float(df_5m["volume"].tail(20).mean())
        if vol_ma <= 0:
            return 0.0
        return _safe_float(df_5m.iloc[-1]["volume"]) / vol_ma
    except Exception:
        return 0.0


def _compute_late_entry_penalty(df_5m: pd.DataFrame, direction: str) -> float:
    """
    Penaliza entradas demasiado alejadas del punto medio de Bollinger.
    No bloquea la señal: solo la baja en el ranking.
    """
    try:
        bb = ta.volatility.BollingerBands(
            close=df_5m["close"],
            window=20,
            window_dev=2.0,
        )
        bb_mid = _safe_float(bb.bollinger_mavg().iloc[-1])
        close = _safe_float(df_5m.iloc[-1]["close"])

        if bb_mid <= 0:
            return 0.0

        if direction == "LONG":
            distance = max(0.0, (close - bb_mid) / bb_mid)
        else:
            distance = max(0.0, (bb_mid - close) / bb_mid)

        return round(distance * RANKING_LATE_ENTRY_WEIGHT, 2)
    except Exception:
        return 0.0


def _compute_bonus_adx(adx_1h: float) -> float:
    capped = min(max(adx_1h, 0.0), RANKING_ADX_CAP)
    return round(capped * RANKING_ADX_WEIGHT, 2)


def _compute_bonus_volume(volume_ratio_5m: float) -> float:
    capped = min(max(volume_ratio_5m, 0.0), RANKING_VOLUME_CAP)
    return round(capped * RANKING_VOLUME_WEIGHT, 2)


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
                        base_score = _safe_float(result.get("score", 0))
                        adx_1h = _compute_adx_1h(df_1h)
                        volume_ratio_5m = _compute_volume_ratio_5m(df_5m)
                        late_entry_penalty = _compute_late_entry_penalty(df_5m, direction)

                        bonus_adx = _compute_bonus_adx(adx_1h)
                        bonus_volume = _compute_bonus_volume(volume_ratio_5m)

                        final_score = round(
                            base_score
                            + bonus_adx
                            + bonus_volume
                            - late_entry_penalty,
                            2,
                        )

                        result["symbol"] = symbol
                        result["adx_1h"] = adx_1h
                        result["volume_ratio_5m"] = round(volume_ratio_5m, 4)
                        result["bonus_adx"] = bonus_adx
                        result["bonus_volume"] = bonus_volume
                        result["late_entry_penalty"] = late_entry_penalty
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
                    x.get("bonus_adx", 0),
                    x.get("bonus_volume", 0),
                    -x.get("late_entry_penalty", 0),
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
                    "✅ %s | %s %s | base_score=%s | final_score=%s | adx_1h=%s | vol_ratio=%s | late_penalty=%s | plan=%s",
                    medal,
                    symbol,
                    direction,
                    signal.get("score", 0),
                    signal.get("final_score", signal.get("score", 0)),
                    round(signal.get("adx_1h", 0), 2),
                    signal.get("volume_ratio_5m", 0),
                    signal.get("late_entry_penalty", 0),
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
