from datetime import datetime, timezone
import requests
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from app.watchlist import format_watchlist

CB_WL_REFRESH = "wl_refresh"
CB_WL_CLEAR = "wl_clear"
CB_WL_REMOVE_PREFIX = "wl_rm:"
CB_BACK_MENU = "back_menu"

BINANCE_FUTURES_24H = "https://fapi.binance.com/fapi/v1/ticker/24hr"

def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

def _safe_float(v, default=0.0):
    try:
        return float(v)
    except:
        return default

def fetch_watchlist_snapshot(symbols):
    data = {}

    for s in symbols:
        try:
            r = requests.get(BINANCE_FUTURES_24H, params={"symbol": s}, timeout=8)

            if r.status_code != 200:
                continue

            j = r.json()

            price = _safe_float(j.get("lastPrice"))
            change = _safe_float(j.get("priceChangePercent"))
            volume = _safe_float(j.get("quoteVolume"))

            data[s] = (price, change, volume)

        except:
            continue

    return data


def watchlist_keyboard(symbols):
    rows = []

    rows.append([
        InlineKeyboardButton("🔄 Actualizar", callback_data=CB_WL_REFRESH),
        InlineKeyboardButton("🧹 Limpiar", callback_data=CB_WL_CLEAR)
    ])

    for s in symbols[:6]:
        rows.append([
            InlineKeyboardButton(f"❌ Quitar {s}", callback_data=f"{CB_WL_REMOVE_PREFIX}{s}")
        ])

    rows.append([
        InlineKeyboardButton("⬅️ Volver", callback_data=CB_BACK_MENU)
    ])

    return InlineKeyboardMarkup(rows)


def render_watchlist_view(symbols):

    base = format_watchlist(symbols)
    snapshot = fetch_watchlist_snapshot(symbols)

    lines = [base]

    if snapshot:

        lines.append("\n📊 Mercado (Futures):")

        for s,(price,chg,vol) in snapshot.items():

            lines.append(
                f"• {s}: {price:g} ({chg:+.2f}%) | Vol: {vol:,.0f}"
            )

    else:
        lines.append("\nℹ️ No pude cargar datos de mercado.")

    lines.append(f"\n🕒 Actualizado: {_now()}")

    kb = watchlist_keyboard(symbols)

    return "\n".join(lines), kb
