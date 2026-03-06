from datetime import datetime, timezone
import requests
from app.database import get_db

BINANCE_INFO_URL = "https://fapi.binance.com/fapi/v1/exchangeInfo"

collection = get_db()["watchlists"]

def now():
return datetime.now(timezone.utc)

def get_valid_symbols():
try:
r = requests.get(BINANCE_INFO_URL, timeout=10)
data = r.json()
symbols = set()

    for s in data["symbols"]:
        if s["contractType"] == "PERPETUAL":
            symbols.add(s["symbol"])

    return symbols

except:
    return set()

VALID_SYMBOLS = get_valid_symbols()

def normalize_symbol(symbol):

symbol = symbol.upper().replace(" ", "")

if not symbol.endswith("USDT"):
    symbol = f"{symbol}USDT"

return symbol

def get_watchlist(user_id):

doc = collection.find_one({"user_id": user_id})

if not doc:
    return []

return doc.get("symbols", [])

def add_symbol(user_id, symbol):

symbol = normalize_symbol(symbol)

if symbol not in VALID_SYMBOLS:

    return False, f"❌ {symbol} no es un contrato válido de Binance Futures."

collection.update_one(
    {"user_id": user_id},
    {
        "$addToSet": {"symbols": symbol},
        "$setOnInsert": {"created_at": now()}
    },
    upsert=True
)

return True, f"✅ {symbol} añadido a tu Watchlist."

def remove_symbol(user_id, symbol):

collection.update_one(
    {"user_id": user_id},
    {"$pull": {"symbols": symbol}}
)

def clear_watchlist(user_id):

collection.update_one(
    {"user_id": user_id},
    {"$set": {"symbols": []}}
)

def format_watchlist(symbols):

if not symbols:
    return "⭐ Watchlist vacía"

text = "⭐ WATCHLIST\n\n"

for s in symbols:
    text += f"{s}\n"

return text
