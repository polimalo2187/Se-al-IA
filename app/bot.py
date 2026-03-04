# app/bot.py

import asyncio
import os
import logging
import threading
import signal
import sys
from telegram import Update
from telegram.ext import Application, CommandHandler

from app.database import users_collection
from app.models import new_user
from app.handlers import get_handlers
from app.scanner import scan_market
from app.scheduler import scheduler_loop
from app.menus import main_menu

# Configurar logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ======================================================
# VARIABLES DE ENTORNO
# ======================================================

BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN no está definido")

# ======================================================
# NOMBRE DEL BOT (PARA ENLACES Y MENSAJES)
# ======================================================

BOT_NAME = "HADES_FT_BOT"

# ======================================================
# /START
# ======================================================

async def start(update: Update, context):
    user = update.effective_user
    args = context.args

    users_col = users_collection()
    existing_user = users_col.find_one({"user_id": user.id})

    referred_by = None
    if args and not existing_user:
        ref_arg = args[0]
        if ref_arg.startswith("ref_"):
            try:
                ref_user_id = int(ref_arg.replace("ref_", ""))
                if ref_user_id != user.id:
                    if users_col.find_one({"user_id": ref_user_id}):
                        referred_by = ref_user_id
            except ValueError:
                referred_by = None

    if not existing_user:
        user_doc = new_user(
            user_id=user.id,
            username=user.username,
            referred_by=referred_by,
        )
        users_col.insert_one(user_doc)

        welcome_text = (
            f"Bienvenido a {BOT_NAME}.\n\n"
            "Tu acceso gratuito de prueba ha sido activado por 7 días.\n\n"
            f"Invita amigos usando tu enlace de referido:\nhttps://t.me/{BOT_NAME}?start=ref_{user.id}\n\n"
            "Utiliza el menú para navegar."
        )
        logger.info(f"Nuevo usuario registrado: {user.id} (@{user.username})")
    else:
        welcome_text = (
            f"Bienvenido de nuevo a {BOT_NAME}.\n\n"
            "Utiliza el menú para acceder a las funciones disponibles."
        )

    await update.message.reply_text(
        text=welcome_text,
        reply_markup=main_menu(),
    )

# ======================================================
# RUN BOT (ENTRYPOINT ÚNICO)
# ======================================================

def run_bot():
    # Crear aplicación
    application = Application.builder().token(BOT_TOKEN).build()

    # Handlers
    application.add_handler(CommandHandler("start", start))
    for handler in get_handlers():
        application.add_handler(handler)

    # Obtener el bot del application
    bot = application.bot

    # ==============================
    # BACKGROUND THREADS CON MANEJO DE ERRORES
    # ==============================

    def run_scanner():
        """Ejecuta el scanner en un thread dedicado."""
        try:
            logger.info("📡 Iniciando thread del scanner...")
            scan_market(bot)
        except Exception as e:
            logger.error(f"❌ Thread scanner falló: {e}", exc_info=True)

    def run_scheduler():
        """Ejecuta el scheduler en un thread dedicado (modo seguro)."""
        try:
            logger.info("⏰ Iniciando thread del scheduler (modo seguro)...")
            asyncio.run(scheduler_loop())
        except Exception as e:
            logger.error(f"❌ Thread scheduler falló: {e}", exc_info=True)

    # Iniciar threads con nombres para debugging
    scanner_thread = threading.Thread(
        target=run_scanner,
        daemon=True,
        name="ScannerThread"
    )
    
    scheduler_thread = threading.Thread(
        target=run_scheduler,
        daemon=True,
        name="SchedulerThread"
    )
    
    scanner_thread.start()
    scheduler_thread.start()
    
    logger.info("✅ Threads de fondo iniciados correctamente")

    # ==============================
    # MANEJO DE SEÑALES PARA SHUTDOWN ELEGANTE
    # ==============================

    def signal_handler(sig, frame):
        """Maneja señales de terminación."""
        logger.info(f"\n🛑 Recibida señal de terminación ({sig})...")
        
        # Detener la aplicación
        if application.running:
            logger.info("Deteniendo aplicación de Telegram...")
            application.stop()
        
        logger.info("Bot detenido correctamente")
        sys.exit(0)

    # Registrar manejadores de señales
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    # ==============================
    # INICIAR POLLING
    # ==============================

    logger.info(f"🤖 {BOT_NAME} iniciando...")
    
    try:
        application.run_polling(
            poll_interval=0.5,
            timeout=30,
            drop_pending_updates=True
        )
    except Exception as e:
        logger.error(f"❌ Error en run_polling: {e}", exc_info=True)
        raise
