import os
import asyncio
import re
import logging
import sys
import json
from datetime import datetime, timedelta, timezone, time
from telethon import TelegramClient, events, Button
from telethon.sessions import StringSession
from aiohttp import web
from config import (
    API_ID, API_HASH, BOT_TOKEN, ADMIN_ID,
    SOURCE_CHANNEL_ID, SOURCE_CHANNEL_2_ID, PORT,
    SUIT_MAPPING, ALL_SUITS, SUIT_DISPLAY
)

PAYMENT_LINK = "https://my.moneyfusion.net/6977f7502181d4ebf722398d"
PAYMENT_LINK_24H = "https://my.moneyfusion.net/6977f7502181d4ebf722398d"
USERS_FILE = "users_data.json"

# --- Configuration et Initialisation ---
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger(__name__)

# Vérifications minimales de la configuration
if not API_ID or API_ID == 0:
    logger.error("API_ID manquant")
    exit(1)
if not API_HASH:
    logger.error("API_HASH manquant")
    exit(1)
if not BOT_TOKEN:
    logger.error("BOT_TOKEN manquant")
    exit(1)

logger.info(f"Configuration: SOURCE_CHANNEL={SOURCE_CHANNEL_ID}, SOURCE_CHANNEL_2={SOURCE_CHANNEL_2_ID}")

# Initialisation du client Telegram avec session string ou nouvelle session
session_string = os.getenv('TELEGRAM_SESSION', '')
client = TelegramClient(StringSession(session_string), API_ID, API_HASH)

# --- Variables Globales d'État ---

# RÈGLE 2 : Variables pour prédiction par statistiques (Prioritaire)
pending_predictions = {}
queued_predictions = {}
processed_messages = set()
current_game_number = 0
last_source_game_number = 0
suit_prediction_counts = {}
USER_A = 1  # Valeur 'a' pour cible N+a

# RÈGLE 1 : Variables pour prédiction par cycle temps + "1 part" (Fallback)
SUIT_CYCLE = ['♥', '♦', '♣', '♠', '♦', '♥', '♠', '♣']
TIME_CYCLE = [5, 8, 3, 7, 9, 4, 6, 8, 3, 5, 9, 7, 4, 6, 8, 3, 5, 9, 7, 4, 6, 8, 3, 5, 9, 7, 4, 6, 8, 5]
current_time_cycle_index = 0
next_prediction_allowed_at = datetime.now()

# Variables pour la logique "1 part" (Règle 1)
last_known_source_game = 0
prediction_target_game = None
waiting_for_one_part = False
cycle_triggered = False

# Compteur pour limiter la Règle 1 (max 3-4 fois consécutifs)
rule1_consecutive_count = 0
MAX_RULE1_CONSECUTIVE = 3  # Max 3 prédictions consécutives pour Règle 1

# Flag pour savoir si une prédiction Règle 2 est en cours
rule2_active = False

# Stats et autres
scp_cooldown = 0
scp_history = []
already_predicted_games = set()
stats_bilan = {
    'total': 0,
    'wins': 0,
    'losses': 0,
    'win_details': {'✅0️⃣': 0, '✅1️⃣': 0, '✅2️⃣': 0},
    'loss_details': {'❌': 0}
}
bilan_interval = 60
last_bilan_time = datetime.now()

source_channel_ok = False
transfer_enabled = True

# --- Système de Paiement et Utilisateurs ---
users_data = {}
user_conversation_state = {}

def load_users_data():
    global users_data
    try:
        if os.path.exists(USERS_FILE):
            with open(USERS_FILE, 'r', encoding='utf-8') as f:
                users_data = json.load(f)
            logger.info(f"Données utilisateurs chargées: {len(users_data)} utilisateurs")
    except Exception as e:
        logger.error(f"Erreur chargement users_data: {e}")
        users_data = {}

def save_users_data():
    try:
        with open(USERS_FILE, 'w', encoding='utf-8') as f:
            json.dump(users_data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"Erreur sauvegarde users_data: {e}")

def get_user(user_id: int) -> dict:
    user_id_str = str(user_id)
    if user_id_str not in users_data:
        users_data[user_id_str] = {
            'registered': False,
            'nom': None,
            'prenom': None,
            'pays': None,
            'trial_started': None,
            'trial_used': False,
            'subscription_end': None,
            'subscription_type': None,
            'pending_payment': False,
            'awaiting_screenshot': False,
            'awaiting_amount': False
        }
        save_users_data()
    return users_data[user_id_str]

def update_user(user_id: int, data: dict):
    user_id_str = str(user_id)
    if user_id_str not in users_data:
        get_user(user_id)
    users_data[user_id_str].update(data)
    save_users_data()

def is_user_subscribed(user_id: int) -> bool:
    admin_id = 1190237801
    if user_id == admin_id:
        return True
    user = get_user(user_id)
    if not user.get('subscription_end'):
        return False
    try:
        sub_end = datetime.fromisoformat(user['subscription_end'])
        return datetime.now() < sub_end
    except:
        return False

def is_trial_active(user_id: int) -> bool:
    """Vérifie si les 60 minutes d'essai sont encore en cours."""
    user = get_user(user_id)
    if user.get('trial_used') or not user.get('trial_started'):
        return False
    try:
        trial_start = datetime.fromisoformat(user['trial_started'])
        trial_end = trial_start + timedelta(minutes=60)  # ← 60 MINUTES D'ESSAI
        return datetime.now() < trial_end
    except:
        return False

def can_receive_predictions(user_id: int) -> bool:
    user = get_user(user_id)
    if not user.get('registered'):
        return False
    return is_user_subscribed(user_id) or is_trial_active(user_id)

def get_subscription_type(user_id: int) -> str:
    user = get_user(user_id)
    return user.get('subscription_type', None)

# ============================================================
# ENVOI DES PRÉDICTIONS AUX UTILISATEURS
# ============================================================

async def send_prediction_to_all_users(prediction_msg: str, target_game: int, rule_type: str = "R2"):
    """
    Envoie la prédiction à TOUS les utilisateurs éligibles (abonnés ou en essai).
    Retourne un dictionnaire {user_id: message_id} pour les éditions futures.
    """
    private_messages = {}
    sent_count = 0
    failed_count = 0
    
    # Envoyer à l'admin aussi
    admin_id = 1190237801
    try:
        admin_msg = await client.send_message(admin_id, prediction_msg)
        private_messages[str(admin_id)] = admin_msg.id
        logger.info(f"✅ Prédiction envoyée à l'admin {admin_id}")
        sent_count += 1
    except Exception as e:
        logger.error(f"❌ Erreur envoi à l'admin {admin_id}: {e}")
        failed_count += 1
    
    # Envoyer à tous les utilisateurs enregistrés
    for user_id_str, user_info in users_data.items():
        try:
            user_id = int(user_id_str)
            
            if not can_receive_predictions(user_id):
                logger.debug(f"Utilisateur {user_id} non éligible, ignoré")
                continue
            
            sent_msg = await client.send_message(user_id, prediction_msg)
            private_messages[user_id_str] = sent_msg.id
            sent_count += 1
            logger.info(f"✅ Prédiction envoyée à {user_id} (Msg ID: {sent_msg.id})")
            
        except Exception as e:
            failed_count += 1
            logger.error(f"❌ Erreur envoi prédiction à {user_id_str}: {e}")
    
    logger.info(f"📊 Envoi terminé: {sent_count} succès, {failed_count} échecs")
    return private_messages

async def edit_prediction_for_all_users(game_number: int, new_status: str, suit: str, rule_type: str, original_game: int = None):
    """
    Édite les messages de prédiction pour TOUS les utilisateurs.
    """
    display_game = original_game if original_game else game_number
    
    # Format du message mis à jour selon le statut
    status_emoji = "✅" if "✅" in new_status else "❌"
    
    if rule_type == "R2":
        if new_status == "❌":
            status_text = "❌ PERDU"
        elif new_status == "✅0️⃣":
            status_text = "✅ VICTOIRE IMMÉDIATE!"
        elif new_status == "✅1️⃣":
            status_text = "✅ VICTOIRE AU 2ÈME JEU!"
        elif new_status == "✅2️⃣":
            status_text = "✅ VICTOIRE AU 3ÈME JEU!"
        elif new_status == "✅3️⃣":
            status_text = "✅ VICTOIRE AU 4ÈME JEU!"
        else:
            status_text = f"{new_status}"
            
        updated_msg = f"""🎰 **PRÉDICTION #{display_game}**

🎯 Couleur: {SUIT_DISPLAY.get(suit, suit)}
📊 Statut: {status_text}
🤖 Algorithme: Règle 2 (Stats)"""
    else:
        if new_status == "❌":
            status_text = "❌ NON TROUVÉ"
        elif new_status == "✅0️⃣":
            status_text = "✅ TROUVÉ!"
        elif new_status == "✅1️⃣":
            status_text = "✅ TROUVÉ AU 2ÈME!"
        elif new_status == "✅2️⃣":
            status_text = "✅ TROUVÉ AU 3ÈME!"
        else:
            status_text = f"{new_status}"
            
        updated_msg = f"""🎰 **PRÉDICTION #{display_game}**

🎯 Couleur: {SUIT_DISPLAY.get(suit, suit)}
📊 Statut: {status_text}
🤖 Algorithme: Règle 1 (Cycle)"""

    if game_number not in pending_predictions:
        logger.warning(f"Jeu #{game_number} non trouvé dans pending_predictions pour édition")
        return 0
    
    pred = pending_predictions[game_number]
    private_msgs = pred.get('private_messages', {})
    
    if not private_msgs:
        logger.warning(f"Aucun message privé trouvé pour le jeu #{game_number}")
        return 0
    
    edited_count = 0
    failed_count = 0
    
    for user_id_str, msg_id in list(private_msgs.items()):
        try:
            user_id = int(user_id_str)
            await client.edit_message(user_id, msg_id, updated_msg)
            edited_count += 1
            logger.info(f"✅ Message édité pour {user_id}: {new_status}")
        except Exception as e:
            failed_count += 1
            logger.error(f"❌ Erreur édition message pour {user_id_str}: {e}")
            if "message to edit not found" in str(e).lower():
                del private_msgs[user_id_str]
    
    logger.info(f"📊 Édition terminée: {edited_count} succès, {failed_count} échecs")
    return edited_count

# --- Fonctions d'Analyse ---

def extract_game_number(message: str):
    """Extrait le numéro de jeu du message."""
    match = re.search(r"#N\s*(\d+)", message, re.IGNORECASE)
    if match:
        return int(match.group(1))
    return None

def parse_stats_message(message: str):
    """Extrait les statistiques du canal source 2."""
    stats = {}
    patterns = {
        '♠': r'♠️?\s*:\s*(\d+)',
        '♥': r'♥️?\s*:\s*(\d+)',
        '♦': r'♦️?\s*:\s*(\d+)',
        '♣': r'♣️?\s*:\s*(\d+)'
    }
    for suit, pattern in patterns.items():
        match = re.search(pattern, message)
        if match:
            stats[suit] = int(match.group(1))
    return stats

def extract_parentheses_groups(message: str):
    """Extrait le contenu entre parenthèses."""
    return re.findall(r"\(([^)]*)\)", message)

def normalize_suits(group_str: str) -> str:
    """Remplace les différentes variantes de symboles par un format unique."""
    normalized = group_str.replace('❤️', '♥').replace('❤', '♥').replace('♥️', '♥')
    normalized = normalized.replace('♠️', '♠').replace('♦️', '♦').replace('♣️', '♣')
    return normalized

def get_suits_in_group(group_str: str):
    """Liste toutes les couleurs (suits) présentes dans une chaîne."""
    normalized = normalize_suits(group_str)
    return [s for s in ALL_SUITS if s in normalized]

def has_suit_in_group(group_str: str, target_suit: str) -> bool:
    """Vérifie si la couleur cible est présente dans le premier groupe du résultat."""
    normalized = normalize_suits(group_str)
    target_normalized = normalize_suits(target_suit)
    for suit in ALL_SUITS:
        if suit in target_normalized and suit in normalized:
            return True
    return False

def get_predicted_suit(missing_suit: str) -> str:
    """Applique le mapping personnalisé (couleur manquante -> couleur prédite)."""
    return SUIT_MAPPING.get(missing_suit, missing_suit)

# --- Fonction "1 part" (Règle 1) ---
def is_one_part_away(current: int, target: int) -> bool:
    """Vérifie si current est à 1 part de target (current impair et différence de 1)"""
    return current % 2 != 0 and target - current == 1

# --- Logique de Prédiction et File d'Attente ---

async def send_prediction_to_users(target_game: int, predicted_suit: str, base_game: int, 
                                     rattrapage=0, original_game=None, rule_type="R2"):
    """Envoie la prédiction à TOUS les utilisateurs en privé."""
    global rule2_active, rule1_consecutive_count
    
    try:
        # Si c'est un rattrapage, on récupère les références des messages originaux
        if rattrapage > 0:
            original_private_msgs = {}
            if original_game and original_game in pending_predictions:
                original_private_msgs = pending_predictions[original_game].get('private_messages', {}).copy()
                logger.info(f"Rattrapage {rattrapage}: récupération de {len(original_private_msgs)} messages privés de l'original #{original_game}")
            
            pending_predictions[target_game] = {
                'message_id': 0,
                'suit': predicted_suit,
                'base_game': base_game,
                'status': '🔮',
                'rattrapage': rattrapage,
                'original_game': original_game,
                'rule_type': rule_type,
                'private_messages': original_private_msgs,
                'created_at': datetime.now().isoformat()
            }
            
            if rule_type == "R2":
                rule2_active = True
            logger.info(f"Rattrapage {rattrapage} actif pour #{target_game} (Original #{original_game}, {rule_type})")
            return True

        # Vérifier si une prédiction Règle 2 est déjà active pour un numéro futur
        if rule_type == "R1":
            active_r2_predictions = [p for game, p in pending_predictions.items() 
                                    if p.get('rule_type') == 'R2' and p.get('rattrapage', 0) == 0 
                                    and game > current_game_number]
            if active_r2_predictions:
                logger.info(f"Règle 2 active, Règle 1 ne peut pas prédire #{target_game}")
                return False
        
        # Format du message selon la règle - MESSAGES SÉDUISANTS
        if rule_type == "R2":
            prediction_msg = f"""🚀 **ALERTE PRÉDICTION EXCLUSIVE!** 🚀

🎰 Jeu #{target_game}
🎯 Couleur à jouer: {SUIT_DISPLAY.get(predicted_suit, predicted_suit)}
⚡ Confiance: HAUTE (Algorithme Stats)
⏳ Statut: EN ATTENTE...

💎 Cette prédiction est basée sur l'analyse statistique avancée des miroirs.

🔔 Restez attentif, mise à jour automatique du résultat!"""
        else:
            prediction_msg = f"""⚡ **NOUVELLE PRÉDISSION CYCLE!** ⚡

🎰 Jeu #{target_game}
🎯 Couleur à jouer: {SUIT_DISPLAY.get(predicted_suit, predicted_suit)}
📊 Algorithme: Cycle Temporel + Pattern "1 part"
⏳ Statut: EN ATTENTE...

🍀 Bonne chance! Mise à jour automatique à venir..."""

        # ENVOI À TOUS LES UTILISATEURS
        private_messages = await send_prediction_to_all_users(prediction_msg, target_game, rule_type)
        
        if not private_messages:
            logger.error(f"❌ Aucun utilisateur n'a reçu la prédiction pour #{target_game}")
            return False

        # Stockage de la prédiction
        pending_predictions[target_game] = {
            'message_id': 0,
            'suit': predicted_suit,
            'base_game': base_game,
            'status': '⌛',
            'check_count': 0,
            'rattrapage': 0,
            'rule_type': rule_type,
            'private_messages': private_messages,
            'created_at': datetime.now().isoformat()
        }

        # Mise à jour des flags
        if rule_type == "R2":
            rule2_active = True
            rule1_consecutive_count = 0
            logger.info(f"✅ Règle 2: Prédiction #{target_game} - {predicted_suit} envoyée à {len(private_messages)} utilisateurs")
        else:
            rule1_consecutive_count += 1
            logger.info(f"✅ Règle 1: Prédiction #{target_game} - {predicted_suit} envoyée à {len(private_messages)} utilisateurs (Consécutif: {rule1_consecutive_count})")

        return True

    except Exception as e:
        logger.error(f"❌ Erreur envoi prédiction: {e}")
        import traceback
        logger.error(traceback.format_exc())
        return False

def queue_prediction(target_game: int, predicted_suit: str, base_game: int, 
                    rattrapage=0, original_game=None, rule_type="R2"):
    """Met une prédiction en file d'attente."""
    global rule2_active
    
    if rule_type == "R2":
        rule2_active = True
        
    if target_game in queued_predictions or (target_game in pending_predictions and rattrapage == 0):
        return False

    queued_predictions[target_game] = {
        'target_game': target_game,
        'predicted_suit': predicted_suit,
        'base_game': base_game,
        'rattrapage': rattrapage,
        'original_game': original_game,
        'rule_type': rule_type,
        'queued_at': datetime.now().isoformat()
    }
    logger.info(f"📋 Prédiction #{target_game} mise en file d'attente ({rule_type}, Rattrapage {rattrapage})")
    return True

async def check_and_send_queued_predictions(current_game: int):
    """Vérifie la file d'attente et envoie les prédictions."""
    global current_game_number, rule2_active
    current_game_number = current_game

    sorted_queued = sorted(queued_predictions.keys())

    for target_game in list(sorted_queued):
        if target_game >= current_game:
            pred_data = queued_predictions.pop(target_game)
            await send_prediction_to_users(
                pred_data['target_game'],
                pred_data['predicted_suit'],
                pred_data['base_game'],
                pred_data.get('rattrapage', 0),
                pred_data.get('original_game'),
                pred_data.get('rule_type', 'R2')
            )

async def update_prediction_status(game_number: int, new_status: str):
    """Met à jour le statut de la prédiction pour tous les utilisateurs."""
    global rule2_active, rule1_consecutive_count
    
    try:
        if game_number not in pending_predictions:
            logger.warning(f"Tentative de mise à jour pour jeu #{game_number} non trouvé")
            return False

        pred = pending_predictions[game_number]
        suit = pred['suit']
        rule_type = pred.get('rule_type', 'R2')
        rattrapage = pred.get('rattrapage', 0)
        original_game = pred.get('original_game', game_number)

        logger.info(f"Mise à jour statut #{game_number} [{rule_type}] vers {new_status}")

        # Éditer les messages pour tous les utilisateurs
        await edit_prediction_for_all_users(game_number, new_status, suit, rule_type, original_game)

        pred['status'] = new_status
        
        # Mise à jour des statistiques et flags
        if new_status in ['✅0️⃣', '✅1️⃣', '✅2️⃣', '✅3️⃣']:
            stats_bilan['total'] += 1
            stats_bilan['wins'] += 1
            stats_bilan['win_details'][new_status] = (stats_bilan['win_details'].get(new_status, 0) + 1)
            
            if rule_type == "R2" and rattrapage == 0:
                rule2_active = False
                logger.info("Règle 2 terminée (victoire), Règle 1 peut reprendre")
            elif rule_type == "R1":
                rule1_consecutive_count = 0
                
            del pending_predictions[game_number]
            asyncio.create_task(check_and_send_queued_predictions(current_game_number))
            
        elif new_status == '❌':
            stats_bilan['total'] += 1
            stats_bilan['losses'] += 1
            stats_bilan['loss_details']['❌'] += 1
            
            if rule_type == "R2" and rattrapage == 0:
                rule2_active = False
                logger.info("Règle 2 terminée (perte), Règle 1 peut reprendre")
            elif rule_type == "R1":
                rule1_consecutive_count = 0
                
            del pending_predictions[game_number]
            asyncio.create_task(check_and_send_queued_predictions(current_game_number))

        return True
        
    except Exception as e:
        logger.error(f"Erreur update_prediction_status: {e}")
        import traceback
        logger.error(traceback.format_exc())
        return False

async def check_prediction_result(game_number: int, first_group: str):
    """Vérifie les résultats selon la séquence ✅0️⃣, ✅1️⃣, ✅2️⃣, ✅3️⃣ ou ❌."""
    logger.info(f"Vérification résultat pour jeu #{game_number}, groupe: {first_group}")
    
    # 1. Vérification pour le jeu actuel (Cible N)
    if game_number in pending_predictions:
        pred = pending_predictions[game_number]
        if pred.get('rattrapage', 0) == 0:
            target_suit = pred['suit']
            rule_type = pred.get('rule_type', 'R2')
            if has_suit_in_group(first_group, target_suit):
                logger.info(f"✅0️⃣ Trouvé pour #{game_number}!")
                await update_prediction_status(game_number, '✅0️⃣')
                return
            else:
                next_target = game_number + 1
                queue_prediction(next_target, target_suit, pred['base_game'], 
                               rattrapage=1, original_game=game_number, rule_type=rule_type)
                logger.info(f"Échec # {game_number}, Rattrapage 1 planifié pour #{next_target}")

    # 2. Vérification pour les rattrapages
    for target_game, pred in list(pending_predictions.items()):
        if target_game == game_number and pred.get('rattrapage', 0) > 0:
            original_game = pred.get('original_game', target_game - pred['rattrapage'])
            target_suit = pred['suit']
            rattrapage_actuel = pred['rattrapage']
            rule_type = pred.get('rule_type', 'R2')
            
            if has_suit_in_group(first_group, target_suit):
                logger.info(f"✅{rattrapage_actuel}️⃣ Trouvé pour #{original_game} au rattrapage!")
                await update_prediction_status(original_game, f'✅{rattrapage_actuel}️⃣')
                if target_game != original_game:
                    del pending_predictions[target_game]
                return
            else:
                if rattrapage_actuel < 3:
                    next_rattrapage = rattrapage_actuel + 1
                    next_target = game_number + 1
                    queue_prediction(next_target, target_suit, pred['base_game'], 
                                   rattrapage=next_rattrapage, original_game=original_game,
                                   rule_type=rule_type)
                    logger.info(f"Échec rattrapage {rattrapage_actuel}, Rattrapage {next_rattrapage} planifié")
                    del pending_predictions[target_game]
                else:
                    logger.info(f"❌ Définitif pour #{original_game} après 3 rattrapages")
                    await update_prediction_status(original_game, '❌')
                    if target_game != original_game:
                        del pending_predictions[target_game]
                return

# ============================================================
# RÈGLE 2 : Prédiction par Statistiques (PRIORITAIRE)
# ============================================================

async def process_stats_message(message_text: str):
    """Traite les statistiques du canal 2 selon les miroirs ♦️<->♠️ et ❤️<->♣️."""
    global last_source_game_number, suit_prediction_counts, rule2_active
    
    stats = parse_stats_message(message_text)
    if not stats:
        return False

    pairs = [('♦', '♠'), ('♥', '♣')]
    
    for s1, s2 in pairs:
        if s1 in stats and s2 in stats:
            v1, v2 = stats[s1], stats[s2]
            diff = abs(v1 - v2)
            
            if diff >= 10:
                predicted_suit = s1 if v1 < v2 else s2
                
                current_count = suit_prediction_counts.get(predicted_suit, 0)
                if current_count >= 3:
                    logger.info(f"Limite de 3 prédictions atteinte pour {predicted_suit}, ignorée.")
                    continue

                logger.info(f"RÈGLE 2 DÉCLENCHÉE: Décalage {diff} entre {s1}({v1}) et {s2}({v2}). Prédiction: {predicted_suit}")
                
                if last_source_game_number > 0:
                    target_game = last_source_game_number + USER_A
                    
                    global rule1_consecutive_count, waiting_for_one_part, cycle_triggered, prediction_target_game
                    rule1_consecutive_count = 0
                    waiting_for_one_part = False
                    cycle_triggered = False
                    prediction_target_game = None
                    
                    if queue_prediction(target_game, predicted_suit, last_source_game_number, rule_type="R2"):
                        suit_prediction_counts[predicted_suit] = current_count + 1
                        for s in ALL_SUITS:
                            if s != predicted_suit:
                                suit_prediction_counts[s] = 0
                        rule2_active = True
                        return True
    return False

# ============================================================
# RÈGLE 1 : Prédiction par Cycle + "1 part" (FALLBACK)
# ============================================================

async def try_launch_prediction_rule1():
    """Tente de lancer la prédiction Règle 1 si condition '1 part' remplie."""
    global waiting_for_one_part, prediction_target_game, cycle_triggered
    global current_time_cycle_index, next_prediction_allowed_at, rule1_consecutive_count
    global rule2_active
    
    if rule2_active:
        logger.info("Règle 2 active, Règle 1 en attente")
        return False
        
    if rule1_consecutive_count >= MAX_RULE1_CONSECUTIVE:
        logger.info(f"Limite Règle 1 atteinte ({MAX_RULE1_CONSECUTIVE}), attente Règle 2")
        return False
    
    if not cycle_triggered or prediction_target_game is None:
        return False
    
    if is_one_part_away(last_known_source_game, prediction_target_game):
        logger.info(f"RÈGLE 1: Condition '1 part' OK: {last_known_source_game} → {prediction_target_game}")
        
        if prediction_target_game >= 6:
            count_valid = 0
            for n in range(6, prediction_target_game + 1, 2):
                if n % 10 != 0:
                    count_valid += 1
            if count_valid > 0:
                index = (count_valid - 1) % 8
                predicted_suit = SUIT_CYCLE[index]
            else:
                predicted_suit = '♥'
        else:
            predicted_suit = '♥'
        
        success = await send_prediction_to_users(
            prediction_target_game, 
            predicted_suit, 
            last_known_source_game,
            rule_type="R1"
        )
        
        if success:
            waiting_for_one_part = False
            cycle_triggered = False
            prediction_target_game = None
            
            wait_min = TIME_CYCLE[current_time_cycle_index]
            next_prediction_allowed_at = datetime.now() + timedelta(minutes=wait_min)
            current_time_cycle_index = (current_time_cycle_index + 1) % len(TIME_CYCLE)
            logger.info(f"Règle 1: Cycle consommé. Prochain dans {wait_min} min")
            return True
    else:
        logger.info(f"Règle 1: Attente '1 part': dernier={last_known_source_game}, cible={prediction_target_game}")
    
    return False

async def process_prediction_logic_rule1(message_text: str, chat_id: int):
    """Gère le déclenchement du cycle de temps Règle 1."""
    global last_known_source_game, current_game_number
    global cycle_triggered, waiting_for_one_part, prediction_target_game
    global rule2_active, rule1_consecutive_count
    
    if chat_id != SOURCE_CHANNEL_ID:
        return
        
    game_number = extract_game_number(message_text)
    if game_number is None:
        return

    last_known_source_game = game_number
    logger.info(f"Règle 1: Dernier numéro source mis à jour: #{game_number}")
    
    if waiting_for_one_part and cycle_triggered:
        await try_launch_prediction_rule1()
        return
    
    now = datetime.now()
    if now < next_prediction_allowed_at:
        return
        
    if rule2_active:
        logger.info("Temps cycle arrivé mais Règle 2 active, attente")
        return
        
    if rule1_consecutive_count >= MAX_RULE1_CONSECUTIVE:
        logger.info(f"Temps cycle arrivé mais limite Règle 1 atteinte ({rule1_consecutive_count})")
        wait_min = TIME_CYCLE[current_time_cycle_index]
        next_prediction_allowed_at = now + timedelta(minutes=wait_min)
        current_time_cycle_index = (current_time_cycle_index + 1) % len(TIME_CYCLE)
        return
    
    logger.info(f"RÈGLE 1: Temps cycle arrivé à {now.strftime('%H:%M:%S')}")
    cycle_triggered = True
    
    candidate = game_number + 2
    while candidate % 2 != 0 or candidate % 10 == 0:
        candidate += 1
    
    prediction_target_game = candidate
    logger.info(f"Règle 1: Cible calculée: #{prediction_target_game}")
    
    success = await try_launch_prediction_rule1()
    
    if not success:
        waiting_for_one_part = True
        logger.info(f"Règle 1: Mise en attente '1 part' pour #{prediction_target_game}")

# ============================================================
# Gestion des Messages
# ============================================================

def is_message_finalized(message: str) -> bool:
    """Vérifie si le message est finalisé."""
    if '⏰' in message:
        return False
    return '✅' in message or '🔰' in message or '▶️' in message or 'Finalisé' in message

async def process_finalized_message(message_text: str, chat_id: int):
    """Traite les messages finalisés pour vérification des résultats."""
    global current_game_number, last_source_game_number
    
    try:
        if chat_id == SOURCE_CHANNEL_2_ID:
            await process_stats_message(message_text)
            await check_and_send_queued_predictions(current_game_number)
            return

        if not is_message_finalized(message_text):
            return

        game_number = extract_game_number(message_text)
        if game_number is None:
            return

        current_game_number = game_number
        last_source_game_number = game_number
        
        message_hash = f"{game_number}_{message_text[:50]}"
        if message_hash in processed_messages:
            return
        processed_messages.add(message_hash)

        groups = extract_parentheses_groups(message_text)
        if len(groups) < 1:
            return
            
        first_group = groups[0]

        await check_prediction_result(game_number, first_group)
        await check_and_send_queued_predictions(game_number)

    except Exception as e:
        logger.error(f"Erreur traitement finalisé: {e}")

async def handle_message(event):
    """Gère les nouveaux messages dans les canaux sources."""
    try:
        sender = await event.get_sender()
        sender_id = getattr(sender, 'id', event.sender_id)
        
        chat = await event.get_chat()
        chat_id = chat.id
        if hasattr(chat, 'broadcast') and chat.broadcast:
            if not str(chat_id).startswith('-100'):
                chat_id = int(f"-100{abs(chat_id)}")
            
        logger.info(f"DEBUG: Message reçu de chat_id={chat_id}: {event.message.message[:50]}...")

        if chat_id == SOURCE_CHANNEL_ID:
            message_text = event.message.message
            
            await process_prediction_logic_rule1(message_text, chat_id)
            
            if is_message_finalized(message_text):
                await process_finalized_message(message_text, chat_id)
            
            if message_text.startswith('/info'):
                active_preds = len(pending_predictions)
                rule1_status = f"Consécutifs: {rule1_consecutive_count}/{MAX_RULE1_CONSECUTIVE}"
                rule2_status = "ACTIVE" if rule2_active else "Inactif"
                
                info_msg = (
                    "ℹ️ **ÉTAT DU SYSTÈME**\n\n"
                    f"🎮 Jeu actuel: #{current_game_number}\n"
                    f"🔮 Prédictions actives: {active_preds}\n"
                    f"⏳ Règle 2: {rule2_status}\n"
                    f"⏱️ Règle 1: {rule1_status}\n"
                    f"🎯 Cible R1: #{prediction_target_game if prediction_target_game else 'Aucune'}\n"
                    f"📍 Dernier source: #{last_known_source_game}\n"
                    f"👥 Utilisateurs enregistrés: {len(users_data)}"
                )
                await event.respond(info_msg)
                return
        
        elif chat_id == SOURCE_CHANNEL_2_ID:
            message_text = event.message.message
            await process_stats_message(message_text)
            await check_and_send_queued_predictions(current_game_number)
            
        if sender_id == ADMIN_ID:
            if event.message.message.startswith('/'):
                logger.info(f"Commande admin reçue: {event.message.message}")

    except Exception as e:
        logger.error(f"Erreur handle_message: {e}")

async def handle_edited_message(event):
    """Gère les messages édités."""
    try:
        chat = await event.get_chat()
        chat_id = chat.id
        if hasattr(chat, 'broadcast') and chat.broadcast:
            if not str(chat_id).startswith('-100'):
                chat_id = int(f"-100{abs(chat_id)}")

        if chat_id == SOURCE_CHANNEL_ID:
            message_text = event.message.message
            await process_prediction_logic_rule1(message_text, chat_id)
            
            if is_message_finalized(message_text):
                await process_finalized_message(message_text, chat_id)
        
        elif chat_id == SOURCE_CHANNEL_2_ID:
            message_text = event.message.message
            await process_stats_message(message_text)
            await check_and_send_queued_predictions(current_game_number)

    except Exception as e:
        logger.error(f"Erreur handle_edited_message: {e}")

# --- Gestion des Messages (Hooks Telethon) ---
client.add_event_handler(handle_message, events.NewMessage())
client.add_event_handler(handle_edited_message, events.MessageEdited())

# --- Commandes Utilisateur et Inscription ---

@client.on(events.NewMessage(pattern='/start'))
async def cmd_start(event):
    if event.is_group or event.is_channel: return
    
    user_id = event.sender_id
    user = get_user(user_id)
    admin_id = 1190237801
    
    # Message de bienvenue séduisant pour nouveaux utilisateurs
    if not user.get('registered'):
        welcome_msg = """🎰 **BIENVENUE DANS L'ELITE DES GAGNANTS!** 🎰

💎 Vous venez de découvrir le bot de prédiction Baccarat le plus avancé du marché!

🚀 **Ce qui vous attend:**
• Prédictions basées sur des algorithmes statistiques de pointe
• Analyse en temps réel des patterns gagnants
• Taux de réussite optimisé par IA
• 60 MINUTES D'ESSAI GRATUIT pour tester la puissance du système!

💰 **Nos utilisateurs gagnants** profitent déjà d'un avantage statistique significatif.

👇 **Commencez votre inscription maintenant!**"""
        await event.respond(welcome_msg)
        return
    
    # Utilisateur déjà inscrit
    if user.get('registered'):
        if is_user_subscribed(user_id) or user_id == admin_id:
            sub_type = "VIP 🔥" if get_subscription_type(user_id) == 'premium' or user_id == admin_id else "Standard"
            sub_end = user.get('subscription_end', 'Illimité' if user_id == admin_id else 'N/A')
            update_user(user_id, {'expiry_notified': False})
            
            active_msg = f"""🎯 **BON RETOUR {user.get('prenom', 'CHAMPION').upper()}!** 🎯

✅ Votre accès **{sub_type}** est ACTIF!
📅 Expiration: {sub_end[:10] if sub_end and user_id != admin_id else sub_end}

🔥 **Vous êtes prêt à gagner!**
Les prédictions arrivent automatiquement ici dès qu'une opportunité se présente.

💡 **Conseil pro:** Restez attentif aux notifications, les meilleures opportunités partent vite!

🚀 **Bonne chance et gros gains!**"""
            await event.respond(active_msg)
            
        elif is_trial_active(user_id):
            trial_start = datetime.fromisoformat(user['trial_started'])
            trial_end = trial_start + timedelta(minutes=60)
            remaining = (trial_end - datetime.now()).seconds // 60
            
            trial_msg = f"""⏰ **VOTRE ESSAI VIP EST EN COURS!** ⏰

🎁 Il vous reste **{remaining} minutes** de test gratuit!

🔥 Profitez-en pour découvrir la puissance de nos algorithmes:
• Prédictions en temps réel
• Mises à jour automatiques des résultats
• Analyse statistique avancée

💎 **Astuce:** Plus vous observez, plus vous comprenez la puissance du système!

⚡ **Ne perdez pas une seule seconde, restez attentif!**"""
            await event.respond(trial_msg)
            
        else:
            # Essai terminé - message de conversion
            update_user(user_id, {'trial_used': True})
            buttons = [
                [Button.url("💳 24H - 200 FCFA", PAYMENT_LINK_24H)],
                [Button.url("💳 1 SEMAINE - 1000 FCFA", PAYMENT_LINK)],
                [Button.url("💳 2 SEMAINES - 2000 FCFA", PAYMENT_LINK)]
            ]
            
            expired_msg = f"""⚠️ **VOTRE ESSAI EST TERMINÉ...** ⚠️

🎰 {user.get('prenom', 'CHAMPION')}, vous avez goûté à la puissance de nos prédictions...

💔 **Ne laissez pas la chance s'échapper!**

🔥 **OFFRE EXCLUSIVE:**
💎 **200 FCFA** = 24H de test prolongé
💎 **1000 FCFA** = 1 semaine complète  
💎 **2000 FCFA** = 2 semaines VIP

📊 **Pourquoi s'abonner?**
• Accès illimité aux prédictions gagnantes
• Algorithmes mis à jour en continu
• Support prioritaire
• Gains potentiels exponentiels!

👇 **CHOISISSEZ VOTRE FORMULE ET REJOIGNEZ LES GAGNANTS!**"""
            
            await event.respond(expired_msg, buttons=buttons)

@client.on(events.NewMessage())
async def handle_registration_and_payment(event):
    if event.is_group or event.is_channel: return
    if event.message.message and event.message.message.startswith('/'): 
        return
    
    user_id = event.sender_id
    user = get_user(user_id)
    
    # Processus d'inscription
    if user_id in user_conversation_state:
        state = user_conversation_state[user_id]
        message_text = event.message.message.strip()
        
        if state == 'awaiting_nom':
            update_user(user_id, {'nom': message_text})
            user_conversation_state[user_id] = 'awaiting_prenom'
            await event.respond(f"""✅ **Nom enregistré: {message_text}**

📝 **Étape 2/3: Votre prénom?**
Cette information nous permet de personnaliser votre expérience.""")
        
        elif state == 'awaiting_prenom':
            update_user(user_id, {'prenom': message_text})
            user_conversation_state[user_id] = 'awaiting_pays'
            await event.respond(f"""✅ **Enchanté {message_text}!**

🌍 **Étape 3/3: Votre pays?**
Cela nous aide à adapter nos services à votre région.""")
        
        elif state == 'awaiting_pays':
            update_user(user_id, {
                'pays': message_text,
                'registered': True,
                'trial_started': datetime.now().isoformat(),
                'trial_used': False
            })
            del user_conversation_state[user_id]
            
            success_msg = f"""🎉 **FÉLICITATIONS {user.get('prenom', '').upper()}!** 🎉

✅ Votre compte est ACTIVÉ!
⏰ **60 MINUTES D'ESSAI GRATUIT** démarrées!

🚀 **Comment ça marche?**
1️⃣ Je surveille les canaux sources en temps réel
2️⃣ Mes algorithmes détectent les patterns gagnants
3️⃣ Vous recevez les prédictions INSTANTANÉMENT ici
4️⃣ Les résultats se mettent à jour automatiquement

💎 **Ce que vous allez recevoir:**
• 🎯 Prédictions précises avec couleur à jouer
• ⚡ Alertes en temps réel
• 📊 Mises à jour automatiques des résultats
• 🔥 Accès aux 2 algorithmes (Stats + Cycle)

⚠️ **IMPORTANT:** Restez dans ce chat, ne fermez pas Telegram!
Les meilleures opportunités arrivent sans prévenir!

🍀 **Bonne chance et bienvenue dans l'élite!**"""
            
            await event.respond(success_msg)
            logger.info(f"Nouvel utilisateur inscrit: {user_id} - {user.get('nom')} {user.get('prenom')}")
        return
    
    # Gestion paiement
    if user.get('awaiting_screenshot') and event.message.photo:
        update_user(user_id, {'awaiting_screenshot': False, 'awaiting_amount': True})
        await event.respond("""📸 **Paiement reçu!**

💰 **Dernière étape:** Indiquez le montant payé:
• `200` pour 24H
• `1000` pour 1 semaine  
• `2000` pour 2 semaines

⏳ Validation sous 5 minutes par notre équipe.""")
        return
    
    if user.get('awaiting_amount'):
        message_text = event.message.message.strip()
        if message_text in ['200', '1000', '2000']:
            amount = message_text
            update_user(user_id, {'awaiting_amount': False})
            
            admin_id = 1190237801
            user_info = get_user(user_id)
            
            if amount == '200':
                dur_text = "24 heures"
                dur_code = "1d"
            elif amount == '1000':
                dur_text = "1 semaine"
                dur_code = "1w"
            else:
                dur_text = "2 semaines"
                dur_code = "2w"

            msg_admin = (
                "🔔 **NOUVELLE DEMANDE D'ABONNEMENT**\n\n"
                f"👤 **Utilisateur:** {user_info.get('nom')} {user_info.get('prenom')}\n"
                f"🆔 **ID:** `{user_id}`\n"


@client.on(events.CallbackQuery(data=re.compile(b'valider_(\d+)_(.*)')))
async def handle_validation(event):
    admin_id = 1190237801
    if event.sender_id != admin_id:
        await event.answer("Accès refusé", alert=True)
        return
        
    user_id = int(event.data_match.group(1).decode())
    duration = event.data_match.group(2).decode()
    
    sub_type = 'premium'
    
    if duration == '1d':
        days = 1
    elif duration == '1w':
        days = 7
    else:
        days = 14
    
    end_date = datetime.now() + timedelta(days=days)
    update_user(user_id, {
        'subscription_end': end_date.isoformat(),
        'subscription_type': sub_type,
        'expiry_notified': False
    })
    
    try:
        @client.on(events.NewMessage())
async def handle_registration_and_payment(event):
    if event.is_group or event.is_channel: 
        return
    
    # Ignorer les commandes
    if event.message.message and event.message.message.startswith('/'): 
        return
    
    user_id = event.sender_id
    user = get_user(user_id)
    
    # Vérifier si on est en mode inscription
    if user_id in user_conversation_state:
        state = user_conversation_state[user_id]
        message_text = event.message.message.strip()
        
        if state == 'awaiting_nom':
            if not message_text:
                await event.respond("❌ Veuillez entrer un nom valide.")
                return
                
            update_user(user_id, {'nom': message_text})
            user_conversation_state[user_id] = 'awaiting_prenom'
            await event.respond(f"""✅ **Nom enregistré: {message_text}**

📝 **Étape 2/3: Votre prénom?**""")
            return
        
        elif state == 'awaiting_prenom':
            if not message_text:
                await event.respond("❌ Veuillez entrer un prénom valide.")
                return
                
            update_user(user_id, {'prenom': message_text})
            user_conversation_state[user_id] = 'awaiting_pays'
            await event.respond(f"""✅ **Enchanté {message_text}!**

🌍 **Étape 3/3: Votre pays?**""")
            return
        
        elif state == 'awaiting_pays':
            if not message_text:
                await event.respond("❌ Veuillez entrer un pays valide.")
                return
            
            # Finaliser l'inscription
            update_user(user_id, {
                'pays': message_text,
                'registered': True,
                'trial_started': datetime.now().isoformat(),
                'trial_used': False
            })
            del user_conversation_state[user_id]
            
            # Message de confirmation
            success_msg = f"""🎉 **FÉLICITATIONS {message_text.upper()}!** 🎉

✅ Votre compte est ACTIVÉ!
⏰ **60 MINUTES D'ESSAI GRATUIT** démarrées!

🚀 **Comment ça marche?**
1️⃣ Je surveille les canaux sources en temps réel
2️⃣ Mes algorithmes détectent les patterns gagnants
3️⃣ Vous recevez les prédictions INSTANTANÉMENT ici
4️⃣ Les résultats se mettent à jour automatiquement

💎 **Ce que vous allez recevoir:**
• 🎯 Prédictions précises avec couleur à jouer
• ⚡ Alertes en temps réel
• 📊 Mises à jour automatiques des résultats
• 🔥 Accès aux 2 algorithmes (Stats + Cycle)

⚠️ **IMPORTANT:** Restez dans ce chat, ne fermez pas Telegram!
Les meilleures opportunités arrivent sans prévenir!

🍀 **Bonne chance et bienvenue dans l'élite!**"""
            
            await event.respond(success_msg)
            logger.info(f"✅ Nouvel utilisateur inscrit: {user_id} - {user.get('nom')} {message_text}")
            return
    
    # Gestion des paiements (hors inscription)
    if user.get('awaiting_screenshot') and event.message.photo:
        update_user(user_id, {'awaiting_screenshot': False, 'awaiting_amount': True})
        await event.respond("""📸 **Paiement reçu!**

💰 **Dernière étape:** Indiquez le montant payé:
• `200` pour 24H
• `1000` pour 1 semaine  
• `2000` pour 2 semaines

⏳ Validation sous 5 minutes par notre équipe.""")
        return
    
    if user.get('awaiting_amount'):
        message_text = event.message.message.strip()
        if message_text in ['200', '1000', '2000']:
            amount = message_text
            update_user(user_id, {'awaiting_amount': False})
            
            admin_id = 1190237801
            user_info = get_user(user_id)
            
            if amount == '200':
                dur_text = "24 heures"
                dur_code = "1d"
            elif amount == '1000':
                dur_text = "1 semaine"
                dur_code = "1w"
            else:
                dur_text = "2 semaines"
                dur_code = "2w"

            msg_admin = (
                "🔔 **NOUVELLE DEMANDE D'ABONNEMENT**\n\n"
                f"👤 **Utilisateur:** {user_info.get('nom')} {user_info.get('prenom')}\n"
                f"🆔 **ID:** `{user_id}`\n"
                f"💰 **Montant:** {amount} FCFA\n"
                f"📅 **Durée:** {dur_text}\n"
                f"📍 **Pays:** {user_info.get('pays')}\n\n"
                "Vérifier le paiement et valider."
            )
            
            buttons = [
                [Button.inline(f"✅ Valider {dur_text}", data=f"valider_{user_id}_{dur_code}")],
                [Button.inline("❌ Rejeter", data=f"rejeter_{user_id}")]
            ]
            
            try:
                await client.send_message(admin_id, msg_admin, buttons=buttons)
            except Exception as e:
                logger.error(f"Erreur notification admin: {e}")

            await event.respond("""✅ **DEMANDE ENVOYÉE!**

⏳ Notre équipe vérifie votre paiement...
🚀 Votre accès sera activé sous 5 minutes maximum!

📱 Vous recevrez une confirmation ici même.

💎 **Préparez-vous à gagner!**""")
        else:
            await event.respond("❌ Montant invalide. Répondez avec `200`, `1000` ou `2000`.")
        return
activation_msg = f"""🎉 **FÉLICITATIONS! VOTRE ACCÈS EST ACTIVÉ!** 🎉

✅ Abonnement **{days} jour(s)** confirmé!
🔥 Vous faites maintenant partie de l'ELITE!

🚀 **Vos avantages:**
• Prédictions prioritaires
• Algorithmes exclusifs
• Mises à jour en temps réel
• Support dédié

💰 **C'est parti pour les gains!**

⚡ Restez attentif, votre première prédiction pourrait arriver dès maintenant!"""
        
        await client.send_message(user_id, activation_msg)
    except Exception as e:
        logger.error(f"Erreur notification user {user_id}: {e}")
        
    await event.edit(f"✅ Abonnement activé pour {user_id}")
    await event.answer("Activé!")

@client.on(events.CallbackQuery(data=re.compile(b'rejeter_(\d+)')))
async def handle_rejection(event):
    admin_id = 1190237801
    if event.sender_id != admin_id:
        await event.answer("Accès refusé", alert=True)
        return
        
    user_id = int(event.data_match.group(1).decode())
    
    try:
        await client.send_message(user_id, "❌ Demande rejetée. Contactez le support si erreur.")
    except:
        pass
        
    await event.edit(f"❌ Rejeté pour {user_id}")
    await event.answer("Rejeté")

@client.on(events.NewMessage(pattern=r'^/a (\d+)$'))
async def cmd_set_a_shortcut(event):
    if event.is_group or event.is_channel: return
    if event.sender_id != ADMIN_ID and ADMIN_ID != 0: return
    
    global USER_A
    try:
        val = int(event.pattern_match.group(1))
        USER_A = val
        await event.respond(f"✅ Valeur 'a' = {USER_A}")
    except Exception as e:
        await event.respond(f"❌ Erreur: {e}")

@client.on(events.NewMessage(pattern=r'^/set_a (\d+)$'))
async def cmd_set_a(event):
    if event.is_group or event.is_channel: return
    if event.sender_id != ADMIN_ID and ADMIN_ID != 0: return
    
    global USER_A
    try:
        val = int(event.pattern_match.group(1))
        USER_A = val
        await event.respond(f"✅ Paramètre 'a' = {USER_A}\nCible: N+{USER_A}")
    except Exception as e:
        await event.respond(f"❌ Erreur: {e}")

@client.on(events.NewMessage(pattern='/status'))
async def cmd_status(event):
    if event.is_group or event.is_channel: return
    if event.sender_id != ADMIN_ID and ADMIN_ID != 0:
        await event.respond("Commande admin uniquement")
        return

    status_msg = f"""📊 **STATUT SYSTÈME**

🎮 Jeu actuel: #{current_game_number}
🔢 Paramètre 'a': {USER_A}
⏳ Règle 2: {'ACTIVE 🔥' if rule2_active else 'Inactif'}
⏱️ Règle 1: {rule1_consecutive_count}/{MAX_RULE1_CONSECUTIVE}
👥 Utilisateurs: {len(users_data)}

**Prédictions actives: {len(pending_predictions)}**"""
    
    if pending_predictions:
        for game_num, pred in sorted(pending_predictions.items()):
            distance = game_num - current_game_number
            ratt = f" [R{pred['rattrapage']}]" if pred.get('rattrapage', 0) > 0 else ""
            rule = pred.get('rule_type', 'R2')
            status_msg += f"\n• #{game_num}{ratt}: {pred['suit']} ({rule}) - {pred['status']} (dans {distance})"

    await event.respond(status_msg)

@client.on(events.NewMessage(pattern='/bilan'))
async def cmd_bilan(event):
    if event.is_group or event.is_channel: return
    admin_id = 1190237801
    if event.sender_id != admin_id: return
    
    if stats_bilan['total'] == 0:
        await event.respond("📊 Aucune prédiction encore.")
        return

    win_rate = (stats_bilan['wins'] / stats_bilan['total']) * 100 if stats_bilan['total'] > 0 else 0
    
    msg = f"""📊 **BILAN PERFORMANCE**

🎯 Total: {stats_bilan['total']} prédictions
✅ Victoires: {stats_bilan['wins']} ({win_rate:.1f}%)
❌ Défaites: {stats_bilan['losses']}

**Détails victoires:**
• Immédiates: {stats_bilan['win_details'].get('✅0️⃣', 0)}
• 2ème jeu: {stats_bilan['win_details'].get('✅1️⃣', 0)}
• 3ème jeu: {stats_bilan['win_details'].get('✅2️⃣', 0)}"""
    
    await event.respond(msg)

@client.on(events.NewMessage(pattern='/reset'))
async def cmd_reset_all(event):
    if event.is_group or event.is_channel: return
    admin_id = 1190237801
    if event.sender_id != admin_id:
        await event.respond("❌ Admin uniquement")
        return
    
    global users_data, pending_predictions, queued_predictions, processed_messages
    global current_game_number, last_source_game_number, stats_bilan
    global rule1_consecutive_count, rule2_active, suit_prediction_counts
    global last_known_source_game, prediction_target_game, waiting_for_one_part, cycle_triggered
    global current_time_cycle_index, next_prediction_allowed_at, already_predicted_games
    
    users_data = {}
    save_users_data()
    pending_predictions.clear()
    queued_predictions.clear()
    processed_messages.clear()
    already_predicted_games.clear()
    suit_prediction_counts.clear()
    
    current_game_number = 0
    last_source_game_number = 0
    last_known_source_game = 0
    prediction_target_game = None
    waiting_for_one_part = False
    cycle_triggered = False
    current_time_cycle_index = 0
    next_prediction_allowed_at = datetime.now()
    
    rule1_consecutive_count = 0
    rule2_active = False
    
    stats_bilan = {
        'total': 0,
        'wins': 0,
        'losses': 0,
        'win_details': {'✅0️⃣': 0, '✅1️⃣': 0, '✅2️⃣': 0},
        'loss_details': {'❌': 0}
    }
    
    logger.warning(f"🚨 RESET par admin {event.sender_id}")
    await event.respond("🚨 **RESET TOTAL EFFECTUÉ**")

@client.on(events.NewMessage(pattern='/help'))
async def cmd_help(event):
    if event.is_group or event.is_channel: return
    
    help_msg = """📖 **CENTRE D'AIDE**

🎯 **Comment utiliser le bot:**
1️⃣ Inscrivez-vous avec /start
2️⃣ Recevez vos 60min d'essai GRATUIT
3️⃣ Attendez les prédictions dans ce chat
4️⃣ Les résultats se mettent à jour auto!

🧠 **Nos algorithmes:**
• **Règle 2 (Stats)** - Prioritaire, analyse les décalages statistiques
• **Règle 1 (Cycle)** - Fallback basé sur les patterns temporels

💰 **Tarifs:**
• 200 FCFA = 24H
• 1000 FCFA = 1 semaine
• 2000 FCFA = 2 semaines

📊 **Commandes:**
/start - Votre profil & statut
/status - État du système (admin)
/bilan - Statistiques (admin)
/payer - S'abonner

❓ **Support:** Contactez @votre_support"""
    
    await event.respond(help_msg)

@client.on(events.NewMessage(pattern='/payer'))
async def cmd_payer(event):
    if event.is_group or event.is_channel: return
    
    user_id = event.sender_id
    user = get_user(user_id)
    
    if not user.get('registered'):
        await event.respond("❌ Inscrivez-vous d'abord avec /start")
        return
    
    buttons = [
        [Button.url("⚡ 24H - 200 FCFA", PAYMENT_LINK_24H)],
        [Button.url("🔥 1 SEMAINE - 1000 FCFA", PAYMENT_LINK)],
        [Button.url("💎 2 SEMAINES - 2000 FCFA", PAYMENT_LINK)]
    ]
    
    payment_msg = f"""💳 **DÉBLOQUEZ VOTRE POTENTIEL GAGNANT!** 💳

🎰 {user.get('prenom', 'CHAMPION')}, choisissez votre formule:

⚡ **24 HEURES - 200 FCFA**
Test prolongé, idéal pour découvrir

🔥 **1 SEMAINE - 1000 FCFA**  
Le choix des gagnants confirmés

💎 **2 SEMAINES - 2000 FCFA**
Le meilleur rapport qualité/prix!

📸 **Après paiement:**
1. Envoyez capture d'écran ici
2. Indiquez le montant (200/1000/2000)
3. Validation sous 5min!

👇 **CLIQUEZ SUR VOTRE FORMULE:**"""
    
    await event.respond(payment_msg, buttons=buttons)
    update_user(user_id, {'pending_payment': True, 'awaiting_screenshot': True})

# --- Serveur Web et Démarrage ---

async def index(request):
    html = f"""<!DOCTYPE html>
<html>
<head>
    <title>Bot Prédiction Baccarat - Elite</title>
    <style>
        body {{ font-family: Arial, sans-serif; background: linear-gradient(135deg, #1e3c72 0%, #2a5298 100%); color: white; text-align: center; padding: 50px; }}
        h1 {{ font-size: 3em; margin-bottom: 20px; text-shadow: 2px 2px 4px rgba(0,0,0,0.3); }}
        .status {{ background: rgba(255,255,255,0.1); padding: 30px; border-radius: 15px; display: inline-block; margin: 20px; }}
        .number {{ font-size: 2.5em; font-weight: bold; color: #ffd700; }}
        .label {{ font-size: 1.2em; opacity: 0.9; }}
    </style>
</head>
<body>
    <h1>🎰 Bot Prédiction Baccarat ELITE</h1>
    <div class="status">
        <div class="label">Jeu Actuel</div>
        <div class="number">#{current_game_number}</div>
    </div>
    <div class="status">
        <div class="label">Utilisateurs</div>
        <div class="number">{len(users_data)}</div>
    </div>
    <div class="status">
        <div class="label">Règle 2</div>
        <div class="number">{'ACTIVE 🔥' if rule2_active else 'Standby'}</div>
    </div>
    <p style="margin-top: 40px; font-size: 1.1em;">Système opérationnel | Algorithmes actifs</p>
</body>
</html>"""
    return web.Response(text=html, content_type='text/html', status=200)

async def health_check(request):
    return web.Response(text="OK", status=200)

async def start_web_server():
    """Démarre le serveur web pour health check."""
    app = web.Application()
    app.router.add_get('/', index)
    app.router.add_get('/health', health_check)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', PORT)
    await site.start() 

async def schedule_daily_reset():
    """Reset quotidien à 00h59 WAT."""
    global rule1_consecutive_count, rule2_active, suit_prediction_counts
    
    wat_tz = timezone(timedelta(hours=1)) 
    reset_time = time(0, 59, tzinfo=wat_tz)

    logger.info(f"Reset planifié à {reset_time} WAT")

    while True:
        now = datetime.now(wat_tz)
        target_datetime = datetime.combine(now.date(), reset_time, tzinfo=wat_tz)
        if now >= target_datetime:
            target_datetime += timedelta(days=1)
            
        time_to_wait = (target_datetime - now).total_seconds()
        logger.info(f"Prochain reset dans {timedelta(seconds=time_to_wait)}")
        await asyncio.sleep(time_to_wait)

        logger.warning("🚨 RESET QUOTIDIEN!")
        
        global pending_predictions, queued_predictions, processed_messages
        global current_game_number, last_source_game_number, stats_bilan
        global last_known_source_game, prediction_target_game, waiting_for_one_part, cycle_triggered
        global current_time_cycle_index, next_prediction_allowed_at, already_predicted_games
        
        pending_predictions.clear()
        queued_predictions.clear()
        processed_messages.clear()
        already_predicted_games.clear()
        suit_prediction_counts.clear()
        
        current_game_number = 0
        last_source_game_number = 0
        last_known_source_game = 0
        prediction_target_game = None
        waiting_for_one_part = False
        cycle_triggered = False
        current_time_cycle_index = 0
        next_prediction_allowed_at = datetime.now()
        
        rule1_consecutive_count = 0
        rule2_active = False
        
        stats_bilan = {
            'total': 0,
            'wins': 0,
            'losses': 0,
            'win_details': {'✅0️⃣': 0, '✅1️⃣': 0, '✅2️⃣': 0},
            'loss_details': {'❌': 0}
        }
        
        logger.warning("✅ Reset effectué.")

async def start_bot():
    """Démarre le client Telegram."""
    try:
        await client.start(bot_token=BOT_TOKEN)
        logger.info("✅ Bot connecté et opérationnel!")
        return True
    except Exception as e:
        logger.error(f"❌ Erreur connexion: {e}")
        return False

async def main():
    """Fonction principale."""
    load_users_data()
    try:
        await start_web_server()
        success = await start_bot()
        if not success:
            logger.error("Échec démarrage")
            return

        asyncio.create_task(schedule_daily_reset())
        
        logger.info("🚀 BOT OPÉRATIONNEL - En attente de messages...")
        await client.run_until_disconnected()

    except Exception as e:
        logger.error(f"Erreur main: {e}")
        import traceback
        logger.error(traceback.format_exc())
    finally:
        if client.is_connected():
            await client.disconnect()

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("👋 Bot arrêté")
    except Exception as e:
        logger.error(f"Erreur fatale: {e}")
        import traceback
        logger.error(traceback.format_exc())
