# app/binance_api.py
# Utilidades para consumir endpoints públicos de Binance Futures (USDT-M)
# Diseñado para ser liviano y estable (timeouts + cache simple).

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional, Tuple

import requests

FAPI_24H_TICKER = "https://fapi.binance.com/fapi/v1/ticker/24hr"

# Cache en memoria (por proceso). Evita spamear Binance si muchos usuarios tocan el botón a la vez.
_CACHE: Dict[str, Tuple[float, Any]] = {}
_DEFAULT_TTL_SECONDS = 20


def _get_json(url: str, timeout: int = 10) -> Any:
    r = requests.get(url, timeout=timeout)
    r.raise_for_status()
    return r.json()


def get_futures_24h_tickers(ttl_seconds: int = _DEFAULT_TTL_SECONDS) -> List[Dict[str, Any]]:
    now = time.time()
    key = "futures_24h_tickers"
    cached = _CACHE.get(key)
    if cached and (now - cached[0]) < ttl_seconds:
        return cached[1]

    data = _get_json(FAPI_24H_TICKER, timeout=10)
    if not isinstance(data, list):
        # Por si Binance responde raro, devolvemos lista vacía para no romper el bot.
        data = []

    _CACHE[key] = (now, data)
    return data


def get_top_movers_usdtm(limit: int = 10, ttl_seconds: int = _DEFAULT_TTL_SECONDS) -> List[Dict[str, Any]]:
    """Top movers por % de cambio 24h en Binance USDT-M Futures.

    Filtra:
    - Solo símbolos que terminan en USDT
    - Excluye símbolos no deseados si aparecieran (ej: BUSD legacy)
    """
    data = get_futures_24h_tickers(ttl_seconds=ttl_seconds)

    pairs = []
    for x in data:
        try:
            sym = str(x.get("symbol", ""))
            if not sym.endswith("USDT"):
                continue
            # Evita pares raros legacy si aparecieran
            if sym.endswith("BUSD"):
                continue
            # Binance también incluye campos útiles:
            # priceChangePercent, quoteVolume, lastPrice
            pairs.append(x)
        except Exception:
            continue

    def pct(item: Dict[str, Any]) -> float:
        try:
            return float(item.get("priceChangePercent", 0.0))
        except Exception:
            return 0.0

    pairs.sort(key=pct, reverse=True)
    return pairs[: max(0, int(limit))]
