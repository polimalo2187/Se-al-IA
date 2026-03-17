# app/handlers.py

import logging
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from app.ui import back_to_menu
from app.plans import PLAN_FREE, PLAN_PLUS, PLAN_PREMIUM
from app.signals import get_latest_base_signal_for_plan, format_user_signal
from app.auth import is_plan_active, is_trial_active
from app.utils import update_timestamp

logger = logging.getLogger(__name__)


async def handle_view_signals(query, user, admin, users_col):
    try:
        user_id = user["user_id"]
        plan = PLAN_PREMIUM if admin else user.get("plan", PLAN_FREE)

        if not admin and not (is_plan_active(user) or is_trial_active(user)):
            await query.edit_message_text(
                "⛔ Acceso expirado.",
                reply_markup=back_to_menu(),
            )
            return

        user_signals = get_latest_base_signal_for_plan(user_id, plan)

        if not user_signals:
            await query.edit_message_text(
                "📭 No hay señales disponibles.",
                reply_markup=back_to_menu(),
            )
            return

        user_signal = user_signals[0]

        await query.edit_message_text(
            format_user_signal(user_signal),
            reply_markup=back_to_menu(),
        )

        users_col.update_one(
            {"user_id": user_id},
            {"$set": update_timestamp(user)}
        )

    except Exception as e:
        logger.error(f"Error en handle_view_signals: {e}", exc_info=True)
        await query.edit_message_text(
            "❌ Error al obtener señales.",
            reply_markup=back_to_menu(),
        )
