import pandas as pd
from typing import Optional, Dict, Tuple
import ta

# =======================================
# CONFIGURACIÓN BASE
# =======================================

EMA_FAST = 20
EMA_MID = 50
EMA_SLOW = 200

ADX_PERIOD = 14
ATR_PERIOD = 14
BREAKOUT_LOOKBACK = 24

MAX_SCORE = 100

# =======================================
# PERFILES POR PLAN
# Premium NO se toca
# =======================================

PREMIUM_PROFILE = {
    "name": "premium",
    "adx_min": 19.0,
    "atr_pct_min": 0.0028,
    "atr_pct_max": 0.0108,
    "retest_tol_atr": 0.32,
    "min_body_ratio_breakout": 0.38,
    "min_body_ratio_continuation": 0.28,
    "score": 100.0,
    "components": [
        ("trend_structure", 25),
        ("adx_strength", 20),
        ("atr_valid", 15),
        ("breakout_retest", 25),
        ("continuation_candle", 15),
    ],
}

PLUS_PROFILE = {
    "name": "plus",
    "adx_min": 17.0,
    "atr_pct_min": 0.0024,
    "atr_pct_max": 0.0125,
    "retest_tol_atr": 0.48,
    "min_body_ratio_breakout": 0.30,
    "min_body_ratio_continuation": 0.22,
    "score": 86.0,
    "components": [
        ("trend_structure", 24),
        ("adx_strength", 18),
        ("atr_valid", 14),
        ("breakout_retest", 18),
        ("continuation_candle", 12),
    ],
}

FREE_PROFILE = {
    "name": "free",
    "adx_min": 15.0,
    "atr_pct_min": 0.0020,
    "atr_pct_max": 0.0140,
    "retest_tol_atr": 0.62,
    "min_body_ratio_breakout": 0.22,
    "min_body_ratio_continuation": 0.16,
    "score": 78.0,
    "components": [
        ("trend_structure", 22),
        ("adx_strength", 16),
        ("atr_valid", 12),
        ("breakout_retest", 16),
        ("continuation_candle", 12),
    ],
}

# Orden importante: primero premium, luego plus, luego free
PROFILES = [PREMIUM_PROFILE, PLUS_PROFILE, FREE_PROFILE]


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
# HELPERS
# =======================================

def _trend_direction(last: pd.Series) -> Optional[str]:
    if float(last["ema20"]) > float(last["ema50"]) > float(last["ema200"]):
        return "LONG"
    if float(last["ema20"]) < float(last["ema50"]) < float(last["ema200"]):
        return "SHORT"
    return None


def _confirm_breakout_retest(df: pd.DataFrame, direction: str, profile: Dict) -> bool:
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
            and float(prev["body_ratio"]) >= profile["min_body_ratio_breakout"]
        )

        retest = (
            float(last["low"]) <= level + (atr * profile["retest_tol_atr"])
            and float(last["close"]) >= level
        )

        return breakout and retest

    breakout = (
        float(prev["close"]) < level
        and float(prev["low"]) < level
        and float(prev["body_ratio"]) >= profile["min_body_ratio_breakout"]
    )

    retest = (
        float(last["high"]) >= level - (atr * profile["retest_tol_atr"])
        and float(last["close"]) <= level
    )

    return breakout and retest


def _continuation_ok(last: pd.Series, direction: str, profile: Dict) -> bool:
    if direction == "LONG":
        if float(last["close"]) <= float(last["open"]):
            return False
    else:
        if float(last["close"]) >= float(last["open"]):
            return False

    if float(last["body_ratio"]) < profile["min_body_ratio_continuation"]:
        return False

    return True


def _evaluate_profile(df: pd.DataFrame, profile: Dict) -> Optional[Tuple[str, float, float, list]]:
    last = df.iloc[-1]

    direction = _trend_direction(last)
    if not direction:
        return None

    if float(last["adx"]) < profile["adx_min"]:
        return None

    atr_pct = float(last["atr_pct"])
    if not (profile["atr_pct_min"] <= atr_pct <= profile["atr_pct_max"]):
        return None

    if not _confirm_breakout_retest(df, direction, profile):
        return None

    if not _continuation_ok(last, direction, profile):
        return None

    # Entrada menos tardía: promedio entre el nivel de ruptura y el close confirmado
    level = breakout_level(df, direction)
    entry_price = (float(level) + float(last["close"])) / 2.0

    return direction, entry_price, float(profile["score"]), list(profile["components"])


# =======================================
# ESTRATEGIA 5M POR NIVELES
# =======================================

def mtf_strategy(
    df_1h: pd.DataFrame,
    df_15m: pd.DataFrame,
    df_5m: pd.DataFrame,
) -> Optional[Dict]:
    # Solo usamos 5M. Se mantiene la firma para no romper scanner.py.
    if len(df_5m) < BREAKOUT_LOOKBACK + 30:
        return None

    df = add_indicators(df_5m)

    if len(df) < BREAKOUT_LOOKBACK + 30:
        return None

    for profile in PROFILES:
        result = _evaluate_profile(df, profile)
        if result:
            direction, entry_price, score, components = result
            return {
                "direction": direction,
                "entry_price": round(float(entry_price), 4),
                "score": round(float(score), 2),
                "components": components,
            }

    return None
