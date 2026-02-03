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

# Configuration des liens de paiement
PAYMENT_LINK_24H = "https://my.moneyfusion.net/6977f7502181d4ebf722398d"
PAYMENT_LINK_1W = "https://my.moneyfusion.net/6977f7502181d4ebf722398d"
PAYMENT_LINK_2W = "https://my.moneyfusion.net/6977f7502181d4ebf722398d"
USERS_FILE = "users_data.json"

# Configuration pour l'administrateur
ADMIN_NAME = "Sossou Kouamé"
ADMIN_TITLE = "Administrateur et développeur de ce Bot"

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
last_source_game_number = 0  # Dernier numéro vu dans le canal source
last_finalized_game_number = 0  # Dernier numéro finalisé
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
MAX_RULE1_CONSECUTIVE = 3

# Flag pour savoir si une prédiction Règle 2 est en cours
rule2_active = False

# NOUVEAU: Gestion des limites R2
r2_consecutive_same_suit = {}  # {suit: count}
MAX_R2_SAME_SUIT = 3
r2_blocked_until_r1_count = 0  # Nombre de prédictions R1 à attendre
r2_current_r1_predictions = 0  # Compteur de R1 depuis blocage

# Stats et autres
already_predicted_games = set()
stats_bilan = {
    'total': 0,
    'wins': 0,
    'losses': 0,
    'win_details': {'✅0️⃣': 0, '✅1️⃣': 0, '✅2️⃣': 0},
    'loss_details': {'❌': 0}
}

# --- Prédictions manuelles ---
manual_predictions = {}  # {game_number: {'suit': suit, 'status': status, 'private_messages': {}}}
admin_manual_state = {}

# --- Système de Paiement et Utilisateurs ---
users_data = {}
user_conversation_state = {}
admin_message_state = {}
payment_pending_state = {}

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
    if user_id == ADMIN_ID:
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
    user = get_user(user_id)
    if user.get('trial_used') or not user.get('trial_started'):
        return False
    try:
        trial_start = datetime.fromisoformat(user['trial_started'])
        trial_end = trial_start + timedelta(minutes=60)
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

def get_user_status(user_id: int) -> str:
    if is_user_subscribed(user_id):
        return "✅ Abonné"
    elif is_trial_active(user_id):
        return "🎁 Essai actif"
    elif get_user(user_id).get('trial_used'):
        return "⏰ Essai terminé"
    else:
        return "❌ Non inscrit"

# ============================================================
# FONCTIONS UTILITAIRES
# ============================================================

def get_next_suit_in_cycle(current_suit: str) -> str:
    try:
        current_index = SUIT_CYCLE.index(current_suit)
        next_index = (current_index + 1) % len(SUIT_CYCLE)
        return SUIT_CYCLE[next_index]
    except ValueError:
        return SUIT_CYCLE[0]

def get_suit_for_game(game_number: int) -> str:
    if game_number >= 6:
        count_valid = 0
        for n in range(6, game_number + 1, 2):
            if n % 10 != 0:
                count_valid += 1
        if count_valid > 0:
            index = (count_valid - 1) % len(SUIT_CYCLE)
            return SUIT_CYCLE[index]
    return '♥'

def get_next_prediction_info(current_game: int, current_suit: str) -> tuple:
    next_game = current_game + 2
    while next_game % 10 == 0:
        next_game += 2
    next_suit = get_next_suit_in_cycle(current_suit)
    return next_game, next_suit

# ============================================================
# ENVOI DES PRÉDICTIONS AUX UTILISATEURS (CORRIGÉ)
# ============================================================

async def send_prediction_to_all_users(prediction_msg: str, target_game: int, rule_type: str = "R2", 
                                       current_suit: str = None, is_manual: bool = False):
    """Envoie la prédiction à TOUS les utilisateurs éligibles."""
    private_messages = {}
    sent_count = 0
    failed_count = 0

    # Le prochain numéro n'est affiché que lors de la mise à jour après vérification
    # Pas au moment de l'envoi initial
    next_game_info = ""

    full_message = prediction_msg + next_game_info

    logger.info(f"📤 Envoi prédiction #{target_game} aux utilisateurs...")

    # Envoyer à l'admin aussi
    try:
        if ADMIN_ID and ADMIN_ID != 0:
            admin_msg = await client.send_message(ADMIN_ID, full_message)
            private_messages[str(ADMIN_ID)] = admin_msg.id
            logger.info(f"✅ Prédiction envoyée à l'admin {ADMIN_ID}")
    except Exception as e:
        logger.error(f"❌ Erreur envoi à l'admin {ADMIN_ID}: {e}")
        failed_count += 1

    # Envoyer à tous les utilisateurs enregistrés
    for user_id_str, user_info in users_data.items():
        try:
            user_id = int(user_id_str)

            if user_id == ADMIN_ID:
                continue

            if not can_receive_predictions(user_id):
                continue

            sent_msg = await client.send_message(user_id, full_message)
            private_messages[user_id_str] = sent_msg.id
            sent_count += 1
            logger.info(f"✅ Prédiction envoyée à {user_id}")

        except Exception as e:
            failed_count += 1
            logger.error(f"❌ Erreur envoi prédiction à {user_id_str}: {e}")

    logger.info(f"📊 Envoi terminé: {sent_count} succès, {failed_count} échecs")
    return private_messages

async def edit_prediction_for_all_users(game_number: int, new_status: str, suit: str, rule_type: str, 
                                        original_game: int = None, is_manual: bool = False):
    """Édite les messages de prédiction pour TOUS les utilisateurs."""
    display_game = original_game if original_game else game_number

    # CORRECTION: Calculer le prochain numéro à partir du NUMÉRO DE PRÉDICTION ORIGINAL
    base_game_for_next = original_game if original_game else game_number

    # Afficher le prochain numéro APRÈS chaque vérification (victoire OU échec)
    next_game_info = ""
    if new_status in ['✅0️⃣', '✅1️⃣', '✅2️⃣', '❌']:
        next_game, next_suit = get_next_prediction_info(base_game_for_next, suit)
        next_game_info = f"\n\n📊 **Prochain:** #{next_game} {SUIT_DISPLAY.get(next_suit, next_suit)}"

    # Format du message mis à jour
    if is_manual:
        if new_status == "❌":
            status_text = "❌ PERDU"
        elif new_status == "✅0️⃣":
            status_text = "✅ VICTOIRE IMMÉDIATE!"
        elif new_status == "✅1️⃣":
            status_text = "✅ VICTOIRE AU 2ÈME JEU!"
        elif new_status == "✅2️⃣":
            status_text = "✅ VICTOIRE AU 3ÈME JEU!"
        else:
            status_text = f"{new_status}"

        updated_msg = f"""🎰 **PRÉDICTION MANUELLE #{display_game}**

🎯 Couleur: {SUIT_DISPLAY.get(suit, suit)}
📊 Statut: {status_text}
🤖 Type: Manuel""" + next_game_info
    elif rule_type == "R2":
        if new_status == "❌":
            status_text = "❌ PERDU"
        elif new_status == "✅0️⃣":
            status_text = "✅ VICTOIRE IMMÉDIATE!"
        elif new_status == "✅1️⃣":
            status_text = "✅ VICTOIRE AU 2ÈME JEU!"
        elif new_status == "✅2️⃣":
            status_text = "✅ VICTOIRE AU 3ÈME JEU!"
        else:
            status_text = f"{new_status}"

        updated_msg = f"""🎰 **PRÉDICTION #{display_game}**

🎯 Couleur: {SUIT_DISPLAY.get(suit, suit)}
📊 Statut: {status_text}
🤖 Algorithme: Règle 2 (Stats)""" + next_game_info
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
🤖 Algorithme: Règle 1 (Cycle)""" + next_game_info

    predictions_dict = manual_predictions if is_manual else pending_predictions

    if game_number not in predictions_dict:
        logger.warning(f"Jeu #{game_number} non trouvé pour édition")
        return 0

    pred = predictions_dict[game_number]
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
    """Extrait le numéro de jeu du message - CORRIGÉ pour être plus robuste."""
    # Chercher #N suivi d'un nombre
    match = re.search(r"#N\s*(\d+)", message, re.IGNORECASE)
    if match:
        return int(match.group(1))
    # Chercher # suivi d'un nombre
    match = re.search(r"#(\d+)", message)
    if match:
        return int(match.group(1))
    # Chercher juste un nombre au début
    match = re.search(r"^(\d+)", message.strip())
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
    """Vérifie si la couleur cible est présente dans le groupe."""
    normalized = normalize_suits(group_str)
    target_normalized = normalize_suits(target_suit)
    for suit in ALL_SUITS:
        if suit in target_normalized and suit in normalized:
            return True
    return False

def get_predicted_suit(missing_suit: str) -> str:
    """Applique le mapping personnalisé."""
    return SUIT_MAPPING.get(missing_suit, missing_suit)

def is_one_part_away(current: int, target: int) -> bool:
    """Vérifie si current est à 1 part de target."""
    return current % 2 != 0 and target - current == 1

def is_message_finalized(message: str) -> bool:
    """Vérifie si le message est finalisé."""
    if '⏰' in message:
        return False
    return '✅' in message or '🔰' in message or '▶️' in message or 'Finalisé' in message or 'FINAL' in message.upper()

# ============================================================
# LOGIQUE DE PRÉDICTION ET FILE D'ATTENTE
# ============================================================

async def send_prediction_to_users(target_game: int, predicted_suit: str, base_game: int, 
                                     rattrapage=0, original_game=None, rule_type="R2"):
    """Envoie la prédiction à TOUS les utilisateurs en privé."""
    global rule2_active, rule1_consecutive_count

    try:
        # Si c'est un rattrapage
        if rattrapage > 0:
            original_private_msgs = {}
            if original_game and original_game in pending_predictions:
                original_private_msgs = pending_predictions[original_game].get('private_messages', {}).copy()

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
            logger.info(f"Rattrapage {rattrapage} actif pour #{target_game}")
            return True

        # Vérifier si une prédiction Règle 2 est déjà active
        if rule_type == "R1":
            active_r2_predictions = [p for game, p in pending_predictions.items() 
                                    if p.get('rule_type') == 'R2' and p.get('rattrapage', 0) == 0 
                                    and game > current_game_number]
            if active_r2_predictions:
                logger.info(f"Règle 2 active, Règle 1 ne peut pas prédire #{target_game}")
                return False

        # Format du message
        if rule_type == "R2":
            prediction_msg = f"""🎰 **PRÉDICTION #{target_game}**

🎯 Couleur: {SUIT_DISPLAY.get(predicted_suit, predicted_suit)}
⏳ Statut: ⏳ EN ATTENTE...
🤖 Algorithme: de confiance"""
        else:
            prediction_msg = f"""🎰 **PRÉDICTION #{target_game}**

🎯 Couleur: {SUIT_DISPLAY.get(predicted_suit, predicted_suit)}
⏳ Statut: ⏳ EN ATTENTE...
🤖 Algorithme: de confiance"""

        # CORRECTION: Stocker la prédiction AVANT l'envoi
        # Même si aucun utilisateur n'est abonné, on garde la trace
        pending_predictions[target_game] = {
            'message_id': 0,
            'suit': predicted_suit,
            'base_game': base_game,
            'status': '⌛',
            'check_count': 0,
            'rattrapage': 0,
            'rule_type': rule_type,
            'private_messages': {},  # Sera rempli après envoi
            'created_at': datetime.now().isoformat()
        }

        # ENVOI À TOUS LES UTILISATEURS
        private_messages = await send_prediction_to_all_users(prediction_msg, target_game, rule_type, predicted_suit)

        # Mettre à jour avec les messages envoyés
        if private_messages:
            pending_predictions[target_game]['private_messages'] = private_messages
            logger.info(f"✅ Prédiction #{target_game} envoyée à {len(private_messages)} utilisateurs")
        else:
            logger.warning(f"⚠️  Prédiction #{target_game} créée mais aucun utilisateur abonné")

        # Mise à jour des flags
        if rule_type == "R2":
            rule2_active = True
            rule1_consecutive_count = 0
            logger.info(f"✅ Règle 2: Prédiction #{target_game} - {predicted_suit} envoyée")
        else:
            rule1_consecutive_count += 1
            logger.info(f"✅ Règle 1: Prédiction #{target_game} - {predicted_suit} envoyée")

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
    logger.info(f"📋 Prédiction #{target_game} mise en file d'attente ({rule_type})")
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

async def update_prediction_status(game_number: int, new_status: str, is_manual: bool = False):
    """Met à jour le statut de la prédiction pour tous les utilisateurs."""
    global rule2_active, rule1_consecutive_count

    try:
        predictions_dict = manual_predictions if is_manual else pending_predictions

        if game_number not in predictions_dict:
            logger.warning(f"Tentative de mise à jour pour jeu #{game_number} non trouvé")
            return False

        pred = predictions_dict[game_number]
        suit = pred['suit']
        rule_type = pred.get('rule_type', 'R2')
        rattrapage = pred.get('rattrapage', 0)
        original_game = pred.get('original_game', game_number)

        logger.info(f"Mise à jour statut #{game_number} [{rule_type}] vers {new_status}")

        # Éditer les messages pour tous les utilisateurs
        await edit_prediction_for_all_users(game_number, new_status, suit, rule_type, original_game, is_manual)

        pred['status'] = new_status

        # Mise à jour des statistiques
        if new_status in ['✅0️⃣', '✅1️⃣', '✅2️⃣']:
            stats_bilan['total'] += 1
            stats_bilan['wins'] += 1
            stats_bilan['win_details'][new_status] = (stats_bilan['win_details'].get(new_status, 0) + 1)

            if not is_manual:
                if rule_type == "R2" and rattrapage == 0:
                    rule2_active = False
                elif rule_type == "R1":
                    rule1_consecutive_count = 0

            if game_number in predictions_dict:
                del predictions_dict[game_number]

            if not is_manual:
                asyncio.create_task(check_and_send_queued_predictions(current_game_number))

        elif new_status == '❌':
            stats_bilan['total'] += 1
            stats_bilan['losses'] += 1
            stats_bilan['loss_details']['❌'] += 1

            if not is_manual:
                if rule_type == "R2" and rattrapage == 0:
                    rule2_active = False
                elif rule_type == "R1":
                    rule1_consecutive_count = 0

            if game_number in predictions_dict:
                del predictions_dict[game_number]

            if not is_manual:
                asyncio.create_task(check_and_send_queued_predictions(current_game_number))

        return True

    except Exception as e:
        logger.error(f"Erreur update_prediction_status: {e}")
        import traceback
        logger.error(traceback.format_exc())
        return False

# ============================================================
# VÉRIFICATION DES RÉSULTATS - CORRIGÉ
# ============================================================

async def check_prediction_result(game_number: int, first_group: str):
    """Vérifie les résultats selon la séquence ✅0️⃣, ✅1️⃣, ✅2️⃣ ou ❌."""
    global current_game_number
    logger.info(f"🔍 Vérification résultat pour jeu #{game_number}, groupe: {first_group}")

    # 1. Vérification pour les prédictions AUTO (Cible N)
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
                # Échec, planifier rattrapage N+1
                next_target = game_number + 1
                if next_target not in pending_predictions:
                    queue_prediction(next_target, target_suit, pred['base_game'], 
                                   rattrapage=1, original_game=game_number, rule_type=rule_type)
                    logger.info(f"📋 Rattrapage 1 planifié pour #{next_target}")

    # 2. Vérification pour les rattrapages AUTO
    for target_game, pred in list(pending_predictions.items()):
        if target_game == game_number and pred.get('rattrapage', 0) > 0:
            original_game = pred.get('original_game', target_game - pred['rattrapage'])
            target_suit = pred['suit']
            rattrapage_actuel = pred['rattrapage']
            rule_type = pred.get('rule_type', 'R2')

            if has_suit_in_group(first_group, target_suit):
                # Victoire au rattrapage
                status_map = {1: '✅1️⃣', 2: '✅2️⃣'}
                status = status_map.get(rattrapage_actuel, f'✅{rattrapage_actuel}️⃣')
                logger.info(f"{status} Trouvé pour #{original_game} au rattrapage {rattrapage_actuel}!")
                await update_prediction_status(original_game, status)
                if target_game != original_game and target_game in pending_predictions:
                    del pending_predictions[target_game]
                return
            else:
                # Échec du rattrapage
                if rattrapage_actuel < 2:  # Max 2 rattrapages (N+1 et N+2)
                    next_rattrapage = rattrapage_actuel + 1
                    next_target = game_number + 1
                    if next_target not in pending_predictions:
                        queue_prediction(next_target, target_suit, pred['base_game'], 
                                       rattrapage=next_rattrapage, original_game=original_game,
                                       rule_type=rule_type)
                        logger.info(f"📋 Rattrapage {next_rattrapage} planifié pour #{next_target}")
                    if target_game in pending_predictions:
                        del pending_predictions[target_game]
                else:
                    # Max rattrapages atteint
                    logger.info(f"❌ Définitif pour #{original_game} après {rattrapage_actuel} rattrapages")
                    await update_prediction_status(original_game, '❌')
                    if target_game != original_game and target_game in pending_predictions:
                        del pending_predictions[target_game]
                return

    # 3. Vérification pour les prédictions MANUELLES
    if game_number in manual_predictions:
        pred = manual_predictions[game_number]
        if pred.get('rattrapage', 0) == 0:
            target_suit = pred['suit']

            if has_suit_in_group(first_group, target_suit):
                logger.info(f"✅0️⃣ Trouvé pour prédiction manuelle #{game_number}!")
                await update_prediction_status(game_number, '✅0️⃣', True)
                return
            else:
                # Échec, planifier rattrapage
                next_target = game_number + 1
                if next_target not in manual_predictions:
                    manual_predictions[next_target] = {
                        'suit': target_suit,
                        'original_game': game_number,
                        'rattrapage': 1,
                        'private_messages': pred.get('private_messages', {}),
                        'created_at': datetime.now().isoformat(),
                        'status': '⌛'
                    }
                    logger.info(f"📋 Rattrapage 1 planifié pour manuelle #{game_number} -> #{next_target}")
                if game_number in manual_predictions:
                    del manual_predictions[game_number]

    # 4. Vérification pour les rattrapages MANUELS
    for target_game, pred in list(manual_predictions.items()):
        if target_game == game_number and pred.get('rattrapage', 0) > 0:
            original_game = pred.get('original_game', target_game)
            target_suit = pred['suit']
            rattrapage_actuel = pred['rattrapage']

            if has_suit_in_group(first_group, target_suit):
                status_map = {1: '✅1️⃣', 2: '✅2️⃣'}
                status = status_map.get(rattrapage_actuel, f'✅{rattrapage_actuel}️⃣')
                logger.info(f"{status} Trouvé pour manuelle #{original_game} au rattrapage {rattrapage_actuel}!")
                await update_prediction_status(original_game, status, True)
                if target_game in manual_predictions:
                    del manual_predictions[target_game]
                return
            else:
                if rattrapage_actuel < 2:
                    next_rattrapage = rattrapage_actuel + 1
                    next_target = game_number + 1
                    manual_predictions[next_target] = {
                        'suit': target_suit,
                        'original_game': original_game,
                        'rattrapage': next_rattrapage,
                        'private_messages': pred.get('private_messages', {}),
                        'created_at': datetime.now().isoformat(),
                        'status': '⌛'
                    }
                    logger.info(f"📋 Rattrapage {next_rattrapage} planifié pour manuelle #{original_game}")
                    if target_game in manual_predictions:
                        del manual_predictions[target_game]
                else:
                    logger.info(f"❌ Définitif pour manuelle #{original_game}")
                    await update_prediction_status(original_game, '❌', True)
                    if target_game in manual_predictions:
                        del manual_predictions[target_game]
                return

# ============================================================
# RÈGLE 2 : Prédiction par Statistiques
# ============================================================

async def process_stats_message(message_text: str):
    """Traite les statistiques du canal 2."""
    global last_source_game_number, suit_prediction_counts, rule2_active
    global r2_blocked_until_r1_count, r2_current_r1_predictions

    # NOUVEAU: Vérifier si R2 est bloqué (doit attendre 2 prédictions R1)
    if r2_blocked_until_r1_count > 0:
        if r2_current_r1_predictions >= r2_blocked_until_r1_count:
            # Assez de prédictions R1, débloquer
            r2_blocked_until_r1_count = 0
            r2_current_r1_predictions = 0
            logger.info("R2 débloqué après 2 prédictions R1")
        else:
            logger.info(f"R2 bloqué, attend encore {r2_blocked_until_r1_count - r2_current_r1_predictions} prédictions R1")
            return False

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
                    continue

                logger.info(f"RÈGLE 2: Décalage {diff} entre {s1}({v1}) et {s2}({v2}). Prédiction: {predicted_suit}")

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
# RÈGLE 1 : Prédiction par Cycle
# ============================================================

async def try_launch_prediction_rule1():
    """Tente de lancer la prédiction Règle 1."""
    global waiting_for_one_part, prediction_target_game, cycle_triggered
    global current_time_cycle_index, next_prediction_allowed_at, rule1_consecutive_count
    global rule2_active

    if rule2_active:
        return False

    if rule1_consecutive_count >= MAX_RULE1_CONSECUTIVE:
        return False

    if not cycle_triggered or prediction_target_game is None:
        return False

    if is_one_part_away(last_known_source_game, prediction_target_game):
        logger.info(f"RÈGLE 1: Condition OK: {last_known_source_game} → {prediction_target_game}")

        predicted_suit = get_suit_for_game(prediction_target_game)

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
            logger.info(f"Règle 1: Prochain dans {wait_min} min")
            return True

    return False

async def process_prediction_logic_rule1(message_text: str, chat_id: int):
    """Gère le déclenchement du cycle de temps Règle 1."""
    global last_known_source_game, current_game_number
    global cycle_triggered, waiting_for_one_part, prediction_target_game
    global rule2_active, rule1_consecutive_count
    global next_prediction_allowed_at

    if chat_id != SOURCE_CHANNEL_ID:
        return

    game_number = extract_game_number(message_text)
    if game_number is None:
        return

    last_known_source_game = game_number
    logger.info(f"Règle 1: Dernier numéro source: #{game_number}")

    if waiting_for_one_part and cycle_triggered:
        await try_launch_prediction_rule1()
        return

    now = datetime.now()
    if now < next_prediction_allowed_at:
        return

    if rule2_active:
        return

    if rule1_consecutive_count >= MAX_RULE1_CONSECUTIVE:
        wait_min = TIME_CYCLE[current_time_cycle_index]
        next_prediction_allowed_at = now + timedelta(minutes=wait_min)
        current_time_cycle_index = (current_time_cycle_index + 1) % len(TIME_CYCLE)
        return

    logger.info(f"RÈGLE 1: Temps cycle arrivé")
    cycle_triggered = True

    candidate = game_number + 2
    while candidate % 2 != 0 or candidate % 10 == 0:
        candidate += 1

    prediction_target_game = candidate
    logger.info(f"Règle 1: Cible calculée: #{prediction_target_game}")

    success = await try_launch_prediction_rule1()

    if not success:
        waiting_for_one_part = True

# ============================================================
# GESTION DES MESSAGES - CORRIGÉ
# ============================================================

async def process_finalized_message(message_text: str, chat_id: int):
    """Traite les messages finalisés pour vérification des résultats."""
    global current_game_number, last_source_game_number, last_finalized_game_number

    try:
        if chat_id == SOURCE_CHANNEL_2_ID:
            await process_stats_message(message_text)
            await check_and_send_queued_predictions(current_game_number)
            return

        game_number = extract_game_number(message_text)
        if game_number is None:
            return

        # Mettre à jour le dernier numéro vu (même si pas finalisé)
        if game_number > last_source_game_number:
            last_source_game_number = game_number
            current_game_number = game_number
            logger.info(f"📊 Dernier numéro vu mis à jour: #{game_number}")

        # Vérifier si finalisé pour traiter les résultats
        if not is_message_finalized(message_text):
            return

        last_finalized_game_number = game_number
        logger.info(f"✅ Message finalisé détecté: #{game_number}")

        message_hash = f"{game_number}_{message_text[:50]}"
        if message_hash in processed_messages:
            return
        processed_messages.add(message_hash)

        groups = extract_parentheses_groups(message_text)
        if len(groups) < 1:
            return

        first_group = groups[0]
        logger.info(f"🎯 Groupe trouvé: {first_group}")

        # Vérifier les résultats
        await check_prediction_result(game_number, first_group)
        await check_and_send_queued_predictions(game_number)

    except Exception as e:
        logger.error(f"Erreur traitement finalisé: {e}")
        import traceback
        logger.error(traceback.format_exc())

async def handle_new_message(event):
    """Gère les nouveaux messages dans les canaux sources - CORRIGÉ."""
    global last_source_game_number, current_game_number
    try:
        # Récupérer le chat
        chat = await event.get_chat()
        chat_id = chat.id

        # Normaliser l'ID du chat (pour les canaux)
        if str(chat_id).startswith('-100'):
            normalized_chat_id = chat_id
        elif str(chat_id).startswith('-'):
            normalized_chat_id = int(f"-100{abs(chat_id)}")
        else:
            normalized_chat_id = chat_id

        message_text = event.message.message

        # EXTRAIRE ET METTRE À JOUR LE NUMÉRO IMMÉDIATEMENT
        game_num = extract_game_number(message_text)
        if game_num and game_num > last_source_game_number:
            last_source_game_number = game_num
            current_game_number = game_num
            logger.info(f"📊 Dernier numéro vu mis à jour: #{game_num}")

        logger.info(f"📨 Message reçu de chat_id={normalized_chat_id}: {message_text[:80]}...")

        # Canal source principal (résultats)
        if normalized_chat_id == SOURCE_CHANNEL_ID:
            logger.info(f"✅ Message du canal source 1 détecté")

            # Traiter la logique Règle 1
            await process_prediction_logic_rule1(message_text, SOURCE_CHANNEL_ID)

            # Traiter les messages finalisés (vérification des résultats)
            await process_finalized_message(message_text, SOURCE_CHANNEL_ID)

        # Canal source 2 (statistiques)
        elif normalized_chat_id == SOURCE_CHANNEL_2_ID:
            logger.info(f"✅ Message du canal source 2 détecté")
            await process_stats_message(message_text)
            await check_and_send_queued_predictions(current_game_number)

    except Exception as e:
        logger.error(f"Erreur handle_new_message: {e}")
        import traceback
        logger.error(traceback.format_exc())

async def handle_edited_message(event):
    """Gère les messages édités."""
    try:
        chat = await event.get_chat()
        chat_id = chat.id

        # Normaliser l'ID du chat
        if str(chat_id).startswith('-100'):
            normalized_chat_id = chat_id
        elif str(chat_id).startswith('-'):
            normalized_chat_id = int(f"-100{abs(chat_id)}")
        else:
            normalized_chat_id = chat_id

        message_text = event.message.message

        if normalized_chat_id == SOURCE_CHANNEL_ID:
            await process_prediction_logic_rule1(message_text, SOURCE_CHANNEL_ID)
            await process_finalized_message(message_text, SOURCE_CHANNEL_ID)

        elif normalized_chat_id == SOURCE_CHANNEL_2_ID:
            await process_stats_message(message_text)
            await check_and_send_queued_predictions(current_game_number)

    except Exception as e:
        logger.error(f"Erreur handle_edited_message: {e}")
        import traceback
        logger.error(traceback.format_exc())

# ============================================================
# PRÉDICTIONS MANUELLES - CORRIGÉ
# ============================================================

async def send_manual_predictions(game_numbers: list, admin_id: int):
    """Envoie des prédictions manuelles pour une liste de numéros."""
    global manual_predictions

    valid_games = []

    # Vérifier et filtrer les numéros
    for game_str in game_numbers:
        try:
            game_num = int(game_str.strip())
            if game_num % 2 == 0 and game_num % 10 != 0:
                valid_games.append(game_num)
            else:
                await client.send_message(admin_id, f"⚠️ Numéro ignoré {game_num}: doit être pair et ne pas terminer par 0")
        except ValueError:
            await client.send_message(admin_id, f"⚠️ Valeur ignorée '{game_str}': n'est pas un nombre valide")

    if not valid_games:
        await client.send_message(admin_id, "❌ Aucun numéro valide trouvé. Format: 202,384,786")
        return

    # Envoyer les prédictions aux utilisateurs
    for game_num in valid_games:
        suit = get_suit_for_game(game_num)

        prediction_msg = f"""🎰 **PRÉDICTION MANUELLE #{game_num}**

🎯 Couleur: {SUIT_DISPLAY.get(suit, suit)}
⏳ Statut: ⏳ EN ATTENTE...
🤖 Type: Manuel"""

        private_messages = await send_prediction_to_all_users(prediction_msg, game_num, "MANUAL", suit, True)

        manual_predictions[game_num] = {
            'suit': suit,
            'private_messages': private_messages,
            'created_at': datetime.now().isoformat(),
            'status': '⌛',
            'rattrapage': 0
        }

        logger.info(f"✅ Prédiction manuelle #{game_num} - {suit} envoyée")

    # Envoyer le récapitulatif à l'admin
    status_lines = ["📊 **STATUT PRÉDICTIONS MANUELLES**\n"]

    for i, game_num in enumerate(valid_games, 1):
        suit = get_suit_for_game(game_num)
        status_lines.append(f"🎮 Jeu {i}: {game_num} 👉🏻 {SUIT_DISPLAY.get(suit, suit)} | Statut: ⏳")

    status_lines.append(f"\n**Prédictions actives: {len(valid_games)}**")

    status_msg = "\n".join(status_lines)
    await client.send_message(admin_id, status_msg)

    await client.send_message(admin_id, f"✅ {len(valid_games)} prédictions manuelles envoyées avec succès!")

# ============================================================
# COMMANDES UTILISATEUR
# ============================================================

@client.on(events.NewMessage(pattern='/start'))
async def cmd_start(event):
    if event.is_group or event.is_channel: 
        return

    user_id = event.sender_id
    user = get_user(user_id)

    if user.get('registered'):
        if is_user_subscribed(user_id) or user_id == ADMIN_ID:
            sub_type = "VIP 🔥" if get_subscription_type(user_id) == 'premium' or user_id == ADMIN_ID else "Standard"
            sub_end = user.get('subscription_end', 'Illimité' if user_id == ADMIN_ID else 'N/A')
            update_user(user_id, {'expiry_notified': False})

            active_msg = f"""🎯 **BON RETOUR {user.get('prenom', 'CHAMPION').upper()}!** 🎯

✅ Votre accès **{sub_type}** est ACTIF!
📅 Expiration: {sub_end[:10] if sub_end and user_id != ADMIN_ID else sub_end}

🔥 **Vous êtes prêt à gagner!**
Les prédictions arrivent automatiquement ici.

💡 **Conseil pro:** Restez attentif aux notifications!

🚀 **Bonne chance et gros gains!**"""
            await event.respond(active_msg)
            return

        elif is_trial_active(user_id):
            trial_start = datetime.fromisoformat(user['trial_started'])
            trial_end = trial_start + timedelta(minutes=60)
            remaining = (trial_end - datetime.now()).seconds // 60

            trial_msg = f"""⏰ **VOTRE ESSAI VIP EST EN COURS!** ⏰

🎁 Il vous reste **{remaining} minutes** de test gratuit!

🔥 Profitez-en pour découvrir la puissance de nos algorithmes!

⚡ **Ne perdez pas une seule seconde, restez attentif!**"""
            await event.respond(trial_msg)
            return

        else:
            update_user(user_id, {'trial_used': True})
            buttons = [
                [Button.url("💳 24H - 500 FCFA", PAYMENT_LINK_24H)],
                [Button.url("💳 1 SEMAINE - 1500 FCFA", PAYMENT_LINK_1W)],
                [Button.url("💳 2 SEMAINES - 2500 FCFA", PAYMENT_LINK_2W)]
            ]

            expired_msg = f"""⚠️ **VOTRE ESSAI EST TERMINÉ...** ⚠️

🎰 {user.get('prenom', 'CHAMPION')}, vous avez goûté à la puissance de nos prédictions...

💔 **Ne laissez pas la chance s'échopper!**

🔥 **OFFRE EXCLUSIVE:**
💎 **500 FCFA** = 24H de test prolongé
💎 **1500 FCFA** = 1 semaine complète  
💎 **2500 FCFA** = 2 semaines VIP

👇 **CHOISISSEZ VOTRE FORMULE ET REJOIGNEZ LES GAGNANTS!**"""

            await event.respond(expired_msg, buttons=buttons)
            return

    welcome_msg = """🎰 **BIENVENUE DANS L'ELITE DES GAGNANTS!** 🎰

💎 Vous venez de découvrir le bot de prédiction Baccarat le plus avancé du marché!

🚀 **Ce qui vous attend:**
• Prédictions basées sur des algorithmes statistiques de pointe
• Analyse en temps réel des patterns gagnants
• Taux de réussite optimisé par IA
• 60 MINUTES D'ESSAI GRATUIT!

💰 **Nos utilisateurs gagnants** profitent déjà d'un avantage statistique significatif.

👇 **Commençons votre inscription!**"""

    await event.respond(welcome_msg)
    user_conversation_state[user_id] = 'awaiting_nom'
    await event.respond("📝 **Étape 1/3: Quel est votre NOM?**")

@client.on(events.NewMessage())
async def handle_registration(event):
    if event.is_group or event.is_channel: 
        return

    if event.message.message and event.message.message.startswith('/'): 
        return

    user_id = event.sender_id
    user = get_user(user_id)

    # Gestion inscription
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

            update_user(user_id, {
                'pays': message_text,
                'registered': True,
                'trial_started': datetime.now().isoformat(),
                'trial_used': False
            })
            del user_conversation_state[user_id]

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

⚠️ **IMPORTANT:** Restez dans ce chat!

🍀 **Bonne chance!**"""

            await event.respond(success_msg)
            logger.info(f"✅ Nouvel utilisateur inscrit: {user_id}")
            return

    # Gestion envoi message admin
    if user_id in admin_message_state:
        state = admin_message_state[user_id]
        if state.get('step') == 'awaiting_message':
            target_user_id = state.get('target_user_id')
            message_content = event.message.message

            current_time = datetime.now().strftime("%H:%M:%S")
            full_message = f"""📨 **Message de {ADMIN_NAME}**
_{ADMIN_TITLE}_

{message_content}

---
⏰ Envoyé à {current_time}"""

            try:
                await client.send_message(target_user_id, full_message)
                await event.respond(f"✅ Message envoyé à l'utilisateur {target_user_id}!")
            except Exception as e:
                await event.respond(f"❌ Erreur: {e}")

            del admin_message_state[user_id]
            return

    # Gestion saisie manuelle
    if user_id in admin_manual_state:
        state = admin_manual_state[user_id]
        if state.get('step') == 'awaiting_numbers':
            message_text = event.message.message.strip()
            game_numbers = [n.strip() for n in message_text.split(',')]
            await send_manual_predictions(game_numbers, user_id)
            del admin_manual_state[user_id]
            return

    # Gestion paiement (capture d'écran)
    if user.get('awaiting_screenshot'):
        if event.message.photo:
            photo = event.message.photo

            payment_pending_state[user_id] = {
                'photo_id': photo.id,
                'timestamp': datetime.now(),
                'user_id': user_id
            }

            update_user(user_id, {'awaiting_screenshot': False})

            user_info = get_user(user_id)

            admin_msg = (
                "🔔 **NOUVELLE DEMANDE D'ABONNEMENT**\n\n"
                f"👤 **Utilisateur:** {user_info.get('nom')} {user_info.get('prenom')}\n"
                f"🆔 **ID:** `{user_id}`\n"
                f"📍 **Pays:** {user_info.get('pays')}\n"
                f"⏰ **Envoyé à:** {datetime.now().strftime('%H:%M:%S')}\n\n"
                "📸 **Capture d'écran ci-dessous**\n"
                "Vérifiez le paiement et validez."
            )

            buttons = [
                [Button.inline("✅ Valider 24H", data=f"valider_{user_id}_1d")],
                [Button.inline("✅ Valider 1 semaine", data=f"valider_{user_id}_1w")],
                [Button.inline("✅ Valider 2 semaines", data=f"valider_{user_id}_2w")],
                [Button.inline("❌ Rejeter", data=f"rejeter_{user_id}")]
            ]

            try:
                await client.send_file(ADMIN_ID, photo, caption=admin_msg, buttons=buttons)

                await event.respond("""✅ **CAPTURE D'ÉCRAN REÇUE!**

📸 Votre paiement a été transmis à l'administrateur.
⏳ Validation en cours...

🚀 Votre accès sera activé sous peu!""")

                asyncio.create_task(send_reminder_if_no_response(user_id))

            except Exception as e:
                logger.error(f"Erreur envoi à l'admin: {e}")
                await event.respond("❌ Erreur lors de l'envoi. Veuillez réessayer.")
        else:
            await event.respond("📸 Veuillez envoyer une capture d'écran de votre paiement.")
        return

async def send_reminder_if_no_response(user_id: int):
    """Envoie un rappel après 10 minutes."""
    await asyncio.sleep(600)

    if user_id in payment_pending_state:
        try:
            reminder_msg = f"""⏰ **INFORMATION**

Veuillez patienter, l'administrateur **{ADMIN_NAME}** est un peu occupé en ce moment.

💪 **Merci pour votre patience et votre confiance!**

🔥 Votre activation sera traitée très bientôt."""

            await client.send_message(user_id, reminder_msg)
        except Exception as e:
            logger.error(f"Erreur envoi rappel: {e}")

# ============================================================
# COMMANDES ADMIN
# ============================================================

@client.on(events.NewMessage(pattern='/users'))
async def cmd_users(event):
    if event.is_group or event.is_channel: 
        return

    if event.sender_id != ADMIN_ID:
        await event.respond("❌ Commande réservée à l'administrateur.")
        return

    if not users_data:
        await event.respond("📊 Aucun utilisateur inscrit.")
        return

    users_list = []
    for user_id_str, user_info in users_data.items():
        user_id = int(user_id_str)
        nom = user_info.get('nom', 'N/A') or 'N/A'
        prenom = user_info.get('prenom', 'N/A') or 'N/A'
        pays = user_info.get('pays', 'N/A') or 'N/A'
        status = get_user_status(user_id)

        user_line = f"🆔 `{user_id}` | {prenom} {nom} | {pays} | {status}"
        users_list.append(user_line)

    chunk_size = 50
    for i in range(0, len(users_list), chunk_size):
        chunk = users_list[i:i+chunk_size]
        chunk_text = '\n'.join(chunk)
        message = f"""📋 **LISTE DES UTILISATEURS** ({i+1}-{min(i+len(chunk), len(users_list))}/{len(users_list)})

{chunk_text}

💡 Pour envoyer un message: `/msg ID_UTILISATEUR`"""
        await event.respond(message)
        await asyncio.sleep(0.5)

@client.on(events.NewMessage(pattern=r'^/msg (\d+)$'))
async def cmd_msg(event):
    if event.is_group or event.is_channel: 
        return

    if event.sender_id != ADMIN_ID:
        await event.respond("❌ Commande réservée à l'administrateur.")
        return

    try:
        target_user_id = int(event.pattern_match.group(1))

        if str(target_user_id) not in users_data:
            await event.respond(f"❌ Utilisateur {target_user_id} non trouvé.")
            return

        user_info = users_data[str(target_user_id)]
        nom = user_info.get('nom', 'N/A')
        prenom = user_info.get('prenom', 'N/A')

        admin_message_state[event.sender_id] = {
            'target_user_id': target_user_id,
            'step': 'awaiting_message'
        }

        await event.respond(f"""✉️ **Envoi de message à {prenom} {nom}** (ID: `{target_user_id}`)

📝 Écrivez votre message ci-dessous:

✏️ **Votre message:**""")

    except Exception as e:
        await event.respond(f"❌ Erreur: {e}")

@client.on(events.NewMessage(pattern='/manual'))
async def cmd_manual(event):
    if event.is_group or event.is_channel:
        return

    if event.sender_id != ADMIN_ID:
        await event.respond("❌ Commande réservée à l'administrateur.")
        return

    admin_manual_state[event.sender_id] = {'step': 'awaiting_numbers'}

    await event.respond("""🎯 **MODE PRÉDICTION MANUELLE**

Veuillez entrer les numéros de jeux à prédire.

⚠️ **Règles:**
• Numéros pairs uniquement (202, 384, etc.)
• Ne pas terminer par 0
• Séparez par des virgules

**Exemple:** `202,384,786,512`

📝 **Entrez vos numéros:**""")

@client.on(events.NewMessage(pattern='/channels'))
async def cmd_channels(event):
    if event.is_group or event.is_channel:
        return

    if event.sender_id != ADMIN_ID:
        await event.respond("❌ Commande réservée à l'administrateur.")
        return

    channels_msg = f"""📡 **INFORMATION CANAUX SOURCES**

🎯 **Canal Principal (Résultats):**
`{SOURCE_CHANNEL_ID}`

📊 **Canal Statistiques:**
`{SOURCE_CHANNEL_2_ID}`

💡 **Note:** Ces IDs sont configurés dans les variables d'environnement."""

    await event.respond(channels_msg)

@client.on(events.CallbackQuery(data=re.compile(b'valider_(\d+)_(.*)')))
async def handle_validation(event):
    if event.sender_id != ADMIN_ID:
        await event.answer("Accès refusé", alert=True)
        return

    user_id = int(event.data_match.group(1).decode())
    duration = event.data_match.group(2).decode()

    sub_type = 'premium'

    if duration == '1d':
        days = 1
        dur_text = "24 heures"
    elif duration == '1w':
        days = 7
        dur_text = "1 semaine"
    else:
        days = 14
        dur_text = "2 semaines"

    end_date = datetime.now() + timedelta(days=days)
    update_user(user_id, {
        'subscription_end': end_date.isoformat(),
        'subscription_type': sub_type,
        'expiry_notified': False
    })

    if user_id in payment_pending_state:
        del payment_pending_state[user_id]

    try:
        activation_msg = f"""🎉 **FÉLICITATIONS! VOTRE ACCÈS EST ACTIVÉ!** 🎉

✅ Abonnement **{dur_text}** confirmé!
🔥 Vous faites maintenant partie de l'ELITE!

🚀 **Vos avantages:**
• Prédictions prioritaires
• Algorithmes exclusifs
• Mises à jour en temps réel
• Support dédié

💰 **C'est parti pour les gains!**"""

        await client.send_message(user_id, activation_msg)
    except Exception as e:
        logger.error(f"Erreur notification user {user_id}: {e}")

    await event.edit(f"✅ Abonnement {dur_text} activé pour {user_id}")
    await event.answer("Activé!")

@client.on(events.CallbackQuery(data=re.compile(b'rejeter_(\d+)')))
async def handle_rejection(event):
    if event.sender_id != ADMIN_ID:
        await event.answer("Accès refusé", alert=True)
        return

    user_id = int(event.data_match.group(1).decode())

    if user_id in payment_pending_state:
        del payment_pending_state[user_id]

    try:
        await client.send_message(user_id, "❌ Demande rejetée. Contactez le support si erreur.")
    except:
        pass

    await event.edit(f"❌ Rejeté pour {user_id}")
    await event.answer("Rejeté")

@client.on(events.NewMessage(pattern=r'^/set_a (\d+)$'))
async def cmd_set_a(event):
    if event.is_group or event.is_channel: 
        return
    if event.sender_id != ADMIN_ID: 
        return

    global USER_A
    try:
        val = int(event.pattern_match.group(1))
        USER_A = val
        await event.respond(f"✅ Paramètre 'a' = {USER_A}\nCible: N+{USER_A}")
    except Exception as e:
        await event.respond(f"❌ Erreur: {e}")

@client.on(events.NewMessage(pattern='/status'))
async def cmd_status(event):
    if event.is_group or event.is_channel: 
        return
    if event.sender_id != ADMIN_ID:
        await event.respond("Commande admin uniquement")
        return

    r2_status = "En cours 🔥" if rule2_active else "Inactif"

    if rule2_active:
        r1_status = f"{rule1_consecutive_count}/{MAX_RULE1_CONSECUTIVE} (Pause)"
    elif rule1_consecutive_count >= MAX_RULE1_CONSECUTIVE:
        r1_status = f"{rule1_consecutive_count}/{MAX_RULE1_CONSECUTIVE} (Limite)"
    else:
        r1_status = f"{rule1_consecutive_count}/{MAX_RULE1_CONSECUTIVE}"

    # Calculer le temps restant pour le prochain cycle
    time_remaining = "DÛ"
    if datetime.now() < next_prediction_allowed_at:
        remaining = (next_prediction_allowed_at - datetime.now()).seconds // 60
        time_remaining = f"{remaining}min"

    status_msg = f"""📊 **STATUT SYSTÈME**

🎮 Dernier vu: #{last_source_game_number}
🎯 Dernier finalisé: #{last_finalized_game_number}
🔢 Paramètre 'a': {USER_A}
⏳ Règle 2: {r2_status}
⏱️ Règle 1: {r1_status}
🕐 Prochain cycle: {time_remaining}
👥 Utilisateurs: {len(users_data)}
🔮 Manuelles: {len(manual_predictions)}

**Prédictions auto actives: {len(pending_predictions)}**"""

    if pending_predictions:
        for game_num, pred in sorted(pending_predictions.items()):
            distance = game_num - last_source_game_number
            ratt = f" [R{pred['rattrapage']}]" if pred.get('rattrapage', 0) > 0 else ""
            rule = pred.get('rule_type', 'R2')
            status_msg += f"\n• #{game_num}{ratt}: {pred['suit']} ({rule}) - {pred['status']}"

    if manual_predictions:
        status_msg += "\n\n**Prédictions manuelles:**"
        for game_num, pred in sorted(manual_predictions.items()):
            status_msg += f"\n• #{game_num}: {pred['suit']} - {pred['status']}"

    await event.respond(status_msg)

@client.on(events.NewMessage(pattern='/bilan'))
async def cmd_bilan(event):
    if event.is_group or event.is_channel: 
        return
    if event.sender_id != ADMIN_ID: 
        return

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
    if event.is_group or event.is_channel: 
        return
    if event.sender_id != ADMIN_ID:
        await event.respond("❌ Admin uniquement")
        return

    global users_data, pending_predictions, queued_predictions, processed_messages
    global current_game_number, last_source_game_number, last_finalized_game_number, stats_bilan
    global rule1_consecutive_count, rule2_active, suit_prediction_counts
    global last_known_source_game, prediction_target_game, waiting_for_one_part, cycle_triggered
    global current_time_cycle_index, next_prediction_allowed_at, already_predicted_games
    global manual_predictions, payment_pending_state

    users_data = {}
    save_users_data()
    pending_predictions.clear()
    queued_predictions.clear()
    processed_messages.clear()
    already_predicted_games.clear()
    suit_prediction_counts.clear()
    manual_predictions.clear()
    payment_pending_state.clear()

    current_game_number = 0
    last_source_game_number = 0
    last_finalized_game_number = 0
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
    if event.is_group or event.is_channel: 
        return

    help_msg = """📖 **CENTRE D'AIDE**

🎯 **Comment utiliser:**
1️⃣ Inscrivez-vous avec /start
2️⃣ Recevez 60min d'essai GRATUIT
3️⃣ Attendez les prédictions ici
4️⃣ Les résultats se mettent à jour auto!

💰 **Tarifs:**
• 500 FCFA = 24H
• 1500 FCFA = 1 semaine
• 2500 FCFA = 2 semaines

📊 **Commandes:**
/start - Profil & statut
/status - État système (admin)
/bilan - Statistiques (admin)
/users - Liste utilisateurs (admin)
/msg ID - Envoyer message (admin)
/manual - Prédictions manuelles (admin)
/channels - IDs canaux (admin)

❓ **Support:** Contactez @Kouamappoloak"""

    await event.respond(help_msg)

@client.on(events.NewMessage(pattern='/payer'))
async def cmd_payer(event):
    if event.is_group or event.is_channel: 
        return

    user_id = event.sender_id
    user = get_user(user_id)

    if not user.get('registered'):
        await event.respond("❌ Inscrivez-vous d'abord avec /start")
        return

    buttons = [
        [Button.url("⚡ 24H - 500 FCFA", PAYMENT_LINK_24H)],
        [Button.url("🔥 1 SEMAINE - 1500 FCFA", PAYMENT_LINK_1W)],
        [Button.url("💎 2 SEMAINES - 2500 FCFA", PAYMENT_LINK_2W)]
    ]

    payment_msg = f"""💳 **DÉBLOQUEZ VOTRE POTENTIEL!** 💳

🎰 {user.get('prenom', 'CHAMPION')}, choisissez:

⚡ **24 HEURES - 500 FCFA**
🔥 **1 SEMAINE - 1500 FCFA**  
💎 **2 SEMAINES - 2500 FCFA**

📸 **Après paiement:**
1. Payez via le lien ci-dessus
2. Revenez ici dans 1 minute
3. Envoyez la capture d'écran

👇 **CLIQUEZ SUR VOTRE FORMULE:**"""

    await event.respond(payment_msg, buttons=buttons)
    asyncio.create_task(request_screenshot_after_delay(user_id))

async def request_screenshot_after_delay(user_id: int):
    """Demande la capture d'écran après 1 minute."""
    await asyncio.sleep(60)

    try:
        update_user(user_id, {'awaiting_screenshot': True})

        await client.send_message(user_id, """⏰ **ÉTAPE SUIVANTE**

Veuillez maintenant envoyer votre capture d'écran de paiement ici.

📸 **Envoyez simplement la photo ici.**

✅ Notre équipe l'examinera rapidement!""")

        logger.info(f"Demande de capture envoyée à {user_id}")
    except Exception as e:
        logger.error(f"Erreur demande capture: {e}")

# ============================================================
# SERVEUR WEB ET DÉMARRAGE
# ============================================================

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
        <div class="label">Dernier Vu</div>
        <div class="number">#{last_source_game_number}</div>
    </div>
    <div class="status">
        <div class="label">Utilisateurs</div>
        <div class="number">{len(users_data)}</div>
    </div>
    <div class="status">
        <div class="label">Règle 2</div>
        <div class="number">{'ACTIVE 🔥' if rule2_active else 'Standby'}</div>
    </div>
    <div class="status">
        <div class="label">Prédictions</div>
        <div class="number">{len(pending_predictions) + len(manual_predictions)}</div>
    </div>
    <p style="margin-top: 40px; font-size: 1.1em;">Système opérationnel | Port 10000</p>
</body>
</html>"""
    return web.Response(text=html, content_type='text/html', status=200)

async def health_check(request):
    return web.Response(text="OK", status=200)

async def start_web_server():
    """Démarre le serveur web."""
    app = web.Application()
    app.router.add_get('/', index)
    app.router.add_get('/health', health_check)

    runner = web.AppRunner(app)
    await runner.setup()

    port = int(os.getenv('PORT', 10000))
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()
    logger.info(f"✅ Serveur web démarré sur le port {port}")

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
        global current_game_number, last_source_game_number, last_finalized_game_number, stats_bilan
        global last_known_source_game, prediction_target_game, waiting_for_one_part, cycle_triggered
        global current_time_cycle_index, next_prediction_allowed_at, already_predicted_games
        global manual_predictions, payment_pending_state

        pending_predictions.clear()
        queued_predictions.clear()
        processed_messages.clear()
        already_predicted_games.clear()
        suit_prediction_counts.clear()
        manual_predictions.clear()
        payment_pending_state.clear()

        current_game_number = 0
        last_source_game_number = 0
        last_finalized_game_number = 0
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

        # Enregistrer les handlers d'événements APRÈS le démarrage
        client.add_event_handler(handle_new_message, events.NewMessage())
        client.add_event_handler(handle_edited_message, events.MessageEdited())

        logger.info("✅ Handlers d'événements enregistrés")
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
        logger.info(f"📡 Surveillance des canaux: {SOURCE_CHANNEL_ID} et {SOURCE_CHANNEL_2_ID}")

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
