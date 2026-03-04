# app/statistics.py

from datetime import datetime, timedelta
from typing import Dict

from app.database import signal_results_collection


# ======================================================
# UTILIDADES DE FECHAS
# ======================================================

def _start_of_day(dt: datetime) -> datetime:
    return datetime(dt.year, dt.month, dt.day)


def _start_of_week(dt: datetime) -> datetime:
    start = dt - timedelta(days=dt.weekday())
    return datetime(start.year, start.month, start.day)


def _start_of_month(dt: datetime) -> datetime:
    return datetime(dt.year, dt.month, 1)


# ======================================================
# CÁLCULO DE ESTADÍSTICAS
# ======================================================

def _calculate_stats(from_date: datetime) -> Dict:
    results = signal_results_collection().find(
        {"evaluated_at": {"$gte": from_date}}
    )

    total = 0
    won = 0
    lost = 0
    expired = 0

    for r in results:
        total += 1
        if r["result"] == "won":
            won += 1
        elif r["result"] == "lost":
            lost += 1
        elif r["result"] == "expired":
            expired += 1

    effective_trades = won + lost
    winrate = round((won / effective_trades) * 100, 2) if effective_trades > 0 else 0.0

    return {
        "total": total,
        "won": won,
        "lost": lost,
        "expired": expired,
        "winrate": winrate,
    }


# ======================================================
# ESTADÍSTICAS PÚBLICAS
# ======================================================

def get_daily_stats() -> Dict:
    now = datetime.utcnow()
    return _calculate_stats(_start_of_day(now))


def get_weekly_stats() -> Dict:
    now = datetime.utcnow()
    return _calculate_stats(_start_of_week(now))


def get_monthly_stats() -> Dict:
    now = datetime.utcnow()
    return _calculate_stats(_start_of_month(now))
