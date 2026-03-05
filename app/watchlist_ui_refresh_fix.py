# app/watchlist_ui.py
"""
UI helper para Watchlist (InlineKeyboard).

✅ NO toca handlers.py
✅ Construye textos y teclados reutilizables.
✅ "Actualizar" ahora muestra un refresco visible (timestamp) y, si Binance responde,
   muestra precio + %24h por símbolo para que el botón tenga sentido.

Notas:
- Usa Binance Futures (USDT-M): https://fapi.binance.com/fapi/v1/ticker/24hr?symbol=BTCUSDT
- Si Binance falla/limita, el bot NO se cae: solo muestra una nota.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Dict, Tuple

import requests
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from app.watchlist import format_watchlist


# Callbacks sugeridos (ya usados por tu integración)
CB_WL_REFRESH = "wl_refresh"
CB_WL_CLEAR = "wl_clear"
CB_WL_REMOVE_PREFIX = "wl_rm:"   # wl_rm:BTCUSDT
CB_BACK_MENU = "back_menu"       # ya existe en tu bot


BINANCE_FUTURES_24H = "https://fapi.binance.com/fapi/v1/ticker/24hr"


def _now_utc_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def _safe_float(x, default: float = 0.0) -> float:
    try:
        return float(x)
    except Exception:
        return float(default)


def fetch_watchlist_snapshot(symbols: list[str]) -> Dict[str, Tuple[float, float]]:
    """
    Retorna dict:
      symbol -> (last_price, change_percent_24h)

    Si falla Binance, devuelve {}.
    """
    out: Dict[str, Tuple[float, float]] = {}
    if not symbols:
        return out

    # Para estabilidad: 1 request por símbolo (limite pequeño: FREE/PLUS)
    for sym in symbols:
        try:
            r = requests.get(BINANCE_FUTURES_24H, params={"symbol": sym}, timeout=8)
            if r.status_code != 200:
                continue
            data = r.json()
            last_price = _safe_float(data.get("lastPrice"))
            chg = _safe_float(data.get("priceChangePercent"))
            out[sym] = (last_price, chg)
        except Exception:
            # Silencioso: no tumbamos el bot por una API externa
            continue

    return out


def watchlist_keyboard(symbols: list[str]) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []

    rows.append([
        InlineKeyboardButton("🔄 Actualizar", callback_data=CB_WL_REFRESH),
        InlineKeyboardButton("🧹 Limpiar", callback_data=CB_WL_CLEAR),
    ])

    # Botones de eliminar (máx 6 para que no se haga gigante)
    for s in symbols[:6]:
        rows.append([InlineKeyboardButton(f"❌ Quitar {s}", callback_data=f"{CB_WL_REMOVE_PREFIX}{s}")])

    rows.append([InlineKeyboardButton("⬅️ Volver", callback_data=CB_BACK_MENU)])
    return InlineKeyboardMarkup(rows)


def render_watchlist_view(symbols: list[str]) -> tuple[str, InlineKeyboardMarkup]:
    """
    Devuelve (texto, teclado). El texto incluye:
    - lista base
    - snapshot de mercado (si disponible)
    - timestamp para que "Actualizar" siempre muestre un cambio
    """
    base = format_watchlist(symbols)

    snapshot = fetch_watchlist_snapshot(symbols[:10])
    lines = [base]

    if symbols and snapshot:
        lines.append("\n📊 Mercado (Futures 24h):")
        for s in symbols[:10]:
            if s not in snapshot:
                continue
            last_price, chg = snapshot[s]
            lines.append(f"• {s}: {last_price:g}  ({chg:+.2f}%)")
    elif symbols and not snapshot:
        lines.append("\nℹ️ No pude cargar precios ahora mismo (Binance).")

    lines.append(f"\n🕒 Actualizado: {_now_utc_str()}")
    kb = watchlist_keyboard(symbols)
    return "\n".join(lines), kb
