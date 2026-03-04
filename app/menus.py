# app/menus.py
from telegram import InlineKeyboardButton, InlineKeyboardMarkup


def main_menu():
    """
    Menú principal del bot.
    """
    keyboard = [
        [InlineKeyboardButton("📊 Ver señales", callback_data="view_signals")],
        [
            InlineKeyboardButton("💼 Planes", callback_data="plans"),
            InlineKeyboardButton("👤 Mi cuenta", callback_data="my_account"),
        ],
        [
            InlineKeyboardButton("👥 Referidos", callback_data="referrals"),
            InlineKeyboardButton("🌐 Registrar exchange", callback_data="register_exchange"),
        ],
        [
            InlineKeyboardButton("📩 Soporte", callback_data="support"),
        ],
    ]
    return InlineKeyboardMarkup(keyboard)


def back_to_menu():
    """
    Botón para volver al menú principal.
    """
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("⬅️ Volver al menú", callback_data="back_menu")]]
    )


def admin_menu():
    """
    Menú de administrador.
    """
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Activar plan PREMIUM", callback_data="admin_activate_plan")],
        [InlineKeyboardButton("📊 Estadísticas", callback_data="admin_stats")],
        [InlineKeyboardButton("⬅️ Volver", callback_data="back_menu")],
    ])
