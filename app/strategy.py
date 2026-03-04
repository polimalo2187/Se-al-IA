import pandas as pd
from typing import Optional, Dict
import ta

# =========================
# CONFIGURACIÓN
# =========================

EMA_TREND = 200

ADX_PERIOD = 14
ADX_MIN_TREND = 25

BB_PERIOD = 20
BB_STD = 2.0

VOLUME_MULTIPLIER = 1.5

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
    df["bb_width"] = (df["bb_high"] - df["bb_low"]) / df["bb_mid"]

    df["adx"] = ta.trend.adx(
        df["high"], df["low"], df["close"], ADX_PERIOD
    )

    df["vol_ma"] = df["volume"].rolling(20).mean()

    return df

# =========================
# FILTROS DE MERCADO
# =========================

def market_has_strength(df: pd.DataFrame) -> bool:
    last = df.iloc[-1]
    return last["adx"] >= ADX_MIN_TREND

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

    if last["volume"] < last["vol_ma"] * VOLUME_MULTIPLIER:
        return False

    if direction == "LONG":
        return last["close"] > last["bb_high"]
    else:
        return last["close"] < last["bb_low"]

# =========================
# SETUP RETROCESO FUERTE
# =========================

def pullback_confirmation(df: pd.DataFrame, direction: str) -> bool:
    last = df.iloc[-1]

    if direction == "LONG":
        return (
            last["close"] >= last["bb_mid"]
            and last["close"] > last["ema_200"]
            and last["adx"] >= ADX_MIN_TREND
        )
    else:
        return (
            last["close"] <= last["bb_mid"]
            and last["close"] < last["ema_200"]
            and last["adx"] >= ADX_MIN_TREND
        )

# =========================
# ESTRATEGIA MTF
# =========================

def mtf_strategy(
    df_1h: pd.DataFrame,
    df_15m: pd.DataFrame,
    df_5m: pd.DataFrame,
) -> Optional[Dict]:

    df_1h = add_indicators(df_1h)
    df_15m = add_indicators(df_15m)
    df_5m = add_indicators(df_5m)

    score = 0
    components = []

    # =====================
    # 1H → FILTRO DURO
    # =====================

    if not market_has_strength(df_1h):
        return None

    direction = trend_direction(df_1h)
    if not direction:
        return None

    score += 35
    components.append(("trend_1h", 35))

    # =====================
    # 15M → CONTEXTO
    # =====================

    if not market_has_strength(df_15m):
        return None

    score += 25
    components.append(("strength_15m", 25))

    # =====================
    # 5M → SETUP (BREAK O PULL)
    # =====================

    last = df_5m.iloc[-1]

    is_breakout = breakout_confirmation(df_5m, direction)
    is_pullback = pullback_confirmation(df_5m, direction)

    if not (is_breakout or is_pullback):
        return None

    if is_breakout:
        score += 30
        components.append(("breakout_5m", 30))
    else:
        score += 25
        components.append(("pullback_5m", 25))

    # =====================
    # BONUS → FUERZA EXTRA
    # =====================

    if last["adx"] >= 35:
        score += 5
        components.append(("adx_bonus", 5))

    score = max(0, min(score, MAX_SCORE))

    return {
        "direction": direction,
        "entry_price": round(float(last["close"]), 4),
        "score": round(score, 2),
        "components": components,
      }
