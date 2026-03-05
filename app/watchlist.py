# app/watchlist.py
"""
Watchlist (USDT-M Futures) - módulo independiente, listo para producción.

✅ NO toca handlers.py.
✅ Usa MongoDB vía app.database.get_db()
✅ CRUD de watchlist por usuario con límites por plan.

Colección: watchlists
Documento:
{
  "user_id": int,
  "symbols": ["BTCUSDT", "ETHUSDT", ...],
  "created_at": datetime,
  "updated_at": datetime
}
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable, List, Optional, Tuple

from pymongo.collection import Collection

from app.database import get_db


WATCHLIST_COLLECTION_NAME = "watchlists"


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def collection() -> Collection:
    return get_db()[WATCHLIST_COLLECTION_NAME]


# -----------------------------
# Plan limits
# -----------------------------
def max_items_for_plan(plan: str) -> Optional[int]:
    """
    Retorna el máximo de símbolos permitidos por plan.
    - FREE: 2
    - PLUS: 10
    - PREMIUM: ilimitado (None)
    """
    p = (plan or "").upper().strip()
    if p in ("PREMIUM", "VIP"):
        return None
    if p in ("PLUS", "PRO"):
        return 10
    return 2


# -----------------------------
# Normalización de símbolos
# -----------------------------
def normalize_symbol(symbol: str) -> Optional[str]:
    """
    Normaliza símbolos para Binance USDT-M futures.
    - Acepta: "btc", "BTCUSDT", "BTC/USDT"
    - Devuelve: "BTCUSDT"
    - Si es inválido, devuelve None.
    """
    if not symbol:
        return None
    s = symbol.strip().upper()
    s = s.replace(" ", "")
    s = s.replace("/", "")
    s = s.replace("-", "")
    # Permitir que escriban "BTCUSDT.P" etc -> cortar no alfanum
    s = "".join(ch for ch in s if ch.isalnum())

    if not s:
        return None

    if s.endswith("USDT"):
        base = s[:-4]
        if 2 <= len(base) <= 15 and base.isalnum():
            return f"{base}USDT"
        return None

    # Si solo pusieron el base (BTC, ETH...)
    if 2 <= len(s) <= 15 and s.isalnum():
        return f"{s}USDT"

    return None


def normalize_many(raw: str) -> List[str]:
    """
    Convierte un texto libre en una lista de símbolos normalizados.
    Ej: "btc, eth solusdt" -> ["BTCUSDT","ETHUSDT","SOLUSDT"]
    """
    if not raw:
        return []
    # separadores comunes
    parts = []
    for chunk in raw.replace("\n", ",").replace(";", ",").split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        # permitir espacios dentro
        parts.extend([p for p in chunk.split() if p.strip()])

    out = []
    seen = set()
    for p in parts:
        sym = normalize_symbol(p)
        if sym and sym not in seen:
            out.append(sym)
            seen.add(sym)
    return out


# -----------------------------
# CRUD
# -----------------------------
@dataclass
class WatchlistResult:
    ok: bool
    message: str
    symbols: List[str]


def get_symbols(user_id: int) -> List[str]:
    doc = collection().find_one({"user_id": int(user_id)}, {"symbols": 1})
    return list(doc.get("symbols", [])) if doc else []


def set_symbols(user_id: int, symbols: Iterable[str]) -> WatchlistResult:
    syms = []
    seen = set()
    for s in symbols:
        ns = normalize_symbol(s)
        if ns and ns not in seen:
            syms.append(ns)
            seen.add(ns)

    collection().update_one(
        {"user_id": int(user_id)},
        {
            "$set": {"symbols": syms, "updated_at": _now_utc()},
            "$setOnInsert": {"created_at": _now_utc()},
        },
        upsert=True,
    )
    return WatchlistResult(True, "✅ Watchlist actualizada.", syms)


def add_symbol(user_id: int, symbol: str, plan: str = "FREE") -> WatchlistResult:
    sym = normalize_symbol(symbol)
    if not sym:
        return WatchlistResult(False, "❌ Símbolo inválido. Ej: BTCUSDT", get_symbols(user_id))

    current = get_symbols(user_id)
    if sym in current:
        return WatchlistResult(True, "✅ Ya estaba en tu watchlist.", current)

    limit = max_items_for_plan(plan)
    if limit is not None and len(current) >= limit:
        return WatchlistResult(
            False,
            f"🔒 Límite alcanzado para tu plan ({limit}).",
            current,
        )

    # Upsert + addToSet
    collection().update_one(
        {"user_id": int(user_id)},
        {
            "$addToSet": {"symbols": sym},
            "$set": {"updated_at": _now_utc()},
            "$setOnInsert": {"created_at": _now_utc()},
        },
        upsert=True,
    )
    updated = get_symbols(user_id)
    return WatchlistResult(True, f"✅ Añadido: {sym}", updated)


def remove_symbol(user_id: int, symbol: str) -> WatchlistResult:
    sym = normalize_symbol(symbol)
    if not sym:
        return WatchlistResult(False, "❌ Símbolo inválido.", get_symbols(user_id))

    collection().update_one(
        {"user_id": int(user_id)},
        {"$pull": {"symbols": sym}, "$set": {"updated_at": _now_utc()}},
        upsert=True,
    )
    updated = get_symbols(user_id)
    return WatchlistResult(True, f"✅ Eliminado: {sym}", updated)


def clear(user_id: int) -> WatchlistResult:
    collection().update_one(
        {"user_id": int(user_id)},
        {"$set": {"symbols": [], "updated_at": _now_utc()}, "$setOnInsert": {"created_at": _now_utc()}},
        upsert=True,
    )
    return WatchlistResult(True, "✅ Watchlist limpiada.", [])


# -----------------------------
# Helpers para UI/Handlers
# -----------------------------
def format_watchlist(symbols: List[str]) -> str:
    if not symbols:
        return "⭐ Watchlist vacía.\n\nEscribe un símbolo para añadir (ej: BTCUSDT)."

    lines = ["⭐ Tu Watchlist (USDT-M Futures)\n"]
    for i, s in enumerate(symbols, 1):
        lines.append(f"{i}) {s}")
    lines.append("\nTip: escribe varios separados por coma. Ej: BTC, ETH, SOL")
    return "\n".join(lines)
