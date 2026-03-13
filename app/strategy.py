import pandas as pd
from typing import Optional, Dict
import ta

# =========================
# CONFIGURACIÓN
# =========================

EMA_TREND = 200

ADX_PERIOD = 14
ADX_MIN_TREND_1H = 20
ADX_MIN_TREND_15M = 17

BB_PERIOD = 20
BB_STD = 2.0

VOLUME_MULTIPLIER_BREAKOUT = 1.20
VOLUME_MULTIPLIER_PULLBACK = 0.95

MAX_SCORE = 100

# =========================
# INDICADORES
# =========================

def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()

    df["ema_200"] = ta.trend.ema_indicator(df["close"], EMA_TREND)

    bb = ta.volatility.BollingerBands(
        close=df["close"],
        window=BB_PERIOD,
        window_dev=BB_STD,
    )
    df["bb_high"] = bb.bollinger_hband()
    df["bb_low"] = bb.bollinger_lband()
    df["bb_mid"] = bb.bollinger_mavg()

    df["adx"] = ta.trend.adx(
        df["high"], df["low"], df["close"], ADX_PERIOD
    )

    df["vol_ma"] = df["volume"].rolling(20).mean()

    return df

# =========================
# FILTROS DE MERCADO
# =========================

def market_has_strength(df: pd.DataFrame, min_adx: float) -> bool:
    last = df.iloc[-1]
    return last["adx"] >= min_adx

def trend_direction(df: pd.DataFrame) -> Optional[str]:
    last = df.iloc[-1]

    if last["close"] > last["ema_200"]:
        return "LONG"
    elif last["close"] < last["ema_200"]:
        return "SHORT"
    return None

# =========================
# SETUP ROMPIMIENTO
# =========================

def breakout_confirmation(df: pd.DataFrame, direction: str) -> bool:
    last = df.iloc[-1]

    if last["volume"] < last["vol_ma"] * VOLUME_MULTIPLIER_BREAKOUT:
        return False

    if direction == "LONG":
        return last["close"] > last["bb_high"]
    else:
        return last["close"] < last["bb_low"]

# =========================
# SETUP RETROCESO SIMPLE
# =========================

def pullback_confirmation(df: pd.DataFrame, direction: str) -> bool:
    last = df.iloc[-1]

    if last["volume"] < last["vol_ma"] * VOLUME_MULTIPLIER_PULLBACK:
        return False

    if direction == "LONG":
        return (
            last["close"] >= last["bb_mid"]
            and last["close"] > last["ema_200"]
        )
    else:
        return (
            last["close"] <= last["bb_mid"]
            and last["close"] < last["ema_200"]
        )

# =========================
# ESTRATEGIA MTF OPERABLE
# =========================

def mtf_strategy(
    df_1h: pd.DataFrame,
    df_15m: pd.DataFrame,
    df_5m: pd.DataFrame,
) -> Optional[Dict]:

    # El scanner actual trabaja con 220 velas. Aquí exigimos lo mismo para que no haya contradicción.
    if len(df_1h) < 220 or len(df_15m) < 220 or len(df_5m) < 220:
        return None

    df_1h = add_indicators(df_1h)
    df_15m = add_indicators(df_15m)
    df_5m = add_indicators(df_5m)

    score = 0
    components = []

    last_5m = df_5m.iloc[-1]

    # =====================
    # 1H → FILTRO PRINCIPAL
    # =====================

    if not market_has_strength(df_1h, ADX_MIN_TREND_1H):
        return None

    direction_1h = trend_direction(df_1h)
    if not direction_1h:
        return None

    score += 30
    components.append(("trend_1h", 30))

    # =====================
    # 15M → ALINEACIÓN BÁSICA
    # =====================

    if not market_has_strength(df_15m, ADX_MIN_TREND_15M):
        return None

    direction_15m = trend_direction(df_15m)
    if direction_15m != direction_1h:
        return None

    score += 20
    components.append(("alignment_15m", 20))

    # =====================
    # 5M → SETUP DE ENTRADA
    # =====================

    is_breakout = breakout_confirmation(df_5m, direction_1h)
    is_pullback = pullback_confirmation(df_5m, direction_1h)

    if not (is_breakout or is_pullback):
        return None

    if is_breakout:
        score += 20
        components.append(("breakout_5m", 20))
    else:
        score += 15
        components.append(("pullback_5m", 15))

    # =====================
    # BONUS SUAVE
    # =====================

    if df_1h.iloc[-1]["adx"] >= 24:
        score += 10
        components.append(("adx_bonus_1h", 10))

    if last_5m["volume"] >= last_5m["vol_ma"] * 1.20:
        score += 5
        components.append(("volume_bonus_5m", 5))

    score = max(0, min(score, MAX_SCORE))

    return {
        "direction": direction_1h,
        "entry_price": round(float(last_5m["close"]), 4),
        "score": round(score, 2),
        "components": components,
    }
