import pandas as pd
from typing import Optional, Dict
import ta

# =========================
# CONFIGURACIÓN
# =========================

EMA_TREND = 200

ADX_PERIOD = 14
ADX_MIN_TREND_1H = 23
ADX_MIN_TREND_15M = 20
ADX_MIN_SETUP_5M = 18

BB_PERIOD = 20
BB_STD = 2.0

VOLUME_MULTIPLIER_BREAKOUT = 1.8
VOLUME_MULTIPLIER_PULLBACK = 1.2

MIN_BB_WIDTH_5M = 0.008
MIN_BB_WIDTH_15M = 0.010

MIN_DISTANCE_EMA_1H = 0.0035
MIN_DISTANCE_EMA_15M = 0.0020

MIN_CANDLE_BODY_RATIO = 0.45
MAX_WICK_RATIO_BREAKOUT = 0.35

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

    # cuerpo y mechas
    df["body"] = (df["close"] - df["open"]).abs()
    df["range"] = (df["high"] - df["low"]).replace(0, 1e-9)
    df["body_ratio"] = df["body"] / df["range"]

    df["upper_wick"] = df["high"] - df[["open", "close"]].max(axis=1)
    df["lower_wick"] = df[["open", "close"]].min(axis=1) - df["low"]
    df["upper_wick_ratio"] = df["upper_wick"] / df["range"]
    df["lower_wick_ratio"] = df["lower_wick"] / df["range"]

    # pendiente EMA 200
    df["ema_200_slope"] = df["ema_200"].diff(5)

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

def distance_from_ema_ok(df: pd.DataFrame, min_distance: float) -> bool:
    last = df.iloc[-1]
    ema = last["ema_200"]
    close = last["close"]

    if ema == 0:
        return False

    distance = abs(close - ema) / ema
    return distance >= min_distance

def ema_slope_confirmed(df: pd.DataFrame, direction: str) -> bool:
    last = df.iloc[-1]
    slope = last["ema_200_slope"]

    if pd.isna(slope):
        return False

    if direction == "LONG":
        return slope > 0
    return slope < 0

def bb_width_ok(df: pd.DataFrame, min_width: float) -> bool:
    last = df.iloc[-1]
    return last["bb_width"] >= min_width

def candle_strength_ok(df: pd.DataFrame, direction: str) -> bool:
    last = df.iloc[-1]

    if last["body_ratio"] < MIN_CANDLE_BODY_RATIO:
        return False

    if direction == "LONG":
        return last["close"] > last["open"]
    return last["close"] < last["open"]

# =========================
# SETUP ROMPIMIENTO
# =========================

def breakout_confirmation(df: pd.DataFrame, direction: str) -> bool:
    last = df.iloc[-1]

    if last["volume"] < last["vol_ma"] * VOLUME_MULTIPLIER_BREAKOUT:
        return False

    if last["body_ratio"] < MIN_CANDLE_BODY_RATIO:
        return False

    if direction == "LONG":
        if last["upper_wick_ratio"] > MAX_WICK_RATIO_BREAKOUT:
            return False
        return (
            last["close"] > last["bb_high"]
            and last["close"] > last["open"]
        )
    else:
        if last["lower_wick_ratio"] > MAX_WICK_RATIO_BREAKOUT:
            return False
        return (
            last["close"] < last["bb_low"]
            and last["close"] < last["open"]
        )

# =========================
# SETUP RETROCESO FUERTE
# =========================

def pullback_confirmation(df: pd.DataFrame, direction: str) -> bool:
    last = df.iloc[-1]
    prev = df.iloc[-2]

    if last["volume"] < last["vol_ma"] * VOLUME_MULTIPLIER_PULLBACK:
        return False

    if last["body_ratio"] < MIN_CANDLE_BODY_RATIO:
        return False

    if direction == "LONG":
        return (
            last["close"] >= last["bb_mid"]
            and last["close"] > last["ema_200"]
            and last["adx"] >= ADX_MIN_SETUP_5M
            and last["close"] > last["open"]
            and prev["close"] <= prev["bb_mid"]
        )
    else:
        return (
            last["close"] <= last["bb_mid"]
            and last["close"] < last["ema_200"]
            and last["adx"] >= ADX_MIN_SETUP_5M
            and last["close"] < last["open"]
            and prev["close"] >= prev["bb_mid"]
        )

# =========================
# ESTRATEGIA MTF MEJORADA
# =========================

def mtf_strategy(
    df_1h: pd.DataFrame,
    df_15m: pd.DataFrame,
    df_5m: pd.DataFrame,
) -> Optional[Dict]:

    df_1h = add_indicators(df_1h)
    df_15m = add_indicators(df_15m)
    df_5m = add_indicators(df_5m)

    if len(df_1h) < 210 or len(df_15m) < 210 or len(df_5m) < 210:
        return None

    score = 0
    components = []

    last_5m = df_5m.iloc[-1]

    # =====================
    # 1H → FILTRO DURO
    # =====================

    if not market_has_strength(df_1h, ADX_MIN_TREND_1H):
        return None

    direction_1h = trend_direction(df_1h)
    if not direction_1h:
        return None

    if not distance_from_ema_ok(df_1h, MIN_DISTANCE_EMA_1H):
        return None

    if not ema_slope_confirmed(df_1h, direction_1h):
        return None

    score += 35
    components.append(("trend_1h", 35))

    # =====================
    # 15M → CONTEXTO Y ALINEACIÓN
    # =====================

    if not market_has_strength(df_15m, ADX_MIN_TREND_15M):
        return None

    direction_15m = trend_direction(df_15m)
    if direction_15m != direction_1h:
        return None

    if not distance_from_ema_ok(df_15m, MIN_DISTANCE_EMA_15M):
        return None

    if not ema_slope_confirmed(df_15m, direction_1h):
        return None

    if not bb_width_ok(df_15m, MIN_BB_WIDTH_15M):
        return None

    score += 25
    components.append(("alignment_15m", 25))

    # =====================
    # 5M → SETUP REAL
    # =====================

    if not bb_width_ok(df_5m, MIN_BB_WIDTH_5M):
        return None

    if not market_has_strength(df_5m, ADX_MIN_SETUP_5M):
        return None

    if not candle_strength_ok(df_5m, direction_1h):
        return None

    is_breakout = breakout_confirmation(df_5m, direction_1h)
    is_pullback = pullback_confirmation(df_5m, direction_1h)

    if not (is_breakout or is_pullback):
        return None

    if is_breakout:
        score += 30
        components.append(("breakout_5m", 30))
    else:
        score += 20
        components.append(("pullback_5m", 20))

    # =====================
    # BONUS → FUERZA EXTRA
    # =====================

    if df_1h.iloc[-1]["adx"] >= 30:
        score += 5
        components.append(("adx_bonus_1h", 5))

    if abs(last_5m["change_pct"]) if "change_pct" in df_5m.columns else False:
        pass

    if last_5m["volume"] >= last_5m["vol_ma"] * 2.0:
        score += 5
        components.append(("volume_bonus_5m", 5))

    score = max(0, min(score, MAX_SCORE))

    # filtro final de calidad
    if score < 75:
        return None

    return {
        "direction": direction_1h,
        "entry_price": round(float(last_5m["close"]), 4),
        "score": round(score, 2),
        "components": components,
  }
