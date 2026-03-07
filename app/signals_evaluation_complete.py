# app/signals.py

import os
import time
import logging
import secrets
import hashlib
import random
from datetime import datetime, timedelta
from typing import List, Dict, Optional

import requests
import pytz

from app.models import new_signal
from app.plans import PLAN_FREE, PLAN_PREMIUM
from app.config import is_admin
from app.database import (
    signals_collection,
    user_signals_collection,
    users_collection,
    signal_results_collection,
)

logger = logging.getLogger(__name__)

# ======================================================
# CONFIGURACIÓN GLOBAL
# ======================================================
BINANCE_FUTURES_API = os.getenv("BINANCE_FUTURES_API", "https://fapi.binance.com")
MAX_SIGNALS_PER_QUERY = int(os.getenv("MAX_SIGNALS_PER_QUERY", "10"))
BINANCE_MAX_RETRIES = int(os.getenv("BINANCE_MAX_RETRIES", "3"))
BINANCE_RETRY_DELAY = float(os.getenv("BINANCE_RETRY_DELAY", "1.0"))
USER_TIMEZONE = os.getenv("USER_TIMEZONE", "America/Havana")

LEVERAGE_PROFILES = {
    "conservador": "5x-10x",
    "moderado": "10x-20x",
    "agresivo": "30x-40x",
}

TIMEFRAME_TO_MINUTES = {
    "5M": 5,
    "15M": 15,
    "1H": 60,
}

DEDUP_MINUTES = int(os.getenv("DEDUP_MINUTES", "10"))
TELEGRAM_SIGNAL_COOLDOWN_MINUTES = 15


# ======================================================
# UTILIDADES
# ======================================================

def calculate_signal_validity(timeframes: List[str]) -> int:
    minutes = [TIMEFRAME_TO_MINUTES.get(tf.upper(), 0) for tf in timeframes]
    return max(minutes) if minutes else 15


def calculate_entry_zone(entry: float, pct: float = 0.0015):
    low = round(entry * (1 - pct), 4)
    high = round(entry * (1 + pct), 4)
    return low, high


def get_current_price(symbol: str) -> float:
    url = f"{BINANCE_FUTURES_API}/fapi/v1/ticker/price"
    for attempt in range(BINANCE_MAX_RETRIES):
        try:
            r = requests.get(url, params={"symbol": symbol}, timeout=10)
            r.raise_for_status()
            return float(r.json()["price"])
        except Exception:
            if attempt == BINANCE_MAX_RETRIES - 1:
                raise
            time.sleep(BINANCE_RETRY_DELAY)


def estimate_minutes_to_entry(symbol: str, entry_zone: Dict[str, float], timeframes: List[str]) -> Dict[str, int]:
    try:
        current_price = get_current_price(symbol)
        zone_mid = (entry_zone["low"] + entry_zone["high"]) / 2

        if entry_zone["low"] <= current_price <= entry_zone["high"]:
            return {"min": 1, "max": 5}

        distance_pct = abs(current_price - zone_mid) / current_price

        if "5M" in timeframes:
            speed = 0.004
            base_tf = 5
        elif "15M" in timeframes:
            speed = 0.0025
            base_tf = 15
        else:
            speed = 0.0015
            base_tf = calculate_signal_validity(timeframes)

        candles_needed = max(1, distance_pct / speed)
        minutes_estimated = candles_needed * base_tf

        return {
            "min": max(1, int(minutes_estimated * 0.6)),
            "max": int(minutes_estimated * 1.4),
        }
    except Exception as e:
        logger.warning(f"Fallback estimate_minutes_to_entry: {e}")
        base = calculate_signal_validity(timeframes)
        return {"min": max(1, int(base * 0.5)), "max": int(base * 1.5)}


def recent_duplicate_exists(symbol: str, direction: str, visibility: str) -> bool:
    since = datetime.utcnow() - timedelta(minutes=DEDUP_MINUTES)
    return signals_collection().find_one({
        "symbol": symbol,
        "direction": direction,
        "visibility": visibility,
        "created_at": {"$gte": since},
    }) is not None


def telegram_signal_blocked(symbol: Optional[str] = None) -> bool:
    since = datetime.utcnow() - timedelta(minutes=TELEGRAM_SIGNAL_COOLDOWN_MINUTES)
    query = {"created_at": {"$gte": since}}
    if symbol:
        query["symbol"] = symbol
    return signals_collection().find_one(query, sort=[("created_at", -1)]) is not None


# ======================================================
# GENERAR SEÑALES POR PLAN
# ======================================================

def generate_user_signal_for_plan(base_signal: Dict):
    visibility = base_signal.get("visibility", PLAN_FREE)

    for user in users_collection().find({}):
        user_id = user.get("user_id")
        user_plan = user.get("plan", PLAN_FREE)
        plan_end = user.get("plan_end")
        admin = is_admin(user_id)

        if plan_end and plan_end < datetime.utcnow():
            continue

        if admin or user_plan == visibility:
            existing = user_signals_collection().find_one({
                "user_id": user_id,
                "symbol": base_signal["symbol"],
                "telegram_valid_until": {"$gt": datetime.utcnow()}
            })
            if existing:
                continue

            generate_user_signal(base_signal, user_id)


# ======================================================
# CREAR SEÑAL BASE
# ======================================================

def create_base_signal(
    symbol: str,
    direction: str,
    entry_price: float,
    stop_loss: float,
    take_profits: List[float],
    timeframes: List[str],
    visibility: str,
    score: Optional[float] = None,
    components: Optional[List[str]] = None
) -> Dict:

    if telegram_signal_blocked(symbol):
        logger.info(f"⏳ Bloqueo activo para {symbol}, no se crea nueva señal")
        return {}

    zone_low, zone_high = calculate_entry_zone(entry_price)
    estimated_minutes = estimate_minutes_to_entry(symbol, {"low": zone_low, "high": zone_high}, timeframes)

    signal = new_signal(
        symbol=symbol,
        direction=direction,
        entry_price=entry_price,
        stop_loss=stop_loss,
        take_profits=take_profits,
        timeframes=timeframes,
        visibility=visibility,
        leverage=LEVERAGE_PROFILES,
        components=components,
        score=score
    )

    now = datetime.utcnow()
    valid_until = now + timedelta(minutes=calculate_signal_validity(timeframes))
    telegram_valid_until = now + timedelta(minutes=TELEGRAM_SIGNAL_COOLDOWN_MINUTES)

    inserted_id = signals_collection().insert_one(signal).inserted_id

    signals_collection().update_one(
        {"_id": inserted_id},
        {"$set": {
            "created_at": now,
            "valid_until": valid_until,
            "telegram_valid_until": telegram_valid_until,
            "entry_zone": {"low": zone_low, "high": zone_high},
            "estimated_entry_minutes": estimated_minutes,
        }}
    )

    signal["_id"] = inserted_id
    signal["created_at"] = now
    signal["valid_until"] = valid_until
    signal["telegram_valid_until"] = telegram_valid_until
    signal["entry_zone"] = {"low": zone_low, "high": zone_high}
    signal["estimated_entry_minutes"] = estimated_minutes

    generate_user_signal_for_plan(signal)
    return signal


# ======================================================
# GENERAR SEÑAL USUARIO (LONG / SHORT CORRECTO)
# ======================================================

def generate_user_signal(base_signal: Dict, user_id: int) -> Dict:
    existing = user_signals_collection().find_one({
        "user_id": user_id,
        "symbol": base_signal["symbol"],
        "telegram_valid_until": {"$gt": datetime.utcnow()}
    })
    if existing:
        return existing

    rnd = random.Random(
        int(hashlib.sha256(f"{base_signal['_id']}_{user_id}".encode()).hexdigest(), 16)
    )

    direction = base_signal["direction"].upper()
    entry = base_signal["entry_price"]

    def vary(val, pct):
        return round(rnd.uniform(val * (1 - pct), val * (1 + pct)), 4)

    if direction == "LONG":
        sl_base = entry * 0.99
        tp1_base = entry * 1.01
        tp2_base = entry * 1.02
    else:
        sl_base = entry * 1.01
        tp1_base = entry * 0.99
        tp2_base = entry * 0.98

    user_signal = {
        "user_id": user_id,
        "signal_id": str(base_signal["_id"]),
        "symbol": base_signal["symbol"],
        "direction": direction,
        "entry_price": vary(entry, 0.0005),
        "entry_zone": dict(zip(["low", "high"], calculate_entry_zone(entry))),
        "profiles": {
            p: {
                "stop_loss": vary(sl_base, 0.001),
                "take_profits": [vary(tp1_base, 0.001), vary(tp2_base, 0.001)]
            }
            for p in LEVERAGE_PROFILES
        },
        "leverage_profiles": LEVERAGE_PROFILES,
        "timeframes": base_signal["timeframes"],
        "created_at": datetime.utcnow(),
        "valid_until": base_signal["valid_until"],
        "telegram_valid_until": base_signal["telegram_valid_until"],
        "fingerprint": secrets.token_hex(4),
        "visibility": base_signal["visibility"],
        "score": base_signal.get("score"),
        "evaluated": False,
    }

    user_signals_collection().insert_one(user_signal)
    return user_signal


# ======================================================
# FORMATO TELEGRAM (EXACTO)
# ======================================================

def format_user_signal(user_signal: Dict) -> str:
    tz = pytz.timezone(USER_TIMEZONE)
    start = user_signal["created_at"].astimezone(tz).strftime("%H:%M")
    end = user_signal["telegram_valid_until"].astimezone(tz).strftime("%H:%M")

    text = (
        f"📊 NUEVA SEÑAL – FUTUROS USDT\n\n"
        f"🏷️ PLAN: {user_signal['visibility'].upper()}\n\n"
        f"Par: {user_signal['symbol']}\n"
        f"Dirección: {user_signal['direction']}\n"
        f"Entrada base: {user_signal['entry_price']}\n\n"
        f"Margen: ISOLATED\n"
        f"Timeframes: {' / '.join(user_signal['timeframes'])}\n\n"
    )

    for profile in ["conservador", "moderado", "agresivo"]:
        p = user_signal["profiles"][profile]
        text += (
            "━━━━━━━━━━━━━━━━━━\n"
            f"{profile.upper()}\n"
            f"SL: {p['stop_loss']}\n"
            f"TP1: {p['take_profits'][0]}\n"
            f"TP2: {p['take_profits'][1]}\n"
            f"Apalancamiento: {LEVERAGE_PROFILES[profile]}\n\n"
        )

    text += f"⏳ Activa: {start} → {end}\n"
    text += f"🔐 ID: {user_signal['fingerprint']}\n"
    return text


# ======================================================
# OBTENER SEÑALES USUARIO
# ======================================================

def get_latest_base_signal_for_plan(user_id: int, user_plan: Optional[str] = None):
    visibility = PLAN_PREMIUM if is_admin(user_id) else (user_plan or PLAN_FREE)
    now = datetime.utcnow()

    return list(
        user_signals_collection()
        .find({
            "user_id": user_id,
            "visibility": visibility,
            "telegram_valid_until": {"$gt": now}
        })
        .sort("created_at", -1)
        .limit(MAX_SIGNALS_PER_QUERY)
    )


# ======================================================
# EVALUACIÓN AUTOMÁTICA DE SEÑALES (PERFIL CONSERVADOR)
# ======================================================

def _dt_to_ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def _fetch_klines_between(symbol: str, start_dt: datetime, end_dt: datetime, interval: str = "1m") -> List[List]:
    """
    Descarga velas entre dos fechas para evaluar una señal.
    """
    url = f"{BINANCE_FUTURES_API}/fapi/v1/klines"
    start_ms = _dt_to_ms(start_dt)
    end_ms = _dt_to_ms(end_dt)
    all_rows: List[List] = []

    while start_ms < end_ms:
        r = requests.get(
            url,
            params={
                "symbol": symbol,
                "interval": interval,
                "startTime": start_ms,
                "endTime": end_ms,
                "limit": 1000,
            },
            timeout=15,
        )
        r.raise_for_status()
        rows = r.json()
        if not rows:
            break

        all_rows.extend(rows)
        last_open_ms = int(rows[-1][0])
        next_start = last_open_ms + 60_000
        if next_start <= start_ms:
            break
        start_ms = next_start

        if len(rows) < 1000:
            break

    return all_rows


def _evaluate_signal_result(user_signal: Dict) -> str:
    """
    Reglas:
    - TP1 conservador = WON
    - SL conservador = LOST
    - si no toca ninguno = EXPIRED
    - si en la misma vela toca TP y SL = LOST (regla conservadora)
    """
    direction = str(user_signal.get("direction", "")).upper()
    symbol = user_signal.get("symbol")

    profiles = user_signal.get("profiles", {})
    conservador = profiles.get("conservador", {})
    stop_loss = conservador.get("stop_loss")
    take_profits = conservador.get("take_profits", [])
    tp1 = take_profits[0] if take_profits else None

    created_at = user_signal.get("created_at")
    valid_until = user_signal.get("valid_until")

    if not symbol or not direction or stop_loss is None or tp1 is None or not created_at or not valid_until:
        return "expired"

    try:
        stop_loss = float(stop_loss)
        tp1 = float(tp1)
    except Exception:
        return "expired"

    try:
        klines = _fetch_klines_between(symbol, created_at, valid_until, interval="1m")
    except Exception as e:
        logger.error(f"❌ Error descargando velas para evaluar {symbol}: {e}")
        return "expired"

    for row in klines:
        try:
            high = float(row[2])
            low = float(row[3])
        except Exception:
            continue

        if direction == "LONG":
            if low <= stop_loss and high >= tp1:
                return "lost"
            if high >= tp1:
                return "won"
            if low <= stop_loss:
                return "lost"

        elif direction == "SHORT":
            if high >= stop_loss and low <= tp1:
                return "lost"
            if low <= tp1:
                return "won"
            if high >= stop_loss:
                return "lost"

    return "expired"


def evaluate_expired_signals(limit: int = 100) -> int:
    """
    Evalúa señales expiradas pendientes y registra el resultado en signal_results.
    Retorna cuántas señales fueron evaluadas.
    """
    now = datetime.utcnow()
    pending = list(
        user_signals_collection()
        .find({
            "valid_until": {"$lte": now},
            "evaluated": {"$ne": True},
        })
        .sort("valid_until", 1)
        .limit(limit)
    )

    processed = 0

    for s in pending:
        try:
            result = _evaluate_signal_result(s)
            evaluated_at = datetime.utcnow()

            result_doc = {
                "user_signal_id": str(s.get("_id")),
                "signal_id": s.get("signal_id"),
                "user_id": s.get("user_id"),
                "symbol": s.get("symbol"),
                "direction": s.get("direction"),
                "visibility": s.get("visibility"),
                "plan": s.get("visibility"),
                "score": s.get("score"),
                "result": result,
                "evaluated_at": evaluated_at,
                "evaluated_profile": "conservador",
                "tp_used": s.get("profiles", {}).get("conservador", {}).get("take_profits", [None])[0],
                "sl_used": s.get("profiles", {}).get("conservador", {}).get("stop_loss"),
                "signal_created_at": s.get("created_at"),
                "signal_valid_until": s.get("valid_until"),
            }

            signal_results_collection().insert_one(result_doc)

            user_signals_collection().update_one(
                {"_id": s["_id"]},
                {
                    "$set": {
                        "evaluated": True,
                        "result": result,
                        "evaluated_at": evaluated_at,
                        "evaluated_profile": "conservador",
                    }
                }
            )

            processed += 1

        except Exception as e:
            logger.error(f"❌ Error evaluando señal {s.get('symbol')}: {e}", exc_info=True)

    if processed:
        logger.info(f"✅ Señales evaluadas automáticamente: {processed}")

    return processed
