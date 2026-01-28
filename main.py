import os
import asyncio
import re
import logging
import sys
import json
import random
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

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger(__name__)

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

session_string = os.getenv('TELEGRAM_SESSION', '')
client = TelegramClient(StringSession(session_string), API_ID, API_HASH)

# Variables Globales d'État
SUIT_CYCLE = ['♥', '♦', '♣', '♠', '♦', '♥', '♠', '♣']
TIME_CYCLE = [3, 4, 5]  # MODIFIÉ: cycle 3, 4, 5 minutes
current_time_cycle_index = 0
next_prediction_allowed_at = datetime.now()

# NOUVEAU: Variable USER_A pour définir le décalage de prédiction (N+A)
USER_A = 4  # Par défaut N+4

def get_rule1_suit(game_number: int) -> str | None:
    if game_number < 6 or game_number > 1436 or game_number % 2 != 0 or game_number % 10 == 0:
        return None
    
    count_valid = 0
    for n in range(6, game_number + 1, 2):
        if n % 10 != 0:
            count_valid += 1
            
    if count_valid == 0: 
        return None
    
    index = (count_valid - 1) % 8
    return SUIT_CYCLE[index]

scp_cooldown = 0
scp_history = []
already_predicted_games = set()

pending_predictions = {}
queued_predictions = {}
processed_messages = set()
current_game_number = 0
last_source_game_number = 0
rule2_authorized_suit = None

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
    user = get_user(user_id)
    if user.get('trial_used') or not user.get('trial_started'):
        return False
    try:
        trial_start = datetime.fromisoformat(user['trial_started'])
        trial_end = trial_start + timedelta(minutes=10)
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

# --- FORMATS JOLIS POUR LES PRÉDICTIONS ---

def get_suit_emoji_big(suit: str) -> str:
    big_emojis = {
        '♠': '♠️',
        '♥': '❤️',
        '♦': '♦️',
        '♣': '♣️'
    }
    return big_emojis.get(suit, suit)

def get_suit_name(suit: str) -> str:
    names = {
        '♠': 'PIQUE',
        '♥': 'COEUR', 
        '♦': 'CARREAU',
        '♣': 'TRÈFLE'
    }
    return names.get(suit, suit)

def get_suit_color(suit: str) -> str:
    colors = {
        '♠': '⬛',
        '♥': '🟥',
        '♦': '🟥',
        '♣': '⬛'
    }
    return colors.get(suit, '⬜')

def generate_prediction_message(target_game: int, predicted_suit: str, status: str = '⏳', is_scp: bool = False) -> str:
    """Génère un message de prédiction attractif."""
    
    suit_emoji = get_suit_emoji_big(predicted_suit)
    suit_name = get_suit_name(predicted_suit)
    suit_color = get_suit_color(predicted_suit)
    
    if status == '⏳':
        banner = "╔══════════════════════╗\n║   🔮 PRÉDICTION 🔮   ║\n╚══════════════════════╝"
        status_text = "⏳ EN ATTENTE..."
        sub_text = "La prédiction est en cours de vérification"
    elif status == '✅0️⃣':
        banner = "╔══════════════════════╗\n║  🎉 VICTOIRE! 🎉  ║\n╚══════════════════════╝"
        status_text = "✅0️⃣ GAGNÉ IMMÉDIAT"
        sub_text = "Trouvé dès le 1er tour! Excellent!"
    elif status == '✅1️⃣':
        banner = "╔══════════════════════╗\n║  ✅ VICTOIRE! ✅  ║\n╚══════════════════════╝"
        status_text = "✅1️⃣ GAGNÉ AU 2ÈME TOUR"
        sub_text = "Trouvé au tour suivant! Super!"
    elif status == '✅2️⃣':
        banner = "╔══════════════════════╗\n║  ✅ VICTOIRE! ✅  ║\n╚══════════════════════╝"
        status_text = "✅2️⃣ GAGNÉ AU 3ÈME TOUR"
        sub_text = "Trouvé au dernier moment! Solide!"
    elif status == '❌':
        banner = "╔══════════════════════╗\n║  😔 PERDU  😔  ║\n╚══════════════════════╝"
        status_text = "❌ NON TROUVÉ"
        sub_text = "Le costume n'est pas sorti..."
    else:
        banner = "╔══════════════════════╗\n║   🔮 PRÉDICTION 🔮   ║\n╚══════════════════════╝"
        status_text = status
        sub_text = ""
    
    scp_badge = "⭐ SYSTÈME CENTRAL ⭐\n" if is_scp else ""
    
    now = datetime.now().strftime("%H:%M")
    
    msg = f"""
{banner}

{scp_badge}🎯 **TOUR #{target_game}**

{suit_color} {suit_emoji} **{suit_name}** {suit_color}

📊 **STATUT:** {status_text}
🕐 {now}

{sub_text}

━━━━━━━━━━━━━━━━━━━━━
"""
    return msg.strip()

async def send_prediction_to_user(user_id: int, prediction_msg: str, target_game: int):
    try:
        if not can_receive_predictions(user_id):
            user = get_user(user_id)
            if user.get('subscription_end') and not user.get('expiry_notified', False):
                expiry_msg = (
                    "⚠️ **VOTRE ABONNEMENT A EXPIRÉ!** ⚠️\n\n"
                    "🎰 Ne manquez pas les prochaines prédictions gagnantes!\n"
                    "💰 Nos algorithmes sont en feu en ce moment!\n\n"
                    "🔥 **RÉACTIVEZ MAINTENANT** 🔥"
                )
                buttons = [
                    [Button.url("💳 24H (200 FCFA)", PAYMENT_LINK_24H)],
                    [Button.url("💳 1 SEMAINE (1000 FCFA)", PAYMENT_LINK)],
                    [Button.url("💳 2 SEMAINES (2000 FCFA)", PAYMENT_LINK)]
                ]
                await client.send_message(user_id, expiry_msg, buttons=buttons)
                update_user(user_id, {'expiry_notified': True})
            return

        sent_msg = await client.send_message(user_id, prediction_msg, parse_mode='md')
        
        user_id_str = str(user_id)
        if target_game not in pending_predictions:
            pending_predictions[target_game] = {'private_messages': {}}
        
        if 'private_messages' not in pending_predictions[target_game]:
            pending_predictions[target_game]['private_messages'] = {}
            
        pending_predictions[target_game]['private_messages'][user_id_str] = sent_msg.id
        logger.info(f"Prédiction envoyée à {user_id}")
    except Exception as e:
        logger.error(f"Erreur envoi à {user_id}: {e}")

# --- Fonctions d'Analyse ---

def extract_game_number(message: str):
    match = re.search(r"#N\s*(\d+)", message, re.IGNORECASE)
    if match:
        return int(match.group(1))
    return None

def parse_stats_message(message: str):
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
    """Extrait tous les groupes entre parenthèses."""
    groups = re.findall(r"\d+\(([^)]*)\)", message)
    return groups

def normalize_suits(group_str: str) -> str:
    normalized = group_str.replace('❤️', '♥').replace('❤', '♥').replace('♥️', '♥')
    normalized = normalized.replace('♠️', '♠').replace('♦️', '♦').replace('♣️', '♣')
    return normalized

def is_suit_in_first_group(message_text: str, target_suit: str) -> bool:
    """
    Vérifie si le costume prédit est dans le PREMIER groupe de parenthèses.
    """
    groups = extract_parentheses_groups(message_text)
    if not groups:
        return False
    
    first_group = normalize_suits(groups[0])
    target_normalized = normalize_suits(target_suit)
    
    logger.info(f"Vérification PREMIER groupe: '{first_group}' contient '{target_normalized}'?")
    
    for char in target_normalized:
        if char in first_group:
            logger.info(f"✅ TROUVÉ dans premier groupe: {char}")
            return True
    
    logger.info(f"❌ PAS trouvé dans premier groupe")
    return False

def get_predicted_suit(missing_suit: str) -> str:
    return SUIT_MAPPING.get(missing_suit, missing_suit)

# --- Logique de Prédiction ---

pending_trigger = None  # {'target_game': 62, 'trigger_at': 61, 'suit': '♠', ...}

async def send_prediction_to_channel(target_game: int, predicted_suit: str, base_game: int, is_scp: bool = False):
    """Envoie la prédiction avec le nouveau format."""
    try:
        active_auto_predictions = [p for game, p in pending_predictions.items() 
                                   if p.get('rattrapage', 0) == 0 and game > current_game_number]
        
        if len(active_auto_predictions) >= 1:
            logger.info(f"Prédiction déjà active, attente pour #{target_game}")
            return None

        prediction_msg = generate_prediction_message(target_game, predicted_suit, '⏳', is_scp)

        for user_id_str, user_info in users_data.items():
            try:
                user_id = int(user_id_str)
                if can_receive_predictions(user_id):
                    await send_prediction_to_user(user_id, prediction_msg, target_game)
            except Exception as e:
                logger.error(f"Erreur envoi à {user_id_str}: {e}")

        pending_predictions[target_game] = {
            'message_id': 0, 
            'suit': predicted_suit,
            'base_game': base_game,
            'status': '⏳',
            'check_count': 0,
            'rattrapage': 0,
            'created_at': datetime.now().isoformat(),
            'private_messages': {},
            'is_scp': is_scp,
            'finished': False
        }

        logger.info(f"✅ PRÉDICTION ENVOYÉE: #{target_game} - {predicted_suit}")
        return 0

    except Exception as e:
        logger.error(f"Erreur envoi prédiction: {e}")
        return None

def get_next_valid_game(start: int) -> int:
    candidate = start
    while candidate % 2 != 0 or candidate % 10 == 0:
        candidate += 1
        if candidate > 1436:
            return None
    return candidate

def get_previous_valid_odd(start: int) -> int:
    """Trouve l'impair valide précédent (pour le trigger)."""
    candidate = start - 1
    while candidate % 2 == 0 or candidate % 10 == 0:
        candidate -= 1
        if candidate < 6:
            return None
    return candidate

async def try_trigger_prediction(current_game: int):
    global pending_trigger
    
    if pending_trigger is None:
        return
    
    trigger_at = pending_trigger['trigger_at']
    
    if current_game == trigger_at:
        target_game = pending_trigger['target_game']
        predicted_suit = pending_trigger['suit']
        base_game = pending_trigger['base_game']
        is_scp = pending_trigger.get('is_scp', False)
        
        logger.info(f"🚀 DÉCLENCHEMENT sur #{current_game}, envoi prédiction #{target_game}")
        
        await send_prediction_to_channel(target_game, predicted_suit, base_game, is_scp)
        
        global next_prediction_allowed_at, current_time_cycle_index
        wait_min = TIME_CYCLE[current_time_cycle_index]
        next_prediction_allowed_at = datetime.now() + timedelta(minutes=wait_min)
        current_time_cycle_index = (current_time_cycle_index + 1) % len(TIME_CYCLE)
        logger.info(f"⏱️ Prochain cycle dans {wait_min} min")
        
        pending_trigger = None

async def prepare_prediction(base_game: int):
    global pending_trigger, scp_cooldown, rule2_authorized_suit, already_predicted_games, USER_A
    
    # Utiliser USER_A pour le décalage (N+USER_A)
    target_game = get_next_valid_game(base_game + USER_A)
    if target_game is None or target_game > 1436:
        logger.info(f"Target hors limites")
        return
    
    if target_game in already_predicted_games:
        logger.info(f"Jeu #{target_game} déjà prédit")
        return
    
    already_predicted_games.add(target_game)
    
    # Calculer le numéro de déclenchement (impair juste avant target)
    trigger_game = get_previous_valid_odd(target_game)
    if trigger_game is None:
        logger.warning(f"Impossible de trouver trigger valide pour #{target_game}")
        return
    
    # Calcul Règle 1
    rule1_suit = None
    count_valid = 0
    for n in range(6, target_game + 1, 2):
        if n % 10 != 0:
            count_valid += 1
    if count_valid > 0:
        index = (count_valid - 1) % 8
        rule1_suit = SUIT_CYCLE[index]
        if target_game == 6:
            rule1_suit = '♥'
    
    # Système Central
    is_scp = False
    scp_imposition_suit = None
    if rule2_authorized_suit and scp_cooldown <= 0:
        scp_imposition_suit = rule2_authorized_suit
        is_scp = True
        logger.info(f"⭐ SCP impose {scp_imposition_suit} sur #{target_game}")
    
    final_suit = None
    if scp_imposition_suit:
        final_suit = scp_imposition_suit
        scp_cooldown = 1
        scp_history.append({
            'game': target_game,
            'suit': final_suit,
            'time': datetime.now().strftime('%H:%M:%S')
        })
        if len(scp_history) > 10: 
            scp_history.pop(0)
    elif rule1_suit:
        final_suit = rule1_suit
        if scp_cooldown > 0:
            scp_cooldown = 0
    
    if not final_suit:
        return
    
    pending_trigger = {
        'target_game': target_game,
        'trigger_at': trigger_game,
        'suit': final_suit,
        'base_game': base_game,
        'is_scp': is_scp,
        'prepared_at': datetime.now().isoformat()
    }
    
    scp_text = "⭐ SCP ⭐ " if is_scp else ""
    logger.info(f"⏳ PRÉPARÉ: {scp_text}#{target_game} ({final_suit}) → déclenchement sur #{trigger_game}")

async def update_prediction_status(game_number: int, new_status: str):
    try:
        if game_number not in pending_predictions:
            return False

        pred = pending_predictions[game_number]
        
        if pred.get('finished', False):
            return False
            
        suit = pred['suit']
        is_scp = pred.get('is_scp', False)

        updated_msg = generate_prediction_message(game_number, suit, new_status, is_scp)

        private_msgs = pred.get('private_messages', {})
        for user_id_str, msg_id in private_msgs.items():
            try:
                user_id = int(user_id_str)
                if can_receive_predictions(user_id):
                    await client.edit_message(user_id, msg_id, updated_msg, parse_mode='md')
            except Exception as e:
                logger.error(f"Erreur édition: {e}")

        pred['status'] = new_status
        
        if new_status in ['✅0️⃣', '✅1️⃣', '✅2️⃣', '❌']:
            pred['finished'] = True
            
            if new_status != '❌':
                stats_bilan['total'] += 1
                stats_bilan['wins'] += 1
                stats_bilan['win_details'][new_status] += 1
            else:
                stats_bilan['total'] += 1
                stats_bilan['losses'] += 1
                stats_bilan['loss_details']['❌'] += 1
            
            asyncio.create_task(delayed_remove_prediction(game_number))
            
            logger.info(f"🏁 PRÉDICTION #{game_number} TERMINÉE: {new_status}")

        return True
    except Exception as e:
        logger.error(f"Erreur update: {e}")
        return False

async def delayed_remove_prediction(game_number: int, delay: int = 300):
    await asyncio.sleep(delay)
    if game_number in pending_predictions:
        del pending_predictions[game_number]
        logger.info(f"🗑️ Prédiction #{game_number} supprimée")

async def check_prediction_result(game_number: int, message_text: str):
    """
    Vérifie les résultats pour TOUTES les prédictions actives.
    Logique: vérifie N (check_count=0), puis N+1 (check_count=1), puis N+2 (check_count=2)
    """
    
    for pred_game, pred in list(pending_predictions.items()):
        if pred.get('finished', False):
            continue
            
        target_suit = pred['suit']
        check_count = pred.get('check_count', 0)
        
        # Cas 1: Prédiction du jeu actuel (N), jamais vérifiée
        if pred_game == game_number and check_count == 0:
            found = is_suit_in_first_group(message_text, target_suit)
            
            if found:
                await update_prediction_status(pred_game, '✅0️⃣')
                return
            else:
                pred['check_count'] = 1
                logger.info(f"🔍 #{pred_game}: Pas trouvé dans N, attente N+1")
        
        # Cas 2: Prédiction du jeu précédent (N-1), déjà vérifiée une fois
        elif pred_game == game_number - 1 and check_count == 1:
            found = is_suit_in_first_group(message_text, target_suit)
            
            if found:
                await update_prediction_status(pred_game, '✅1️⃣')
                return
            else:
                pred['check_count'] = 2
                logger.info(f"🔍 #{pred_game}: Pas trouvé dans N+1, attente N+2")
        
        # Cas 3: Prédiction du jeu N-2, déjà vérifiée deux fois
        elif pred_game == game_number - 2 and check_count == 2:
            found = is_suit_in_first_group(message_text, target_suit)
            
            if found:
                await update_prediction_status(pred_game, '✅2️⃣')
                return
            else:
                await update_prediction_status(pred_game, '❌')
                return

async def process_stats_message(message_text: str):
    global rule2_authorized_suit
    stats = parse_stats_message(message_text)
    if not stats:
        rule2_authorized_suit = None
        return

    miroirs = [('♠', '♦'), ('♥', '♣')]
    selected_target_suit = None
    max_diff = 0
    
    for s1, s2 in miroirs:
        v1 = stats.get(s1, 0)
        v2 = stats.get(s2, 0)
        diff = abs(v1 - v2)
        
        if diff >= 6:
            if diff > max_diff:
                max_diff = diff
                selected_target_suit = s1 if v1 < v2 else s2
                
    if selected_target_suit:
        rule2_authorized_suit = selected_target_suit
        logger.info(f"SCP: Écart {max_diff}, cible {selected_target_suit}")
    else:
        rule2_authorized_suit = None

async def send_bilan():
    if stats_bilan['total'] == 0:
        return

    win_rate = (stats_bilan['wins'] / stats_bilan['total']) * 100
    loss_rate = (stats_bilan['losses'] / stats_bilan['total']) * 100
    
    msg = f"""
╔══════════════════════════════════╗
║     📊 BILAN DES PRÉDICTIONS     ║
╚══════════════════════════════════╝

🎯 **RÉSULTATS**

✅ **Taux de réussite:** {win_rate:.1f}%
❌ **Taux de perte:** {loss_rate:.1f}%

📈 **DÉTAILS**
┌─────────────────┬────────┐
│  ✅ Immédiat    │  {stats_bilan['win_details']['✅0️⃣']:>4}   │
│  ✅ 1 délai     │  {stats_bilan['win_details']['✅1️⃣']:>4}   │
│  ✅ 2 délais    │  {stats_bilan['win_details']['✅2️⃣']:>4}   │
│  ❌ Perdus      │  {stats_bilan['loss_details']['❌']:>4}   │
└─────────────────┴────────┘

🎰 **Total:** {stats_bilan['total']} prédictions
"""
    
    for user_id_str in users_data.keys():
        try:
            user_id = int(user_id_str)
            if can_receive_predictions(user_id):
                await client.send_message(user_id, msg, parse_mode='md')
        except:
            pass

async def auto_bilan_task():
    global last_bilan_time
    while True:
        try:
            await asyncio.sleep(60)
            now = datetime.now()
            if now >= last_bilan_time + timedelta(minutes=bilan_interval):
                await send_bilan()
                last_bilan_time = now
        except Exception as e:
            logger.error(f"Erreur bilan: {e}")

async def cycle_manager():
    global next_prediction_allowed_at, pending_trigger
    while True:
        await asyncio.sleep(1)

async def handle_message(event):
    global current_game_number, next_prediction_allowed_at
    
    try:
        chat = await event.get_chat()
        chat_id = chat.id
        if hasattr(chat, 'broadcast') and chat.broadcast:
            if not str(chat_id).startswith('-100'):
                chat_id = int(f"-100{abs(chat_id)}")
        
        message_text = event.message.text or event.message.message or ""
        
        if chat_id == SOURCE_CHANNEL_ID:
            game_number = extract_game_number(message_text)
            if game_number:
                current_game_number = game_number
                logger.info(f"📨 Message reçu: Jeu #{game_number}")
                
                await try_trigger_prediction(game_number)
                
                # Vérifier dans TOUS les messages (pas besoin d'attendre finalisation)
                if '(' in message_text and ')' in message_text:
                    await check_prediction_result(game_number, message_text)
                
                now = datetime.now()
                if now >= next_prediction_allowed_at and pending_trigger is None:
                    logger.info(f"🎯 CYCLE ACTIVÉ à {now.strftime('%H:%M:%S')}")
                    await prepare_prediction(game_number)
                
                if message_text.startswith('/info') and event.sender_id == ADMIN_ID:
                    pending_text = f"#{pending_trigger['target_game']}" if pending_trigger else "Aucune"
                    active_preds = [f"#{g}({p['status']})" for g, p in pending_predictions.items() if not p.get('finished', False)]
                    info_msg = f"""
ℹ️ **ÉTAT DU SYSTÈME**

🎰 Jeu actuel: #{current_game_number}
⏳ Cycle: {TIME_CYCLE[current_time_cycle_index]} min
🔮 Préparée: {pending_text}
🎯 Actives: {', '.join(active_preds) if active_preds else 'Aucune'}
⏱️ Prochain: {next_prediction_allowed_at.strftime('%H:%M:%S')}
📏 USER_A: {USER_A} (prédit N+{USER_A})
"""
                    await event.respond(info_msg, parse_mode='md')
        
        elif chat_id == SOURCE_CHANNEL_2_ID:
            await process_stats_message(message_text)
            
    except Exception as e:
        logger.error(f"Erreur: {e}")

# NOUVEAU: Gestion des messages édités (comme dans votre fichier original)
async def handle_edited_message(event):
    """Gère les messages édités dans les canaux sources."""
    try:
        chat = await event.get_chat()
        chat_id = chat.id
        if hasattr(chat, 'broadcast') and chat.broadcast:
            if not str(chat_id).startswith('-100'):
                chat_id = int(f"-100{abs(chat_id)}")

        if chat_id == SOURCE_CHANNEL_ID:
            message_text = event.message.message
            
            # Même logique que les nouveaux messages
            game_number = extract_game_number(message_text)
            if game_number:
                logger.info(f"✏️ Message édité reçu: Jeu #{game_number}")
                
                await try_trigger_prediction(game_number)
                
                if '(' in message_text and ')' in message_text:
                    await check_prediction_result(game_number, message_text)
                
                now = datetime.now()
                if now >= next_prediction_allowed_at and pending_trigger is None:
                    await prepare_prediction(game_number)
        
        elif chat_id == SOURCE_CHANNEL_2_ID:
            message_text = event.message.message
            await process_stats_message(message_text)
            
    except Exception as e:
        logger.error(f"Erreur handle_edited_message: {e}")

# --- Commandes ---

@client.on(events.NewMessage(pattern='/start'))
async def cmd_start(event):
    if event.is_group or event.is_channel: 
        return
    
    user_id = event.sender_id
    user = get_user(user_id)
    admin_id = 1190237801
    
    welcome_msg = f"""
╔══════════════════════════════════╗
║   🎰 BOT PRÉDICTION BACCARAT    ║
╚══════════════════════════════════╝

Bonjour {'Admin' if user_id == admin_id else user.get('prenom', '')}! 👋
"""
    
    if user.get('registered'):
        if is_user_subscribed(user_id) or user_id == admin_id:
            sub_type = "⭐ PREMIUM" if get_subscription_type(user_id) == 'premium' or user_id == admin_id else "Standard"
            sub_end = user.get('subscription_end', '♾️' if user_id == admin_id else 'N/A')
            update_user(user_id, {'expiry_notified': False})
            msg = f"""
{welcome_msg}

✅ **VOTRE ACCÈS EST ACTIF**

📋 Type: {sub_type}
📅 Expire: {sub_end[:10] if sub_end and user_id != admin_id else sub_end}

🔮 Les prédictions arrivent automatiquement ici!

💡 **Conseil:** Misez toujours avec modération
"""
            await event.respond(msg, parse_mode='md')
            
        elif is_trial_active(user_id):
            remaining = ((datetime.fromisoformat(user['trial_started']) + timedelta(minutes=10)) - datetime.now()).seconds // 60
            await event.respond(f"⏳ Essai actif: {remaining} min restantes", parse_mode='md')
        else:
            buttons = [[Button.url("💳 S'ABONNER", PAYMENT_LINK)]]
            await event.respond("⚠️ Essai terminé! Réabonnez-vous.", buttons=buttons)
            update_user(user_id, {'pending_payment': True})
    else:
        user_conversation_state[user_id] = 'awaiting_nom'
        await event.respond("🎰 Bienvenue!\n\n📝 Quel est votre **NOM**?", parse_mode='md')

@client.on(events.NewMessage())
async def handle_registration(event):
    if event.is_group or event.is_channel: 
        return
    if event.message.message.startswith('/'): 
        return
    
    user_id = event.sender_id
    
    if user_id in user_conversation_state:
        state = user_conversation_state[user_id]
        text = event.message.message.strip()
        
        if state == 'awaiting_nom':
            update_user(user_id, {'nom': text})
            user_conversation_state[user_id] = 'awaiting_prenom'
            await event.respond(f"✅ **{text}**\n\n📝 **PRÉNOM?**", parse_mode='md')
        
        elif state == 'awaiting_prenom':
            update_user(user_id, {'prenom': text})
            user_conversation_state[user_id] = 'awaiting_pays'
            await event.respond(f"✅ **{text}**\n\n🌍 **PAYS?**", parse_mode='md')
        
        elif state == 'awaiting_pays':
            update_user(user_id, {
                'pays': text, 'registered': True,
                'trial_started': datetime.now().isoformat(), 'trial_used': False
            })
            del user_conversation_state[user_id]
            await event.respond("🎉 **INSCRIPTION OK!**\n\n⏰ 10 min d'essai gratuite!", parse_mode='md')

# NOUVEAU: Commande /tim (comme dans votre fichier)
@client.on(events.NewMessage(pattern=r'^/tim (\d+)$'))
async def cmd_set_tim(event):
    if event.is_group or event.is_channel: 
        return
    admin_id = 1190237801
    if event.sender_id != admin_id: 
        return
    
    global bilan_interval
    try:
        bilan_interval = int(event.pattern_match.group(1))
        await event.respond(f"✅ Intervalle de bilan: {bilan_interval} minutes", parse_mode='md')
        logger.info(f"Intervalle modifié à {bilan_interval} min")
    except Exception as e:
        await event.respond(f"❌ Erreur: {e}")

# NOUVEAU: Commande /bilan (comme dans votre fichier)
@client.on(events.NewMessage(pattern='/bilan'))
async def cmd_bilan(event):
    if event.is_group or event.is_channel: 
        return
    admin_id = 1190237801
    if event.sender_id != admin_id: 
        return
    await send_bilan()
    await event.respond("✅ Bilan envoyé!", parse_mode='md')

# NOUVEAU: Commande /a (raccourci de /set_a)
@client.on(events.NewMessage(pattern=r'^/a (\d+)$'))
async def cmd_set_a_shortcut(event):
    if event.is_group or event.is_channel: 
        return
    if event.sender_id != ADMIN_ID and ADMIN_ID != 0: 
        return
    
    global USER_A
    try:
        val = int(event.pattern_match.group(1))
        USER_A = val
        await event.respond(f"✅ USER_A = {USER_A}\nProchaines prédictions: N+{USER_A}", parse_mode='md')
        logger.info(f"USER_A modifié à {val}")
    except Exception as e:
        await event.respond(f"❌ Erreur: {e}")

# NOUVEAU: Commande /set_a (comme dans votre fichier)
@client.on(events.NewMessage(pattern=r'^/set_a (\d+)$'))
async def cmd_set_a(event):
    if event.is_group or event.is_channel: 
        return
    if event.sender_id != ADMIN_ID and ADMIN_ID != 0: 
        return
    
    global USER_A
    try:
        val = int(event.pattern_match.group(1))
        USER_A = val
        await event.respond(f"✅ USER_A = {USER_A}\nLes prochaines prédictions seront sur N+{USER_A}", parse_mode='md')
        logger.info(f"USER_A modifié à {val}")
    except Exception as e:
        await event.respond(f"❌ Erreur: {e}")

# NOUVEAU: Commande /info (utilisateur et admin)
@client.on(events.NewMessage(pattern='/info'))
async def cmd_info(event):
    if event.is_group or event.is_channel: 
        return
    
    active_preds = len([p for p in pending_predictions.values() if not p.get('finished', False)])
    history_text = "\n".join([f"🔹 #{h['game']} ({h['suit']}) à {h['time']}" for h in scp_history[-5:]]) if scp_history else "Aucune"
    
    info_msg = (
        "ℹ️ **ÉTAT DU SYSTÈME**\n\n"
        f"🎮 Jeu actuel: #{current_game_number}\n"
        f"🔮 Prédictions actives: {active_preds}\n"
        f"⏳ Cooldown SCP: {'Actif' if scp_cooldown > 0 else 'Prêt'}\n"
        f"📏 USER_A: {USER_A} (N+{USER_A})\n\n"
        "📌 **DERNIÈRES IMPOSITIONS:**\n"
        f"{history_text}\n\n"
        "📈 Le bot suit le cycle de la Règle 1 par défaut."
    )
    await event.respond(info_msg, parse_mode='md')

# NOUVEAU: Commande /status (comme dans votre fichier)
@client.on(events.NewMessage(pattern='/status'))
async def cmd_status(event):
    if event.is_group or event.is_channel: 
        return
    if event.sender_id != ADMIN_ID and ADMIN_ID != 0:
        await event.respond("Commande réservée à l'administrateur")
        return

    status_msg = f"📊 **État du Bot:**\n\n"
    status_msg += f"🎮 Jeu actuel: #{current_game_number}\n"
    status_msg += f"📏 USER_A: {USER_A} (prédit N+{USER_A})\n\n"
    
    active = [(g, p) for g, p in pending_predictions.items() if not p.get('finished', False)]
    if active:
        status_msg += f"**🔮 Actives ({len(active)}):**\n"
        for game_num, pred in sorted(active):
            distance = game_num - current_game_number
            status_msg += f"• #{game_num}: {pred['suit']} - {pred['status']} (dans {distance})\n"
    else: 
        status_msg += "**🔮 Aucune prédiction active**\n"

    await event.respond(status_msg, parse_mode='md')

# NOUVEAU: Commande /reset (comme dans votre fichier)
@client.on(events.NewMessage(pattern='/reset'))
async def cmd_reset_all(event):
    if event.is_group or event.is_channel: 
        return
    admin_id = 1190237801
    if event.sender_id != admin_id:
        await event.respond("❌ Commande réservée à l'administrateur principal.")
        return
    
    global users_data, pending_predictions, queued_predictions, processed_messages
    global current_game_number, last_source_game_number, stats_bilan
    global current_time_cycle_index, next_prediction_allowed_at, already_predicted_games
    global USER_A, pending_trigger
    
    users_data = {}
    save_users_data()
    
    pending_predictions.clear()
    queued_predictions.clear()
    processed_messages.clear()
    already_predicted_games.clear()
    pending_trigger = None
    current_game_number = 0
    last_source_game_number = 0
    current_time_cycle_index = 0
    next_prediction_allowed_at = datetime.now()
    USER_A = 4  # Reset à la valeur par défaut
    stats_bilan = {
        'total': 0,
        'wins': 0,
        'losses': 0,
        'win_details': {'✅0️⃣': 0, '✅1️⃣': 0, '✅2️⃣': 0},
        'loss_details': {'❌': 0}
    }
    
    logger.warning(f"🚨 RESET TOTAL effectué par l'admin {event.sender_id}")
    await event.respond("🚨 **RÉINITIALISATION TOTALE EFFECTUÉE** 🚨\n\n- Tous les comptes supprimés\n- Statistiques remises à zéro\n- USER_A reset à 4")

# NOUVEAU: Commande /dif (comme dans votre fichier)
@client.on(events.NewMessage(pattern='/dif'))
async def cmd_dif(event):
    if event.is_group or event.is_channel: 
        return
    admin_id = 1190237801
    if event.sender_id != admin_id:
        await event.respond("❌ Commande réservée à l'administrateur principal.")
        return
    
    message = event.message.message[4:].strip()
    if not message:
        await event.respond("❌ Utilisation: `/dif <message>`")
        return
    
    count = 0
    for user_id_str in users_data.keys():
        try:
            await client.send_message(int(user_id_str), f"📢 **MESSAGE DE L'ADMINISTRATEUR**\n\n{message}")
            count += 1
        except:
            pass
    
    await event.respond(f"✅ Message diffusé à {count} utilisateurs.")

# NOUVEAU: Commande /help (comme dans votre fichier)
@client.on(events.NewMessage(pattern='/help'))
async def cmd_help(event):
    if event.is_group or event.is_channel: 
        return
    
    await event.respond("""📖 **Aide - Bot de Prédiction Baccarat**

**🎯 Comment ça marche:**
1. Inscrivez-vous avec /start
2. Profitez de 10 minutes d'essai gratuit
3. Abonnez-vous pour continuer

**💰 Tarifs:**
- 200 FCFA = 24 heures
- 1000 FCFA = 1 semaine
- 2000 FCFA = 2 semaines

**📝 Commandes:**
- `/start` - Démarrer / État abonnement
- `/payer` - S'abonner ou renouveler
- `/help` - Cette aide
- `/info` - Informations système
""", parse_mode='md')

# NOUVEAU: Commande /payer (comme dans votre fichier)
@client.on(events.NewMessage(pattern='/payer'))
async def cmd_payer(event):
    if event.is_group or event.is_channel: 
        return
    
    user_id = event.sender_id
    user = get_user(user_id)
    
    if not user.get('registered'):
        await event.respond("❌ Vous devez d'abord vous inscrire avec /start")
        return
    
    buttons = [
        [Button.url("💳 24 HEURES (200 FCFA)", PAYMENT_LINK_24H)],
        [Button.url("💳 1 SEMAINE (1000 FCFA)", PAYMENT_LINK)],
        [Button.url("💳 2 SEMAINES (2000 FCFA)", PAYMENT_LINK)]
    ]
    
    await event.respond(
        "💳 **ABONNEMENT**\n\n"
        "**Tarifs:**\n"
        "💰 200 FCFA = 24 heures\n"
        "💰 1000 FCFA = 1 semaine\n"
        "💰 2000 FCFA = 2 semaines\n\n"
        f"👤 Votre ID: `{user_id}`",
        buttons=buttons
    )
    
    await asyncio.sleep(2)
    await event.respond(
        "📸 **Après paiement:**\n"
        "1. Envoyez une capture d'écran\n"
        "2. Indiquez le montant (200, 1000 ou 2000)"
    )
    update_user(user_id, {'pending_payment': True, 'awaiting_screenshot': True})

# --- Démarrage ---

async def start_web_server():
    app = web.Application()
    app.router.add_get('/', lambda r: web.Response(text=f"🎰 Bot OK - Jeu #{current_game_number}"))
    app.router.add_get('/health', lambda r: web.Response(text="OK"))
    
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', PORT)
    await site.start()

async def schedule_daily_reset():
    wat_tz = timezone(timedelta(hours=1))
    reset_time = time(0, 59, tzinfo=wat_tz)
    
    while True:
        now = datetime.now(wat_tz)
        target = datetime.combine(now.date(), reset_time, tzinfo=wat_tz)
        if now >= target:
            target += timedelta(days=1)
        await asyncio.sleep((target - now).total_seconds())
        
        global pending_predictions, stats_bilan, already_predicted_games, pending_trigger
        pending_predictions.clear()
        already_predicted_games.clear()
        pending_trigger = None
        stats_bilan = {
            'total': 0, 'wins': 0, 'losses': 0,
            'win_details': {'✅0️⃣': 0, '✅1️⃣': 0, '✅2️⃣': 0},
            'loss_details': {'❌': 0}
        }
        logger.warning("🚨 RESET QUOTIDIEN")

async def start_bot():
    try:
        await client.connect()
        if not await client.is_user_authorized():
            await client.sign_in(bot_token=BOT_TOKEN)
        logger.info("✅ Bot connecté")
        return True
    except Exception as e:
        logger.error(f"Erreur: {e}")
        return False

async def main():
    load_users_data()
    await start_web_server()
    
    if not await start_bot():
        return
    
    # Enregistrer les handlers d'événements
    client.add_event_handler(handle_message, events.NewMessage())
    client.add_event_handler(handle_edited_message, events.MessageEdited())  # NOUVEAU
    
    asyncio.create_task(schedule_daily_reset())
    asyncio.create_task(auto_bilan_task())
    asyncio.create_task(cycle_manager())
    
    logger.info("🚀 Bot opérationnel!")
    await client.run_until_disconnected()

if __name__ == '__main__':
    asyncio.run(main())
