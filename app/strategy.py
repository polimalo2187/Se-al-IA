import pandas as pd
from typing import Optional, Dict
import ta

# =======================================
# CONFIGURACIÓN (adaptada del bot trading)
# =======================================

EMA_FAST = 20
EMA_MID = 50
EMA_SLOW = 200

ADX_PERIOD = 14
ATR_PERIOD = 14

ADX_MIN = 19
ATR_PCT_MIN = 0.0028
ATR_PCT_MAX = 0.0108

BREAKOUT_LOOKBACK = 24
RETEST_TOL_ATR = 0.32

MIN_BODY_RATIO = 0.38

MAX_SCORE = 100
MIN_SCORE_TO_SIGNAL = 76


# =======================================
# INDICADORES
# =======================================

def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()

    df["ema20"] = ta.trend.ema_indicator(df["close"], EMA_FAST)
    df["ema50"] = ta.trend.ema_indicator(df["close"], EMA_MID)
    df["ema200"] = ta.trend.ema_indicator(df["close"], EMA_SLOW)

    df["adx"] = ta.trend.adx(df["high"], df["low"], df["close"], ADX_PERIOD)

    atr = ta.volatility.average_true_range(
        df["high"], df["low"], df["close"], ATR_PERIOD
    )
    df["atr"] = atr
    df["atr_pct"] = df["atr"] / df["close"]

    df["body"] = (df["close"] - df["open"]).abs()
    df["range"] = (df["high"] - df["low"]).replace(0, 1e-9)
    df["body_ratio"] = df["body"] / df["range"]

    return df


# =======================================
# BREAKOUT LEVEL
# =======================================

def breakout_level(df: pd.DataFrame, direction: str) -> float:
    ref = df.iloc[-(BREAKOUT_LOOKBACK + 2):-2]

    if direction == "LONG":
        return float(ref["high"].max())

    return float(ref["low"].min())


# =======================================
# CONFIRM BREAKOUT + RETEST
# =======================================

def confirm_breakout_retest(df: pd.DataFrame, direction: str) -> bool:
    last = df.iloc[-1]
    prev = df.iloc[-2]

    level = breakout_level(df, direction)
    atr = float(last["atr"])

    if atr <= 0:
        return False

    if direction == "LONG":
        breakout = (
            float(prev["close"]) > level
            and float(prev["high"]) > level
            and float(prev["body_ratio"]) >= MIN_BODY_RATIO
        )

        retest = (
            float(last["low"]) <= level + (atr * RETEST_TOL_ATR)
            and float(last["close"]) >= level
        )

        return breakout and retest

    breakout = (
        float(prev["close"]) < level
        and float(prev["low"]) < level
        and float(prev["body_ratio"]) >= MIN_BODY_RATIO
    )

    retest = (
        float(last["high"]) >= level - (atr * RETEST_TOL_ATR)
        and float(last["close"]) <= level
    )

    return breakout and retest


# =======================================
# ESTRATEGIA 5M
# =======================================

def mtf_strategy(
    df_1h: pd.DataFrame,
    df_15m: pd.DataFrame,
    df_5m: pd.DataFrame,
) -> Optional[Dict]:
    # Solo usamos 5M. Mantenemos la firma para no romper scanner.py.
    if len(df_5m) < BREAKOUT_LOOKBACK + 30:
        return None

    df = add_indicators(df_5m)

    if len(df) < BREAKOUT_LOOKBACK + 30:
        return None

    last = df.iloc[-1]

    score = 0
    components = []

    # =========================
    # TENDENCIA
    # =========================
    long_trend = (
        float(last["ema20"]) > float(last["ema50"]) > float(last["ema200"])
    )
    short_trend = (
        float(last["ema20"]) < float(last["ema50"]) < float(last["ema200"])
    )

    if not (long_trend or short_trend):
        return None

    direction = "LONG" if long_trend else "SHORT"

    score += 25
    components.append(("trend_structure", 25))

    # =========================
    # ADX
    # =========================
    if float(last["adx"]) < ADX_MIN:
        return None

    score += 20
    components.append(("adx_strength", 20))

    # =========================
    # ATR %
    # =========================
    atr_pct = float(last["atr_pct"])
    if not (ATR_PCT_MIN <= atr_pct <= ATR_PCT_MAX):
        return None

    score += 15
    components.append(("atr_valid", 15))

    # =========================
    # BREAKOUT + RETEST
    # =========================
    if not confirm_breakout_retest(df, direction):
        return None

    score += 25
    components.append(("breakout_retest", 25))

    # =========================
    # VELA CONTINUACIÓN
    # =========================
    if direction == "LONG":
        if float(last["close"]) <= float(last["open"]):
            return None
    else:
        if float(last["close"]) >= float(last["open"]):
            return None

    if float(last["body_ratio"]) < 0.28:
        return None

    score += 15
    components.append(("continuation_candle", 15))

    score = max(0, min(score, MAX_SCORE))

    if score < MIN_SCORE_TO_SIGNAL:
        return None

    return {
        "direction": direction,
        "entry_price": round(float(last["close"]), 4),
        "score": round(score, 2),
        "components": components,
  }
