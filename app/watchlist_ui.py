# app/watchlist_ui.py
"""
UI helper para Watchlist (InlineKeyboard).
✅ No toca handlers.py
✅ Solo construye textos y teclados reutilizables.
"""

from __future__ import annotations

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from app.watchlist import format_watchlist


# Callbacks sugeridos (no rompen nada si aún no se conectan en handlers)
CB_WL_REFRESH = "wl_refresh"
CB_WL_CLEAR = "wl_clear"
CB_WL_REMOVE_PREFIX = "wl_rm:"   # wl_rm:BTCUSDT
CB_BACK_MENU = "back_menu"       # ya existe en tu bot


def watchlist_keyboard(symbols: list[str]) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []

    # Acción rápida
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
    text = format_watchlist(symbols)
    kb = watchlist_keyboard(symbols)
    return text, kb
