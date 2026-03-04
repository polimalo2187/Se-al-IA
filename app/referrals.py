# app/referrals.py - VERSIÓN ACTUALIZADA CON HADES_FT_BOT

import logging
from typing import Optional, Dict, List
from datetime import datetime

from app.database import users_collection, referrals_collection
from app.plans import (
    activate_plus,
    activate_premium,
    extend_current_plan,
    PLAN_FREE,
    PLAN_PLUS,
    PLAN_PREMIUM,
)
from app.models import update_timestamp, is_plan_active

logger = logging.getLogger(__name__)


# =========================
# REGISTRO DE REFERIDO VÁLIDO
# =========================

def register_valid_referral(
    referred_user_id: int,
    activated_plan: str,
) -> bool:
    """
    Registra un referido válido cuando un usuario activa un plan.
    Retorna True si se registró correctamente.
    """
    try:
        users_col = users_collection()
        refs_col = referrals_collection()

        # 1. Obtener usuario referido
        referred_user = users_col.find_one({"user_id": referred_user_id})
        if not referred_user:
            logger.warning(f"Usuario referido {referred_user_id} no encontrado")
            return False

        # 2. Obtener ID del referidor
        referrer_id = referred_user.get("referred_by")
        if not referrer_id:
            logger.debug(f"Usuario {referred_user_id} no fue referido por nadie")
            return False

        # 3. Verificar que no sea auto-referido
        if referrer_id == referred_user_id:
            logger.warning(f"Auto-referido detectado: {referred_user_id}")
            return False

        # 4. Verificar que el referidor exista
        referrer = users_col.find_one({"user_id": referrer_id})
        if not referrer:
            logger.warning(f"Referidor {referrer_id} no encontrado")
            return False

        # 5. Verificar que no sea doble conteo (mismo referido para el mismo referidor)
        existing = refs_col.find_one({
            "referrer_id": referrer_id,
            "referred_id": referred_user_id
        })
        if existing:
            logger.debug(f"Referido {referred_user_id} ya registrado para {referrer_id}")
            return False

        # 6. Registrar en colección de referidos (histórico)
        ref_doc = {
            "referrer_id": referrer_id,
            "referred_id": referred_user_id,
            "activated_plan": activated_plan,
            "activated_at": datetime.utcnow(),
            "reward_applied": False  # Para tracking de recompensas
        }
        refs_col.insert_one(ref_doc)

        # 7. Actualizar contadores en el usuario referidor
        if activated_plan == PLAN_PLUS:
            users_col.update_one(
                {"user_id": referrer_id},
                {
                    "$inc": {"ref_plus_valid": 1, "ref_plus_total": 1},
                    "$set": update_timestamp(referrer)
                }
            )
        elif activated_plan == PLAN_PREMIUM:
            users_col.update_one(
                {"user_id": referrer_id},
                {
                    "$inc": {"ref_premium_valid": 1, "ref_premium_total": 1},
                    "$set": update_timestamp(referrer)
                }
            )

        # 8. Evaluar recompensas automáticas
        check_ref_rewards(referrer_id)

        logger.info(f"✅ Referido registrado: {referrer_id} → {referred_user_id} ({activated_plan})")
        return True

    except Exception as e:
        logger.error(f"❌ Error en register_valid_referral: {e}", exc_info=True)
        return False


# =========================
# ESTADÍSTICAS DE REFERIDOS (VERSIÓN CORREGIDA)
# =========================

def get_user_referral_stats(user_id: int) -> Optional[Dict]:
    """
    Obtiene estadísticas de referidos para un usuario.
    Ahora devuelve EXACTAMENTE lo que handlers.py espera.
    """
    try:
        users_col = users_collection()
        refs_col = referrals_collection()
        
        # 1. Obtener usuario
        user = users_col.find_one({"user_id": user_id})
        if not user:
            # Retornar estructura que handlers.py espera
            return _get_empty_stats(user_id)
        
        # 2. Obtener código de referencia
        ref_code = user.get("ref_code", f"ref_{user_id}")
        
        # 3. Calcular referidos TOTALES (históricos)
        total_referred = refs_col.count_documents({"referrer_id": user_id})
        
        # 4. Calcular referidos por plan (históricos)
        plus_referred = refs_col.count_documents({
            "referrer_id": user_id,
            "activated_plan": PLAN_PLUS
        })
        
        premium_referred = refs_col.count_documents({
            "referrer_id": user_id,
            "activated_plan": PLAN_PREMIUM
        })
        
        # 5. Obtener contadores ACTUALES (para recompensas)
        current_plus = user.get("ref_plus_valid", 0)
        current_premium = user.get("ref_premium_valid", 0)
        
        # 6. Calcular recompensas pendientes
        pending_rewards = _calculate_pending_rewards(user)
        
        # 7. Retornar estructura EXACTA que handlers.py espera
        return {
            "ref_code": ref_code,
            "total_referred": total_referred,
            "plus_referred": plus_referred,      # Histórico
            "premium_referred": premium_referred, # Histórico
            "current_plus": current_plus,        # Actual (para recompensas)
            "current_premium": current_premium,  # Actual (para recompensas)
            "pending_rewards": pending_rewards,
        }
        
    except Exception as e:
        logger.error(f"❌ Error en get_user_referral_stats para user_id {user_id}: {e}", exc_info=True)
        return _get_empty_stats(user_id)


def _get_empty_stats(user_id: int) -> Dict:
    """Retorna estadísticas vacías para usuario no encontrado"""
    return {
        "ref_code": f"ref_{user_id}",
        "total_referred": 0,
        "plus_referred": 0,
        "premium_referred": 0,
        "current_plus": 0,
        "current_premium": 0,
        "pending_rewards": [],
    }


def _calculate_pending_rewards(user: Dict) -> List[str]:
    """Calcula recompensas pendientes basadas en contadores actuales"""
    plan = user.get("plan", PLAN_FREE)
    current_plus = user.get("ref_plus_valid", 0)
    current_premium = user.get("ref_premium_valid", 0)
    
    pending_rewards = []
    
    if plan == PLAN_FREE:
        if current_premium >= 5:
            pending_rewards.append("🎯 5 PREMIUM = Plan PREMIUM GRATIS")
        elif current_plus >= 5:
            pending_rewards.append("🎯 5 PLUS = Plan PLUS GRATIS")
    
    elif plan == PLAN_PLUS:
        if current_premium >= 5:
            pending_rewards.append("🎯 5 PREMIUM = Subir a PREMIUM")
        if current_plus >= 5:
            pending_rewards.append("🎯 5 PLUS = Extender plan PLUS (30 días)")
    
    elif plan == PLAN_PREMIUM:
        if current_premium >= 5:
            pending_rewards.append("🎯 5 PREMIUM = Extender plan PREMIUM (30 días)")
        if current_plus >= 10:
            pending_rewards.append("🎯 10 PLUS = Extender plan PREMIUM (30 días)")
    
    return pending_rewards


# =========================
# EVALUACIÓN DE RECOMPENSAS (VERSIÓN MEJORADA)
# =========================

def check_ref_rewards(referrer_id: int) -> bool:
    """
    Evalúa y aplica recompensas automáticas.
    Retorna True si se aplicó alguna recompensa.
    """
    try:
        users_col = users_collection()
        referrer = users_col.find_one({"user_id": referrer_id})
        
        if not referrer or not is_plan_active(referrer):
            return False
        
        plan = referrer.get("plan", PLAN_FREE)
        plus_count = referrer.get("ref_plus_valid", 0)
        premium_count = referrer.get("ref_premium_valid", 0)
        
        reward_applied = False
        
        # USUARIO FREE
        if plan == PLAN_FREE:
            if premium_count >= 5:
                if activate_premium(referrer_id):
                    users_col.update_one(
                        {"user_id": referrer_id},
                        {"$inc": {"ref_premium_valid": -5}}
                    )
                    reward_applied = True
                    logger.info(f"🎁 Recompensa: {referrer_id} recibió PREMIUM por 5 referidos premium")
            
            elif plus_count >= 5:
                if activate_plus(referrer_id):
                    users_col.update_one(
                        {"user_id": referrer_id},
                        {"$inc": {"ref_plus_valid": -5}}
                    )
                    reward_applied = True
                    logger.info(f"🎁 Recompensa: {referrer_id} recibió PLUS por 5 referidos plus")
        
        # USUARIO PLUS
        elif plan == PLAN_PLUS:
            if premium_count >= 5:
                if activate_premium(referrer_id):
                    users_col.update_one(
                        {"user_id": referrer_id},
                        {"$inc": {"ref_premium_valid": -5}}
                    )
                    reward_applied = True
                    logger.info(f"🎁 Recompensa: {referrer_id} ascendió a PREMIUM por 5 referidos premium")
            
            elif plus_count >= 5:
                if extend_current_plan(referrer_id, days=30):
                    users_col.update_one(
                        {"user_id": referrer_id},
                        {"$inc": {"ref_plus_valid": -5}}
                    )
                    reward_applied = True
                    logger.info(f"🎁 Recompensa: {referrer_id} extendió PLUS por 5 referidos plus")
        
        # USUARIO PREMIUM
        elif plan == PLAN_PREMIUM:
            if premium_count >= 5:
                if extend_current_plan(referrer_id, days=30):
                    users_col.update_one(
                        {"user_id": referrer_id},
                        {"$inc": {"ref_premium_valid": -5}}
                    )
                    reward_applied = True
                    logger.info(f"🎁 Recompensa: {referrer_id} extendió PREMIUM por 5 referidos premium")
            
            elif plus_count >= 10:
                if extend_current_plan(referrer_id, days=30):
                    users_col.update_one(
                        {"user_id": referrer_id},
                        {"$inc": {"ref_plus_valid": -10}}
                    )
                    reward_applied = True
                    logger.info(f"🎁 Recompensa: {referrer_id} extendió PREMIUM por 10 referidos plus")
        
        if reward_applied:
            # Actualizar timestamp
            users_col.update_one(
                {"user_id": referrer_id},
                {"$set": update_timestamp(referrer)}
            )
        
        return reward_applied
        
    except Exception as e:
        logger.error(f"❌ Error en check_ref_rewards para {referrer_id}: {e}", exc_info=True)
        return False


# =========================
# FUNCIONES AUXILIARES
# =========================

def get_referral_link(user_id: int) -> str:
    """Genera enlace de referido para un usuario"""
    users_col = users_collection()
    user = users_col.find_one({"user_id": user_id})
    
    if not user:
        return f"https://t.me/HADES_FT_BOT?start=ref_{user_id}"
    
    ref_code = user.get("ref_code", f"ref_{user_id}")
    return f"https://t.me/HADES_FT_BOT?start={ref_code}"


def get_referral_summary(user_id: int) -> Dict:
    """Resumen rápido de referidos para notificaciones"""
    stats = get_user_referral_stats(user_id)
    if not stats:
        return {"total": 0, "plus": 0, "premium": 0}
    
    return {
        "total": stats["total_referred"],
        "plus": stats["plus_referred"],
        "premium": stats["premium_referred"],
        "current_plus": stats["current_plus"],
        "current_premium": stats["current_premium"],
    }


def reset_referral_counters(user_id: int) -> bool:
    """Resetea contadores de referidos (solo para admin/debug)"""
    try:
        users_col = users_collection()
        users_col.update_one(
            {"user_id": user_id},
            {
                "$set": {
                    "ref_plus_valid": 0,
                    "ref_premium_valid": 0,
                    "ref_plus_total": 0,
                    "ref_premium_total": 0,
                }
            }
        )
        logger.info(f"♻️ Contadores de referidos reseteados para {user_id}")
        return True
    except Exception as e:
        logger.error(f"Error reseteando contadores: {e}")
        return False
