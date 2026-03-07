# app/binance_api.py
# Utilidades para consumir endpoints públicos de Binance Futures (USDT-M)
# Radar PRO v2: más dinámico, usando 15m + 1h + liquidez.

from __future__ import annotations

import random
import time
from typing import Any, Dict, List, Tuple

import requests


# Endpoints públicos (USDT-M Futures)
FAPI_24H_TICKER = "https://fapi.binance.com/fapi/v1/ticker/24hr"
FAPI_PREMIUM_INDEX = "https://fapi.binance.com/fapi/v1/premiumIndex?symbol={symbol}"
FAPI_OPEN_INTEREST = "https://fapi.binance.com/fapi/v1/openInterest?symbol={symbol}"
FAPI_KLINES = "https://fapi.binance.com/fapi/v1/klines?symbol={symbol}&interval={interval}&limit={limit}"

# Cache en memoria (por proceso)
_CACHE: Dict[str, Tuple[float, Any]] = {}

# TTLs
_TTL_TICKERS = 20
_TTL_SYMBOL_DETAILS = 60
_TTL_KLINES = 45

# Radar cooldown / rotación
_RADAR_RECENT_SYMBOLS: Dict[str, float] = {}
_RADAR_SYMBOL_COOLDOWN_SECONDS = 1800  # 30 minutos
_RADAR_MAX_CANDIDATES = 60

# Filtros de radar
_MIN_QUOTE_VOLUME = 2_000_000  # liquidez mínima 24h en USDT
_DEFAULT_RADAR_LIMIT = 12


def _get_json(url: str, timeout: int = 10) -> Any:
    try:
        r = requests.get(url, timeout=timeout)
        r.raise_for_status()
        return r.json()
    except Exception:
        return []


def _cache_get(key: str) -> Any | None:
    now = time.time()
    item = _CACHE.get(key)
    if not item:
        return None
    expires_at, value = item
    if now >= expires_at:
        _CACHE.pop(key, None)
        return None
    return value


def _cache_set(key: str, value: Any, ttl_seconds: int) -> None:
    _CACHE[key] = (time.time() + ttl_seconds, value)


def _prune_radar_recent_symbols() -> None:
    now = time.time()
    expired = [sym for sym, ts in _RADAR_RECENT_SYMBOLS.items() if (now - ts) >= _RADAR_SYMBOL_COOLDOWN_SECONDS]
    for sym in expired:
        _RADAR_RECENT_SYMBOLS.pop(sym, None)


def _mark_radar_symbols(symbols: List[str]) -> None:
    now = time.time()
    for sym in symbols:
        _RADAR_RECENT_SYMBOLS[sym] = now


def _symbol_in_radar_cooldown(symbol: str) -> bool:
    _prune_radar_recent_symbols()
    last_seen = _RADAR_RECENT_SYMBOLS.get(symbol)
    if last_seen is None:
        return False
    return (time.time() - last_seen) < _RADAR_SYMBOL_COOLDOWN_SECONDS


def get_futures_24h_tickers() -> List[Dict[str, Any]]:
    key = "futures_24h_tickers"
    cached = _cache_get(key)
    if cached is not None:
        return cached

    data = _get_json(FAPI_24H_TICKER, timeout=10)
    if not isinstance(data, list):
        return []

    _cache_set(key, data, _TTL_TICKERS)
    return data


def _is_usdt_symbol(symbol: str) -> bool:
    return symbol.endswith("USDT") and not symbol.endswith("BUSD")


def get_top_movers_usdtm(limit: int = 10, *, kind: str = "gainers") -> List[Dict[str, Any]]:
    tickers = [t for t in get_futures_24h_tickers() if _is_usdt_symbol(t.get("symbol", ""))]

    def pct(x: Dict[str, Any]) -> float:
        try:
            return float(x.get("priceChangePercent", 0.0))
        except Exception:
            return 0.0

    if kind == "losers":
        tickers.sort(key=pct)
    elif kind == "absolute":
        tickers.sort(key=lambda x: abs(pct(x)), reverse=True)
    else:
        tickers.sort(key=pct, reverse=True)

    return tickers[: max(1, int(limit))]


def get_premium_index(symbol: str) -> Dict[str, Any]:
    symbol = symbol.upper().strip()
    key = f"premium_index:{symbol}"
    cached = _cache_get(key)
    if cached is not None:
        return cached

    data = _get_json(FAPI_PREMIUM_INDEX.format(symbol=symbol), timeout=10)
    _cache_set(key, data, _TTL_SYMBOL_DETAILS)
    return data


def get_open_interest(symbol: str) -> Dict[str, Any]:
    symbol = symbol.upper().strip()
    key = f"open_interest:{symbol}"
    cached = _cache_get(key)
    if cached is not None:
        return cached

    data = _get_json(FAPI_OPEN_INTEREST.format(symbol=symbol), timeout=10)
    _cache_set(key, data, _TTL_SYMBOL_DETAILS)
    return data


def _get_klines(symbol: str, interval: str, limit: int = 4) -> List[List[Any]]:
    symbol = symbol.upper().strip()
    key = f"klines:{symbol}:{interval}:{limit}"
    cached = _cache_get(key)
    if cached is not None:
        return cached

    data = _get_json(FAPI_KLINES.format(symbol=symbol, interval=interval, limit=limit), timeout=10)
    if not isinstance(data, list):
        return []

    _cache_set(key, data, _TTL_KLINES)
    return data


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


def _pct_change_from_kline(symbol: str, interval: str) -> float:
    """
    Calcula % de cambio de la vela más reciente:
    (close - open) / open * 100
    """
    klines = _get_klines(symbol, interval, limit=2)
    if not klines:
        return 0.0

    row = klines[-1]
    try:
        open_price = float(row[1])
        close_price = float(row[4])
        if open_price <= 0:
            return 0.0
        return ((close_price - open_price) / open_price) * 100
    except Exception:
        return 0.0


def _rank(values: List[float]) -> Dict[int, float]:
    if not values:
        return {}
    sorted_idx = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    n = max(1, len(values) - 1)
    for r, i in enumerate(sorted_idx):
        ranks[i] = r / n
    return {i: ranks[i] for i in range(len(values))}


def _momentum_label(change_15m: float, change_1h: float) -> str:
    strength = abs(change_15m) * 0.7 + abs(change_1h) * 0.3
    if strength >= 5:
        return "Muy alto"
    if strength >= 3:
        return "Alto"
    if strength >= 1.5:
        return "Medio"
    return "Bajo"


def _build_radar_rows_v2() -> List[Dict[str, Any]]:
    tickers = [t for t in get_futures_24h_tickers() if _is_usdt_symbol(t.get("symbol", ""))]

    rows: List[Dict[str, Any]] = []
    for t in tickers:
        symbol = t.get("symbol", "")
        if not symbol:
            continue

        quote_volume = _safe_float(t.get("quoteVolume", 0.0))
        if quote_volume < _MIN_QUOTE_VOLUME:
            continue

        trades = _safe_float(t.get("count", 0.0))
        last_price = _safe_float(t.get("lastPrice", 0.0))

        change_15m = _pct_change_from_kline(symbol, "15m")
        change_1h = _pct_change_from_kline(symbol, "1h")

        rows.append(
            {
                "symbol": symbol,
                "quote_volume": quote_volume,
                "trades": trades,
                "last_price": last_price,
                "change_15m": change_15m,
                "change_1h": change_1h,
                "abs_change_15m": abs(change_15m),
                "abs_change_1h": abs(change_1h),
            }
        )

    if not rows:
        return []

    v_15m = [r["abs_change_15m"] for r in rows]
    v_1h = [r["abs_change_1h"] for r in rows]
    v_vol = [r["quote_volume"] for r in rows]

    r_15m = _rank(v_15m)
    r_1h = _rank(v_1h)
    r_vol = _rank(v_vol)

    for i, r in enumerate(rows):
        # Score PRO v2:
        # 50% movimiento reciente 15m
        # 30% liquidez (volumen 24h)
        # 20% dirección/tendencia 1h
        combined = (0.50 * r_15m[i]) + (0.30 * r_vol[i]) + (0.20 * r_1h[i])
        score = int(round(100 * combined))
        r["score"] = max(1, min(100, score))

        direction_seed = (r["change_15m"] * 0.7) + (r["change_1h"] * 0.3)
        r["direction"] = "LONG" if direction_seed >= 0 else "SHORT"
        r["momentum"] = _momentum_label(r["change_15m"], r["change_1h"])

    return rows


def get_radar_opportunities(limit: int = _DEFAULT_RADAR_LIMIT) -> List[Dict[str, Any]]:
    """
    Radar PRO v2:
    - basado en 15m + 1h + liquidez
    - penaliza símbolos recientes
    - rota más
    - genera oportunidades más frescas
    """
    rows = _build_radar_rows_v2()
    if not rows:
        return []

    # Orden base por score + frescura
    rows.sort(
        key=lambda x: (
            x["score"],
            x["abs_change_15m"],
            x["abs_change_1h"],
            x["quote_volume"],
        ),
        reverse=True,
    )

    candidates = rows[: max(limit * 4, _RADAR_MAX_CANDIDATES)]

    for r in candidates:
        symbol = r["symbol"]
        penalty = 18 if _symbol_in_radar_cooldown(symbol) else 0
        r["radar_penalty"] = penalty
        r["final_score"] = max(1, r["score"] - penalty)

    # pools
    longs = [r for r in candidates if r["direction"] == "LONG"]
    shorts = [r for r in candidates if r["direction"] == "SHORT"]
    mixed = sorted(
        candidates,
        key=lambda x: (
            x["final_score"],
            x["abs_change_15m"],
            x["abs_change_1h"],
            x["quote_volume"],
        ),
        reverse=True,
    )

    longs.sort(
        key=lambda x: (x["final_score"], x["abs_change_15m"], x["quote_volume"]),
        reverse=True,
    )
    shorts.sort(
        key=lambda x: (x["final_score"], x["abs_change_15m"], x["quote_volume"]),
        reverse=True,
    )

    selected: List[Dict[str, Any]] = []
    used = set()

    def _add_from(pool: List[Dict[str, Any]], max_items: int) -> None:
        added = 0
        for row in pool:
            if len(selected) >= limit or added >= max_items:
                return
            sym = row["symbol"]
            if sym in used:
                continue
            selected.append(row)
            used.add(sym)
            added += 1

    # mezcla equilibrada
    # 50% oportunidades generales
    _add_from(mixed, max(1, limit // 2))
    # 25% longs
    _add_from(longs, max(1, limit // 4))
    # 25% shorts
    _add_from(shorts, max(1, limit // 4))

    if len(selected) < limit:
        for row in mixed:
            if len(selected) >= limit:
                break
            sym = row["symbol"]
            if sym in used:
                continue
            selected.append(row)
            used.add(sym)

    # shuffle ligero entre scores similares
    selected = sorted(selected, key=lambda x: x["final_score"], reverse=True)

    grouped: List[Dict[str, Any]] = []
    i = 0
    while i < len(selected):
        block = [selected[i]]
        j = i + 1
        while j < len(selected) and abs(selected[j]["final_score"] - selected[i]["final_score"]) <= 3:
            block.append(selected[j])
            j += 1
        random.shuffle(block)
        grouped.extend(block)
        i = j

    selected = grouped[:limit]

    for r in selected:
        r["score"] = r["final_score"]
        r["change_pct"] = round(r["change_15m"], 2)  # lo que verá el radar
        r["priceChangePercent"] = round(r["change_15m"], 2)
        r["quoteVolume"] = r["quote_volume"]
        r["trades"] = r["trades"]
        r["lastPrice"] = r["last_price"]
        r.pop("final_score", None)
        r.pop("radar_penalty", None)

    _mark_radar_symbols([r["symbol"] for r in selected])

    return selected[: max(1, int(limit))]
