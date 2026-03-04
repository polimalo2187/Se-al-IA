# app/scanner.py

import os
import time
import logging
import asyncio
from datetime import datetime, timedelta
from typing import List, Dict, Optional, Tuple

import requests
import pandas as pd
from telegram import Bot

from app.strategy import mtf_strategy
from app.signals import create_base_signal, telegram_signal_blocked
from app.plans import PLAN_FREE, PLAN_PLUS, PLAN_PREMIUM
from app.notifier import notify_new_signal_alert
from app.database import signals_collection

logger = logging.getLogger(__name__)

# ======================================================
# CONFIG
# ======================================================
BINANCE_FUTURES_API = "https://fapi.binance.com"

SCAN_INTERVAL_SECONDS = 300  # 5 minutos
MIN_QUOTE_VOLUME = int(os.getenv("MIN_QUOTE_VOLUME", "50000000"))  # 50M USDT
DEDUP_MINUTES = int(os.getenv("DEDUP_MINUTES", "10"))
REQUEST_DELAY = float(os.getenv("REQUEST_DELAY", "0.2"))
REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "15"))

# Precio de entrada actual
# Valores: "MARK" | "LAST"
ENTRY_PRICE_SOURCE = os.getenv("ENTRY_PRICE_SOURCE", "MARK").upper()

# =========================
# BTC FILTERS CONFIG
# =========================

# 1) BTC Trend (1H) => EMA200 + ADX
BTC_TREND_EMA = 200
BTC_TREND_ADX_PERIOD = 14
BTC_TREND_ADX_MIN = float(os.getenv("BTC_TREND_ADX_MIN", "18"))  # <18: lateral

# 2) BTC Momentum / Spike filter (15m)
BTC_MOM_INTERVAL = os.getenv("BTC_MOM_INTERVAL", "15m")  # recomendado 15m
BTC_MOM_ATR_PERIOD = 14
BTC_MOM_ROC_BARS = int(os.getenv("BTC_MOM_ROC_BARS", "3"))  # 3 velas de 15m = 45m
BTC_MOM_ROC_ABS_MAX = float(os.getenv("BTC_MOM_ROC_ABS_MAX", "0.012"))  # 1.2% en 45m (ajusta)
BTC_MOM_SPIKE_ATR_MULT = float(os.getenv("BTC_MOM_SPIKE_ATR_MULT", "3.0"))  # vela > 3*ATR => latigazo

# Si True, cuando BTC está en spike bloquea SOLO ALTs (recomendado)
BTC_MOM_BLOCK_ALTS_ONLY = os.getenv("BTC_MOM_BLOCK_ALTS_ONLY", "1") == "1"

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
# DATA FETCH (SOLO VELAS CERRADAS, SIN CRASH)
# ======================================================
def get_klines(symbol: str, interval: str, limit: int = 210) -> pd.DataFrame:
    """
    Binance devuelve la última vela en FORMACIÓN.
    Solución robusta: SIEMPRE quitamos la última fila (df.iloc[:-1]).
    """
    rate_limiter.wait()
    url = f"{BINANCE_FUTURES_API}/fapi/v1/klines"
    params = {"symbol": symbol, "interval": interval, "limit": limit}

    r = requests.get(url, params=params, timeout=REQUEST_TIMEOUT)
    r.raise_for_status()
    data = r.json()

    if not data or len(data) < 3:
        raise ValueError(f"Klines insuficientes para {symbol} {interval}")

    df = pd.DataFrame(
        data,
        columns=[
            "open_time", "open", "high", "low", "close", "volume",
            "close_time", "quote_volume", "trades",
            "taker_buy_base", "taker_buy_quote", "ignore"
        ],
    )

    df[["open", "high", "low", "close", "volume"]] = df[
        ["open", "high", "low", "close", "volume"]
    ].astype(float)

    # ✅ ELIMINAR VELA EN FORMACIÓN (última fila)
    df = df.iloc[:-1].copy()

    if df.empty:
        raise ValueError(f"DF vacío tras cortar vela en formación: {symbol} {interval}")

    return df[["open", "high", "low", "close", "volume"]]

# ======================================================
# PRECIO ACTUAL (Futures)
# ======================================================
def get_current_price(symbol: str) -> float:
    """
    Retorna el precio actual para usar como entry_price.
    Por defecto usa MARK PRICE (más estable en Futures).
    Fallback automático a LAST PRICE si falla el endpoint.
    """
    # MARK PRICE
    if ENTRY_PRICE_SOURCE == "MARK":
        try:
            rate_limiter.wait()
            url = f"{BINANCE_FUTURES_API}/fapi/v1/premiumIndex"
            r = requests.get(url, params={"symbol": symbol}, timeout=REQUEST_TIMEOUT)
            r.raise_for_status()
            data = r.json()
            mp = float(data["markPrice"])
            if mp > 0:
                return mp
        except Exception as e:
            logger.debug(f"Mark price falló para {symbol}: {e}")

    # LAST PRICE
    try:
        rate_limiter.wait()
        url = f"{BINANCE_FUTURES_API}/fapi/v1/ticker/price"
        r = requests.get(url, params={"symbol": symbol}, timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
        data = r.json()
        lp = float(data["price"])
        if lp > 0:
            return lp
    except Exception as e:
        logger.debug(f"Last price falló para {symbol}: {e}")

    raise ValueError(f"No se pudo obtener precio actual para {symbol}")

# ======================================================
# BTC FILTERS
# ======================================================
def get_btc_trend_direction() -> Optional[str]:
    """
    BTC trend filter (1H):
      - Si ADX < BTC_TREND_ADX_MIN => lateral => None
      - Si close > EMA200 => LONG
      - Si close < EMA200 => SHORT
    """
    try:
        import ta

        df = get_klines("BTCUSDT", "1h", 260)

        df["ema200"] = ta.trend.ema_indicator(df["close"], BTC_TREND_EMA)
        df["adx"] = ta.trend.adx(df["high"], df["low"], df["close"], BTC_TREND_ADX_PERIOD)

        last = df.iloc[-1]
        adx = float(last["adx"]) if pd.notna(last["adx"]) else 0.0

        if adx < BTC_TREND_ADX_MIN:
            return None

        if float(last["close"]) > float(last["ema200"]):
            return "LONG"
        if float(last["close"]) < float(last["ema200"]):
            return "SHORT"

        return None
    except Exception as e:
        logger.warning(f"⚠️ BTC trend filter error: {e}")
        return None


def btc_is_in_momentum_spike() -> bool:
    """
    BTC momentum/spike filter (15m por defecto):
    - Si ROC abs en N velas supera umbral => spike
    - O si la última vela tiene rango > BTC_MOM_SPIKE_ATR_MULT * ATR => spike
    """
    try:
        import ta

        df = get_klines("BTCUSDT", BTC_MOM_INTERVAL, 260)

        # ATR
        df["atr"] = ta.volatility.average_true_range(
            high=df["high"], low=df["low"], close=df["close"], window=BTC_MOM_ATR_PERIOD
        )

        if len(df) < (BTC_MOM_ROC_BARS + 2):
            return False

        last = df.iloc[-1]
        prev = df.iloc[-(BTC_MOM_ROC_BARS + 1)]

        # ROC (aprox) en N velas
        roc = (float(last["close"]) - float(prev["close"])) / float(prev["close"])
        if abs(roc) >= BTC_MOM_ROC_ABS_MAX:
            return True

        # Spike por rango vs ATR
        atr = float(last["atr"]) if pd.notna(last["atr"]) else 0.0
        if atr > 0:
            rng = float(last["high"]) - float(last["low"])
            if rng >= (BTC_MOM_SPIKE_ATR_MULT * atr):
                return True

        return False
    except Exception as e:
        logger.warning(f"⚠️ BTC momentum filter error: {e}")
        return False

# ======================================================
# SYMBOLS
# ======================================================
def get_active_futures_symbols() -> List[str]:
    rate_limiter.wait()
    url = f"{BINANCE_FUTURES_API}/fapi/v1/ticker/24hr"
    r = requests.get(url, timeout=REQUEST_TIMEOUT)
    r.raise_for_status()

    symbols = [
        item["symbol"]
        for item in r.json()
        if item.get("symbol", "").endswith("USDT")
        and float(item.get("quoteVolume", 0.0)) >= MIN_QUOTE_VOLUME
    ]

    logger.info(f"📊 {len(symbols)} símbolos activos con volumen suficiente")
    return symbols

# ======================================================
# DEDUP
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
# SCANNER
# ======================================================
async def scan_market_async(bot: Bot):
    logger.info("📡 Scanner iniciado — señales SOLO por calidad")

    while True:
        try:
            if telegram_signal_blocked():
                logger.info("⏳ Señales aún vigentes en Telegram. Escaneo pausado.")
                await asyncio.sleep(SCAN_INTERVAL_SECONDS)
                continue

            # ======================================================
            # BTC CONTEXTO (una vez por ciclo)
            # ======================================================
            btc_trend = get_btc_trend_direction()          # "LONG" | "SHORT" | None(lateral)
            btc_spike = btc_is_in_momentum_spike()         # True = latigazo / momentum extremo

            if btc_spike:
                logger.info("⚠️ BTC momentum/spike detectado: filtro activo (señales ALTs bloqueadas este ciclo)")

            if btc_trend:
                logger.info(f"🧭 BTC trend: {btc_trend}")
            else:
                logger.info("🧭 BTC trend: LATERAL/NEUTRAL (no se filtra por dirección)")

            symbols = get_active_futures_symbols()
            candidates: List[Dict] = []

            for symbol in symbols:
                try:
                    df_1h = get_klines(symbol, "1h")
                    df_15m = get_klines(symbol, "15m")
                    df_5m = get_klines(symbol, "5m")

                    result = mtf_strategy(df_1h, df_15m, df_5m)
                    if not result:
                        await asyncio.sleep(0.05)
                        continue

                    direction = result["direction"]

                    # ======================================================
                    # FILTRO BTC TREND (solo ALTs)
                    # - si btc_trend es LONG => alts solo LONG
                    # - si btc_trend es SHORT => alts solo SHORT
                    # - si btc_trend None => no filtramos por dirección
                    # ======================================================
                    is_btc = (symbol == "BTCUSDT")
                    if (not is_btc) and btc_trend and direction != btc_trend:
                        await asyncio.sleep(0.05)
                        continue

                    # ======================================================
                    # FILTRO BTC MOMENTUM/SPIKE (solo ALTs por defecto)
                    # ======================================================
                    if btc_spike:
                        if BTC_MOM_BLOCK_ALTS_ONLY and (not is_btc):
                            await asyncio.sleep(0.05)
                            continue
                        # Si quieres bloquear todo (incluyendo BTC), cambia BTC_MOM_BLOCK_ALTS_ONLY=0

                    result["symbol"] = symbol
                    candidates.append(result)

                    await asyncio.sleep(0.05)

                except Exception as e:
                    logger.debug(f"⚠️ Error procesando {symbol}: {e}")

            if len(candidates) < 3:
                logger.info("📭 No hay oportunidades fuertes en este ciclo")
                await asyncio.sleep(SCAN_INTERVAL_SECONDS)
                continue

            # Ranking TOP 3
            candidates.sort(key=lambda x: x.get("score", 0), reverse=True)
            top_3 = candidates[:3]

            plan_map = [
                (PLAN_PREMIUM, "🥇 ORO"),
                (PLAN_PLUS, "🥈 PLATA"),
                (PLAN_FREE, "🥉 BRONCE"),
            ]

            for idx, signal in enumerate(top_3):
                visibility, medal = plan_map[idx]

                symbol = signal["symbol"]
                direction = signal["direction"]
                score = signal.get("score", 0)

                if recent_duplicate_exists(symbol, direction, visibility):
                    continue

                # ✅ ENTRY PRICE = PRECIO ACTUAL DEL EXCHANGE
                try:
                    entry_price = float(get_current_price(symbol))
                except Exception as e:
                    logger.warning(f"⚠️ No pude obtener precio actual para {symbol}, uso close del setup: {e}")
                    entry_price = float(signal["entry_price"])

                # SL/TP basados en el entry_price real (actual)
                if direction == "LONG":
                    stop_loss = round(entry_price * 0.99, 4)
                    take_profits = [round(entry_price * 1.01, 4), round(entry_price * 1.02, 4)]
                else:
                    stop_loss = round(entry_price * 1.01, 4)
                    take_profits = [round(entry_price * 0.99, 4), round(entry_price * 0.98, 4)]

                base_signal = create_base_signal(
                    symbol=symbol,
                    direction=direction,
                    entry_price=entry_price,
                    stop_loss=stop_loss,
                    take_profits=take_profits,
                    timeframes=["5M", "15M", "1H"],
                    visibility=visibility,
                    score=score,
                    components=signal.get("components", []),
                )

                try:
                    await notify_new_signal_alert(bot, visibility, base_signal=base_signal)
                    logger.info(
                        f"✅ {medal} | {symbol} {direction} | score={score} | plan={visibility} | entry={entry_price}"
                    )
                except Exception as e:
                    logger.error(f"⚠️ Error notificando señal: {e}")

            await asyncio.sleep(SCAN_INTERVAL_SECONDS)

        except Exception:
            logger.error("❌ Error crítico en scanner", exc_info=True)
            await asyncio.sleep(60)

def scan_market(bot: Bot):
    logger.info("🚀 Iniciando scanner en thread separado")
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(scan_market_async(bot))
