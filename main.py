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

# Configuration depuis les variables d'environnement (Render.com)
API_ID = int(os.getenv('API_ID', 0))
API_HASH = os.getenv('API_HASH', '')
BOT_TOKEN = os.getenv('BOT_TOKEN', '')
ADMIN_ID = int(os.getenv('ADMIN_ID', 0))
SOURCE_CHANNEL_ID = int(os.getenv('SOURCE_CHANNEL_ID', 0))
SOURCE_CHANNEL_2_ID = int(os.getenv('SOURCE_CHANNEL_2_ID', 0))
PORT = int(os.getenv('PORT', 10000))  # Render.com utilise le port 10000 par défaut

# Mapping des costumes
SUIT_MAPPING = {'♠': '♥', '♥': '♦', '♦': '♣', '♣': '♠'}
ALL_SUITS = ['♠', '♥', '♦', '♣']
SUIT_DISPLAY = {'♠': '♠️ Pique', '♥': '♥️ Cœur', '♦': '♦️ Carreau', '♣': '♣️ Trèfle'}

# Liens de paiement
PAYMENT_LINK_24H = os.getenv('PAYMENT_LINK_24H', 'https://payment.example.com/500')
PAYMENT_LINK_1W = os.getenv('PAYMENT_LINK_1W', 'https://payment.example.com/1500')
PAYMENT_LINK_2W = os.getenv('PAYMENT_LINK_2W', 'https://payment.example.com/2500')

USERS_FILE = "users_data.json"

# Configuration administrateur
ADMIN_NAME = os.getenv('ADMIN_NAME', 'Sossou Kouamé')
ADMIN_TITLE = os.getenv('ADMIN_TITLE', 'Administrateur et développeur')

# Logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger(__name__)

# Vérifications configuration
if not API_ID:
    logger.error("API_ID manquant")
    exit(1)
if not API_HASH:
    logger.error("API_HASH manquant")
    exit(1)
if not BOT_TOKEN:
    logger.error("BOT_TOKEN manquant")
    exit(1)

logger.info(f"Configuration: SOURCE_CHANNEL={SOURCE_CHANNEL_ID}, PORT={PORT}")

# Initialisation client Telegram
session_string = os.getenv('TELEGRAM_SESSION', '')
client = TelegramClient(StringSession(session_string), API_ID, API_HASH)

# Variables globales
pending_predictions = {}
queued_predictions = {}
processed_messages = set()
current_game_number = 0
last_source_game_number = 0
suit_prediction_counts = {}
USER_A = 1

SUIT_CYCLE = ['♥', '♦', '♣', '♠', '♦', '♥', '♠', '♣']
TIME_CYCLE = [5, 8, 3, 7, 9, 4, 6, 8, 3, 5, 9, 7, 4, 6, 8, 3, 5, 9, 7, 4, 6, 8, 3, 5, 9, 7, 4, 6, 8, 5]
current_time_cycle_index = 0
next_prediction_allowed_at = datetime.now()

last_known_source_game = 0
prediction_target_game = None
waiting_for_one_part = False
cycle_triggered = False
rule1_consecutive_count = 0
MAX_RULE1_CONSECUTIVE = 3
rule2_active = False

stats_bilan = {'total': 0, 'wins': 0, 'losses': 0, 'win_details': {'✅0️⃣': 0, '✅1️⃣': 0, '✅2️⃣': 0}, 'loss_details': {'❌': 0}}

users_data = {}
user_conversation_state = {}
admin_message_state = {}
admin_predict_state = {}
payment_photos_pending = {}

# Fonctions utilitaires
def load_users_data():
    global users_data
    try:
        if os.path.exists(USERS_FILE):
            with open(USERS_FILE, 'r', encoding='utf-8') as f:
                users_data = json.load(f)
            logger.info(f"Données chargées: {len(users_data)} utilisateurs")
    except Exception as e:
        logger.error(f"Erreur chargement: {e}")
        users_data = {}

def save_users_data():
    try:
        with open(USERS_FILE, 'w', encoding='utf-8') as f:
            json.dump(users_data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"Erreur sauvegarde: {e}")

def get_user(user_id: int) -> dict:
    user_id_str = str(user_id)
    if user_id_str not in users_data:
        users_data[user_id_str] = {
            'registered': False, 'nom': None, 'prenom': None, 'pays': None,
            'trial_started': None, 'trial_used': False, 'subscription_end': None,
            'subscription_type': None, 'pending_payment': False
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

def get_user_status(user_id: int) -> str:
    if is_user_subscribed(user_id):
        return "✅ Abonné"
    elif is_trial_active(user_id):
        return "🎁 Essai actif"
    elif get_user(user_id).get('trial_used'):
        return "⏰ Essai terminé"
    return "❌ Non inscrit"

def get_next_in_cycle(current_suit: str) -> str:
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

def get_next_prediction_info(current_game: int, current_suit: str):
    next_game = current_game + 2
    while next_game % 10 == 0:
        next_game += 2
    next_suit = get_next_in_cycle(current_suit)
    return next_game, next_suit

def extract_game_number(message: str):
    match = re.search(r"#N\s*(\d+)", message, re.IGNORECASE)
    if match:
        return int(match.group(1))
    return None

def parse_stats_message(message: str):
    stats = {}
    patterns = {'♠': r'♠️?\s*:\s*(\d+)', '♥': r'♥️?\s*:\s*(\d+)', '♦': r'♦️?\s*:\s*(\d+)', '♣': r'♣️?\s*:\s*(\d+)'}
    for suit, pattern in patterns.items():
        match = re.search(pattern, message)
        if match:
            stats[suit] = int(match.group(1))
    return stats

def extract_parentheses_groups(message: str):
    return re.findall(r"\(([^)]*)\)", message)

def normalize_suits(group_str: str) -> str:
    normalized = group_str.replace('❤️', '♥').replace('❤', '♥').replace('♥️', '♥')
    normalized = normalized.replace('♠️', '♠').replace('♦️', '♦').replace('♣️', '♣')
    return normalized

def has_suit_in_group(group_str: str, target_suit: str) -> bool:
    normalized = normalize_suits(group_str)
    target_normalized = normalize_suits(target_suit)
    for suit in ALL_SUITS:
        if suit in target_normalized and suit in normalized:
            return True
    return False

def is_one_part_away(current: int, target: int) -> bool:
    return current % 2 != 0 and target - current == 1

# Envoi des prédictions
async def send_prediction_to_all_users(prediction_msg: str, target_game: int, rule_type: str = "R2", show_next: bool = True, current_suit: str = None):
    private_messages = {}
    sent_count = 0
    failed_count = 0

    next_info = ""
    if show_next and current_suit:
        next_game, next_suit = get_next_prediction_info(target_game, current_suit)
        next_info = f"\n\n📊 PROCHAIN:\n🎮 Jeu #{next_game} 👉🏻 {SUIT_DISPLAY.get(next_suit, next_suit)}"

    full_message = prediction_msg + next_info

    try:
        if ADMIN_ID and ADMIN_ID != 0:
            admin_msg = await client.send_message(ADMIN_ID, full_message)
            private_messages[str(ADMIN_ID)] = admin_msg.id
    except Exception as e:
        logger.error(f"Erreur envoi admin: {e}")
        failed_count += 1

    for user_id_str, user_info in users_data.items():
        try:
            user_id = int(user_id_str)
            if user_id == ADMIN_ID or user_id_str == BOT_TOKEN.split(':')[0]:
                continue
            if not can_receive_predictions(user_id):
                continue

            sent_msg = await client.send_message(user_id, full_message)
            private_messages[user_id_str] = sent_msg.id
            sent_count += 1
        except Exception as e:
            failed_count += 1
            logger.error(f"Erreur envoi à {user_id_str}: {e}")

    logger.info(f"Envoi terminé: {sent_count} succès, {failed_count} échecs")
    return private_messages

async def edit_prediction_for_all_users(game_number: int, new_status: str, suit: str, rule_type: str, original_game: int = None):
    display_game = original_game if original_game else game_number

    status_map = {
        "❌": "❌ PERDU" if rule_type != "R1" else "❌ NON TROUVÉ",
        "✅0️⃣": "✅ VICTOIRE IMMÉDIATE!" if rule_type != "R1" else "✅ TROUVÉ!",
        "✅1️⃣": "✅ VICTOIRE AU 2ÈME JEU!" if rule_type != "R1" else "✅ TROUVÉ AU 2ÈME!",
        "✅2️⃣": "✅ VICTOIRE AU 3ÈME JEU!" if rule_type != "R1" else "✅ TROUVÉ AU 3ÈME!",
        "✅3️⃣": "✅ VICTOIRE AU 4ÈME JEU!" if rule_type != "R1" else "✅ TROUVÉ AU 4ÈME!"
    }
    status_text = status_map.get(new_status, new_status)

    algo_text = "Règle 2 (Stats)" if rule_type == "R2" else ("Manuel" if rule_type == "MANUEL" else "Règle 1 (Cycle)")

    updated_msg = f"""🎰 **PRÉDICTION #{display_game}**

🎯 Couleur: {SUIT_DISPLAY.get(suit, suit)}
📊 Statut: {status_text}
🤖 Algorithme: {algo_text}"""

    if game_number not in pending_predictions:
        return 0

    pred = pending_predictions[game_number]
    private_msgs = pred.get('private_messages', {})

    if not private_msgs:
        return 0

    edited_count = 0
    for user_id_str, msg_id in list(private_msgs.items()):
        try:
            user_id = int(user_id_str)
            await client.edit_message(user_id, msg_id, updated_msg)
            edited_count += 1
        except Exception as e:
            if "message to edit not found" in str(e).lower():
                del private_msgs[user_id_str]

    return edited_count

async def send_prediction_to_users(target_game: int, predicted_suit: str, base_game: int, rattrapage=0, original_game=None, rule_type="R2"):
    global rule2_active, rule1_consecutive_count

    try:
        if rattrapage > 0:
            original_private_msgs = {}
            if original_game and original_game in pending_predictions:
                original_private_msgs = pending_predictions[original_game].get('private_messages', {}).copy()

            pending_predictions[target_game] = {
                'message_id': 0, 'suit': predicted_suit, 'base_game': base_game,
                'status': '🔮', 'rattrapage': rattrapage, 'original_game': original_game,
                'rule_type': rule_type, 'private_messages': original_private_msgs,
                'created_at': datetime.now().isoformat()
            }

            if rule_type == "R2":
                rule2_active = True
            return True

        if rule_type == "R1":
            active_r2 = [p for game, p in pending_predictions.items() if p.get('rule_type') == 'R2' and p.get('rattrapage', 0) == 0 and game > current_game_number]
            if active_r2:
                return False

        if rule_type == "R2":
            prediction_msg = f"""🎰 **PRÉDICTION #{target_game}**

🎯 Couleur: {SUIT_DISPLAY.get(predicted_suit, predicted_suit)}
⏳ Statut: ⏳ EN ATTENTE...
🤖 Algorithme: de confiance"""
        elif rule_type == "MANUEL":
            prediction_msg = f"""🎰 **PRÉDICTION MANUELLE #{target_game}**

🎯 Couleur: {SUIT_DISPLAY.get(predicted_suit, predicted_suit)}
⏳ Statut: ⏳ EN ATTENTE...
🤖 Algorithme: Manuel"""
        else:
            prediction_msg = f"""🎰 **PRÉDICTION #{target_game}**

🎯 Couleur: {SUIT_DISPLAY.get(predicted_suit, predicted_suit)}
⏳ Statut: ⏳ EN ATTENTE...
🤖 Algorithme: de confiance"""

        private_messages = await send_prediction_to_all_users(prediction_msg, target_game, rule_type, show_next=True, current_suit=predicted_suit)

        if not private_messages:
            return False

        pending_predictions[target_game] = {
            'message_id': 0, 'suit': predicted_suit, 'base_game': base_game,
            'status': '⌛', 'check_count': 0, 'rattrapage': 0, 'rule_type': rule_type,
            'private_messages': private_messages, 'created_at': datetime.now().isoformat()
        }

        if rule_type == "R2":
            rule2_active = True
            rule1_consecutive_count = 0
        elif rule_type == "R1":
            rule1_consecutive_count += 1

        return True

    except Exception as e:
        logger.error(f"Erreur envoi prédiction: {e}")
        return False

def queue_prediction(target_game: int, predicted_suit: str, base_game: int, rattrapage=0, original_game=None, rule_type="R2"):
    global rule2_active

    if rule_type == "R2":
        rule2_active = True

    if target_game in queued_predictions or (target_game in pending_predictions and rattrapage == 0):
        return False

    queued_predictions[target_game] = {
        'target_game': target_game, 'predicted_suit': predicted_suit, 'base_game': base_game,
        'rattrapage': rattrapage, 'original_game': original_game, 'rule_type': rule_type,
        'queued_at': datetime.now().isoformat()
    }
    return True

async def check_and_send_queued_predictions(current_game: int):
    global current_game_number, rule2_active
    current_game_number = current_game

    sorted_queued = sorted(queued_predictions.keys())

    for target_game in list(sorted_queued):
        if target_game >= current_game:
            pred_data = queued_predictions.pop(target_game)
            await send_prediction_to_users(
                pred_data['target_game'], pred_data['predicted_suit'], pred_data['base_game'],
                pred_data.get('rattrapage', 0), pred_data.get('original_game'), pred_data.get('rule_type', 'R2')
            )

async def update_prediction_status(game_number: int, new_status: str):
    global rule2_active, rule1_consecutive_count

    try:
        if game_number not in pending_predictions:
            return False

        pred = pending_predictions[game_number]
        suit = pred['suit']
        rule_type = pred.get('rule_type', 'R2')
        rattrapage = pred.get('rattrapage', 0)
        original_game = pred.get('original_game', game_number)

        await edit_prediction_for_all_users(game_number, new_status, suit, rule_type, original_game)
        pred['status'] = new_status

        if new_status in ['✅0️⃣', '✅1️⃣', '✅2️⃣', '✅3️⃣']:
            stats_bilan['total'] += 1
            stats_bilan['wins'] += 1
            stats_bilan['win_details'][new_status] = stats_bilan['win_details'].get(new_status, 0) + 1

            if rule_type == "R2" and rattrapage == 0:
                rule2_active = False
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
            elif rule_type == "R1":
                rule1_consecutive_count = 0

            if game_number in pending_predictions:
                del pending_predictions[game_number]
            asyncio.create_task(check_and_send_queued_predictions(current_game_number))

        return True

    except Exception as e:
        logger.error(f"Erreur update status: {e}")
        return False

async def check_prediction_result(game_number: int, first_group: str):
    logger.info(f"Vérification résultat pour jeu #{game_number}")

    if game_number in pending_predictions:
        pred = pending_predictions[game_number]
        if pred.get('rattrapage', 0) == 0:
            target_suit = pred['suit']
            rule_type = pred.get('rule_type', 'R2')
            if has_suit_in_group(first_group, target_suit):
                await update_prediction_status(game_number, '✅0️⃣')
                return
            else:
                next_target = game_number + 1
                queue_prediction(next_target, target_suit, pred['base_game'], 1, game_number, rule_type)

    for target_game, pred in list(pending_predictions.items()):
        if target_game == game_number and pred.get('rattrapage', 0) > 0:
            original_game = pred.get('original_game', target_game - pred['rattrapage'])
            target_suit = pred['suit']
            rattrapage_actuel = pred['rattrapage']
            rule_type = pred.get('rule_type', 'R2')

            if has_suit_in_group(first_group, target_suit):
                await update_prediction_status(original_game, f'✅{rattrapage_actuel}️⃣')
                if target_game != original_game and target_game in pending_predictions:
                    del pending_predictions[target_game]
                return
            else:
                if rattrapage_actuel < 3:
                    next_rattrapage = rattrapage_actuel + 1
                    next_target = game_number + 1
                    queue_prediction(next_target, target_suit, pred['base_game'], next_rattrapage, original_game, rule_type)
                    if target_game in pending_predictions:
                        del pending_predictions[target_game]
                else:
                    await update_prediction_status(original_game, '❌')
                    if target_game != original_game and target_game in pending_predictions:
                        del pending_predictions[target_game]
                return

async def process_stats_message(message_text: str):
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
                    continue

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

async def try_launch_prediction_rule1():
    global waiting_for_one_part, prediction_target_game, cycle_triggered
    global current_time_cycle_index, next_prediction_allowed_at, rule1_consecutive_count
    global rule2_active

    if rule2_active or rule1_consecutive_count >= MAX_RULE1_CONSECUTIVE:
        return False

    if not cycle_triggered or prediction_target_game is None:
        return False

    if is_one_part_away(last_known_source_game, prediction_target_game):
        predicted_suit = get_suit_for_game(prediction_target_game)

        success = await send_prediction_to_users(prediction_target_game, predicted_suit, last_known_source_game, rule_type="R1")

        if success:
            waiting_for_one_part = False
            cycle_triggered = False
            prediction_target_game = None

            wait_min = TIME_CYCLE[current_time_cycle_index]
            next_prediction_allowed_at = datetime.now() + timedelta(minutes=wait_min)
            current_time_cycle_index = (current_time_cycle_index + 1) % len(TIME_CYCLE)
            return True

    return False

async def process_prediction_logic_rule1(message_text: str, chat_id: int):
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

    if waiting_for_one_part and cycle_triggered:
        await try_launch_prediction_rule1()
        return

    now = datetime.now()
    if now < next_prediction_allowed_at:
        return

    if rule2_active or rule1_consecutive_count >= MAX_RULE1_CONSECUTIVE:
        wait_min = TIME_CYCLE[current_time_cycle_index]
        next_prediction_allowed_at = now + timedelta(minutes=wait_min)
        current_time_cycle_index = (current_time_cycle_index + 1) % len(TIME_CYCLE)
        return

    cycle_triggered = True

    candidate = game_number + 2
    while candidate % 2 != 0 or candidate % 10 == 0:
        candidate += 1

    prediction_target_game = candidate

    success = await try_launch_prediction_rule1()

    if not success:
        waiting_for_one_part = True

def is_message_finalized(message: str) -> bool:
    if '⏰' in message:
        return False
    return '✅' in message or '🔰' in message or '▶️' in message or 'Finalisé' in message

async def process_finalized_message(message_text: str, chat_id: int):
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

        await check_prediction_result(game_number, groups[0])
        await check_and_send_queued_predictions(game_number)

    except Exception as e:
        logger.error(f"Erreur traitement finalisé: {e}")

async def handle_message(event):
    try:
        sender = await event.get_sender()
        sender_id = getattr(sender, 'id', event.sender_id)

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
        logger.error(f"Erreur handle_message: {e}")

async def handle_edited_message(event):
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

client.add_event_handler(handle_message, events.NewMessage())
client.add_event_handler(handle_edited_message, events.MessageEdited())

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

💔 **Ne laissez pas la chance s'échapper!**

🔥 **NOUVEAUX TARIFS:**
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
• 60 MINUTES D'ESSAI GRATUIT pour tester la puissance du système!

💰 **Nos utilisateurs gagnants** profitent déjà d'un avantage statistique significatif.

👇 **Commençons votre inscription!**"""

    await event.respond(welcome_msg)
    user_conversation_state[user_id] = 'awaiting_nom'
    await event.respond("📝 **Étape 1/3: Quel est votre NOM?**")

@client.on(events.NewMessage(pattern='/predict'))
async def cmd_predict(event):
    if event.is_group or event.is_channel:
        return

    if event.sender_id != ADMIN_ID:
        await event.respond("❌ Commande réservée à l'administrateur.")
        return

    admin_predict_state[event.sender_id] = {'step': 'awaiting_numbers'}

    await event.respond("""🎯 **MODE PRÉDICTION MANUELLE**

📝 Entrez les numéros de jeux **pairs** (sauf ceux terminant par 0)
Séparés par des virgules.

**Exemple:** `202,384,786,452`

⚠️ **Règles:**
• Numéros pairs uniquement
• Pas de numéros terminant par 0
• Minimum 1 numéro

✏️ **Vos numéros:**""")

@client.on(events.NewMessage())
async def handle_registration_and_payment(event):
    if event.is_group or event.is_channel: 
        return

    if event.message.message and event.message.message.startswith('/'): 
        return

    user_id = event.sender_id
    user = get_user(user_id)

    if user_id in admin_predict_state:
        state = admin_predict_state[user_id]
        if state.get('step') == 'awaiting_numbers':
            message_text = event.message.message.strip()

            try:
                numbers_str = [n.strip() for n in message_text.split(',')]
                numbers = []
                errors = []

                for num_str in numbers_str:
                    if not num_str.isdigit():
                        errors.append(f"'{num_str}' n'est pas un nombre valide")
                        continue
                    num = int(num_str)
                    if num % 2 != 0:
                        errors.append(f"#{num} est impair (doit être pair)")
                        continue
                    if num % 10 == 0:
                        errors.append(f"#{num} termine par 0 (interdit)")
                        continue
                    numbers.append(num)

                if errors:
                    error_msg = "❌ **Erreurs détectées:**\n" + "\n".join(errors)
                    await event.respond(error_msg)
                    return

                if not numbers:
                    await event.respond("❌ Aucun numéro valide trouvé. Réessayez.")
                    return

                numbers.sort()

                predictions_list = []
                for i, num in enumerate(numbers):
                    suit = get_suit_for_game(num)
                    predictions_list.append((num, suit))

                status_lines = ["📊 **STATUT PRÉDICTION MANUELLE**\n"]
                for i, (num, suit) in enumerate(predictions_list, 1):
                    status_lines.append(f"🎮 Jeu {i}: {num} 👉🏻 {SUIT_DISPLAY.get(suit, suit)}")

                active_preds_details = []
                for i, (num, suit) in enumerate(predictions_list):
                    estimated_minutes = i * 3
                    active_preds_details.append(f"• #{num}: {SUIT_DISPLAY.get(suit, suit)} (MANUEL) - ⌛ (dans ~{estimated_minutes}min)")

                full_message = "\n".join(status_lines) + f"\n\nPrédictions actives: {len(predictions_list)}\n" + "\n".join(active_preds_details)

                await event.respond("⏳ Envoi des prédictions en cours...")

                for num, suit in predictions_list:
                    await send_prediction_to_users(num, suit, num, rule_type="MANUEL")
                    await asyncio.sleep(0.5)

                await event.respond(full_message)
                logger.info(f"✅ Prédictions manuelles envoyées: {numbers}")

                del admin_predict_state[user_id]
                return

            except Exception as e:
                logger.error(f"Erreur prédiction manuelle: {e}")
                await event.respond(f"❌ Erreur: {e}")
                return

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
                await event.respond(f"✅ Message envoyé avec succès à l'utilisateur {target_user_id}!")
            except Exception as e:
                await event.respond(f"❌ Erreur lors de l'envoi: {e}")

            del admin_message_state[user_id]
            return

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
• 🔥 Accès aux 2 algorithmes (Stats + Cycle)

⚠️ **IMPORTANT:** Restez dans ce chat, ne fermez pas Telegram!
Les meilleures opportunités arrivent sans prévenir!

🍀 **Bonne chance et bienvenue dans l'élite!**"""

            await event.respond(success_msg)
            return

    if user.get('pending_payment') and event.message.photo:
        photo = event.message.photo

        payment_photos_pending[user_id] = {
            'photo_id': photo.id,
            'sent_at': datetime.now(),
            'message_id': event.message.id
        }

        await event.respond("""✅ **Capture d'écran reçue!**

⏳ Transmission à l'administrateur pour validation...
🚀 Votre accès sera activé sous peu!

💎 **Préparez-vous à gagner!**""")

        user_info = get_user(user_id)
        current_time = datetime.now().strftime("%H:%M:%S")

        admin_caption = f"""🔔 **NOUVELLE DEMANDE D'ABONNEMENT**

👤 **Utilisateur:** {user_info.get('nom', 'N/A')} {user_info.get('prenom', 'N/A')}
🆔 **ID:** `{user_id}`
📍 **Pays:** {user_info.get('pays', 'N/A')}
⏰ **Reçu à:** {current_time}

💰 **Choisissez la durée à valider:**"""

        buttons = [
            [Button.inline("✅ 24H (500 FCFA)", data=f"valider_{user_id}_1d")],
            [Button.inline("✅ 1 SEMAINE (1500 FCFA)", data=f"valider_{user_id}_1w")],
            [Button.inline("✅ 2 SEMAINES (2500 FCFA)", data=f"valider_{user_id}_2w")],
            [Button.inline("❌ Rejeter", data=f"rejeter_{user_id}")]
        ]

        try:
            await client.send_file(ADMIN_ID, photo, caption=admin_caption, buttons=buttons)
            asyncio.create_task(send_reminder_if_no_response(user_id))
        except Exception as e:
            logger.error(f"Erreur envoi photo à l'admin: {e}")
            await event.respond("❌ Erreur lors de l'envoi. Veuillez réessayer.")

        return

async def send_reminder_if_no_response(user_id: int):
    await asyncio.sleep(600)

    if user_id in payment_photos_pending:
        try:
            patience_msg = f"""⏰ **INFORMATION**

👤 Cher(e) {get_user(user_id).get('prenom', 'champion')},

Votre paiement est en cours d'examen par **{ADMIN_NAME}**.

🙏 *"La patience est la clé du succès. Chaque grand accomplissement demande du temps."*

✅ Votre accès sera activé très bientôt!

💪 **Restez positif, vos gains vous attendent!**"""

            await client.send_message(user_id, patience_msg)
            del payment_photos_pending[user_id]
        except Exception as e:
            logger.error(f"Erreur envoi message patience: {e}")

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
        users_list.append(f"🆔 `{user_id}` | {prenom} {nom} | {pays} | {status}")

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
        admin_message_state[event.sender_id] = {
            'target_user_id': target_user_id,
            'step': 'awaiting_message'
        }

        await event.respond(f"""✉️ **Envoi à {user_info.get('prenom', 'N/A')} {user_info.get('nom', 'N/A')}**

📝 Écrivez votre message:""")
    except Exception as e:
        await event.respond(f"❌ Erreur: {e}")

@client.on(events.CallbackQuery(data=re.compile(b'valider_(\d+)_(.*)')))
async def handle_validation(event):
    if event.sender_id != ADMIN_ID:
        await event.answer("Accès refusé", alert=True)
        return

    user_id = int(event.data_match.group(1).decode())
    duration = event.data_match.group(2).decode()

    if duration == '1d':
        days = 1
        price = "500 FCFA"
    elif duration == '1w':
        days = 7
        price = "1500 FCFA"
    else:
        days = 14
        price = "2500 FCFA"

    end_date = datetime.now() + timedelta(days=days)
    update_user(user_id, {
        'subscription_end': end_date.isoformat(),
        'subscription_type': 'premium',
        'expiry_notified': False,
        'pending_payment': False
    })

    if user_id in payment_photos_pending:
        del payment_photos_pending[user_id]

    try:
        activation_msg = f"""🎉 **FÉLICITATIONS! VOTRE ACCÈS EST ACTIVÉ!** 🎉

✅ Abonnement **{days} jour(s)** confirmé!
💰 Montant payé: **{price}**
🔥 Vous faites maintenant partie de l'ELITE!

🚀 **Vos avantages:**
• Prédictions prioritaires
• Algorithmes exclusifs
• Mises à jour en temps réel
• Support dédié

💰 **C'est parti pour les gains!**"""

        await client.send_message(user_id, activation_msg)
    except Exception as e:
        logger.error(f"Erreur notification: {e}")

    await event.edit(f"✅ Activé pour {user_id} ({price})")
    await event.answer("Activé!")

@client.on(events.CallbackQuery(data=re.compile(b'rejeter_(\d+)')))
async def handle_rejection(event):
    if event.sender_id != ADMIN_ID:
        await event.answer("Accès refusé", alert=True)
        return

    user_id = int(event.data_match.group(1).decode())

    if user_id in payment_photos_pending:
        del payment_photos_pending[user_id]

    try:
        await client.send_message(user_id, "❌ Demande rejetée. Contactez le support.")
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

    r2_status = "ACTIVE 🔥" if rule2_active else "Inactif"
    r1_status = f"{rule1_consecutive_count}/{MAX_RULE1_CONSECUTIVE}"

    status_msg = f"""📊 **STATUT SYSTÈME**

🎮 Jeu actuel: #{last_source_game_number}
🔢 Paramètre 'a': {USER_A}
⏳ Règle 2: {r2_status}
⏱️ Règle 1: {r1_status}
👥 Utilisateurs: {len(users_data)}

**Prédictions actives: {len(pending_predictions)}**"""

    if pending_predictions:
        for game_num, pred in sorted(pending_predictions.items()):
            distance = game_num - last_source_game_number
            ratt = f" [R{pred['rattrapage']}]" if pred.get('rattrapage', 0) > 0 else ""
            rule = pred.get('rule_type', 'R2')
            status_msg += f"\n• #{game_num}{ratt}: {pred['suit']} ({rule}) - {pred['status']} (dans {distance})"

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

    win_rate = (stats_bilan['wins'] / stats_bilan['total']) * 100

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
    global current_game_number, last_source_game_number, stats_bilan
    global rule1_consecutive_count, rule2_active, suit_prediction_counts
    global last_known_source_game, prediction_target_game, waiting_for_one_part, cycle_triggered
    global current_time_cycle_index, next_prediction_allowed_at, already_predicted_games
    global payment_photos_pending, admin_predict_state

    users_data = {}
    save_users_data()
    pending_predictions.clear()
    queued_predictions.clear()
    processed_messages.clear()
    already_predicted_games.clear()
    suit_prediction_counts.clear()
    payment_photos_pending.clear()
    admin_predict_state.clear()

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

    stats_bilan = {'total': 0, 'wins': 0, 'losses': 0, 'win_details': {'✅0️⃣': 0, '✅1️⃣': 0, '✅2️⃣': 0}, 'loss_details': {'❌': 0}}

    await event.respond("🚨 **RESET TOTAL EFFECTUÉ**")

@client.on(events.NewMessage(pattern='/help'))
async def cmd_help(event):
    if event.is_group or event.is_channel: 
        return

    help_msg = """📖 **CENTRE D'AIDE**

🎯 **Comment utiliser:**
1️⃣ /start - Inscription
2️⃣ 60min d'essai GRATUIT
3️⃣ Attendez les prédictions
4️⃣ Résultats auto!

💰 **Tarifs:**
• 500 FCFA = 24H
• 1500 FCFA = 1 semaine
• 2500 FCFA = 2 semaines

📊 **Commandes:**
/start - Profil & statut
/status - État système (admin)
/bilan - Stats (admin)
/users - Liste users (admin)
/msg ID - Message privé (admin)
/predict - Prédiction manuelle (admin)

❓ **Support:** Contactez l'admin"""

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

⚡ **24H - 500 FCFA**
🔥 **1 SEMAINE - 1500 FCFA**  
💎 **2 SEMAINES - 2500 FCFA**

📸 **Après paiement:**
Envoyez la capture d'écran DIRECTEMENT ICI

👇 **CLIQUEZ SUR VOTRE FORMULE:**"""

    await event.respond(payment_msg, buttons=buttons)
    update_user(user_id, {'pending_payment': True})

# Serveur Web pour Render.com
async def index(request):
    html = f"""<!DOCTYPE html>
<html>
<head>
    <title>Bot Baccarat Elite</title>
    <style>
        body {{ font-family: Arial, sans-serif; background: linear-gradient(135deg, #1e3c72 0%, #2a5298 100%); color: white; text-align: center; padding: 50px; }}
        h1 {{ font-size: 3em; margin-bottom: 20px; }}
        .status {{ background: rgba(255,255,255,0.1); padding: 30px; border-radius: 15px; display: inline-block; margin: 20px; }}
        .number {{ font-size: 2.5em; font-weight: bold; color: #ffd700; }}
        .label {{ font-size: 1.2em; opacity: 0.9; }}
    </style>
</head>
<body>
    <h1>🎰 Bot Baccarat ELITE</h1>
    <div class="status">
        <div class="label">Jeu Actuel</div>
        <div class="number">#{current_game_number}</div>
    </div>
    <div class="status">
        <div class="label">Utilisateurs</div>
        <div class="number">{len(users_data)}</div>
    </div>
    <div class="status">
        <div class="label">Statut</div>
        <div class="number">{'🟢 ON' if client.is_connected() else '🔴 OFF'}</div>
    </div>
    <p style="margin-top: 40px;">Système opérationnel | Port {PORT}</p>
</body>
</html>"""
    return web.Response(text=html, content_type='text/html', status=200)

async def health_check(request):
    return web.Response(text="OK", status=200)

async def start_web_server():
    app = web.Application()
    app.router.add_get('/', index)
    app.router.add_get('/health', health_check)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', PORT)
    await site.start()
    logger.info(f"✅ Serveur web démarré sur le port {PORT}")

async def schedule_daily_reset():
    global rule1_consecutive_count, rule2_active, suit_prediction_counts

    wat_tz = timezone(timedelta(hours=1))
    reset_time = time(0, 59, tzinfo=wat_tz)

    while True:
        now = datetime.now(wat_tz)
        target_datetime = datetime.combine(now.date(), reset_time, tzinfo=wat_tz)
        if now >= target_datetime:
            target_datetime += timedelta(days=1)

        await asyncio.sleep((target_datetime - now).total_seconds())

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

        stats_bilan = {'total': 0, 'wins': 0, 'losses': 0, 'win_details': {'✅0️⃣': 0, '✅1️⃣': 0, '✅2️⃣': 0}, 'loss_details': {'❌': 0}}

        logger.warning("✅ Reset quotidien effectué")

async def start_bot():
    try:
        await client.start(bot_token=BOT_TOKEN)
        logger.info("✅ Bot Telegram connecté!")
        return True
    except Exception as e:
        logger.error(f"❌ Erreur connexion: {e}")
        return False

async def main():
    load_users_data()
    try:
        await start_web_server()
        success = await start_bot()
        if not success:
            return

        asyncio.create_task(schedule_daily_reset())
        logger.info("🚀 BOT OPÉRATIONNEL")
        await client.run_until_disconnected()
    except Exception as e:
        logger.error(f"Erreur main: {e}")
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
