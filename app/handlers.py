import logging
import asyncio
from datetime import datetime, date
from functools import partial
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes, CallbackQueryHandler, MessageHandler, filters

from app.database import users_collection
from app.models import is_trial_active, is_plan_active, update_timestamp
from app.plans import PLAN_FREE, PLAN_PLUS, PLAN_PREMIUM, activate_plus, activate_premium, extend_current_plan
from app.signals import get_latest_base_signal_for_plan, generate_user_signal, format_user_signal
from app.config import is_admin, get_admin_whatsapps
from app.menus import main_menu, back_to_menu, admin_menu, MENU_TEXT, ADMIN_TEXT
from app.referrals import get_user_referral_stats, get_referral_link, register_valid_ref
from app.statistics import get_last_days_stats, get_signal_activity_stats, get_winrate_by_scoreerral, check_ref_rewards

logger = logging.getLogger(__name__)

DAILY_LIMITS = {
    PLAN_FREE: 3,
    PLAN_PLUS: 5,
    PLAN_PREMIUM: 7,
}


def _effective_plan(user: dict, admin: bool) -> str:
    """Devuelve el plan efectivo (admins ven como premium)."""
    return PLAN_PREMIUM if admin else user.get("plan", PLAN_FREE)


def _has_active_access(user: dict, admin: bool) -> bool:
    """Acceso activo a funciones premium (plan o trial)."""
    return True if admin else (is_plan_active(user) or is_trial_active(user))


def _locked_message(feature: str, required: str) -> str:
    return f"🔒 {feature}\n\nDisponible en: {required}\n\nVe a 💼 Planes para activarlo."
# ======================================================
# FUNCIONES AUXILIARES
# ======================================================

def format_whatsapp_contacts():
    whatsapps = get_admin_whatsapps()
    if not whatsapps:
        return "WhatsApp: (no configurado)"
    if len(whatsapps) == 1:
        return f"WhatsApp: {whatsapps[0]}"
    return "WhatsApps:\n- " + "\n- ".join(whatsapps)

def parse_ref_code(start_param: str) -> int | None:
    """Extrae user_id del referidor desde start parameter de Telegram"""
    if not start_param:
        return None
    if start_param.startswith("ref_"):
        try:
            return int(start_param.split("_")[1])
        except ValueError:
            return None
    return None

# ======================================================
# HANDLER /start (inserta referidos)
# ======================================================

async def handle_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Maneja /start y captura referidos"""
    try:
        user_id = update.effective_user.id
        start_param = context.args[0] if context.args else None
        referrer_id = parse_ref_code(start_param)

        users_col = users_collection()
        user = users_col.find_one({"user_id": user_id})

        if not user:
            doc = {"user_id": user_id, "plan": PLAN_FREE, "ref_plus_valid": 0,
                   "ref_premium_valid": 0, "ref_plus_total": 0, "ref_premium_total": 0}
            if referrer_id and referrer_id != user_id:
                doc["referred_by"] = referrer_id
            users_col.insert_one(doc)
        else:
            if referrer_id and referrer_id != user_id and "referred_by" not in user:
                users_col.update_one({"user_id": user_id}, {"$set": {"referred_by": referrer_id}})

        await update.message.reply_text(
            MENU_TEXT,
            reply_markup=main_menu(is_admin=is_admin(user_id)),
        )

    except Exception as e:
        logger.error(f"Error en handle_start: {e}", exc_info=True)
        await update.message.reply_text("❌ Error al iniciar el bot.")

# ======================================================
# HANDLER MENÚ PRINCIPAL
# ======================================================

async def handle_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query is None:
        return
    await query.answer()

    try:
        user_id = query.from_user.id
        users_col = users_collection()
        user = users_col.find_one({"user_id": user_id})

        if not user:
            await query.edit_message_text(
                "Usuario no encontrado. Usa /start nuevamente.",
                reply_markup=main_menu(is_admin=is_admin(user_id)),
            )
            return

        action = query.data
        admin = is_admin(user_id)

        if action == "admin_panel" and admin:
            await query.edit_message_text(
                ADMIN_TEXT,
                reply_markup=admin_menu()
            )
            return

        if action in {"admin_activate_plus", "admin_activate_premium", "admin_extend_plan"} and admin:
            # Pedimos el user_id del usuario objetivo, y guardamos qué acción quiere hacer el admin.
            context.user_data["awaiting_user_id"] = True
            context.user_data["admin_action"] = action
            await query.edit_message_text("🆔 Envía el User ID del usuario a gestionar:")
            return

        if action == "admin_stats" and admin:
            await query.edit_message_text(
                "📊 Estadísticas (próximamente).",
                reply_markup=back_to_menu(),
            )
            return

        if action == "view_signals":
            await handle_view_signals(query, user, admin, users_col)
            return
        if action == "radar":
            await handle_radar(query, user, admin)
            return

        if action == "performance":
            await handle_performance(query, user, admin)
            return

        if action == "movers":
            await handle_movers(query, user, admin)
            return

        if action == "market":
            await handle_market(query, user, admin)
            return

        if action == "watchlist":
            await handle_watchlist(query, user, admin)
            return

        if action == "alerts":
            await handle_alerts(query, user, admin)
            return

        if action == "history":
            await handle_history(query, user, admin)
            return


        if action == "plans":
            await handle_plans(query)
            return

        if action == "my_account":
            await handle_my_account(query, user, admin)
            return

        if action == "referrals":
            await handle_referrals(query, user)
            return

        if action == "support":
            await handle_support(query)
            return

        if action == "back_menu":
            await query.edit_message_text(
                MENU_TEXT,
                reply_markup=main_menu(is_admin=is_admin(user_id)),
            )
            return

    except Exception as e:
        logger.error(f"Error en handle_menu: {e}", exc_info=True)
        await query.edit_message_text(
            "❌ Ocurrió un error inesperado.",
            reply_markup=main_menu(is_admin=is_admin(user_id)),
        )

# ======================================================
# HANDLER REFERRALS
# ======================================================

async def handle_referrals(query, user):
    try:
        user_id = user["user_id"]
        stats = get_user_referral_stats(user_id)
        if not stats:
            await query.edit_message_text(
                "❌ No se pudo cargar la información de referidos.",
                reply_markup=back_to_menu(),
            )
            return

        ref_link = get_referral_link(user_id)

        message = "👥 SISTEMA DE REFERIDOS\n\n"
        message += f"🔗 Tu enlace de referido:\n{ref_link}\n\n"

        message += "📊 ESTADÍSTICAS:\n"
        message += f"• Total referidos: {stats['total_referred']}\n"
        message += f"• Referidos PLUS: {stats['plus_referred']}\n"
        message += f"• Referidos PREMIUM: {stats['premium_referred']}\n\n"

        message += "🎯 CONTADORES ACTUALES:\n"
        message += f"• PLUS válidos: {stats['current_plus']}/5 → Ganancia: {stats['current_plus']*2} USDT\n"
        message += f"• PREMIUM válidos: {stats['current_premium']}/5 → Ganancia: {stats['current_premium']*4} USDT\n\n"

        if stats["pending_rewards"]:
            message += "✨ RECOMPENSAS PENDIENTES:\n"
            for reward in stats["pending_rewards"]:
                message += f"• {reward}\n"
            message += "\n"
        else:
            message += "📝 No tienes recompensas pendientes\n\n"

        message += "📢 CÓMO REFERIR:\n1. Comparte tu enlace\n2. Ellos entran al bot\n3. Activan un plan\n\n"
        message += "📌 REGLAS:\n"
        message += "• FREE: 5 PLUS = Plan PLUS gratis\n"
        message += "• FREE: 5 PREMIUM = Plan PREMIUM gratis\n"
        message += "• PLUS: 5 PLUS = Extender plan\n"
        message += "• PLUS: 5 PREMIUM = Subir a PREMIUM\n"
        message += "• PREMIUM: 5 PREMIUM = Extender plan\n"
        message += "• PREMIUM: 10 PLUS = Extender plan\n"

        keyboard = [
            [InlineKeyboardButton("📋 Copiar enlace", callback_data="copy_ref_code")],
            [InlineKeyboardButton("⬅️ Volver al menú", callback_data="back_menu")]
        ]

        await query.edit_message_text(
            text=message,
            reply_markup=InlineKeyboardMarkup(keyboard),
        )

    except Exception as e:
        logger.error(f"Error en handle_referrals: {e}", exc_info=True)
        await query.edit_message_text(
            "❌ Error al cargar información de referidos.",
            reply_markup=back_to_menu(),
                   )

  # ======================================================
# HANDLER COPIAR ENLACE DE REFERIDO
# ======================================================

async def handle_copy_ref_code(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    try:
        user_id = query.from_user.id
        ref_link = get_referral_link(user_id)

        await query.edit_message_text(
            text=(
                f"📋 Tu enlace de referido es:\n\n{ref_link}\n\nCópialo y compártelo."
            ),
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("⬅️ Volver a referidos", callback_data="referrals")],
                [InlineKeyboardButton("🏠 Menú principal", callback_data="back_menu")]
            ]),
        )

    except Exception as e:
        logger.error(f"Error en handle_copy_ref_code: {e}", exc_info=True)
        await update.message.reply_text(
            "❌ Error al copiar enlace.",
            reply_markup=main_menu(is_admin=is_admin(user_id)),
        )

# ======================================================
# HANDLER DE MENSAJES DE TEXTO COMBINADO
# ======================================================

async def handle_text_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Maneja todos los mensajes de texto, decidiendo el flujo correcto"""
    
    if context.user_data.get("awaiting_user_id"):
        await handle_admin_text_input(update, context)
        return

# ======================================================
# HANDLER VIEW SIGNALS (CORREGIDO SIN LÍMITE)
# ======================================================

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

        base_signals = get_latest_base_signal_for_plan(user_id, plan)
        if not base_signals:
            await query.edit_message_text(
                "📭 No hay señales disponibles.",
                reply_markup=back_to_menu(),
            )
            return

        for base_signal in base_signals:
            user_signal = generate_user_signal(base_signal, user_id)
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

# ======================================================
# HANDLER PLANS
# ======================================================

async def handle_plans(query):
    await query.edit_message_text(
        "💼 PLANES DISPONIBLES\n\n"
        "🟢 FREE – todas las señales del plan\n"
        "🟡 PLUS – todas las señales del plan\n"
        "🔴 PREMIUM – todas las señales del plan\n\n"
        f"{format_whatsapp_contacts()}",
        reply_markup=back_to_menu(),
    )

# ======================================================
# HANDLER MY ACCOUNT
# ======================================================

async def handle_my_account(query, user, admin=False):
    plan = PLAN_PREMIUM if admin else user.get("plan", PLAN_FREE)
    message = (
        f"👤 MI CUENTA\n\n"
        f"ID: {user['user_id']}\n"
        f"Plan: {plan}\n"
    )

    if admin:
        message += "\n👑 PANEL ADMINISTRADOR\n"
        keyboard = [
            [InlineKeyboardButton("➕ Activar plan", callback_data="admin_activate_plan")],
            [InlineKeyboardButton("⬅️ Volver", callback_data="back_menu")],
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
    else:
        reply_markup = back_to_menu()

    await query.edit_message_text(
        text=message,
        reply_markup=reply_markup,
    )

# ======================================================
# HANDLER SUPPORT
# ======================================================

async def handle_support(query):
    await query.edit_message_text(
        f"📩 SOPORTE\n\n{format_whatsapp_contacts()}",
        reply_markup=back_to_menu(),
    )

# ======================================================
# HANDLER ADMIN TEXT INPUT
# ======================================================


async def handle_admin_text_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Recibe el user_id objetivo para acciones de admin (activar/extend)."""
    try:
        target_user_id_str = (update.message.text or "").strip()

        try:
            target_user_id = int(target_user_id_str)
        except ValueError:
            await update.message.reply_text("❌ ID inválido. Debe ser un número.")
            context.user_data["awaiting_user_id"] = False
            context.user_data.pop("admin_action", None)
            return

        if not is_admin(update.effective_user.id):
            await update.message.reply_text("❌ No tienes permisos de administrador.")
            context.user_data["awaiting_user_id"] = False
            context.user_data.pop("admin_action", None)
            return

        users_col = users_collection()
        loop = asyncio.get_event_loop()
        target_user = await loop.run_in_executor(None, lambda: users_col.find_one({"user_id": target_user_id}))

        if not target_user:
            await update.message.reply_text("❌ Usuario no encontrado en la base de datos.")
            context.user_data["awaiting_user_id"] = False
            context.user_data.pop("admin_action", None)
            return

        action = context.user_data.get("admin_action")
        context.user_data["awaiting_user_id"] = False

        # 1) Activar PLUS / PREMIUM
        if action == "admin_activate_plus":
            success = await loop.run_in_executor(None, partial(activate_plus, target_user_id))
            if success:
                # Si el usuario fue referido, esto marca el referido como válido (porque hubo compra/activación real)
                register_valid_referral(target_user_id, "PLUS")
                await update.message.reply_text(f"✅ PLAN PLUS activado para {target_user_id}.", reply_markup=back_to_menu())
            else:
                await update.message.reply_text("❌ No se pudo activar PLUS (¿usuario ya tenía plan activo?).", reply_markup=back_to_menu())

        elif action == "admin_activate_premium":
            success = await loop.run_in_executor(None, partial(activate_premium, target_user_id))
            if success:
                register_valid_referral(target_user_id, "PREMIUM")
                await update.message.reply_text(f"✅ PLAN PREMIUM activado para {target_user_id}.", reply_markup=back_to_menu())
            else:
                await update.message.reply_text("❌ No se pudo activar PREMIUM (¿usuario ya tenía plan activo?).", reply_markup=back_to_menu())

        # 2) Extender plan
        elif action == "admin_extend_plan":
            context.user_data["target_user_id"] = target_user_id
            keyboard = [
                [InlineKeyboardButton("➕ +7 días", callback_data="extend_7")],
                [InlineKeyboardButton("➕ +30 días", callback_data="extend_30")],
                [InlineKeyboardButton("❌ Cancelar", callback_data="back_menu")],
            ]
            await update.message.reply_text(
                f"⏳ ¿Cuántos días deseas extender el plan de {target_user_id}?",
                reply_markup=InlineKeyboardMarkup(keyboard),
            )

        else:
            await update.message.reply_text("⚠️ Acción admin no válida.", reply_markup=back_to_menu())

        context.user_data.pop("admin_action", None)

    except Exception as e:
        logger.error(f"Error en handle_admin_text_input: {e}", exc_info=True)
        context.user_data["awaiting_user_id"] = False
        context.user_data.pop("admin_action", None)
        await update.message.reply_text("❌ Error procesando la solicitud de admin.", reply_markup=back_to_menu())

# ======================================================
# REGISTRO DE HANDLERS
# ======================================================

def get_handlers():
    return [
        CallbackQueryHandler(
            handle_menu,
            pattern="^(view_signals|plans|my_account|referrals|support|admin_panel|admin_activate_plan|register_exchange|back_menu|choose_plus_plan|choose_premium_plan)$"
        ),
        CallbackQueryHandler(handle_copy_ref_code, pattern="^copy_ref_code$"),
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_messages),
          ]
# ======================================================
# NUEVAS SECCIONES (UI PRO) - por ahora placeholders
# ======================================================

async def handle_radar(query, user, admin):
    if not _has_active_access(user, admin):
        await query.edit_message_text("⛔ Acceso expirado.", reply_markup=back_to_menu())
        return
    plan = _effective_plan(user, admin)
    if plan == PLAN_FREE:
        await query.edit_message_text(_locked_message("📡 Radar Futures", "PLUS / PREMIUM"), reply_markup=back_to_menu())
        return
    await query.edit_message_text("📡 Radar Futures\n\n✅ Próximamente (implementación en curso).", reply_markup=back_to_menu())


async def handle_performance(query, user, admin):
    # Visible para PLUS/PREMIUM (FREE: bloqueado)
    if not _has_active_access(user):
        await query.edit_message_text(
            "🔒 **Rendimiento**\n\nDisponible en **PLUS** y **PREMIUM**.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Volver", callback_data="menu:home")]]),
        )
        return

    plan = (user.get("plan") or "free").lower()

    # Stats (basadas en signal_results) + actividad (basada en signals)
    stats_7d = get_last_days_stats(7)
    stats_30d = get_last_days_stats(30)
    activity_7d = get_signal_activity_stats(7)
    activity_30d = get_signal_activity_stats(30)
    score_7d = get_winrate_by_score(7)
    score_30d = get_winrate_by_score(30)

    def _fmt_block(title: str, s: dict, a: dict) -> str:
        lines = [f"**{title}**"]
        # Resultados evaluados
        if s.get("total", 0) > 0:
            lines.append(
                f"• Evaluadas: {s['total']}  | ✅ {s['won']}  | ❌ {s['lost']}  | ⏳ {s['expired']}"
            )
            lines.append(f"• Win rate (sin expiradas): {s['winrate']}%")
        else:
            lines.append("• Evaluadas: 0 (aún no hay resultados evaluados)")
        # Actividad de señales
        lines.append(
            f"• Señales generadas: {a.get('signals_total', 0)} | Score prom.: {a.get('avg_score', '—')}"
        )
        return "\n".join(lines)

    def _fmt_score(title: str, d: dict) -> str:
        if not d.get("buckets"):
            return f"**{title}**\n• (sin datos todavía)"
        out = [f"**{title}**"]
        for b in d["buckets"]:
            out.append(f"• {b['label']}: {b['winrate']}%  (n={b['n']})")
        return "\n".join(out)

    msg = "🎯 **RENDIMIENTO DEL BOT**\n"
    msg += f"Plan: **{plan.upper()}**\n\n"
    msg += _fmt_block("Últimos 7 días", stats_7d, activity_7d) + "\n\n"
    msg += _fmt_block("Últimos 30 días", stats_30d, activity_30d) + "\n\n"
    msg += _fmt_score("Win rate por score (7 días)", score_7d) + "\n\n"
    msg += _fmt_score("Win rate por score (30 días)", score_30d)

    await query.edit_message_text(
        msg,
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Volver", callback_data="menu:home")]]),
    )

async def handle_movers(query, user, admin):
    # Esta sección será visible para todos (FREE también)
    await query.edit_message_text("🔥 Movers del mercado\n\n✅ Próximamente (top gainers/losers, volumen 24h).", reply_markup=back_to_menu())


async def handle_market(query, user, admin):
    await query.edit_message_text("📊 Estado del mercado\n\n✅ Próximamente (tendencia BTC, funding promedio, volatilidad).", reply_markup=back_to_menu())


async def handle_watchlist(query, user, admin):
    if not _has_active_access(user, admin):
        await query.edit_message_text("⛔ Acceso expirado.", reply_markup=back_to_menu())
        return
    plan = _effective_plan(user, admin)
    if plan == PLAN_FREE:
        await query.edit_message_text(_locked_message("⭐ Watchlist", "PLUS / PREMIUM"), reply_markup=back_to_menu())
        return
    await query.edit_message_text("⭐ Watchlist\n\n✅ Próximamente (añadir/quitar pares + alertas).", reply_markup=back_to_menu())


async def handle_alerts(query, user, admin):
    if not _has_active_access(user, admin):
        await query.edit_message_text("⛔ Acceso expirado.", reply_markup=back_to_menu())
        return
    plan = _effective_plan(user, admin)
    if plan == PLAN_FREE:
        await query.edit_message_text(_locked_message("🔔 Alertas inteligentes", "PLUS / PREMIUM"), reply_markup=back_to_menu())
        return
    await query.edit_message_text("🔔 Alertas inteligentes\n\n✅ Próximamente (rupturas, volumen, funding extremo).", reply_markup=back_to_menu())


async def handle_history(query, user, admin):
    if not _has_active_access(user, admin):
        await query.edit_message_text("⛔ Acceso expirado.", reply_markup=back_to_menu())
        return
    plan = _effective_plan(user, admin)
    if plan == PLAN_FREE:
        await query.edit_message_text(_locked_message("🧾 Historial de señales", "PLUS / PREMIUM"), reply_markup=back_to_menu())
        return
    await query.edit_message_text("🧾 Historial de señales\n\n✅ Próximamente (últimas señales + resultados).", reply_markup=back_to_menu())

