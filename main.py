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

# ============ CONSTANTES ============
PAYMENT_LINK_500 = "https://my.moneyfusion.net/6977f7502181d4ebf722398d"   # 24h
PAYMENT_LINK_1500 = "https://my.moneyfusion.net/6977f7502181d4ebf722398d"  # 1 semaine
PAYMENT_LINK_2800 = "https://my.moneyfusion.net/6977f7502181d4ebf722398d"  # 2 semaines
USERS_FILE = "users_data.json"

ADMIN_NAME = "Sossou Kouamé"
ADMIN_TITLE = "Administrateur et développeur de ce Bot"

# ============ CONFIGURATION ============
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

# ============ VARIABLES GLOBALES ============
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

stats_bilan = {
    'total': 0,
    'wins': 0,
    'losses': 0,
    'win_details': {'✅0️⃣': 0, '✅1️⃣': 0, '✅2️⃣': 0},
    'loss_details': {'❌': 0}
}

users_data = {}
user_conversation_state = {}
admin_message_state = {}
admin_predict_state = {}
pending_screenshots = {}

# ============ FONCTIONS UTILISATEURS ============
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
    """VÉRIFICATION CRITIQUE: Abonné OU Essai actif"""
    user = get_user(user_id)
    if not user.get('registered'):
        logger.debug(f"User {user_id} non enregistré")
        return False
    
    subscribed = is_user_subscribed(user_id)
    trial = is_trial_active(user_id)
    
    logger.info(f"User {user_id}: subscribed={subscribed}, trial={trial}")
    
    return subscribed or trial

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

# ============ FONCTIONS CALCUL COSTUME ET SIGNATURE ============
def get_suit_for_number(n: int) -> str:
    """Calcule le costume pour un numéro donné basé sur SUIT_CYCLE"""
    if n < 6:
        return '♥'
    
    count_valid = 0
    for i in range(6, n + 1, 2):
        if i % 10 != 0:
            count_valid += 1
    
    if count_valid > 0:
        return SUIT_CYCLE[(count_valid - 1) % 8]
    return '♥'

def calculate_signature(target_game: int, current_index: int) -> tuple:
    """
    Calcule la signature: prochain numéro à prédire, son costume, et le temps d'attente
    Retourne: (next_target, next_suit, wait_min, next_index)
    """
    wait_min = TIME_CYCLE[current_index]
    next_index = (current_index + 1) % len(TIME_CYCLE)
    
    # Calcule le prochain numéro valide
    candidate = target_game + wait_min
    
    # Si impair, +1 pour avoir pair
    if candidate % 2 != 0:
        candidate += 1
    
    # Si finit par 0, +2 pour avoir pair valide (2,4,6,8)
    if candidate % 10 == 0:
        candidate += 2
    
    # Vérifie encore une fois
    if candidate % 2 != 0:
        candidate += 1
    
    next_suit = get_suit_for_number(candidate)
    
    return candidate, next_suit, wait_min, next_index

# ============ FONCTIONS ENVOI PRÉDICTIONS ============
async def send_prediction_to_all_users(prediction_msg: str, target_game: int, rule_type: str = "R2"):
    """
    ENVOI CRITIQUE: Prédiction à tous les utilisateurs éligibles (abonnés ou essai)
    """
    private_messages = {}
    sent_count = 0
    failed_count = 0
    skipped_count = 0
    
    logger.info(f"📤 DÉBUT ENVOI prédiction #{target_game} ({rule_type})")
    
    # Envoyer à l'admin aussi
    try:
        if ADMIN_ID and ADMIN_ID != 0:
            admin_msg = await client.send_message(ADMIN_ID, prediction_msg)
            private_messages[str(ADMIN_ID)] = admin_msg.id
            logger.info(f"✅ Admin {ADMIN_ID}: envoyé")
        else:
            logger.warning("Admin ID non configuré")
    except Exception as e:
        logger.error(f"❌ Erreur envoi admin: {e}")
        failed_count += 1
    
    # Envoyer à tous les utilisateurs
    logger.info(f"👥 Total utilisateurs: {len(users_data)}")
    
    for user_id_str, user_info in users_data.items():
        try:
            user_id = int(user_id_str)
            
            # Skip admin (déjà envoyé)
            if user_id == ADMIN_ID:
                continue
            
            # Skip bot token
            if user_id_str == BOT_TOKEN.split(':')[0]:
                continue
            
            # VÉRIFICATION ÉLIGIBILITÉ
            if not can_receive_predictions(user_id):
                logger.debug(f"⏭️ User {user_id}: non éligible (pas abonné ni essai)")
                skipped_count += 1
                continue
            
            # ENVOI DU MESSAGE
            sent_msg = await client.send_message(user_id, prediction_msg)
            private_messages[user_id_str] = sent_msg.id
            sent_count += 1
            logger.info(f"✅ User {user_id}: envoyé (msg_id: {sent_msg.id})")
            
        except Exception as e:
            failed_count += 1
            logger.error(f"❌ Erreur envoi user {user_id_str}: {e}")
    
    logger.info(f"📊 RÉSULTAT ENVOI #{target_game}: {sent_count} envoyés, {skipped_count} ignorés, {failed_count} échecs")
    return private_messages

async def edit_prediction_for_all_users(game_number: int, new_status: str, suit: str, rule_type: str, original_game: int = None):
    """Édite les messages de prédiction pour TOUS les utilisateurs."""
    display_game = original_game if original_game else game_number
    
    if rule_type == "R2":
        status_texts = {
            "❌": "❌ PERDU",
            "✅0️⃣": "✅ VICTOIRE IMMÉDIATE!",
            "✅1️⃣": "✅ VICTOIRE AU 2ÈME JEU!",
            "✅2️⃣": "✅ VICTOIRE AU 3ÈME JEU!",
            "✅3️⃣": "✅ VICTOIRE AU 4ÈME JEU!"
        }
        status_text = status_texts.get(new_status, new_status)
            
        updated_msg = f"""🎰 **PRÉDICTION #{display_game}**

🎯 Couleur: {SUIT_DISPLAY.get(suit, suit)}
📊 Statut: {status_text}
🤖 Algorithme: Règle 2 (Stats)"""
    else:
        status_texts = {
            "❌": "❌ NON TROUVÉ",
            "✅0️⃣": "✅ TROUVÉ!",
            "✅1️⃣": "✅ TROUVÉ AU 2ÈME!",
            "✅2️⃣": "✅ TROUVÉ AU 3ÈME!",
            "✅3️⃣": "✅ TROUVÉ AU 4ÈME!"
        }
        status_text = status_texts.get(new_status, new_status)
            
        updated_msg = f"""🎰 **PRÉDICTION #{display_game}**

🎯 Couleur: {SUIT_DISPLAY.get(suit, suit)}
📊 Statut: {status_text}
🤖 Algorithme: Règle 1 (Cycle)"""

    if game_number not in pending_predictions:
        logger.warning(f"Jeu #{game_number} non trouvé pour édition")
        return 0
    
    pred = pending_predictions[game_number]
    private_msgs = pred.get('private_messages', {})
    
    if not private_msgs:
        logger.warning(f"Aucun message privé pour #{game_number}")
        return 0
    
    edited_count = 0
    failed_count = 0
    
    for user_id_str, msg_id in list(private_msgs.items()):
        try:
            user_id = int(user_id_str)
            await client.edit_message(user_id, msg_id, updated_msg)
            edited_count += 1
            logger.info(f"✅ Édité pour {user_id}: {new_status}")
        except Exception as e:
            failed_count += 1
            logger.error(f"❌ Erreur édition {user_id_str}: {e}")
            if "message to edit not found" in str(e).lower():
                del private_msgs[user_id_str]
    
    logger.info(f"📊 Édition #{game_number}: {edited_count} succès, {failed_count} échecs")
    return edited_count

# ============ FONCTIONS ANALYSE ============
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
    """Remplace les différentes variantes de symboles."""
    normalized = group_str.replace('❤️', '♥').replace('❤', '♥').replace('♥️', '♥')
    normalized = normalized.replace('♠️', '♠').replace('♦️', '♦').replace('♣️', '♣')
    return normalized

def has_suit_in_group(group_str: str, target_suit: str) -> bool:
    """Vérifie si le costume cible est présent dans le groupe."""
    normalized = normalize_suits(group_str)
    target_normalized = normalize_suits(target_suit)
    for suit in ALL_SUITS:
        if suit in target_normalized and suit in normalized:
            return True
    return False

def get_predicted_suit(missing_suit: str) -> str:
    """Applique le mapping personnalisé."""
    return SUIT_MAPPING.get(missing_suit, missing_suit)

# ============ FONCTION "1 PART" (RÈGLE 1) ============
def is_one_part_away(current: int, target: int) -> bool:
    """Vérifie si current est à 1 part de target (current impair et différence de 1)"""
    return current % 2 != 0 and target - current == 1

# ============ LOGIQUE PRÉDICTION ET FILE D'ATTENTE ============
async def send_prediction_to_users(target_game: int, predicted_suit: str, base_game: int, 
                                     rattrapage=0, original_game=None, rule_type="R2"):
    """Envoie la prédiction avec SIGNATURE."""
    global rule2_active, rule1_consecutive_count, current_time_cycle_index
    
    try:
        # Mode rattrapage
        if rattrapage > 0:
            original_private_msgs = {}
            if original_game and original_game in pending_predictions:
                original_private_msgs = pending_predictions[original_game].get('private_messages', {}).copy()
                logger.info(f"Rattrapage {rattrapage}: récupération {len(original_private_msgs)} msgs de #{original_game}")
            
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

        # Vérifier blocage R2 pour R1
        if rule_type == "R1":
            active_r2_predictions = [p for game, p in pending_predictions.items() 
                                    if p.get('rule_type') == 'R2' and p.get('rattrapage', 0) == 0 
                                    and game > current_game_number]
            if active_r2_predictions:
                logger.info(f"Règle 2 active, R1 bloquée pour #{target_game}")
                return False
        
        # ========== CALCUL DE LA SIGNATURE ==========
        next_target, next_suit, wait_min, next_index = calculate_signature(target_game, current_time_cycle_index)
        
        # Format du message avec SIGNATURE
        algo_name = "R2" if rule_type == "R2" else "R1"
        
        prediction_msg = f"""🎰 **#{target_game}** → {SUIT_DISPLAY.get(predicted_suit, predicted_suit)}

🔮 Suivante: #{next_target} ({SUIT_DISPLAY.get(next_suit, next_suit)}) dans {wait_min}min | {algo_name}"""

        logger.info(f"📨 Message préparé pour #{target_game}:\n{prediction_msg}")

        # ENVOI À TOUS LES UTILISATEURS ÉLIGIBLES
        private_messages = await send_prediction_to_all_users(prediction_msg, target_game, rule_type)
        
        if not private_messages:
            logger.error(f"❌ ÉCHEC ENVOI #{target_game}: aucun destinataire")
            return False

        logger.info(f"✅ SUCCÈS ENVOI #{target_game}: {len(private_messages)} destinataires")

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
            logger.info(f"🔥 R2: #{target_game} envoyé | Prochaine: #{next_target} dans {wait_min}min")
        else:
            rule1_consecutive_count += 1
            current_time_cycle_index = next_index
            logger.info(f"⏱️ R1: #{target_game} envoyé | Prochaine: #{next_target} dans {wait_min}min")

        return True

    except Exception as e:
        logger.error(f"❌ Erreur envoi prédiction #{target_game}: {e}")
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
        logger.warning(f"Prédiction #{target_game} déjà en file d'attente")
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
    logger.info(f"📋 File d'attente: #{target_game} ({rule_type}, R{rattrapage})")
    return True

async def check_and_send_queued_predictions(current_game: int):
    """Envoie les prédictions en file d'attente."""
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
    """Met à jour le statut et édite tous les messages."""
    global rule2_active, rule1_consecutive_count, current_time_cycle_index
    
    try:
        if game_number not in pending_predictions:
            logger.warning(f"Mise à jour impossible: #{game_number} non trouvé")
            return False

        pred = pending_predictions[game_number]
        suit = pred['suit']
        rule_type = pred.get('rule_type', 'R2')
        rattrapage = pred.get('rattrapage', 0)
        original_game = pred.get('original_game', game_number)

        logger.info(f"Mise à jour #{game_number} [{rule_type}] → {new_status}")

        # Édition des messages
        await edit_prediction_for_all_users(game_number, new_status, suit, rule_type, original_game)

        pred['status'] = new_status
        
        # Gestion fin de prédiction
        if new_status in ['✅0️⃣', '✅1️⃣', '✅2️⃣', '✅3️⃣']:
            stats_bilan['total'] += 1
            stats_bilan['wins'] += 1
            stats_bilan['win_details'][new_status] = stats_bilan['win_details'].get(new_status, 0) + 1
            
            if rule_type == "R2" and rattrapage == 0:
                rule2_active = False
                # RESET CYCLE R1 AU DÉBUT !
                current_time_cycle_index = 0
                logger.info("R2 terminée (victoire), cycle R1 reset à 0")
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
                # RESET CYCLE R1 AU DÉBUT !
                current_time_cycle_index = 0
                logger.info("R2 terminée (défaite), cycle R1 reset à 0")
            elif rule_type == "R1":
                rule1_consecutive_count = 0
                
            if game_number in pending_predictions:
                del pending_predictions[game_number]
            asyncio.create_task(check_and_send_queued_predictions(current_game_number))

        return True
        
    except Exception as e:
        logger.error(f"Erreur update statut: {e}")
        import traceback
        logger.error(traceback.format_exc())
        return False

async def check_prediction_result(game_number: int, first_group: str):
    """Vérifie les résultats."""
    logger.info(f"Vérification #{game_number}: {first_group[:30]}...")
    
    # Vérification prédiction principale
    if game_number in pending_predictions:
        pred = pending_predictions[game_number]
        if pred.get('rattrapage', 0) == 0:
            target_suit = pred['suit']
            rule_type = pred.get('rule_type', 'R2')
            if has_suit_in_group(first_group, target_suit):
                logger.info(f"✅0️⃣ #{game_number} trouvé!")
                await update_prediction_status(game_number, '✅0️⃣')
                return
            else:
                next_target = game_number + 1
                queue_prediction(next_target, target_suit, pred['base_game'], 
                               rattrapage=1, original_game=game_number, rule_type=rule_type)
                logger.info(f"Échec #{game_number}, rattrapage #{next_target}")

    # Vérification rattrapages
    for target_game, pred in list(pending_predictions.items()):
        if target_game == game_number and pred.get('rattrapage', 0) > 0:
            original_game = pred.get('original_game', target_game - pred['rattrapage'])
            target_suit = pred['suit']
            rattrapage_actuel = pred['rattrapage']
            rule_type = pred.get('rule_type', 'R2')
            
            if has_suit_in_group(first_group, target_suit):
                status_map = {1: '✅1️⃣', 2: '✅2️⃣', 3: '✅3️⃣'}
                status_code = status_map.get(rattrapage_actuel, f'✅{rattrapage_actuel}️⃣')
                logger.info(f"{status_code} #{original_game} au rattrapage {rattrapage_actuel}!")
                await update_prediction_status(original_game, status_code)
                if target_game != original_game and target_game in pending_predictions:
                    del pending_predictions[target_game]
                return
            else:
                if rattrapage_actuel < 3:
                    next_rattrapage = rattrapage_actuel + 1
                    next_target = game_number + 1
                    queue_prediction(next_target, target_suit, pred['base_game'], 
                                   rattrapage=next_rattrapage, original_game=original_game,
                                   rule_type=rule_type)
                    logger.info(f"Échec rattrapage {rattrapage_actuel}, planifié {next_rattrapage}")
                    if target_game in pending_predictions:
                        del pending_predictions[target_game]
                else:
                    logger.info(f"❌ #{original_game} définitif après 3 rattrapages")
                    await update_prediction_status(original_game, '❌')
                    if target_game != original_game and target_game in pending_predictions:
                        del pending_predictions[target_game]
                return

# ============ RÈGLE 2 ============
async def process_stats_message(message_text: str):
    """Traite les statistiques du canal 2."""
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
                    logger.info(f"Limite 3 atteinte pour {predicted_suit}")
                    continue

                logger.info(f"R2 DÉCLENCHÉE: décalage {diff} entre {s1}({v1}) et {s2}({v2})")
                
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

# ============ RÈGLE 1 ============
async def try_launch_prediction_rule1():
    """Tente de lancer R1 si condition '1 part' remplie."""
    global waiting_for_one_part, prediction_target_game, cycle_triggered
    global current_time_cycle_index, next_prediction_allowed_at, rule1_consecutive_count
    global rule2_active
    
    if rule2_active:
        logger.info("R2 active, R1 en attente")
        return False
        
    if rule1_consecutive_count >= MAX_RULE1_CONSECUTIVE:
        logger.info(f"Limite R1 atteinte ({rule1_consecutive_count})")
        return False
    
    if not cycle_triggered or prediction_target_game is None:
        return False
    
    if is_one_part_away(last_known_source_game, prediction_target_game):
        logger.info(f"R1: '1 part' OK {last_known_source_game} → {prediction_target_game}")
        
        predicted_suit = get_suit_for_number(prediction_target_game)
        
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
            
            next_prediction_allowed_at = datetime.now() + timedelta(minutes=TIME_CYCLE[current_time_cycle_index])
            logger.info(f"R1: prochaine autorisée dans {TIME_CYCLE[current_time_cycle_index]} min")
            return True
    else:
        logger.info(f"R1: attente '1 part' {last_known_source_game} → {prediction_target_game}")
    
    return False

async def process_prediction_logic_rule1(message_text: str, chat_id: int):
    """Gère le déclenchement de R1."""
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
    logger.info(f"R1: dernier numéro #{game_number}")
    
    if waiting_for_one_part and cycle_triggered:
        await try_launch_prediction_rule1()
        return
    
    now = datetime.now()
    if now < next_prediction_allowed_at:
        return
        
    if rule2_active:
        logger.info("Temps cycle arrivé mais R2 active")
        return
        
    if rule1_consecutive_count >= MAX_RULE1_CONSECUTIVE:
        logger.info(f"Temps cycle arrivé mais limite R1 atteinte")
        wait_min = TIME_CYCLE[current_time_cycle_index]
        next_prediction_allowed_at = now + timedelta(minutes=wait_min)
        current_time_cycle_index = (current_time_cycle_index + 1) % len(TIME_CYCLE)
        return
    
    logger.info(f"R1: temps cycle arrivé {now.strftime('%H:%M:%S')}")
    cycle_triggered = True
    
    # Calcule cible: prochain pair valide après +2
    candidate = game_number + 2
    while candidate % 2 != 0 or candidate % 10 == 0:
        candidate += 1
    
    prediction_target_game = candidate
    logger.info(f"R1: cible calculée #{prediction_target_game}")
    
    success = await try_launch_prediction_rule1()
    
    if not success:
        waiting_for_one_part = True
        logger.info(f"R1: attente '1 part' pour #{prediction_target_game}")

# ============ GESTION MESSAGES ============
def is_message_finalized(message: str) -> bool:
    """Vérifie si le message est finalisé."""
    if '⏰' in message:
        return False
    return '✅' in message or '🔰' in message or '▶️' in message or 'Finalisé' in message

async def process_finalized_message(message_text: str, chat_id: int):
    """Traite les messages finalisés."""
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
    """Gère les nouveaux messages."""
    try:
        sender = await event.get_sender()
        sender_id = getattr(sender, 'id', event.sender_id)
        
        chat = await event.get_chat()
        chat_id = chat.id
        if hasattr(chat, 'broadcast') and chat.broadcast:
            if not str(chat_id).startswith('-100'):
                chat_id = int(f"-100{abs(chat_id)}")
            
        logger.info(f"Message chat_id={chat_id}: {event.message.message[:50]}...")

        if chat_id == SOURCE_CHANNEL_ID:
            message_text = event.message.message
            
            await process_prediction_logic_rule1(message_text, chat_id)
            
            if is_message_finalized(message_text):
                await process_finalized_message(message_text, chat_id)
            
            if message_text.startswith('/info'):
                active_preds = len(pending_predictions)
                rule1_status = f"{rule1_consecutive_count}/{MAX_RULE1_CONSECUTIVE}"
                rule2_status = "ACTIVE" if rule2_active else "Inactif"
                
                info_msg = (
                    f"ℹ️ ÉTAT SYSTÈME\n\n"
                    f"🎮 Jeu: #{current_game_number}\n"
                    f"🔮 Actives: {active_preds}\n"
                    f"⏳ R2: {rule2_status}\n"
                    f"⏱️ R1: {rule1_status}\n"
                    f"🎯 Cible R1: #{prediction_target_game if prediction_target_game else 'Aucune'}\n"
                    f"📍 Source: #{last_known_source_game}\n"
                    f"👥 Users: {len(users_data)}"
                )
                await event.respond(info_msg)
                return
        
        elif chat_id == SOURCE_CHANNEL_2_ID:
            message_text = event.message.message
            await process_stats_message(message_text)
            await check_and_send_queued_predictions(current_game_number)

    except Exception as e:
        logger.error(f"Erreur handle_message: {e}")
        import traceback
        logger.error(traceback.format_exc())

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

client.add_event_handler(handle_message, events.NewMessage())
client.add_event_handler(handle_edited_message, events.MessageEdited())

# ============ TIMEOUT PAIEMENT ============
async def check_payment_timeout(user_id: int):
    """Vérifie après 10min si l'admin n'a pas répondu."""
    await asyncio.sleep(600)  # 10 minutes
    
    if user_id in pending_screenshots and not pending_screenshots[user_id].get('validated', False):
        user = get_user(user_id)
        
        try:
            await client.send_message(
                user_id,
                f"""⏰ **PATIENTEZ S'IL VOUS PLAÎT...**

Cher {user.get('prenom', 'Client')},

{ADMIN_NAME} {ADMIN_TITLE.split()[-1]} est un peu occupé en ce moment.

✅ Il confirmera votre abonnement très prochainement.

🙏 Merci pour votre paiement et votre patience!"""
            )
            pending_screenshots[user_id]['notified'] = True
            logger.info(f"Timeout 10min: message envoyé à {user_id}")
        except Exception as e:
            logger.error(f"Erreur timeout message à {user_id}: {e}")

# ============ COMMANDES UTILISATEUR ============
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
                [Button.url("💳 24H - 500 FCFA", PAYMENT_LINK_500)],
                [Button.url("💳 1 SEMAINE - 1500 FCFA", PAYMENT_LINK_1500)],
                [Button.url("💳 2 SEMAINES - 2800 FCFA", PAYMENT_LINK_2800)]
            ]
            
            expired_msg = f"""⚠️ **VOTRE ESSAI EST TERMINÉ...** ⚠️

🎰 {user.get('prenom', 'CHAMPION')}, vous avez goûté à la puissance de nos prédictions...

💔 **Ne laissez pas la chance s'échapper!**

🔥 **OFFRE EXCLUSIVE:**
💎 **500 FCFA** = 24H de test prolongé
💎 **1500 FCFA** = 1 semaine complète  
💎 **2800 FCFA** = 2 semaines VIP

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

@client.on(events.NewMessage())
async def handle_registration_and_payment(event):
    if event.is_group or event.is_channel: 
        return
    
    if event.message.message and event.message.message.startswith('/'): 
        return
    
    user_id = event.sender_id
    user = get_user(user_id)
    
    # Admin message
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
                await event.respond(f"✅ Message envoyé à {target_user_id}!")
                logger.info(f"Message admin envoyé à {target_user_id}")
            except Exception as e:
                await event.respond(f"❌ Erreur: {e}")
                logger.error(f"Erreur envoi message admin: {e}")
            
            del admin_message_state[user_id]
            return
    
    # Admin predict
    if user_id in admin_predict_state:
        state = admin_predict_state[user_id]
        if state.get('step') == 'nums':
            nums = [int(n) for n in re.findall(r'\d+', event.message.message) 
                   if int(n) >= 6 and int(n) % 2 == 0 and int(n) % 10 != 0]
            if not nums:
                await event.respond("❌ Aucun numéro valide.")
                return
            
            sent = 0
            details = []
            for n in nums:
                suit = get_suit_for_number(n)
                if await send_prediction_to_users(n, suit, last_known_source_game, rule_type="R1"):
                    sent += 1
                    details.append(f"#{n} {SUIT_DISPLAY.get(suit, suit)}")
            
            await event.respond(f"✅ **{sent} envoyées**\n\n" + "\n".join(details[:20]))
            del admin_predict_state[user_id]
            return
    
    # Inscription
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
            logger.info(f"✅ Nouvel utilisateur inscrit: {user_id}")
            return
    
    # ========== GESTION PAIEMENT ==========
    if user.get('awaiting_screenshot') and event.message.photo:
        try:
            # Forward la capture à l'admin
            forwarded = await client.forward_messages(ADMIN_ID, event.message)
            
            # Boutons validation avec BONS PRIX
            buttons = [
                [Button.inline("✅ 24H - 500F", data=f"val_{user_id}_1d")],
                [Button.inline("✅ 1 Sem - 1500F", data=f"val_{user_id}_1w")],
                [Button.inline("✅ 2 Sem - 2800F", data=f"val_{user_id}_2w")],
                [Button.inline("❌ Rejeter", data=f"rej_{user_id}")]
            ]
            
            # Envoie infos à l'admin
            await client.send_message(
                ADMIN_ID,
                f"🔔 **NOUVEAU PAIEMENT**\n\n"
                f"👤 {user.get('prenom', 'User')} {user.get('nom', '')}\n"
                f"🆔 `{user_id}`\n"
                f"📍 {user.get('pays', 'N/A')}\n\n"
                f"⏰ Reçu à: {datetime.now().strftime('%H:%M:%S')}\n"
                f"⏳ Timeout dans 10 min",
                buttons=buttons,
                reply_to=forwarded.id
            )
            
            # Stocke pour timeout
            pending_screenshots[user_id] = {
                'sent_at': datetime.now(),
                'notified': False,
                'validated': False
            }
            
            # Lance timeout 10min
            asyncio.create_task(check_payment_timeout(user_id))
            
            update_user(user_id, {'awaiting_screenshot': False})
            
            await event.respond("""📸 **REÇU ENVOYÉ!**

✅ Votre paiement est en cours de validation.
⏳ Délai maximum: 10 minutes.

🔔 Vous recevrez une confirmation dès que possible.""")
            
        except Exception as e:
            logger.error(f"Erreur forward paiement: {e}")
            await event.respond("❌ Erreur, veuillez réessayer.")
        return
    
    # Validation montant avec BONS PRIX
    if user.get('awaiting_amount'):
        message_text = event.message.message.strip()
        
        # VÉRIFICATION DES MONTANTS CORRECTS: 500, 1500, 2800
        if message_text in ['500', '1500', '2800']:
            amount = message_text
            update_user(user_id, {'awaiting_amount': False})
            
            user_info = get_user(user_id)
            
            # CORRESPONDANCE MONTANT → DURÉE
            if amount == '500':
                dur_text = "24 heures"
                dur_code = "1d"
                days = 1
            elif amount == '1500':
                dur_text = "1 semaine"
                dur_code = "1w"
                days = 7
            else:  # 2800
                dur_text = "2 semaines"
                dur_code = "2w"
                days = 14

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
                await client.send_message(ADMIN_ID, msg_admin, buttons=buttons)
            except Exception as e:
                logger.error(f"Erreur notification admin: {e}")

            await event.respond("""✅ **DEMANDE ENVOYÉE!**

⏳ Notre équipe vérifie votre paiement...
🚀 Votre accès sera activé sous 5 minutes maximum!

📱 Vous recevrez une confirmation ici même.

💎 **Préparez-vous à gagner!**""")
        else:
            await event.respond("❌ Montant invalide. Répondez avec `500`, `1500` ou `2800`.")
        return

# ============ COMMANDES ADMIN ============
@client.on(events.NewMessage(pattern='/users'))
async def cmd_users(event):
    if event.is_group or event.is_channel or event.sender_id != ADMIN_ID:
        return
    
    if not users_data:
        await event.respond("📊 Aucun utilisateur.")
        return
    
    users_list = []
    for uid_str, info in users_data.items():
        uid = int(uid_str)
        status = get_user_status(uid)
        users_list.append(f"🆔 `{uid}` | {info.get('prenom', 'N/A')} {info.get('nom', 'N/A')} | {status}")
    
    chunk_size = 50
    for i in range(0, len(users_list), chunk_size):
        chunk = users_list[i:i+chunk_size]
        await event.respond(f"""📋 **UTILISATEURS** ({i+1}-{min(i+len(chunk), len(users_list))}/{len(users_list)})

{'\n'.join(chunk)}

💡 `/msg ID` pour envoyer un message""")
        await asyncio.sleep(0.5)

@client.on(events.NewMessage(pattern=r'^/msg (\d+)$'))
async def cmd_msg(event):
    if event.is_group or event.is_channel or event.sender_id != ADMIN_ID:
        return
    
    try:
        target_uid = int(event.pattern_match.group(1))
        if str(target_uid) not in users_data:
            await event.respond(f"❌ Utilisateur {target_uid} non trouvé.")
            return
        
        info = users_data[str(target_uid)]
        admin_message_state[event.sender_id] = {
            'target_user_id': target_uid,
            'step': 'awaiting_message'
        }
        
        await event.respond(f"""✉️ **Message à {info.get('prenom', 'User')}** (ID: `{target_uid}`)

📝 Écrivez votre message:""")
    except Exception as e:
        await event.respond(f"❌ Erreur: {e}")

@client.on(events.CallbackQuery(data=re.compile(b'val_(\d+)_(.*)')))
async def handle_validation(event):
    if event.sender_id != ADMIN_ID:
        await event.answer("Accès refusé", alert=True)
        return
    
    user_id = int(event.data_match.group(1).decode())
    duration = event.data_match.group(2).decode()
    
    # Marque comme validé pour annuler timeout
    if user_id in pending_screenshots:
        pending_screenshots[user_id]['validated'] = True
    
    days = {'1d': 1, '1w': 7, '2w': 14}.get(duration, 1)
    end = datetime.now() + timedelta(days=days)
    
    update_user(user_id, {
        'subscription_end': end.isoformat(),
        'subscription_type': 'premium'
    })
    
    try:
        await client.send_message(user_id, f"🎉 **ACTIVÉ!**\n\n✅ {days} jour(s) confirmé!\n🔥 Bonne chance!")
    except:
        pass
    
    await event.edit(f"✅ {user_id} validé ({days}j)")
    await event.answer("Activé!")

@client.on(events.CallbackQuery(data=re.compile(b'rej_(\d+)')))
async def handle_rejection(event):
    if event.sender_id != ADMIN_ID:
        await event.answer("Accès refusé", alert=True)
        return
    
    user_id = int(event.data_match.group(1).decode())
    
    # Marque comme traité
    if user_id in pending_screenshots:
        pending_screenshots[user_id]['validated'] = True
    
    try:
        await client.send_message(user_id, "❌ Demande rejetée.")
    except:
        pass
    
    await event.edit(f"❌ {user_id} rejeté")
    await event.answer("Rejeté")

@client.on(events.CallbackQuery(data=re.compile(b'valider_(\d+)_(.*)')))
async def handle_validation_old(event):
    """Compatibilité anciens boutons"""
    await handle_validation(event)

@client.on(events.CallbackQuery(data=re.compile(b'rejeter_(\d+)')))
async def handle_rejection_old(event):
    """Compatibilité anciens boutons"""
    await handle_rejection(event)

@client.on(events.NewMessage(pattern=r'^/a (\d+)$'))
async def cmd_set_a_shortcut(event):
    if event.is_group or event.is_channel or event.sender_id != ADMIN_ID:
        return
    
    global USER_A
    try:
        val = int(event.pattern_match.group(1))
        USER_A = val
        await event.respond(f"✅ Valeur 'a' = {USER_A}")
    except Exception as e:
        await event.respond(f"❌ Erreur: {e}")

@client.on(events.NewMessage(pattern='/status'))
async def cmd_status(event):
    if event.is_group or event.is_channel or event.sender_id != ADMIN_ID:
        return
    
    info = f"#{prediction_target_game}" if prediction_target_game else "Aucune"
    eligible = sum(1 for u in users_data if can_receive_predictions(int(u)))
    
    await event.respond(f"""📊 **STATUT**

🎮 Source: #{last_known_source_game}
⏳ R2: {'🔥' if rule2_active else 'Off'}
⏱️ R1: {rule1_consecutive_count}/{MAX_RULE1_CONSECUTIVE}
🎯 Cycle: {current_time_cycle_index} ({TIME_CYCLE[current_time_cycle_index]}min)
📅 Cible: {info}
👥 Users: {len(users_data)} | Éligibles: {eligible}
📋 Actives: {len(pending_predictions)}""")

@client.on(events.NewMessage(pattern='/reset'))
async def cmd_reset(event):
    if event.is_group or event.is_channel or event.sender_id != ADMIN_ID:
        return
    
    global users_data, pending_predictions, queued_predictions, processed_messages
    global current_game_number, last_source_game_number, stats_bilan
    global rule1_consecutive_count, rule2_active, suit_prediction_counts
    global last_known_source_game, current_time_cycle_index
    global prediction_target_game, waiting_for_one_part, cycle_triggered
    global pending_screenshots
    
    users_data = {}
    save_users_data()
    pending_predictions.clear()
    queued_predictions.clear()
    processed_messages.clear()
    suit_prediction_counts.clear()
    pending_screenshots.clear()
    
    current_game_number = 0
    last_source_game_number = 0
    last_known_source_game = 0
    current_time_cycle_index = 0
    prediction_target_game = None
    waiting_for_one_part = False
    cycle_triggered = False
    
    rule1_consecutive_count = 0
    rule2_active = False
    
    stats_bilan = {'total': 0, 'wins': 0, 'losses': 0, 'win_details': {'✅0️⃣': 0, '✅1️⃣': 0, '✅2️⃣': 0}, 'loss_details': {'❌': 0}}
    
    logger.warning("🚨 RESET TOTAL")
    await event.respond("🚨 **RESET OK**")

@client.on(events.NewMessage(pattern='/bilan'))
async def cmd_bilan(event):
    if event.is_group or event.is_channel or event.sender_id != ADMIN_ID:
        return
    
    if stats_bilan['total'] == 0:
        await event.respond("📊 Aucune prédiction.")
        return
    
    win = (stats_bilan['wins'] / stats_bilan['total']) * 100
    
    await event.respond(f"""📊 **BILAN**

🎯 Total: {stats_bilan['total']}
✅ Gains: {stats_bilan['wins']} ({win:.1f}%)
❌ Pertes: {stats_bilan['losses']}

Détails:
• Immédiat: {stats_bilan['win_details'].get('✅0️⃣', 0)}
• 2ème: {stats_bilan['win_details'].get('✅1️⃣', 0)}
• 3ème: {stats_bilan['win_details'].get('✅2️⃣', 0)}
• 4ème: {stats_bilan['win_details'].get('✅3️⃣', 0)}""")

@client.on(events.NewMessage(pattern='/help'))
async def cmd_help(event):
    if event.is_group or event.is_channel:
        return
    
    await event.respond(f"""📖 **AIDE**

🎯 **Utilisation:**
1. /start pour s'inscrire
2. Attendre les prédictions ici
3. Les résultats se mettent à jour auto!

🎲 **Numéros:** pairs >= 6 (fin 2,4,6,8)

💰 **Tarifs:** 500FCFA(24h) | 1500FCFA(1sem) | 2800FCFA(2sem)

📊 **Commandes admin:**
/status - État du bot
/predict - Prédiction manuelle
/bilan - Statistiques
/reset - Reset total
/users - Liste utilisateurs
/msg ID - Envoyer message
/force - Forcer/régulariser""")

@client.on(events.NewMessage(pattern='/payer'))
async def cmd_payer(event):
    if event.is_group or event.is_channel:
        return
    
    user_id = event.sender_id
    user = get_user(user_id)
    
    if not user.get('registered'):
        await event.respond("❌ /start d'abord")
        return
    
    # BONS PRIX: 500, 1500, 2800
    buttons = [
        [Button.url("⚡ 24H - 500 FCFA", PAYMENT_LINK_500)],
        [Button.url("🔥 1 SEMAINE - 1500 FCFA", PAYMENT_LINK_1500)],
        [Button.url("💎 2 SEMAINES - 2800 FCFA", PAYMENT_LINK_2800)]
    ]
    
    await event.respond(f"""💳 **PAIEMENT**

🎰 {user.get('prenom', 'CHAMPION')}, choisissez:

👇 **VOTRE FORMULE:**""", buttons=buttons)
    update_user(user_id, {'awaiting_screenshot': True})

@client.on(events.NewMessage(pattern='/predict'))
async def cmd_predict(event):
    if event.is_group or event.is_channel or event.sender_id != ADMIN_ID:
        return
    
    if last_known_source_game <= 0:
        await event.respond("⚠️ Non synchronisé.")
        return
    
    admin_predict_state[event.sender_id] = {'step': 'nums'}
    
    info = f"Cible: #{prediction_target_game}" if prediction_target_game else "En attente..."
    await event.respond(f"""🎯 **PRÉDICTION MANUELLE**

📍 Source: #{last_known_source_game}
📅 {info}

Entrez numéros (pairs >= 6, fin 2/4/6/8):""")

@client.on(events.NewMessage(pattern='/force'))
async def cmd_force(event):
    """Force/régularise les prédictions."""
    if event.is_group or event.is_channel or event.sender_id != ADMIN_ID:
        return
    
    global cycle_triggered, waiting_for_one_part, prediction_target_game
    global rule2_active, rule1_consecutive_count, current_time_cycle_index
    global next_prediction_allowed_at, last_known_source_game
    
    now = datetime.now()
    
    # Vérifie R2 active
    if rule2_active:
        active_r2 = [g for g, p in pending_predictions.items() 
                     if p.get('rule_type') == 'R2' and p.get('rattrapage', 0) == 0]
        if active_r2:
            await event.respond(f"""🔴 **R2 ACTIVE**

Prédictions en cours: {', '.join([f'#{g}' for g in active_r2[:5]])}

⏳ Attendez fin R2 ou /reset""")
            return
    
    # Vérifie limite R1
    if rule1_consecutive_count >= MAX_RULE1_CONSECUTIVE:
        await event.respond(f"""🟡 **R1 EN LIMITE**

{rule1_consecutive_count}/{MAX_RULE1_CONSECUTIVE}
Attendez déclenchement R2.""")
        return
    
    # Vérifie 1 part away en cours
    if waiting_for_one_part and prediction_target_game:
        trigger = prediction_target_game - 1
        
        if last_known_source_game >= trigger:
            await event.respond(f"""🚨 **BLOCAGE!**

Déclencheur #{trigger} PASSÉ!
Dernier: #{last_known_source_game}

🔧 Forçage...""")
            
            success = await try_launch_prediction_rule1()
            if success:
                await event.respond(f"✅ #{prediction_target_game} forcé!")
            else:
                await event.respond("❌ Échec forçage")
            return
        
        minutes_wait = trigger - last_known_source_game
        await event.respond(f"""⏳ **EN COURS**

Cible: #{prediction_target_game}
Déclencheur: #{trigger}
⏱️ Dans ~{minutes_wait} min""")
        return
    
    # Vérifie temps cycle
    if now < next_prediction_allowed_at:
        wait_sec = (next_prediction_allowed_at - now).total_seconds()
        wait_min = int(wait_sec / 60)
        
        wait_cycle = TIME_CYCLE[current_time_cycle_index]
        candidate = last_known_source_game + wait_cycle
        
        if candidate % 2 != 0:
            candidate += 1
        if candidate % 10 == 0:
            candidate += 2
        
        suit = get_suit_for_number(candidate)
        
        await event.respond(f"""⏳ **TEMPS CYCLE**

Dans {wait_min} min
Prévu: #{candidate} ({SUIT_DISPLAY.get(suit, suit)})""")
        return
    
    # Force déclenchement
    if not cycle_triggered:
        cycle_triggered = True
        
        candidate = last_known_source_game + 2
        while candidate % 2 != 0 or candidate % 10 == 0:
            candidate += 1
        
        prediction_target_game = candidate
        
        await event.respond(f"""🔧 **FORÇAGE**

Cible: #{candidate}
Attente #{candidate - 1}...""")
        
        if is_one_part_away(last_known_source_game, candidate):
            success = await try_launch_prediction_rule1()
            if success:
                await event.respond(f"✅ #{candidate} envoyé immédiatement!")
        
        return
    
    # Cycle déclenché, en attente
    if cycle_triggered and prediction_target_game:
        trigger = prediction_target_game - 1
        
        if last_known_source_game >= trigger:
            await event.respond(f"""🚨 **RÉCUPÉRATION**

Déclencheur passé, recalcul...""")
            
            cycle_triggered = False
            waiting_for_one_part = False
            
            candidate = last_known_source_game + 2
            while candidate % 2 != 0 or candidate % 10 == 0:
                candidate += 1
            
            prediction_target_game = candidate
            
            if is_one_part_away(last_known_source_game, candidate):
                await try_launch_prediction_rule1()
                await event.respond(f"✅ Nouveau #{candidate} envoyé!")
            else:
                waiting_for_one_part = True
                await event.respond(f"⏳ Nouvelle attente #{candidate}")
        else:
            minutes_wait = trigger - last_known_source_game
            await event.respond(f"""⏳ **ATTENTE**

Cible: #{prediction_target_game}
Dans ~{minutes_wait} min""")
        
        return
    
    # Initialise
    await event.respond("🔄 **INITIALISATION**")
    next_prediction_allowed_at = now
    
    await process_prediction_logic_rule1(f"#N {last_known_source_game}", SOURCE_CHANNEL_ID)
    await event.respond("✅ Cycle démarré!")

@client.on(events.NewMessage(pattern='/next'))
async def cmd_next(event):
    """Affiche le prochain numéro à prédire."""
    if event.is_group or event.is_channel:
        return
    
    if event.sender_id != ADMIN_ID:
        await event.respond("❌ Admin uniquement")
        return
    
    if last_known_source_game <= 0:
        await event.respond("⚠️ Aucun numéro source")
        return
    
    # Calcul prochain numéro
    wait_min = TIME_CYCLE[current_time_cycle_index]
    candidate = last_known_source_game + wait_min
    
    if candidate % 2 != 0:
        candidate += 1
    if candidate % 10 == 0:
        candidate += 2
    if candidate % 2 != 0:
        candidate += 1
    
    suit = get_suit_for_number(candidate)
    
    # Signature
    next_wait = TIME_CYCLE[(current_time_cycle_index + 1) % len(TIME_CYCLE)]
    sig_candidate = candidate + next_wait
    
    if sig_candidate % 2 != 0:
        sig_candidate += 1
    if sig_candidate % 10 == 0:
        sig_candidate += 2
    if sig_candidate % 2 != 0:
        sig_candidate += 1
    
    sig_suit = get_suit_for_number(sig_candidate)
    
    # État
    if rule2_active:
        r1_status = "🔴 BLOQUÉE (R2 active)"
    elif rule1_consecutive_count >= MAX_RULE1_CONSECUTIVE:
        r1_status = "🔴 BLOQUÉE (limite)"
    else:
        r1_status = "🟢 ACTIVE"
    
    waiting = f"⏳ Attente #{prediction_target_game - 1}" if prediction_target_game else "⏳ En attente temps cycle"
    
    await event.respond(f"""🔮 **PROCHAIN NUMÉRO**

📍 Source: #{last_known_source_game}
⏱️ Temps cycle: {wait_min} min (index {current_time_cycle_index})

🧮 Calcul: {last_known_source_game} + {wait_min} = {last_known_source_game + wait_min}
→ Ajusté: **#{candidate}** ({SUIT_DISPLAY.get(suit, suit)})

📋 Signature: "#{sig_candidate} ({SUIT_DISPLAY.get(sig_suit, sig_suit)}) dans {next_wait}min"

📡 État R1: {r1_status}
{waiting}

🎯 Cible: #{prediction_target_game if prediction_target_game else candidate}""")

# ============ SERVEUR WEB ============
async def index(request):
    html = f"""<!DOCTYPE html>
<html>
<head>
    <title>Bot Baccarat ELITE</title>
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
        <div class="label">Règle 2</div>
        <div class="number">{'ACTIVE 🔥' if rule2_active else 'Standby'}</div>
    </div>
    <p style="margin-top: 40px;">Système opérationnel | Algorithmes actifs</p>
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
    logger.info(f"Serveur web port {PORT}")

# ============ RESET QUOTIDIEN ============
async def schedule_daily_reset():
    wat_tz = timezone(timedelta(hours=1))
    
    while True:
        now = datetime.now(wat_tz)
        target = datetime.combine(now.date(), time(0, 59, tzinfo=wat_tz))
        
        if now >= target:
            target += timedelta(days=1)
            
        wait_seconds = (target - now).total_seconds()
        logger.info(f"Prochain reset dans {timedelta(seconds=wait_seconds)}")
        
        await asyncio.sleep(wait_seconds)
        
        logger.warning("🚨 RESET QUOTIDIEN!")
        
        global pending_predictions, queued_predictions, processed_messages
        global current_game_number, last_source_game_number
        global last_known_source_game, current_time_cycle_index
        global prediction_target_game, waiting_for_one_part, cycle_triggered
        global rule1_consecutive_count, rule2_active, pending_screenshots
        
        pending_predictions.clear()
        queued_predictions.clear()
        processed_messages.clear()
        pending_screenshots.clear()
        
        current_game_number = 0
        last_source_game_number = 0
        last_known_source_game = 0
        current_time_cycle_index = 0
        prediction_target_game = None
        waiting_for_one_part = False
        cycle_triggered = False
        
        rule1_consecutive_count = 0
        rule2_active = False
        
        logger.warning("✅ Reset effectué.")

# ============ DÉMARRAGE ============
async def start_bot():
    try:
        await client.start(bot_token=BOT_TOKEN)
        logger.info("✅ Bot connecté!")
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
            logger.error("Échec démarrage")
            return

        asyncio.create_task(schedule_daily_reset())
        
        logger.info("🚀 BOT OPÉRATIONNEL")
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
