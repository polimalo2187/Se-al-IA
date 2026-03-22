
# app/scanner.py

import os
import time
import logging
import asyncio
from datetime import datetime, timedelta
from typing import List, Dict, Optional

import requests
import pandas as pd
from telegram import Bot

from app.strategy import mtf_strategy
from app.signals import create_base_signal, telegram_signal_blocked
from app.plans import PLAN_FREE, PLAN_PLUS, PLAN_PREMIUM
from app.notifier import notify_new_signal_alert
from app.database import signals_collection

logger = logging.getLogger(__name__)

BINANCE_FUTURES_API = "https://fapi.binance.com"

SCAN_INTERVAL_SECONDS = int(os.getenv("SCAN_INTERVAL_SECONDS", "60"))
MIN_QUOTE_VOLUME = int(os.getenv("MIN_QUOTE_VOLUME", "20000000"))
DEDUP_MINUTES = int(os.getenv("DEDUP_MINUTES", "10"))
REQUEST_DELAY = float(os.getenv("REQUEST_DELAY", "0.2"))
REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "15"))

# Thresholds sobre raw_score real, no score inflado por plan.
PREMIUM_RAW_SCORE_MIN = float(os.getenv("PREMIUM_RAW_SCORE_MIN", "78"))
PLUS_RAW_SCORE_MIN = float(os.getenv("PLUS_RAW_SCORE_MIN", "72"))
FREE_RAW_SCORE_MIN = float(os.getenv("FREE_RAW_SCORE_MIN", "64"))

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

def _qualifies_for_premium(signal: Dict) -> bool:
    return (
        str(signal.get("setup_group")) == "shared"
        and float(signal.get("raw_score", signal.get("score", 0))) >= PREMIUM_RAW_SCORE_MIN
    )

def _qualifies_for_plus(signal: Dict) -> bool:
    return (
        str(signal.get("setup_group")) == "shared"
        and float(signal.get("raw_score", signal.get("score", 0))) >= PLUS_RAW_SCORE_MIN
    )

def _qualifies_for_free(signal: Dict) -> bool:
    return float(signal.get("raw_score", signal.get("score", 0))) >= FREE_RAW_SCORE_MIN

def _pick_best(
    pool: List[Dict],
    predicate,
    used_symbols: set[str],
) -> Optional[Dict]:
    for signal in pool:
        symbol = signal["symbol"]
        if symbol in used_symbols:
            continue
        if predicate(signal):
            used_symbols.add(symbol)
            return signal
    return None

async def scan_market_async(bot: Bot):
    logger.info("📡 Scanner iniciado — raw_score real + clasificación por plan")

    while True:
        try:
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
                        raw_score = float(result.get("raw_score", result.get("score", 0)))

                        # Bonuses suaves para ranking operativo, sin reetiquetar el score real.
                        entry_quality = _entry_quality(df_5m, direction)
                        volume_quality = _volume_quality(df_5m)

                        final_score = round(
                            raw_score
                            + (entry_quality * 0.35)
                            + (volume_quality * 0.40),
                            2,
                        )

                        result["symbol"] = symbol
                        result["raw_score"] = raw_score
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

            candidates.sort(
                key=lambda x: (
                    x.get("final_score", x.get("raw_score", x.get("score", 0))),
                    x.get("raw_score", x.get("score", 0)),
                    x.get("entry_quality", 0),
                    x.get("volume_quality", 0),
                    x["symbol"],
                ),
                reverse=True,
            )

            used_symbols: set[str] = set()

            premium_signal = _pick_best(candidates, _qualifies_for_premium, used_symbols)
            plus_signal = _pick_best(candidates, _qualifies_for_plus, used_symbols)
            free_signal = _pick_best(candidates, _qualifies_for_free, used_symbols)

            selected = [
                (PLAN_PREMIUM, "🥇 ORO", premium_signal),
                (PLAN_PLUS, "🥈 PLATA", plus_signal),
                (PLAN_FREE, "🥉 BRONCE", free_signal),
            ]

            for visibility, medal, signal in selected:
                if not signal:
                    continue

                symbol = signal["symbol"]
                direction = signal["direction"]
                entry_price = float(signal["entry_price"])
                raw_score = float(signal.get("raw_score", signal.get("score", 0)))
                final_score = float(signal.get("final_score", raw_score))

                if recent_duplicate_exists(symbol, direction, visibility):
                    continue

                base_signal = create_base_signal(
                    symbol=symbol,
                    direction=direction,
                    entry_price=entry_price,
                    stop_loss=float(signal["stop_loss"]),
                    take_profits=list(signal["take_profits"]),
                    timeframes=list(signal.get("timeframes", ["5M"])),
                    visibility=visibility,
                    score=raw_score,
                    components=signal.get("components", []),
                    profiles=signal.get("profiles"),
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
                    "✅ %s | %s %s | raw_score=%s | final_score=%s | entry_q=%s | vol_q=%s | setup=%s | plan=%s",
                    medal,
                    symbol,
                    direction,
                    raw_score,
                    final_score,
                    signal.get("entry_quality", 0),
                    signal.get("volume_quality", 0),
                    signal.get("setup_group", "unknown"),
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
