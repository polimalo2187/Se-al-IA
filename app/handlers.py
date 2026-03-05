import logging
import asyncio
from datetime import datetime, date
from functools import partial
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes, CallbackQueryHandler, MessageHandler, filters

from app.database import users_collection
from app.models import is_trial_active, is_plan_active, update_timestamp
from app.binance_api import get_top_movers_usdtm, get_radar_opportunities, get_premium_index, get_open_interest
from app.plans import PLAN_FREE, PLAN_PLUS, PLAN_PREMIUM, activate_plus, activate_premium, extend_current_plan
from app.signals import get_latest_base_signal_for_plan, generate_user_signal, format_user_signal
from app.config import is_admin, get_admin_whatsapps
from app.menus import main_menu, back_to_menu
from app.referrals import get_user_referral_stats, get_referral_link, register_valid_referral, check_ref_rewards

try:
    from app.statistics import get_last_days_stats, get_signal_activity_stats, get_winrate_by_score
except ImportError:  # compat
    get_last_days_stats = None
    get_signal_activity_stats = None
    get_winrate_by_score = None


logger = logging.getLogger(__name__)

DAILY_LIMITS = {
    PLAN_FREE: 3,
    PLAN_PLUS: 5,
    PLAN_PREMIUM: 7,
}

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
            "👋 Bienvenido al bot de señales.\nMenú principal:",
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
                reply_markup=main_menu(),
            )
            return

        action = query.data
        admin = is_admin(user_id)

        if action == "admin_panel" and admin:
            await query.edit_message_text(
                "👑 PANEL ADMINISTRADOR",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("➕ Activar plan", callback_data="admin_activate_plan")],
                    [InlineKeyboardButton("⬅️ Volver", callback_data="back_menu")],
                ])
            )
            return

        if action == "admin_activate_plan" and admin:
            context.user_data["awaiting_user_id"] = True
            await query.edit_message_text("🆔 Envía el User ID del usuario:")
            return

        if action == "view_signals":
            await handle_view_signals(query, user, admin, users_col)
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


        # Nuevos módulos (Menú PRO)
        if action == "performance":
            await handle_performance(query, user)
            return

        if action == "radar":
            await handle_radar(query, user, plan)
            return

        if action == "radar_refresh":
            await handle_radar(query, user, plan)
            return

        if action == "movers":
            await handle_movers(query, user)
            return

        if action == "market":
            await handle_locked_or_soon(query, user, feature="📊 Mercado", required_plan=PLAN_FREE)
            return

        if action == "watchlist":
            await handle_locked_or_soon(query, user, feature="⭐ Watchlist", required_plan=PLAN_PLUS)
            return

        if action == "alerts":
            await handle_locked_or_soon(query, user, feature="🔔 Alertas", required_plan=PLAN_PLUS)
            return

        if action == "history":
            await handle_locked_or_soon(query, user, feature="🧾 Historial", required_plan=PLAN_PLUS)
            return
        if action == "support":
            await handle_support(query)
            return

        if action == "register_exchange":
            context.user_data["awaiting_exchange"] = True
            await query.edit_message_text(
                "🌐 Envía el nombre de tu exchange (ej: Binance, CoinEx, KuCoin):"
            )
            return

        if action == "back_menu":
            await query.edit_message_text(
"🏠 MENÚ PRINCIPAL — Selecciona una opción abajo",
                reply_markup=main_menu(is_admin=admin),
            )
            return

        if action in ["choose_plus_plan", "choose_premium_plan"]:
            target_user_id = context.user_data.get("target_user_id")
            if target_user_id:
                loop = asyncio.get_event_loop()
                if action == "choose_plus_plan":
                    success = await loop.run_in_executor(
                        None,
                        partial(activate_plus, target_user_id)
                    )
                    plan_name = "PLUS"
                else:
                    success = await loop.run_in_executor(
                        None,
                        partial(activate_premium, target_user_id)
                    )
                    plan_name = "PREMIUM"

                if success:
                    register_valid_referral(target_user_id, plan_name)
                    await query.edit_message_text(f"✅ Plan {plan_name} activado correctamente.")
                else:
                    await query.edit_message_text(f"❌ No se pudo activar el plan {plan_name}.")

                context.user_data.pop("awaiting_plan_choice", None)
                context.user_data.pop("target_user_id", None)
            return

    except Exception as e:
        logger.error(f"Error en handle_menu: {e}", exc_info=True)
        await query.edit_message_text(
            "❌ Ocurrió un error inesperado.",
            reply_markup=main_menu(),
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
            reply_markup=main_menu(),
        )

# ======================================================
# HANDLER DE MENSAJES DE TEXTO COMBINADO
# ======================================================

async def handle_text_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Maneja todos los mensajes de texto, decidiendo el flujo correcto"""
    
    if context.user_data.get("awaiting_user_id"):
        await handle_admin_text_input(update, context)
        return
    
    if context.user_data.get("awaiting_exchange"):
        await handle_exchange_text_input(update, context)
        return

# ======================================================
# HANDLER REGISTRAR EXCHANGE (MENSAJE CONFIRMACIÓN)
# ======================================================

async def handle_exchange_text_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        context.user_data["awaiting_exchange"] = False
        exchange_name = update.message.text.strip()
        users_col = users_collection()
        user_id = update.effective_user.id
        
        loop = asyncio.get_event_loop()
        user = await loop.run_in_executor(
            None,
            lambda: users_col.find_one({"user_id": user_id})
        )

        if not user:
            await update.message.reply_text("❌ Usuario no encontrado.")
            return

        await loop.run_in_executor(
            None,
            lambda: users_col.update_one(
                {"user_id": user_id},
                {"$set": {"exchange": exchange_name}}
            )
        )

        await update.message.reply_text(
            f"✅ Exchange confirmado: {exchange_name}\nMenú principal:",
            reply_markup=main_menu(),
        )

    except Exception as e:
        logger.error(f"Error en handle_exchange_text: {e}", exc_info=True)
        await update.message.reply_text("❌ Error al registrar exchange.")
        context.user_data["awaiting_exchange"] = False

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


# ======================================================
# MENÚ PRO - MÓDULOS NUEVOS
# ======================================================

def _plan_rank(plan: str) -> int:
    if plan == PLAN_PREMIUM:
        return 3
    if plan == PLAN_PLUS:
        return 2
    return 1

async def handle_locked_or_soon(query, user, feature: str, required_plan: str):
    """Muestra mensaje de bloqueado por plan o "próximamente" si aún no está implementado."""
    plan = user.get("plan", PLAN_FREE)

    # Bloqueo por plan (si requiere PLUS/PREMIUM)
    if _plan_rank(plan) < _plan_rank(required_plan):
        await query.edit_message_text(
            f"🔒 {feature}\n\nDisponible en plan {required_plan.upper()}.\n\nPulsa *Planes* para activar tu acceso.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("💼 Planes", callback_data="plans")],
                [InlineKeyboardButton("⬅️ Volver", callback_data="back_menu")],
            ]),
            parse_mode="Markdown",
        )
        return

    # Si el plan permite, pero aún no está implementado
    await query.edit_message_text(
        f"🚧 {feature}\n\nEsta función está en desarrollo y se activará muy pronto.",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("⬅️ Volver", callback_data="back_menu")],
        ]),
    )

async def handle_performance(query, user):
    plan = user.get("plan", PLAN_FREE)
    if _plan_rank(plan) < _plan_rank(PLAN_PLUS):
        await query.edit_message_text(
            "🔒 🎯 Rendimiento\n\nDisponible para *PLUS* y *PREMIUM*.\n\nActiva tu plan para ver estadísticas reales del bot.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("💼 Planes", callback_data="plans")],
                [InlineKeyboardButton("⬅️ Volver", callback_data="back_menu")],
            ]),
            parse_mode="Markdown",
        )
        return

    # Stats (compatibilidad: si no existe el módulo nuevo, usamos weekly/monthly)
    s7 = s30 = act7 = act30 = None
    by_score_30 = None

    if callable(get_last_days_stats):
        s7 = get_last_days_stats(7)
        s30 = get_last_days_stats(30)
    else:
        from app.statistics import get_weekly_stats, get_monthly_stats
        s7 = get_weekly_stats()
        s30 = get_monthly_stats()

    if callable(get_signal_activity_stats):
        act7 = get_signal_activity_stats(7)
        act30 = get_signal_activity_stats(30)

    if callable(get_winrate_by_score):
        try:
            by_score_30 = get_winrate_by_score(30)
        except Exception:
            by_score_30 = None

    def _fmt_stats(label: str, s: dict) -> str:
        total = s.get("total", 0)
        won = s.get("won", 0)
        lost = s.get("lost", 0)
        expired = s.get("expired", 0)
        winrate = s.get("winrate", 0.0)
        return (
            f"**{label}**\n"
            f"• Evaluadas: {total}\n"
            f"• Ganadas: {won} | Perdidas: {lost} | Expiradas: {expired}\n"
            f"• Win rate: {winrate}%\n"
        )

    parts = ["🎯 **RENDIMIENTO DEL BOT**\n"]
    parts.append(_fmt_stats("Últimos 7 días", s7 or {}))
    parts.append(_fmt_stats("Últimos 30 días", s30 or {}))

    if act7 or act30:
        parts.append("📈 **Actividad de señales (scanner)**")
        if act7:
            parts.append(f"• 7D: {act7.get('signals_total', 0)} señales | Score prom: {act7.get('avg_score', '—')}")
        if act30:
            parts.append(f"• 30D: {act30.get('signals_total', 0)} señales | Score prom: {act30.get('avg_score', '—')}")

    if by_score_30:
        parts.append("\n🏷️ **Win rate por score (30D)**")
        for k,v in by_score_30.items():
            parts.append(f"• {k}: {v}%")

    parts.append("\n⬅️ Usa *Volver* para regresar al menú.")

    await query.edit_message_text(
        "\n".join(parts),
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("⬅️ Volver", callback_data="back_menu")],
        ]),
        parse_mode="Markdown",
    )

async def handle_movers(query, user):
    """Muestra Top Movers 24h de Binance USDT-M Futures."""
    # Movers es info pública: disponible para todos (incluye free)
    try:
        movers = get_top_movers_usdtm(limit=10)
    except Exception as e:
        logger.exception("Error obteniendo movers de Binance: %s", e)
        await query.edit_message_text(
            "⚠️ No pude obtener los movers ahora mismo. Intenta de nuevo en unos segundos.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Volver", callback_data="back_menu")]]),
        )
        return

    if not movers:
        await query.edit_message_text(
            "⚠️ No hay datos disponibles ahora mismo. Intenta de nuevo.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Volver", callback_data="back_menu")]]),
        )
        return

    lines = ["🔥 TOP MOVERS FUTURES (24h)", "", "*Binance USDT-M*", ""]

    for i, m in enumerate(movers, start=1):
        symbol = str(m.get("symbol", ""))
        try:
            change = float(m.get("priceChangePercent", 0.0))
        except Exception:
            change = 0.0
        try:
            qv = float(m.get("quoteVolume", 0.0))
        except Exception:
            qv = 0.0
        try:
            last = float(m.get("lastPrice", 0.0))
        except Exception:
            last = 0.0

        sign = "+" if change >= 0 else ""
        lines.append(f"{i}. *{symbol}*  —  {sign}{change:.2f}%")
        if last > 0:
            lines.append(f"   Precio: `{last}`")
        if qv > 0:
            lines.append(f"   Volumen (USDT): `{qv:,.0f}`")
        lines.append("")

    text_out = "\n".join(lines).strip()

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 Actualizar", callback_data="movers")],
        [InlineKeyboardButton("⬅️ Volver", callback_data="back_menu")],
    ])

    await query.edit_message_text(text_out, reply_markup=keyboard, parse_mode="Markdown")


async def handle_radar(query, user, plan: str):
    # Radar es PLUS/PREMIUM
    # PLUS: radar básico
    # PREMIUM: radar con funding + open interest para top oportunidades
    try:
        opportunities = get_radar_opportunities(limit=8)
    except Exception as e:
        logging.exception("Radar error")
        text = "📡 RADAR FUTURES\n\n❌ No pude cargar el radar ahora mismo. Intenta de nuevo."
        # Si eres admin, muestra un código corto del error
        if user and is_admin(user.get('user_id', 0)):
            text += f"\n\n(Admin) Detalle: {type(e).__name__}"
        keyboard = [[InlineKeyboardButton("⬅️ Volver", callback_data="back_menu")]]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
        return

    if not opportunities:
        text = "📡 RADAR FUTURES\n\nNo pude cargar oportunidades ahora mismo. Intenta de nuevo."
        keyboard = [[InlineKeyboardButton("⬅️ Volver", callback_data="back_menu")]]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
        return

    lines = ["📡 RADAR FUTURES", "", "Top oportunidades (USDT-M):", ""]

    for idx, o in enumerate(opportunities, start=1):
        sym = o["symbol"]
        score = o["score"]
        change = o["change_pct"]
        vol = o["quote_volume"]
        trades = o["trades"]
        direction = o["direction"]

        # formato compacto y claro
        lines.append(f"{idx}️⃣ {sym} — {direction}")
        lines.append(f"Score: {score} | 24h: {change:+.2f}%")
        lines.append(f"Volumen 24h: ${vol:,.0f} | Trades: {trades:,.0f}")

        if plan == PLAN_PREMIUM:
            # Enriquecemos con 2 llamadas por símbolo (cached)
            try:
                pi = get_premium_index(sym)
                fr = float(pi.get("lastFundingRate", 0.0))
            except Exception:
                fr = 0.0
            try:
                oi = get_open_interest(sym)
                oi_val = float(oi.get("openInterest", 0.0))
            except Exception:
                oi_val = 0.0

            lines.append(f"Funding: {fr:+.4f} | Open Interest: {oi_val:,.0f}")

        lines.append("")

    # Botones
    keyboard = [
        [
            InlineKeyboardButton("🔄 Actualizar", callback_data="radar_refresh"),
            InlineKeyboardButton("⬅️ Volver", callback_data="back_menu"),
        ]
    ]
    await query.edit_message_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(keyboard))
async def handle_admin_text_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        target_user_id_str = update.message.text.strip()
        logger.info(f"[ADMIN] Recibido User ID: {target_user_id_str}")
        
        try:
            target_user_id = int(target_user_id_str)
        except ValueError:
            await update.message.reply_text("❌ ID inválido. Debe ser un número.")
            context.user_data["awaiting_user_id"] = False
            return

        if not is_admin(update.effective_user.id):
            await update.message.reply_text("❌ Permisos revocados.")
            context.user_data["awaiting_user_id"] = False
            return

        users_col = users_collection()
        loop = asyncio.get_event_loop()
        
        target_user = await loop.run_in_executor(
            None,
            lambda: users_col.find_one({"user_id": target_user_id})
        )
        
        if not target_user:
            await update.message.reply_text("❌ Usuario no encontrado en la base de datos.")
            context.user_data["awaiting_user_id"] = False
            return

        context.user_data["awaiting_user_id"] = False
        context.user_data["awaiting_plan_choice"] = True
        context.user_data["target_user_id"] = target_user_id

        keyboard = [
            [InlineKeyboardButton("🟡 Activar PLAN PLUS", callback_data="choose_plus_plan")],
            [InlineKeyboardButton("🔴 Activar PLAN PREMIUM", callback_data="choose_premium_plan")],
            [InlineKeyboardButton("❌ Cancelar", callback_data="back_menu")]
        ]

        await update.message.reply_text(
            f"✅ Usuario encontrado: {target_user_id}\nSeleccione el plan a activar:",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )

    except Exception as e:
        logger.error(f"[ADMIN] Error en handle_admin_text: {e}", exc_info=True)
        await update.message.reply_text("❌ Error procesando la solicitud.")
        context.user_data["awaiting_user_id"] = False

# ======================================================
# REGISTRO DE HANDLERS
# ======================================================

def get_handlers():
    return [
        CallbackQueryHandler(
            handle_menu,
            pattern="^(view_signals|radar|radar_refresh|performance|movers|market|watchlist|alerts|history|plans|my_account|referrals|support|admin_panel|admin_activate_plan|back_menu|choose_plus_plan|choose_premium_plan|register_exchange)$"
        ),
        CallbackQueryHandler(handle_copy_ref_code, pattern="^copy_ref_code$"),
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_messages),
          ]
