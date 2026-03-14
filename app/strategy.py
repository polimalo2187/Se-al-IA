
import pandas as pd
from typing import Optional, Dict
import ta

# =======================================
# CONFIGURACIÓN (basada en tu bot trading)
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
    df["atr_pct"] = atr / df["close"]

    df["body"] = (df["close"] - df["open"]).abs()
    df["range"] = (df["high"] - df["low"]).replace(0, 1e-9)
    df["body_ratio"] = df["body"] / df["range"]

    return df


# =======================================
# BREAKOUT DETECTION
# =======================================

def breakout_level(df: pd.DataFrame, direction: str):

    lookback = df.iloc[-BREAKOUT_LOOKBACK:]

    if direction == "LONG":
        return lookback["high"].max()

    return lookback["low"].min()


# =======================================
# ESTRATEGIA 5M BREAKOUT RETEST
# =======================================

def mtf_strategy(
    df_1h: pd.DataFrame,
    df_15m: pd.DataFrame,
    df_5m: pd.DataFrame,
) -> Optional[Dict]:

    # Solo usamos 5M (scanner sigue pasando los otros pero no se usan)

    if len(df_5m) < BREAKOUT_LOOKBACK + 20:
        return None

    df = add_indicators(df_5m)

    last = df.iloc[-1]
    prev = df.iloc[-2]

    score = 0
    components = []

    # =======================================
    # FILTRO TENDENCIA
    # =======================================

    if not (
        last["ema20"] > last["ema50"] > last["ema200"]
        or last["ema20"] < last["ema50"] < last["ema200"]
    ):
        return None

    direction = "LONG" if last["ema20"] > last["ema50"] else "SHORT"

    score += 25
    components.append(("trend_structure", 25))

    # =======================================
    # ADX FUERZA
    # =======================================

    if last["adx"] < ADX_MIN:
        return None

    score += 20
    components.append(("adx_strength", 20))

    # =======================================
    # ATR VOLATILIDAD
    # =======================================

    if not (ATR_PCT_MIN <= last["atr_pct"] <= ATR_PCT_MAX):
        return None

    score += 15
    components.append(("atr_valid", 15))

    # =======================================
    # BREAKOUT
    # =======================================

    level = breakout_level(df, direction)

    if direction == "LONG":
        if prev["close"] <= level and last["close"] > level:
            score += 20
            components.append(("breakout", 20))
        else:
            return None
    else:
        if prev["close"] >= level and last["close"] < level:
            score += 20
            components.append(("breakout", 20))
        else:
            return None

    # =======================================
    # FILTRO VELA FUERTE
    # =======================================

    if last["body_ratio"] < MIN_BODY_RATIO:
        return None

    score += 10
    components.append(("strong_candle", 10))

    # =======================================
    # RETEST SUAVE
    # =======================================

    if direction == "LONG":
        retest = abs(last["low"] - level) / last["atr"]
    else:
        retest = abs(last["high"] - level) / last["atr"]

    if retest <= RETEST_TOL_ATR:
        score += 10
        components.append(("retest", 10))

    score = max(0, min(score, MAX_SCORE))

    if score < MIN_SCORE_TO_SIGNAL:
        return None

    return {
        "direction": direction,
        "entry_price": round(float(last["close"]), 4),
        "score": round(score, 2),
        "components": components,
    }
