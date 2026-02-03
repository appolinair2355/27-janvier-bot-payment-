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

# Configuration
API_ID = int(os.getenv('API_ID', 0))
API_HASH = os.getenv('API_HASH', '')
BOT_TOKEN = os.getenv('BOT_TOKEN', '')
ADMIN_ID = int(os.getenv('ADMIN_ID', 0))
SOURCE_CHANNEL_ID = int(os.getenv('SOURCE_CHANNEL_ID', 0))
SOURCE_CHANNEL_2_ID = int(os.getenv('SOURCE_CHANNEL_2_ID', 0))
PORT = int(os.getenv('PORT', 10000))

SUIT_MAPPING = {'♠': '♥', '♥': '♦', '♦': '♣', '♣': '♠'}
ALL_SUITS = ['♠', '♥', '♦', '♣']
SUIT_DISPLAY = {'♠': '♠️ Pique', '♥': '♥️ Cœur', '♦': '♦️ Carreau', '♣': '♣️ Trèfle'}

PAYMENT_LINK_24H = os.getenv('PAYMENT_LINK_24H', 'https://payment.example.com/500')
PAYMENT_LINK_1W = os.getenv('PAYMENT_LINK_1W', 'https://payment.example.com/1500')
PAYMENT_LINK_2W = os.getenv('PAYMENT_LINK_2W', 'https://payment.example.com/2500')

USERS_FILE = "users_data.json"
ADMIN_NAME = os.getenv('ADMIN_NAME', 'Sossou Kouamé')
ADMIN_TITLE = os.getenv('ADMIN_TITLE', 'Administrateur')

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s', handlers=[logging.StreamHandler(sys.stdout)])
logger = logging.getLogger(__name__)

if not API_ID or not API_HASH or not BOT_TOKEN:
    logger.error("Configuration manquante")
    exit(1)

session_string = os.getenv('TELEGRAM_SESSION', '')
client = TelegramClient(StringSession(session_string), API_ID, API_HASH)

# === VARIABLES GLOBALES ===
pending_predictions = {}
queued_predictions = {}
processed_messages = set()
current_game_number = 0
last_source_game_number = 0
last_seen_game_number = 0
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
manual_prediction_messages = {}

def load_users_data():
    global users_data
    try:
        if os.path.exists(USERS_FILE):
            with open(USERS_FILE, 'r', encoding='utf-8') as f:
                users_data = json.load(f)
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
        return datetime.now() < datetime.fromisoformat(user['subscription_end'])
    except:
        return False

def is_trial_active(user_id: int) -> bool:
    user = get_user(user_id)
    if user.get('trial_used') or not user.get('trial_started'):
        return False
    try:
        trial_end = datetime.fromisoformat(user['trial_started']) + timedelta(minutes=60)
        return datetime.now() < trial_end
    except:
        return False

def can_receive_predictions(user_id: int) -> bool:
    user = get_user(user_id)
    return user.get('registered') and (is_user_subscribed(user_id) or is_trial_active(user_id))

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
        idx = SUIT_CYCLE.index(current_suit)
        return SUIT_CYCLE[(idx + 1) % len(SUIT_CYCLE)]
    except:
        return SUIT_CYCLE[0]

def get_suit_for_game(game_number: int) -> str:
    if game_number >= 6:
        count = sum(1 for n in range(6, game_number + 1, 2) if n % 10 != 0)
        if count > 0:
            return SUIT_CYCLE[(count - 1) % len(SUIT_CYCLE)]
    return '♥'

def extract_game_number(message: str):
    match = re.search(r'#N\s*(\d+)', message, re.IGNORECASE)
    if match:
        return int(match.group(1))
    match = re.search(r'#(\d+)', message)
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
    return re.findall(r'\(([^)]+)\)', message)

def normalize_suits(group_str: str) -> str:
    normalized = group_str.replace('❤️', '♥').replace('❤', '♥').replace('♥️', '♥')
    normalized = normalized.replace('♠️', '♠').replace('♦️', '♦').replace('♣️', '♣')
    return normalized

def has_suit_in_group(group_str: str, target_suit: str) -> bool:
    normalized = normalize_suits(group_str)
    target = normalize_suits(target_suit)
    return any(s in target and s in normalized for s in ALL_SUITS)

# === FONCTIONS D'ENVOI ===

async def send_prediction_to_all_users(prediction_msg: str, target_game: int, rule_type: str = "R2"):
    private_messages = {}
    sent_count = 0

    try:
        if ADMIN_ID and ADMIN_ID != 0:
            admin_msg = await client.send_message(ADMIN_ID, prediction_msg)
            private_messages[str(ADMIN_ID)] = admin_msg.id
    except Exception as e:
        logger.error(f"Erreur envoi admin: {e}")

    for user_id_str in users_data:
        try:
            user_id = int(user_id_str)
            if user_id == ADMIN_ID:
                continue
            if not can_receive_predictions(user_id):
                continue

            sent_msg = await client.send_message(user_id, prediction_msg)
            private_messages[user_id_str] = sent_msg.id
            sent_count += 1
        except Exception as e:
            logger.error(f"Erreur envoi à {user_id_str}: {e}")

    logger.info(f"Prédiction envoyée à {sent_count} utilisateurs")
    return private_messages

async def edit_prediction_for_all_users(game_number: int, new_status: str, suit: str, rule_type: str, original_game: int = None):
    display_game = original_game if original_game else game_number

    status_map = {
        "❌": "❌ PERDU",
        "✅0️⃣": "✅ VICTOIRE!",
        "✅1️⃣": "✅ 2ÈME JEU!",
        "✅2️⃣": "✅ 3ÈME JEU!",
        "✅3️⃣": "✅ 4ÈME JEU!"
    }
    status_text = status_map.get(new_status, new_status)
    algo_text = "Règle 2" if rule_type == "R2" else ("Manuel" if rule_type == "MANUEL" else "Règle 1")

    updated_msg = f"""🎰 **PRÉDICTION #{display_game}**

🎯 Couleur: {SUIT_DISPLAY.get(suit, suit)}
📊 Statut: {status_text}
🤖 Algorithme: {algo_text}"""

    if game_number not in pending_predictions:
        return 0

    pred = pending_predictions[game_number]
    private_msgs = pred.get('private_messages', {})
    edited_count = 0

    for user_id_str, msg_id in list(private_msgs.items()):
        try:
            await client.edit_message(int(user_id_str), msg_id, updated_msg)
            edited_count += 1
        except Exception as e:
            if "message to edit not found" in str(e).lower():
                del private_msgs[user_id_str]

    return edited_count

async def send_prediction_to_users(target_game: int, predicted_suit: str, base_game: int, 
                                     rattrapage=0, original_game=None, rule_type="R2"):
    global rule2_active, rule1_consecutive_count

    try:
        if rattrapage > 0:
            original_msgs = {}
            if original_game and original_game in pending_predictions:
                original_msgs = pending_predictions[original_game].get('private_messages', {}).copy()

            pending_predictions[target_game] = {
                'message_id': 0, 'suit': predicted_suit, 'base_game': base_game,
                'status': '🔮', 'rattrapage': rattrapage, 'original_game': original_game,
                'rule_type': rule_type, 'private_messages': original_msgs,
                'created_at': datetime.now().isoformat()
            }

            if rule_type == "R2":
                rule2_active = True
            return True

        if rule_type == "R1":
            active_r2 = [p for game, p in pending_predictions.items() 
                        if p.get('rule_type') == 'R2' and p.get('rattrapage', 0) == 0 and game > current_game_number]
            if active_r2:
                logger.info(f"Règle 2 active, Règle 1 bloquée pour #{target_game}")
                return False

        # === MESSAGE DE PRÉDICTION AUTOMATIQUE RÈGLE 2 ===
        if rule_type == "R2":
            prediction_msg = f"""🎰 **PRÉDICTION #{target_game}**

🎯 Couleur: {SUIT_DISPLAY.get(predicted_suit, predicted_suit)}
⏳ Statut: ⏳ EN ATTENTE...
🤖 Algorithme: Règle 2 (Stats)"""

        # === MESSAGE DE PRÉDICTION AUTOMATIQUE RÈGLE 1 ===
        elif rule_type == "R1":
            prediction_msg = f"""🎰 **PRÉDICTION #{target_game}**

🎯 Couleur: {SUIT_DISPLAY.get(predicted_suit, predicted_suit)}
⏳ Statut: ⏳ EN ATTENTE...
🤖 Algorithme: Règle 1 (Cycle)"""

        # === MESSAGE DE PRÉDICTION MANUELLE ===
        else:
            prediction_msg = f"""🎰 **PRÉDICTION MANUELLE #{target_game}**

🎯 Couleur: {SUIT_DISPLAY.get(predicted_suit, predicted_suit)}
⏳ Statut: ⏳ EN ATTENTE...
🤖 Algorithme: Manuel"""

        private_messages = await send_prediction_to_all_users(prediction_msg, target_game, rule_type)

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
            logger.info(f"✅ Règle 2: #{target_game} - {predicted_suit}")
        elif rule_type == "R1":
            rule1_consecutive_count += 1
            logger.info(f"✅ Règle 1: #{target_game} - {predicted_suit} (consécutif: {rule1_consecutive_count})")

        return True

    except Exception as e:
        logger.error(f"Erreur envoi prédiction: {e}")
        return False

def queue_prediction(target_game: int, predicted_suit: str, base_game: int, 
                    rattrapage=0, original_game=None, rule_type="R2"):
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
    global current_game_number
    current_game_number = current_game

    for target_game in sorted(list(queued_predictions.keys())):
        if target_game >= current_game:
            pred = queued_predictions.pop(target_game)
            await send_prediction_to_users(
                pred['target_game'], pred['predicted_suit'], pred['base_game'],
                pred.get('rattrapage', 0), pred.get('original_game'), pred.get('rule_type', 'R2')
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
            await check_and_send_queued_predictions(current_game_number)

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
            await check_and_send_queued_predictions(current_game_number)

        return True

    except Exception as e:
        logger.error(f"Erreur update status: {e}")
        return False

async def check_prediction_result(game_number: int, first_group: str):
    logger.info(f"🔍 Vérification résultat pour jeu #{game_number}")

    if game_number in pending_predictions:
        pred = pending_predictions[game_number]
        if pred.get('rattrapage', 0) == 0:
            target_suit = pred['suit']
            rule_type = pred.get('rule_type', 'R2')

            if has_suit_in_group(first_group, target_suit):
                logger.info(f"✅0️⃣ TROUVÉ pour #{game_number}!")
                await update_prediction_status(game_number, '✅0️⃣')
                return
            else:
                next_target = game_number + 1
                queue_prediction(next_target, target_suit, pred['base_game'], 1, game_number, rule_type)
                logger.info(f"❌ #{game_number} échoué, rattrapage #{next_target} planifié")

    for target_game, pred in list(pending_predictions.items()):
        if target_game == game_number and pred.get('rattrapage', 0) > 0:
            original_game = pred.get('original_game', target_game - pred['rattrapage'])
            target_suit = pred['suit']
            rattrapage_actuel = pred['rattrapage']
            rule_type = pred.get('rule_type', 'R2')

            if has_suit_in_group(first_group, target_suit):
                status = f'✅{rattrapage_actuel}️⃣'
                logger.info(f"{status} TROUVÉ pour #{original_game} au rattrapage #{game_number}!")
                await update_prediction_status(original_game, status)
                if target_game != original_game and target_game in pending_predictions:
                    del pending_predictions[target_game]
                return
            else:
                if rattrapage_actuel < 3:
                    next_rattrapage = rattrapage_actuel + 1
                    next_target = game_number + 1
                    queue_prediction(next_target, target_suit, pred['base_game'], 
                                   next_rattrapage, original_game, rule_type)
                    logger.info(f"❌ Rattrapage {rattrapage_actuel} échoué, #{next_target} planifié")
                    if target_game in pending_predictions:
                        del pending_predictions[target_game]
                else:
                    logger.info(f"❌ DÉFINITIF pour #{original_game} après 3 rattrapages")
                    await update_prediction_status(original_game, '❌')
                    if target_game != original_game and target_game in pending_predictions:
                        del pending_predictions[target_game]
                return

# === RÈGLE 2 ===

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
                    logger.info(f"Règle 2: Limite atteinte pour {predicted_suit}")
                    continue

                logger.info(f"🎯 RÈGLE 2 DÉCLENCHÉE: {s1}({v1}) vs {s2}({v2}) = {diff} → {predicted_suit}")

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

# === RÈGLE 1 ===

async def cycle_time_checker():
    global cycle_triggered, waiting_for_one_part, prediction_target_game
    global current_time_cycle_index, next_prediction_allowed_at, rule1_consecutive_count
    global rule2_active, last_known_source_game

    logger.info("⏰ Cycle temporel démarré")

    while True:
        try:
            await asyncio.sleep(10)

            now = datetime.now()

            if now >= next_prediction_allowed_at and not rule2_active and rule1_consecutive_count < MAX_RULE1_CONSECUTIVE:

                if not cycle_triggered and last_known_source_game > 0:
                    logger.info(f"⏰ CYCLE TEMPS: Déclenchement à {now.strftime('%H:%M:%S')}")

                    candidate = last_known_source_game + 2
                    while candidate % 2 != 0 or candidate % 10 == 0:
                        candidate += 1

                    prediction_target_game = candidate
                    cycle_triggered = True
                    waiting_for_one_part = True

                    logger.info(f"⏰ Cible: #{prediction_target_game}")

                    await try_launch_prediction_rule1()

            elif waiting_for_one_part and cycle_triggered and not rule2_active:
                await try_launch_prediction_rule1()

        except Exception as e:
            logger.error(f"❌ Erreur cycle checker: {e}")

async def try_launch_prediction_rule1():
    global waiting_for_one_part, prediction_target_game, cycle_triggered
    global current_time_cycle_index, next_prediction_allowed_at, rule1_consecutive_count
    global rule2_active, last_known_source_game

    if rule2_active or rule1_consecutive_count >= MAX_RULE1_CONSECUTIVE:
        return False

    if not cycle_triggered or prediction_target_game is None or last_known_source_game == 0:
        return False

    diff = prediction_target_game - last_known_source_game

    if diff == 1 and last_known_source_game % 2 != 0:
        logger.info(f"🚀 RÈGLE 1: Condition remplie! {last_known_source_game} → {prediction_target_game}")

        predicted_suit = get_suit_for_game(prediction_target_game)

        success = await send_prediction_to_users(
            prediction_target_game, predicted_suit, last_known_source_game, rule_type="R1"
        )

        if success:
            waiting_for_one_part = False
            cycle_triggered = False
            prediction_target_game = None

            wait_min = TIME_CYCLE[current_time_cycle_index]
            next_prediction_allowed_at = datetime.now() + timedelta(minutes=wait_min)
            current_time_cycle_index = (current_time_cycle_index + 1) % len(TIME_CYCLE)

            logger.info(f"✅ Règle 1 envoyée. Prochain dans {wait_min}min")
            return True
    elif diff > 1:
        logger.debug(f"⏳ Attente: {diff-1} parts restantes")

    return False

async def process_prediction_logic_rule1(message_text: str, chat_id: int):
    global last_known_source_game, current_game_number

    if chat_id != SOURCE_CHANNEL_ID:
        return

    game_number = extract_game_number(message_text)
    if game_number is None:
        return

    last_known_source_game = game_number
    current_game_number = game_number

    if waiting_for_one_part and cycle_triggered:
        await try_launch_prediction_rule1()

def is_message_finalized(message: str) -> bool:
    if '⏰' in message:
        return False
    return any(x in message for x in ['✅', '🔰', '▶️', 'Finalisé'])

async def process_finalized_message(message_text: str, chat_id: int):
    global current_game_number, last_source_game_number, last_seen_game_number

    try:
        game_number = extract_game_number(message_text)
        if game_number:
            last_seen_game_number = game_number

        if chat_id == SOURCE_CHANNEL_2_ID:
            await process_stats_message(message_text)
            await check_and_send_queued_predictions(current_game_number)
            return

        if not is_message_finalized(message_text):
            return

        if game_number is None:
            return

        current_game_number = game_number
        last_source_game_number = game_number

        message_hash = f"{game_number}_{message_text[:50]}"
        if message_hash in processed_messages:
            return
        processed_messages.add(message_hash)

        groups = extract_parentheses_groups(message_text)
        if not groups:
            return

        await check_prediction_result(game_number, groups[0])
        await check_and_send_queued_predictions(game_number)

    except Exception as e:
        logger.error(f"Erreur traitement finalisé: {e}")

# === GESTION MESSAGES ===

async def handle_message(event):
    try:
        chat = await event.get_chat()
        chat_id = chat.id
        if hasattr(chat, 'broadcast') and chat.broadcast:
            if not str(chat_id).startswith('-100'):
                chat_id = int(f"-100{abs(chat_id)}")

        if chat_id == SOURCE_CHANNEL_ID:
            message_text = event.message.message
            game_num = extract_game_number(message_text)
            if game_num:
                global last_seen_game_number
                last_seen_game_number = game_num

            await process_prediction_logic_rule1(message_text, chat_id)

            if is_message_finalized(message_text):
                await process_finalized_message(message_text, chat_id)

        elif chat_id == SOURCE_CHANNEL_2_ID:
            await process_stats_message(event.message.message)
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
            game_num = extract_game_number(message_text)
            if game_num:
                global last_seen_game_number
                last_seen_game_number = game_num

            await process_prediction_logic_rule1(message_text, chat_id)

            if is_message_finalized(message_text):
                await process_finalized_message(message_text, chat_id)

        elif chat_id == SOURCE_CHANNEL_2_ID:
            await process_stats_message(event.message.message)
            await check_and_send_queued_predictions(current_game_number)

    except Exception as e:
        logger.error(f"Erreur handle_edited_message: {e}")

client.add_event_handler(handle_message, events.NewMessage())
client.add_event_handler(handle_edited_message, events.MessageEdited())

# === COMMANDES ===

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

            msg = f"""🎯 **BON RETOUR {user.get('prenom', 'CHAMPION').upper()}!**

✅ Votre accès **{sub_type}** est ACTIF!
📅 Expiration: {sub_end[:10] if sub_end and user_id != ADMIN_ID else sub_end}

🔥 Les prédictions arrivent automatiquement ici."""
            await event.respond(msg)
            return

        elif is_trial_active(user_id):
            trial_start = datetime.fromisoformat(user['trial_started'])
            remaining = (trial_start + timedelta(minutes=60) - datetime.now()).seconds // 60
            await event.respond(f"⏰ **Essai en cours:** {remaining} minutes restantes")
            return

        else:
            update_user(user_id, {'trial_used': True})
            buttons = [
                [Button.url("💳 24H - 500 FCFA", PAYMENT_LINK_24H)],
                [Button.url("💳 1 SEMAINE - 1500 FCFA", PAYMENT_LINK_1W)],
                [Button.url("💳 2 SEMAINES - 2500 FCFA", PAYMENT_LINK_2W)]
            ]
            await event.respond("⚠️ Essai terminé. Choisissez une formule:", buttons=buttons)
            return

    await event.respond("""🎰 **BIENVENUE DANS L'ELITE!**

💎 Bot de prédiction Baccarat avancé
🚀 60 MINUTES D'ESSAI GRATUIT

👇 Commençons votre inscription!""")

    user_conversation_state[user_id] = 'awaiting_nom'
    await event.respond("📝 **Étape 1/3: Quel est votre NOM?**")

def get_subscription_type(user_id):
    return get_user(user_id).get('subscription_type')

@client.on(events.NewMessage(pattern='/predict'))
async def cmd_predict(event):
    if event.is_group or event.is_channel:
        return

    if event.sender_id != ADMIN_ID:
        await event.respond("❌ Commande admin uniquement")
        return

    admin_predict_state[event.sender_id] = {'step': 'awaiting_numbers'}

    await event.respond("""🎯 **MODE PRÉDICTION MANUELLE**

📝 Entrez les numéros **pairs** (sans 0) séparés par des virgules
**Exemple:** `202,384,786`

⚠️ Règles: pairs uniquement, pas de 0 à la fin

✏️ **Vos numéros:**""")

@client.on(events.NewMessage())
async def handle_conversations(event):
    if event.is_group or event.is_channel:
        return
    if event.message.message.startswith('/'):
        return

    user_id = event.sender_id
    user = get_user(user_id)
    text = event.message.message.strip()

    if user_id in admin_predict_state:
        state = admin_predict_state[user_id]
        if state.get('step') == 'awaiting_numbers':
            try:
                numbers_str = [n.strip() for n in text.split(',')]
                numbers = []
                errors = []

                for num_str in numbers_str:
                    if not num_str.isdigit():
                        errors.append(f"'{num_str}' invalide")
                        continue
                    num = int(num_str)
                    if num % 2 != 0:
                        errors.append(f"#{num} impair")
                        continue
                    if num % 10 == 0:
                        errors.append(f"#{num} termine par 0")
                        continue
                    numbers.append(num)

                if errors:
                    await event.respond("❌ **Erreurs:**\n" + "\n".join(errors))
                    return

                if not numbers:
                    await event.respond("❌ Aucun numéro valide")
                    return

                numbers.sort()

                predictions_data = {}
                lines = ["📊 **PRÉDICTIONS MANUELLES**\n"]

                for i, num in enumerate(numbers, 1):
                    suit = get_suit_for_game(num)
                    predictions_data[num] = {'suit': suit, 'status': '⏳', 'original_num': num}
                    lines.append(f"🎮 Jeu {i}: {num} 👉🏻 {SUIT_DISPLAY.get(suit, suit)} | Statut: ⏳")

                lines.append(f"\n**Total:** {len(numbers)} prédictions")
                message_text = "\n".join(lines)

                msg = await event.respond(message_text)

                manual_prediction_messages[user_id] = {
                    'message_id': msg.id,
                    'chat_id': event.chat_id,
                    'predictions': predictions_data,
                    'numbers_list': numbers
                }

                await send_manual_predictions_to_users(numbers, predictions_data)

                del admin_predict_state[user_id]
                logger.info(f"✅ Prédictions manuelles créées: {numbers}")
                return

            except Exception as e:
                logger.error(f"Erreur predict: {e}")
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

        if state == 'awaiting_nom':
            update_user(user_id, {'nom': text})
            user_conversation_state[user_id] = 'awaiting_prenom'
            await event.respond("✅ Nom enregistré. **Étape 2/3: Votre prénom?**")
            return

        elif state == 'awaiting_prenom':
            update_user(user_id, {'prenom': text})
            user_conversation_state[user_id] = 'awaiting_pays'
            await event.respond("✅ Prénom enregistré. **Étape 3/3: Votre pays?**")
            return

        elif state == 'awaiting_pays':
            update_user(user_id, {
                'pays': text, 'registered': True,
                'trial_started': datetime.now().isoformat(), 'trial_used': False
            })
            del user_conversation_state[user_id]
            await event.respond("""🎉 **FÉLICITATIONS!**

✅ Compte activé!
⏰ 60 minutes d'essai gratuites!

🚀 Les prédictions arriveront automatiquement ici.""")
            return

    if user.get('pending_payment') and event.message.photo:
        photo = event.message.photo
        payment_photos_pending[user_id] = {'photo_id': photo.id, 'sent_at': datetime.now()}

        await event.respond("✅ Capture reçue! Transmission à l'admin...")

        user_info = get_user(user_id)
        caption = f"""🔔 **NOUVELLE DEMANDE**

👤 {user_info.get('nom', 'N/A')} {user_info.get('prenom', 'N/A')}
🆔 `{user_id}`
📍 {user_info.get('pays', 'N/A')}

**Choisir la durée:**"""

        buttons = [
            [Button.inline("✅ 24H (500 FCFA)", data=f"valider_{user_id}_1d")],
            [Button.inline("✅ 1 SEMAINE (1500 FCFA)", data=f"valider_{user_id}_1w")],
            [Button.inline("✅ 2 SEMAINES (2500 FCFA)", data=f"valider_{user_id}_2w")],
            [Button.inline("❌ Rejeter", data=f"rejeter_{user_id}")]
        ]

        try:
            await client.send_file(ADMIN_ID, photo, caption=caption, buttons=buttons)
            asyncio.create_task(send_reminder_if_no_response(user_id))
        except Exception as e:
            logger.error(f"Erreur envoi admin: {e}")
        return

async def send_manual_predictions_to_users(numbers, predictions_data):
    lines = ["📊 **PRÉDICTIONS MANUELLES**\n"]
    for i, num in enumerate(numbers, 1):
        suit = predictions_data[num]['suit']
        lines.append(f"🎮 Jeu {i}: {num} 👉🏻 {SUIT_DISPLAY.get(suit, suit)} | Statut: ⏳")
    lines.append(f"\n**Total:** {len(numbers)} prédictions")

    message_text = "\n".join(lines)

    for user_id_str in users_data:
        try:
            user_id = int(user_id_str)
            if not can_receive_predictions(user_id):
                continue
            await client.send_message(user_id, message_text)
        except Exception as e:
            logger.error(f"Erreur envoi manuel à {user_id_str}: {e}")

async def send_reminder_if_no_response(user_id: int):
    await asyncio.sleep(600)
    if user_id in payment_photos_pending:
        try:
            msg = f"""⏰ **PATIENCE**

Cher {get_user(user_id).get('prenom', 'champion')},

Votre paiement est en cours d'examen par **{ADMIN_NAME}**.

🙏 *"La patience est la clé du succès."*

✅ Activation bientôt!"""
            await client.send_message(user_id, msg)
            del payment_photos_pending[user_id]
        except Exception as e:
            logger.error(f"Erreur rappel: {e}")

@client.on(events.NewMessage(pattern='/users'))
async def cmd_users(event):
    if event.is_group or event.is_channel or event.sender_id != ADMIN_ID:
        return

    if not users_data:
        await event.respond("📊 Aucun utilisateur")
        return

    users_list = []
    for uid_str, uinfo in users_data.items():
        uid = int(uid_str)
        status = get_user_status(uid)
        users_list.append(f"🆔 `{uid}` | {uinfo.get('prenom', 'N/A')} {uinfo.get('nom', 'N/A')} | {status}")

    for i in range(0, len(users_list), 50):
        chunk = users_list[i:i+50]
        await event.respond("📋 **UTILISATEURS**\n" + "\n".join(chunk))
        await asyncio.sleep(0.5)

@client.on(events.NewMessage(pattern=r'^/msg (\d+)$'))
async def cmd_msg(event):
    if event.is_group or event.is_channel or event.sender_id != ADMIN_ID:
        return

    try:
        target_id = int(event.pattern_match.group(1))
        if str(target_id) not in users_data:
            await event.respond("❌ Utilisateur non trouvé")
            return

        admin_message_state[event.sender_id] = {'target_user_id': target_id, 'step': 'awaiting_message'}
        await event.respond("✉️ **Écrivez votre message:**")
    except Exception as e:
        await event.respond(f"❌ Erreur: {e}")

@client.on(events.CallbackQuery(data=re.compile(b'valider_(\d+)_(.*)')))
async def handle_validation(event):
    if event.sender_id != ADMIN_ID:
        await event.answer("Accès refusé", alert=True)
        return

    user_id = int(event.data_match.group(1).decode())
    duration = event.data_match.group(2).decode()

    days = {'1d': 1, '1w': 7, '2w': 14}.get(duration, 1)
    price = {'1d': '500 FCFA', '1w': '1500 FCFA', '2w': '2500 FCFA'}.get(duration, '500 FCFA')

    end_date = datetime.now() + timedelta(days=days)
    update_user(user_id, {
        'subscription_end': end_date.isoformat(),
        'subscription_type': 'premium',
        'pending_payment': False
    })

    if user_id in payment_photos_pending:
        del payment_photos_pending[user_id]

    try:
        await client.send_message(user_id, f"""🎉 **ACTIVÉ!**

✅ {days} jour(s) confirmé
💰 {price}
🔥 Bienvenue dans l'élite!""")
    except Exception as e:
        logger.error(f"Erreur notif: {e}")

    await event.edit(f"✅ Activé {user_id} ({price})")

@client.on(events.CallbackQuery(data=re.compile(b'rejeter_(\d+)')))
async def handle_rejection(event):
    if event.sender_id != ADMIN_ID:
        await event.answer("Accès refusé", alert=True)
        return

    user_id = int(event.data_match.group(1).decode())
    if user_id in payment_photos_pending:
        del payment_photos_pending[user_id]

    try:
        await client.send_message(user_id, "❌ Demande rejetée")
    except:
        pass

    await event.edit(f"❌ Rejeté {user_id}")

@client.on(events.NewMessage(pattern='/status'))
async def cmd_status(event):
    if event.is_group or event.is_channel:
        return
    if event.sender_id != ADMIN_ID:
        await event.respond("Commande admin uniquement")
        return

    current_display = last_seen_game_number if last_seen_game_number > 0 else last_source_game_number

    r2_status = "ACTIVE 🔥" if rule2_active else "Inactif"
    r1_status = f"{rule1_consecutive_count}/{MAX_RULE1_CONSECUTIVE}"

    now = datetime.now()
    time_to_next = "N/A"
    if next_prediction_allowed_at > now:
        diff = next_prediction_allowed_at - now
        time_to_next = f"{diff.seconds//60}min"
    else:
        time_to_next = "DÛ"

    msg = f"""📊 **STATUT SYSTÈME**

🎮 **Dernier vu:** #{current_display}
🔢 Paramètre 'a': {USER_A}
⏳ Règle 2: {r2_status}
⏱️ Règle 1: {r1_status}
🕐 Prochain cycle: {time_to_next}
👥 Utilisateurs: {len(users_data)}

**Prédictions actives:** {len(pending_predictions)}"""

    if pending_predictions:
        for game_num, pred in sorted(pending_predictions.items()):
            distance = game_num - current_display
            ratt = f"[R{pred['rattrapage']}]" if pred.get('rattrapage', 0) > 0 else ""
            rule = pred.get('rule_type', 'R2')
            msg += f"\n• #{game_num}{ratt}: {pred['suit']} ({rule}) - {pred['status']} (dans {distance})"

    await event.respond(msg)

@client.on(events.NewMessage(pattern='/bilan'))
async def cmd_bilan(event):
    if event.is_group or event.is_channel or event.sender_id != ADMIN_ID:
        return

    if stats_bilan['total'] == 0:
        await event.respond("📊 Aucune prédiction")
        return

    win_rate = (stats_bilan['wins'] / stats_bilan['total']) * 100

    msg = f"""📊 **BILAN**

🎯 Total: {stats_bilan['total']}
✅ Victoires: {stats_bilan['wins']} ({win_rate:.1f}%)
❌ Défaites: {stats_bilan['losses']}

**Détails:**
• Immédiates: {stats_bilan['win_details'].get('✅0️⃣', 0)}
• 2ème: {stats_bilan['win_details'].get('✅1️⃣', 0)}
• 3ème: {stats_bilan['win_details'].get('✅2️⃣', 0)}"""

    await event.respond(msg)

@client.on(events.NewMessage(pattern='/reset'))
async def cmd_reset(event):
    if event.is_group or event.is_channel or event.sender_id != ADMIN_ID:
        return

    global users_data, pending_predictions, queued_predictions, processed_messages
    global current_game_number, last_source_game_number, last_seen_game_number, stats_bilan
    global rule1_consecutive_count, rule2_active, suit_prediction_counts
    global last_known_source_game, prediction_target_game, waiting_for_one_part, cycle_triggered
    global current_time_cycle_index, next_prediction_allowed_at, already_predicted_games
    global payment_photos_pending, admin_predict_state, manual_prediction_messages

    users_data = {}
    save_users_data()
    pending_predictions.clear()
    queued_predictions.clear()
    processed_messages.clear()
    already_predicted_games.clear()
    suit_prediction_counts.clear()
    payment_photos_pending.clear()
    admin_predict_state.clear()
    manual_prediction_messages.clear()

    current_game_number = 0
    last_source_game_number = 0
    last_seen_game_number = 0
    last_known_source_game = 0
    prediction_target_game = None
    waiting_for_one_part = False
    cycle_triggered = False
    current_time_cycle_index = 0
    next_prediction_allowed_at = datetime.now()
    rule1_consecutive_count = 0
    rule2_active = False

    stats_bilan = {'total': 0, 'wins': 0, 'losses': 0, 'win_details': {'✅0️⃣': 0, '✅1️⃣': 0, '✅2️⃣': 0}, 'loss_details': {'❌': 0}}

    await event.respond("🚨 **RESET EFFECTUÉ**")

@client.on(events.NewMessage(pattern='/help'))
async def cmd_help(event):
    if event.is_group or event.is_channel:
        return

    await event.respond("""📖 **AIDE**

🎯 **Utilisation:**
1️⃣ /start - Inscription
2️⃣ 60min essai gratuit
3️⃣ Attendez les prédictions

💰 **Tarifs:**
• 500 FCFA = 24H
• 1500 FCFA = 1 semaine
• 2500 FCFA = 2 semaines

📊 **Commandes Admin:**
/status - État système
/predict - Prédiction manuelle
/users - Liste utilisateurs
/bilan - Statistiques
/reset - Reset total""")

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

    await event.respond(f"""💳 **PAIEMENT**

🎰 {user.get('prenom', 'CHAMPION')}, choisissez:

⚡ 24H - 500 FCFA
🔥 1 SEMAINE - 1500 FCFA
💎 2 SEMAINES - 2500 FCFA

📸 Envoyez la capture d'écran ici après paiement""", buttons=buttons)

    update_user(user_id, {'pending_payment': True})

# === SERVEUR WEB ===

async def index(request):
    html = f"""<!DOCTYPE html>
<html>
<head><title>Bot Baccarat</title>
<style>
body {{ font-family: Arial; background: linear-gradient(135deg, #1e3c72, #2a5298); color: white; text-align: center; padding: 50px; }}
.status {{ background: rgba(255,255,255,0.1); padding: 30px; border-radius: 15px; display: inline-block; margin: 20px; }}
.number {{ font-size: 2.5em; font-weight: bold; color: #ffd700; }}
</style>
</head>
<body>
<h1>🎰 Bot Baccarat ELITE</h1>
<div class="status"><div>Jeu Actuel</div><div class="number">#{last_seen_game_number or last_source_game_number}</div></div>
<div class="status"><div>Utilisateurs</div><div class="number">{len(users_data)}</div></div>
<div class="status"><div>Règle 2</div><div class="number">{'🔥' if rule2_active else '⏸️'}</div></div>
<p>Port {PORT} | Opérationnel</p>
</body>
</html>"""
    return web.Response(text=html, content_type='text/html')

async def health_check(request):
    return web.Response(text="OK")

async def start_web_server():
    app = web.Application()
    app.router.add_get('/', index)
    app.router.add_get('/health', health_check)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', PORT)
    await site.start()
    logger.info(f"✅ Serveur web port {PORT}")

async def schedule_daily_reset():
    wat_tz = timezone(timedelta(hours=1))
    while True:
        now = datetime.now(wat_tz)
        target = datetime.combine(now.date(), time(0, 59, tzinfo=wat_tz))
        if now >= target:
            target += timedelta(days=1)
        await asyncio.sleep((target - now).total_seconds())

        global pending_predictions, queued_predictions, processed_messages
        global current_game_number, last_source_game_number, last_seen_game_number
        global last_known_source_game, prediction_target_game, waiting_for_one_part, cycle_triggered
        global current_time_cycle_index, next_prediction_allowed_at
        global rule1_consecutive_count, rule2_active, stats_bilan

        pending_predictions.clear()
        queued_predictions.clear()
        processed_messages.clear()
        current_game_number = 0
        last_source_game_number = 0
        last_seen_game_number = 0
        last_known_source_game = 0
        prediction_target_game = None
        waiting_for_one_part = False
        cycle_triggered = False
        current_time_cycle_index = 0
        next_prediction_allowed_at = datetime.now()
        rule1_consecutive_count = 0
        rule2_active = False
        stats_bilan = {'total': 0, 'wins': 0, 'losses': 0, 'win_details': {'✅0️⃣': 0, '✅1️⃣': 0, '✅2️⃣': 0}, 'loss_details': {'❌': 0}}

        logger.warning("🚨 Reset quotidien effectué")

async def start_bot():
    try:
        await client.start(bot_token=BOT_TOKEN)
        logger.info("✅ Bot Telegram connecté")
        return True
    except Exception as e:
        logger.error(f"❌ Erreur connexion: {e}")
        return False

async def main():
    load_users_data()
    try:
        await start_web_server()
        if not await start_bot():
            return

        asyncio.create_task(schedule_daily_reset())
        asyncio.create_task(cycle_time_checker())

        logger.info("🚀 BOT OPÉRATIONNEL")
        await client.run_until_disconnected()
    except Exception as e:
        logger.error(f"Erreur: {e}")
    finally:
        if client.is_connected():
            await client.disconnect()

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("👋 Arrêt")
    except Exception as e:
        logger.error(f"Erreur fatale: {e}")
