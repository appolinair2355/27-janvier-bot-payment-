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
MIN_GAME = 1           # Vérification de 1 à 1440
MAX_GAME = 1440
MIN_PREDICT = 6        # Prédiction seulement 6-1436 pairs sans 0
MAX_PREDICT = 1436

PAYMENT_LINK_24H = "https://my.moneyfusion.net/6977f7502181d4ebf722398d"
PAYMENT_LINK_1W = "https://my.moneyfusion.net/6977f7502181d4ebf722398d"
PAYMENT_LINK_2W = "https://my.moneyfusion.net/6977f7502181d4ebf722398d"
USERS_FILE = "users_data.json"

ADMIN_NAME = "Sossou Kouamé"
ADMIN_TITLE = "Administrateur et développeur de ce Bot"

# ============ CONFIGURATION LOGGING ============
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger(__name__)

# ============ VÉRIFICATIONS CONFIG ============
if not API_ID or API_ID == 0:
    logger.error("API_ID manquant")
    exit(1)
if not API_HASH:
    logger.error("API_HASH manquant")
    exit(1)
if not BOT_TOKEN:
    logger.error("BOT_TOKEN manquant")
    exit(1)

# ============ INITIALISATION CLIENT ============
session_string = os.getenv('TELEGRAM_SESSION', '')
client = TelegramClient(StringSession(session_string), API_ID, API_HASH)

# ============ VARIABLES GLOBALES ============
pending_predictions = {}
queued_predictions = {}
processed_messages = set()
current_game_number = 0
last_source_game_number = 0
last_known_source_game = 0
suit_prediction_counts = {}
USER_A = 1

SUIT_CYCLE = ['♥', '♦', '♣', '♠', '♦', '♥', '♠', '♣']
TIME_CYCLE = [5, 8, 3, 7, 9, 4, 6, 8, 3, 5, 9, 7, 4, 6, 8, 3, 5, 9, 7, 4, 6, 8, 3, 5, 9, 7, 4, 6, 8, 5]
current_time_cycle_index = 0

next_rule1_prediction = None
rule1_consecutive_count = 0
MAX_RULE1_CONSECUTIVE = 3

rule2_active = False
rule2_predicted_games = set()

stats_bilan = {'total': 0, 'wins': 0, 'losses': 0, 'win_details': {'✅0️⃣': 0, '✅1️⃣': 0, '✅2️⃣': 0}, 'loss_details': {'❌': 0}}

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
            'subscription_type': None, 'pending_payment': False,
            'awaiting_screenshot': False, 'payment_amount': None
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

# ============ FONCTIONS VALIDATION NUMÉROS ============
def is_valid_for_prediction(n: int) -> bool:
    """Valide pour PRÉDICTION: 6-1436, pair, finit par 2/4/6/8"""
    return MIN_PREDICT <= n <= MAX_PREDICT and n % 2 == 0 and n % 10 != 0

def is_valid_for_check(n: int) -> bool:
    """Valide pour VÉRIFICATION: 1-1440, n'importe quel numéro"""
    return MIN_GAME <= n <= MAX_GAME

def get_next_for_prediction(n: int) -> int:
    """Prochain numéro valide pour prédiction après n"""
    candidate = n + 1
    while candidate <= MAX_PREDICT:
        if is_valid_for_prediction(candidate):
            return candidate
        candidate += 1
    return MAX_PREDICT

def get_next_for_check(n: int) -> int:
    """Prochain numéro pour vérification (tous les numéros)"""
    if n >= MAX_GAME:
        return MAX_GAME
    return n + 1

def count_valid_predictions_up_to(n: int) -> int:
    """Compte les numéros valides pour prédiction de MIN_PREDICT à n"""
    count = 0
    for i in range(MIN_PREDICT, min(n + 1, MAX_PREDICT + 1)):
        if is_valid_for_prediction(i):
            count += 1
    return count

def get_suit(n: int) -> str:
    """Retourne le costume basé sur le rang du numéro valide pour prédiction"""
    if not is_valid_for_prediction(n):
        return '♥'
    count = count_valid_predictions_up_to(n)
    return SUIT_CYCLE[(count - 1) % 8]

def calc_next_prediction(base: int, wait: int) -> tuple:
    """Calcule: base + wait = prochain numéro valide pour prédiction"""
    target = base + wait
    while not is_valid_for_prediction(target) and target <= MAX_PREDICT:
        target += 1
    if target > MAX_PREDICT:
        target = MIN_PREDICT
    return target, get_suit(target)

# ============ FONCTIONS EXTRACTION ============
def extract_game_number(message: str, for_prediction=False):
    """
    Extrait le numéro de jeu.
    for_prediction=True: valide strictement 6-1436 pairs
    for_prediction=False: accepte 1-1440 (vérification)
    """
    match = re.search(r"#N\s*(\d+)", message, re.IGNORECASE)
    if match:
        n = int(match.group(1))
        if for_prediction:
            if is_valid_for_prediction(n):
                return n
            else:
                logger.debug(f"Numéro {n} ignoré pour prédiction")
                return None
        else:
            if is_valid_for_check(n):
                return n
            return None
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

def is_message_finalized(message: str) -> bool:
    """Vérifie si le message est finalisé."""
    if '⏰' in message:
        return False
    return '✅' in message or '🔰' in message or '▶️' in message or 'Finalisé' in message

# ============ FONCTIONS ENVOI ============
async def send_to_all(msg: str, game: int):
    """Envoie un message à tous les utilisateurs éligibles + admin"""
    sent = {}
    
    try:
        if ADMIN_ID and ADMIN_ID != 0:
            m = await client.send_message(ADMIN_ID, msg)
            sent[str(ADMIN_ID)] = m.id
    except Exception as e:
        logger.error(f"Erreur envoi admin: {e}")
    
    for uid_str, user_info in users_data.items():
        try:
            uid = int(uid_str)
            if uid == ADMIN_ID:
                continue
            if not can_receive_predictions(uid):
                continue
            m = await client.send_message(uid, msg)
            sent[uid_str] = m.id
        except Exception as e:
            logger.error(f"Erreur envoi user {uid_str}: {e}")
    
    return sent

async def send_prediction_to_users(target: int, suit: str, base: int, 
                                     rattrapage=0, original_game=None, rule_type="R2"):
    """
    Envoie une prédiction. 
    La prédiction se fait UNIQUEMENT sur les numéros pairs valides (6-1436, fin 2/4/6/8).
    """
    global rule2_active, rule1_consecutive_count, rule2_predicted_games
    global next_rule1_prediction, current_time_cycle_index
    
    try:
        if not is_valid_for_prediction(target) and rattrapage == 0:
            logger.error(f"❌ Numéro {target} invalide pour prédiction")
            return False
        
        # Mode rattrapage
        if rattrapage > 0:
            orig_msgs = {}
            if original_game and original_game in pending_predictions:
                orig_msgs = pending_predictions[original_game].get('private_messages', {}).copy()
            
            pending_predictions[target] = {
                'message_id': 0,
                'suit': suit,
                'base_game': base,
                'status': '🔮',
                'check_count': 0,
                'rattrapage': rattrapage,
                'original_game': original_game,
                'rule_type': rule_type,
                'private_messages': orig_msgs,
                'created_at': datetime.now().isoformat()
            }
            
            if rule_type == "R2":
                rule2_active = True
                rule2_predicted_games.add(target)
                
            logger.info(f"Rattrapage {rattrapage} actif pour #{target} (Original #{original_game})")
            return True
        
        # Vérification blocage R2
        if rule_type == "R1" and target in rule2_predicted_games:
            logger.info(f"🚫 R1 bloquée: #{target} déjà pris par R2")
            return False
        
        # Calcul prochaine prédiction
        next_idx = (current_time_cycle_index + 1) % len(TIME_CYCLE)
        next_wait = TIME_CYCLE[next_idx]
        
        raw_next = target + next_wait
        next_target = raw_next
        while not is_valid_for_prediction(next_target) and next_target <= MAX_PREDICT:
            next_target += 1
        if next_target > MAX_PREDICT:
            next_target = MIN_PREDICT
        
        next_suit = get_suit(next_target)
        
        attempts = 0
        while next_target in rule2_predicted_games and attempts < 10:
            next_idx = (next_idx + 1) % len(TIME_CYCLE)
            next_wait = TIME_CYCLE[next_idx]
            raw_next = next_target + next_wait
            next_target = raw_next
            while not is_valid_for_prediction(next_target) and next_target <= MAX_PREDICT:
                next_target += 1
            if next_target > MAX_PREDICT:
                next_target = MIN_PREDICT
            attempts += 1
        
        # Construction message
        algo_name = "R2" if rule_type == "R2" else "R1"
        prediction_msg = f"""🎰 **#{target}** → {SUIT_DISPLAY.get(suit, suit)}

🔮 Suivante: #{next_target} ({SUIT_DISPLAY.get(next_suit, next_suit)}) dans {next_wait}min | {algo_name}"""
        
        private_messages = await send_to_all(prediction_msg, target)
        
        if not private_messages:
            logger.error(f"❌ Aucun utilisateur n'a reçu la prédiction pour #{target}")
            return False
        
        # Stockage pour R1
        if rule_type == "R1":
            next_rule1_prediction = {
                'target': next_target,
                'suit': next_suit,
                'wait': next_wait,
                'base': target,
                'idx': next_idx
            }
        
        # Stockage prédiction
        pending_predictions[target] = {
            'message_id': 0,
            'suit': suit,
            'base_game': base,
            'status': '⌛',
            'check_count': 0,
            'rattrapage': 0,
            'rule_type': rule_type,
            'private_messages': private_messages,
            'created_at': datetime.now().isoformat()
        }
        
        # Mise à jour flags
        if rule_type == "R2":
            rule2_active = True
            rule2_predicted_games.add(target)
            rule1_consecutive_count = 0
            logger.info(f"🔥 R2: #{target}, prochaine: #{next_target}")
        else:
            rule1_consecutive_count += 1
            logger.info(f"⏱️ R1: #{target}, prochaine: #{next_target}")
        
        return True
        
    except Exception as e:
        logger.error(f"❌ Erreur envoi prédiction: {e}")
        import traceback
        logger.error(traceback.format_exc())
        return False

def queue_prediction(target: int, suit: str, base: int, 
                    rattrapage=0, original_game=None, rule_type="R2"):
    """Met une prédiction en file d'attente."""
    global rule2_active, rule2_predicted_games
    
    if rule_type == "R2":
        rule2_active = True
        rule2_predicted_games.add(target)
    
    if target in queued_predictions or (target in pending_predictions and rattrapage == 0):
        logger.warning(f"Prédiction #{target} déjà en file d'attente ou active")
        return False

    queued_predictions[target] = {
        'target_game': target,
        'predicted_suit': suit,
        'base_game': base,
        'rattrapage': rattrapage,
        'original_game': original_game,
        'rule_type': rule_type,
        'queued_at': datetime.now().isoformat()
    }
    
    logger.info(f"📋 Prédiction #{target} mise en file d'attente ({rule_type}, Rattrapage {rattrapage})")
    return True

async def check_and_send_queued_predictions(current: int):
    """Envoie les prédictions en attente."""
    global current_game_number
    current_game_number = current
    
    for target in sorted(list(queued_predictions.keys())):
        if target >= current:
            p = queued_predictions.pop(target)
            await send_prediction_to_users(
                p['target_game'],
                p['predicted_suit'],
                p['base_game'],
                p.get('rattrapage', 0),
                p.get('original_game'),
                p.get('rule_type', 'R2')
            )

# ============ FONCTIONS VÉRIFICATION RÉSULTATS ============
async def update_prediction_status(game: int, new_status: str):
    """Met à jour le statut d'une prédiction et édite tous les messages."""
    global rule2_active, rule1_consecutive_count
    
    try:
        if game not in pending_predictions:
            logger.warning(f"Tentative de mise à jour pour jeu #{game} non trouvé")
            return False

        pred = pending_predictions[game]
        suit = pred['suit']
        rule_type = pred.get('rule_type', 'R2')
        rattrapage = pred.get('rattrapage', 0)
        original_game = pred.get('original_game', game)

        logger.info(f"Mise à jour statut #{game} [{rule_type}] vers {new_status}")

        # Construction message mis à jour
        status_texts = {
            '✅0️⃣': '✅ GAGNÉ!',
            '✅1️⃣': '✅ Gagné (2ème)',
            '✅2️⃣': '✅ Gagné (3ème)',
            '✅3️⃣': '✅ Gagné (4ème)',
            '❌': '❌ Perdu',
            '⏳ R1': '⏳ Rattrapage 1...',
            '⏳ R2': '⏳ Rattrapage 2...',
            '⏳ R3': '⏳ Rattrapage 3...'
        }
        status_text = status_texts.get(new_status, f'⏳ {new_status}')
        
        algo_name = "R2" if rule_type == "R2" else "R1"
        
        updated_msg = f"""🎰 **#{original_game}** → {SUIT_DISPLAY.get(suit, suit)}

📊 {status_text} | {algo_name}"""

        # Édition des messages
        private_msgs = pred.get('private_messages', {})
        edited_count = 0
        
        for user_id_str, msg_id in list(private_msgs.items()):
            try:
                user_id = int(user_id_str)
                await client.edit_message(user_id, msg_id, updated_msg)
                edited_count += 1
            except Exception as e:
                logger.error(f"❌ Erreur édition message pour {user_id_str}: {e}")
                if "message to edit not found" in str(e).lower():
                    del private_msgs[user_id_str]

        logger.info(f"✅ Messages édités: {edited_count} succès")
        pred['status'] = new_status
        
        # Gestion fin de prédiction
        if new_status in ['✅0️⃣', '✅1️⃣', '✅2️⃣', '✅3️⃣']:
            stats_bilan['total'] += 1
            stats_bilan['wins'] += 1
            stats_bilan['win_details'][new_status] = stats_bilan['win_details'].get(new_status, 0) + 1
            
            if rule_type == "R2" and rattrapage == 0:
                rule2_active = False
                logger.info("Règle 2 terminée (victoire), Règle 1 peut reprendre")
            elif rule_type == "R1":
                rule1_consecutive_count = 0
                
            if game in pending_predictions:
                del pending_predictions[game]
            
            asyncio.create_task(check_and_send_queued_predictions(current_game_number))
            
        elif new_status == '❌':
            stats_bilan['total'] += 1
            stats_bilan['losses'] += 1
            
            if rule_type == "R2" and rattrapage == 0:
                rule2_active = False
                logger.info("Règle 2 terminée (défaite), Règle 1 peut reprendre")
            elif rule_type == "R1":
                rule1_consecutive_count = 0
                
            if game in pending_predictions:
                del pending_predictions[game]
            
            asyncio.create_task(check_and_send_queued_predictions(current_game_number))

        return True
        
    except Exception as e:
        logger.error(f"Erreur update_prediction_status: {e}")
        import traceback
        logger.error(traceback.format_exc())
        return False

async def check_prediction_result(game_number: int, first_group: str):
    """
    Vérifie si le costume prédit apparaît dans la séquence:
    - Jeu N (prédit): vérifie groupe
    - Si non trouvé → Jeu N+1: vérifie groupe  
    - Si non trouvé → Jeu N+2: vérifie groupe
    - Si non trouvé → Jeu N+3: vérifie groupe
    - Si toujours pas → ❌ PERDU
    """
    logger.info(f"Vérification résultat pour jeu #{game_number}, groupe: {first_group[:30]}...")
    
    # Vérification prédiction principale (N)
    if game_number in pending_predictions:
        pred = pending_predictions[game_number]
        
        if pred.get('rattrapage', 0) == 0:
            target_suit = pred['suit']
            rule_type = pred.get('rule_type', 'R2')
            original_game = game_number
            
            # Vérifie N
            if has_suit_in_group(first_group, target_suit):
                logger.info(f"✅0️⃣ Trouvé immédiatement pour #{original_game} au jeu #{game_number}!")
                await update_prediction_status(original_game, '✅0️⃣')
                return
            
            # Prépare les rattrapages N+1, N+2, N+3
            rattrapage_1 = get_next_for_check(game_number)
            rattrapage_2 = get_next_for_check(rattrapage_1)
            rattrapage_3 = get_next_for_check(rattrapage_2)
            
            if rattrapage_1 > MAX_GAME:
                logger.info(f"❌ Pas de rattrapage possible après #{game_number}")
                await update_prediction_status(original_game, '❌')
                return
            
            # Met en file d'attente le rattrapage 1
            if queue_prediction(rattrapage_1, target_suit, pred['base_game'], 
                              rattrapage=1, original_game=original_game, rule_type=rule_type):
                await update_prediction_status(original_game, '⏳ R1')
                logger.info(f"Échec #{game_number}, Rattrapage 1 planifié pour #{rattrapage_1}")
                
                # Prépare les suivants silencieusement
                if rattrapage_2 <= MAX_GAME:
                    queued_predictions[rattrapage_2] = {
                        'target_game': rattrapage_2,
                        'predicted_suit': target_suit,
                        'base_game': pred['base_game'],
                        'rattrapage': 2,
                        'original_game': original_game,
                        'rule_type': rule_type,
                        'queued_at': datetime.now().isoformat()
                    }
                if rattrapage_3 <= MAX_GAME:
                    queued_predictions[rattrapage_3] = {
                        'target_game': rattrapage_3,
                        'predicted_suit': target_suit,
                        'base_game': pred['base_game'],
                        'rattrapage': 3,
                        'original_game': original_game,
                        'rule_type': rule_type,
                        'queued_at': datetime.now().isoformat()
                    }
            return

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
                logger.info(f"{status_code} Trouvé pour #{original_game} au rattrapage {rattrapage_actuel}!")
                
                await update_prediction_status(original_game, status_code)
                
                # Nettoyage des suivants
                for r in range(rattrapage_actuel + 1, 4):
                    potential_game = original_game + r
                    if potential_game in pending_predictions:
                        del pending_predictions[potential_game]
                    if potential_game in queued_predictions:
                        del queued_predictions[potential_game]
                
                if target_game != original_game and target_game in pending_predictions:
                    del pending_predictions[target_game]
                return
            
            # Pas trouvé, passe au suivant ou défaite
            if rattrapage_actuel < 3:
                next_rattrapage = rattrapage_actuel + 1
                next_target = original_game + next_rattrapage
                
                if next_target > MAX_GAME:
                    logger.info(f"❌ Définitif pour #{original_game} (hors limite)")
                    await update_prediction_status(original_game, '❌')
                    if target_game in pending_predictions:
                        del pending_predictions[target_game]
                    return
                
                if next_target in queued_predictions:
                    q = queued_predictions.pop(next_target)
                    pending_predictions[next_target] = {
                        'message_id': 0,
                        'suit': q['predicted_suit'],
                        'base_game': q['base_game'],
                        'status': '🔮',
                        'check_count': 0,
                        'rattrapage': next_rattrapage,
                        'original_game': original_game,
                        'rule_type': rule_type,
                        'private_messages': pending_predictions.get(original_game, {}).get('private_messages', {}).copy(),
                        'created_at': datetime.now().isoformat()
                    }
                    await update_prediction_status(original_game, f'⏳ R{next_rattrapage}')
                    logger.info(f"Échec rattrapage {rattrapage_actuel}, activation R{next_rattrapage}")
                
                if target_game != original_game and target_game in pending_predictions:
                    del pending_predictions[target_game]
            else:
                logger.info(f"❌ Définitif pour #{original_game} après 3 rattrapages")
                await update_prediction_status(original_game, '❌')
                
                if target_game != original_game and target_game in pending_predictions:
                    del pending_predictions[target_game]
            return

# ============ TRAITEMENT STATS (RÈGLE 2) ============
async def process_stats_message(message_text: str):
    """Traite les statistiques du canal 2 (Règle 2)."""
    global last_source_game_number, suit_prediction_counts, rule2_active, rule2_predicted_games
    
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
                    target = last_source_game_number + USER_A
                    
                    if not is_valid_for_prediction(target):
                        target = get_next_for_prediction(last_source_game_number + USER_A - 1)
                    
                    global rule1_consecutive_count, next_rule1_prediction
                    rule1_consecutive_count = 0
                    next_rule1_prediction = None
                    
                    rule2_predicted_games.add(target)
                    
                    if queue_prediction(target, predicted_suit, last_source_game_number, rule_type="R2"):
                        suit_prediction_counts[predicted_suit] = current_count + 1
                        for s in ALL_SUITS:
                            if s != predicted_suit:
                                suit_prediction_counts[s] = 0
                        rule2_active = True
                        logger.info(f"🔥 R2 en file d'attente: #{target}")
                        return True
    return False

# ============ RÈGLE 1 ============
async def process_prediction_logic_rule1(message_text: str, chat_id: int):
    """Gère Règle 1. Se déclenche sur réception d'un numéro impair."""
    global last_known_source_game, current_game_number
    global rule2_active, rule1_consecutive_count
    global next_rule1_prediction, current_time_cycle_index
    
    if chat_id != SOURCE_CHANNEL_ID:
        return
    
    game = extract_game_number(message_text, for_prediction=False)
    
    if not game:
        # Vérifie si c'est un impair
        m = re.search(r"#N\s*(\d+)", message_text, re.IGNORECASE)
        if m:
            n = int(m.group(1))
            if is_valid_for_check(n) and n % 2 == 1:
                last_known_source_game = n
                
                if next_rule1_prediction and next_rule1_prediction['target'] == n + 1:
                    if (n + 1) in rule2_predicted_games:
                        logger.info(f"🚫 #{n+1} pris par R2")
                        next_rule1_prediction = None
                        return
                    
                    p = next_rule1_prediction
                    logger.info(f"🎯 Déclenché par impair #{n}: envoi #{p['target']}")
                    
                    if await send_prediction_to_users(p['target'], p['suit'], p['base'], rule_type="R1"):
                        current_time_cycle_index = p['idx']
                    
                    next_rule1_prediction = None
                    return
                
                if (not rule2_active and 
                    rule1_consecutive_count < MAX_RULE1_CONSECUTIVE and 
                    not next_rule1_prediction):
                    
                    next_pair = n + 1
                    if not is_valid_for_prediction(next_pair):
                        next_pair = get_next_for_prediction(n)
                    
                    wait = TIME_CYCLE[current_time_cycle_index]
                    target, suit = calc_next_prediction(next_pair - 1, wait)
                    
                    while target in rule2_predicted_games:
                        current_time_cycle_index = (current_time_cycle_index + 1) % len(TIME_CYCLE)
                        wait = TIME_CYCLE[current_time_cycle_index]
                        target, suit = calc_next_prediction(target, wait)
                    
                    idx = (current_time_cycle_index + 1) % len(TIME_CYCLE)
                    next_rule1_prediction = {
                        'target': target,
                        'suit': suit,
                        'wait': wait,
                        'base': next_pair - 1,
                        'idx': idx
                    }
                    logger.info(f"📝 Promesse R1: #{target} (déclenchera sur impair #{n})")
        return
    
    if is_valid_for_prediction(game):
        last_known_source_game = game
        last_source_game_number = game
    else:
        last_source_game_number = game

# ============ GESTION MESSAGES ============
async def process_finalized_message(message_text: str, chat_id: int):
    """Traite les messages finalisés."""
    global current_game_number, last_source_game_number, last_known_source_game
    
    try:
        if chat_id == SOURCE_CHANNEL_2_ID:
            await process_stats_message(message_text)
            await check_and_send_queued_predictions(current_game_number)
            return

        if not is_message_finalized(message_text):
            return

        game = extract_game_number(message_text, for_prediction=False)
        if game is None:
            return

        current_game_number = game
        last_source_game_number = game
        
        if is_valid_for_prediction(game):
            last_known_source_game = game

        message_hash = f"{game}_{message_text[:50]}"
        if message_hash in processed_messages:
            return
        processed_messages.add(message_hash)

        groups = extract_parentheses_groups(message_text)
        if not groups:
            return
            
        first_group = groups[0]
        logger.info(f"Finalisé #{game}: {first_group[:50]}")

        await check_prediction_result(game, first_group)
        await check_and_send_queued_predictions(game)

    except Exception as e:
        logger.error(f"Erreur traitement finalisé: {e}")

async def handle_message(event):
    """Gère les nouveaux messages."""
    try:
        chat = await event.get_chat()
        chat_id = chat.id
        
        if hasattr(chat, 'broadcast') and chat.broadcast:
            if not str(chat_id).startswith('-100'):
                chat_id = int(f"-100{abs(chat_id)}")
        
        message_text = event.message.message
        logger.info(f"Message de chat_id={chat_id}: {message_text[:80]}...")

        if chat_id == SOURCE_CHANNEL_ID:
            m = re.search(r"#N\s*(\d+)", message_text, re.IGNORECASE)
            if m:
                n = int(m.group(1))
                if is_valid_for_prediction(n):
                    last_known_source_game = n
                    last_source_game_number = n
            
            await process_prediction_logic_rule1(message_text, chat_id)
            
            if is_message_finalized(message_text):
                await process_finalized_message(message_text, chat_id)
            
            if message_text.startswith('/info'):
                await cmd_info(event)
        
        elif chat_id == SOURCE_CHANNEL_2_ID:
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

        message_text = event.message.message

        if chat_id == SOURCE_CHANNEL_ID:
            m = re.search(r"#N\s*(\d+)", message_text, re.IGNORECASE)
            if m:
                n = int(m.group(1))
                if is_valid_for_prediction(n):
                    global last_known_source_game, last_source_game_number
                    last_known_source_game = n
                    last_source_game_number = n
            
            await process_prediction_logic_rule1(message_text, chat_id)
            
            if is_message_finalized(message_text):
                await process_finalized_message(message_text, chat_id)
        
        elif chat_id == SOURCE_CHANNEL_2_ID:
            await process_stats_message(message_text)
            await check_and_send_queued_predictions(current_game_number)

    except Exception as e:
        logger.error(f"Erreur handle_edited_message: {e}")

client.add_event_handler(handle_message, events.NewMessage())
client.add_event_handler(handle_edited_message, events.MessageEdited())

# ============ COMMANDES ============
async def cmd_info(event):
    """Commande /info pour admin."""
    if event.sender_id != ADMIN_ID:
        return
        
    active_preds = len(pending_predictions)
    rule1_status = f"{rule1_consecutive_count}/{MAX_RULE1_CONSECUTIVE}"
    rule2_status = "ACTIVE" if rule2_active else "Inactif"
    next_r1 = f"#{next_rule1_prediction['target']}" if next_rule1_prediction else "Aucune"
    
    info_msg = (
        f"ℹ️ **ÉTAT**\n\n"
        f"🎮 Actuel: #{current_game_number}\n"
        f"🔮 Actives: {active_preds}\n"
        f"⏳ R2: {rule2_status} ({len(rule2_predicted_games)} bloqués)\n"
        f"⏱️ R1: {rule1_status}\n"
        f"🎯 Promesse: {next_r1}\n"
        f"📍 Source: #{last_known_source_game}\n"
        f"👥 Users: {len(users_data)}"
    )
    await event.respond(info_msg)

@client.on(events.NewMessage(pattern='/start'))
async def cmd_start(event):
    if event.is_group or event.is_channel:
        return
    
    uid = event.sender_id
    user = get_user(uid)
    
    if user.get('registered'):
        if is_user_subscribed(uid) or uid == ADMIN_ID:
            await event.respond(f"""🎯 **BON RETOUR {user.get('prenom', 'CHAMPION').upper()}!**

✅ Accès ACTIF! Les prédictions arrivent ici.

🔥 Restez attentif!""")
            return
        
        if is_trial_active(uid):
            mins = (datetime.fromisoformat(user['trial_started']) + timedelta(minutes=60) - datetime.now()).seconds // 60
            await event.respond(f"""⏰ **ESSAI EN COURS**

🎁 {mins} minutes restantes!

🔥 Profitez-en!""")
            return
        
        update_user(uid, {'trial_used': True})
        buttons = [
            [Button.url("💳 24H - 500 FCFA", PAYMENT_LINK_24H)],
            [Button.url("💳 1 SEMAINE - 1500 FCFA", PAYMENT_LINK_1W)],
            [Button.url("💳 2 SEMAINES - 2500 FCFA", PAYMENT_LINK_2W)]
        ]
        await event.respond(f"""⚠️ **ESSAI TERMINÉ**

🎰 {user.get('prenom', 'CHAMPION')}, votre essai est fini!

👇 **CHOISISSEZ VOTRE FORMULE:**""", buttons=buttons)
        return
    
    await event.respond("""🎰 **BIENVENUE!**

💎 60 MINUTES D'ESSAI GRATUIT!

🚀 Inscription rapide:""")
    user_conversation_state[uid] = 'nom'
    await event.respond("📝 **Votre NOM?**")

@client.on(events.NewMessage())
async def handle_conv(event):
    if event.is_group or event.is_channel:
        return
    if event.message.message and event.message.message.startswith('/'):
        return
    
    uid = event.sender_id
    user = get_user(uid)
    
    # Admin prédiction manuelle
    if uid in admin_predict_state:
        state = admin_predict_state[uid]
        if state.get('step') == 'nums':
            nums = [int(n) for n in re.findall(r'\d+', event.message.message) 
                   if is_valid_for_prediction(int(n))]
            
            if not nums:
                await event.respond("❌ Aucun numéro valide (6-1436, pair, fin 2/4/6/8).")
                return
            
            sent = 0
            details = []
            for n in nums:
                suit = get_suit(n)
                if await send_prediction_to_users(n, suit, last_known_source_game, rule_type="R1"):
                    sent += 1
                    details.append(f"#{n} {SUIT_DISPLAY.get(suit, suit)}")
            
            await event.respond(f"✅ **{sent} prédictions envoyées**\n\n" + "\n".join(details[:20]))
            del admin_predict_state[uid]
            return
    
    # Admin message direct
    if uid in admin_message_state:
        state = admin_message_state[uid]
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
            except Exception as e:
                await event.respond(f"❌ Erreur: {e}")
            
            del admin_message_state[uid]
            return
    
    # Inscription
    if uid in user_conversation_state:
        step = user_conversation_state[uid]
        txt = event.message.message.strip()
        
        if step == 'nom':
            update_user(uid, {'nom': txt})
            user_conversation_state[uid] = 'prenom'
            await event.respond(f"✅ **{txt}**\n\n📝 **Prénom?**")
            return
        
        if step == 'prenom':
            update_user(uid, {'prenom': txt})
            user_conversation_state[uid] = 'pays'
            await event.respond(f"✅ **{txt}**\n\n🌍 **Pays?**")
            return
        
        if step == 'pays':
            update_user(uid, {
                'pays': txt, 'registered': True,
                'trial_started': datetime.now().isoformat(), 'trial_used': False
            })
            del user_conversation_state[uid]
            await event.respond(f"""🎉 **ACTIVÉ!**

⏰ 60min d'essai!

🚀 Les prédictions arrivent ici automatiquement.""")
            return
    
    # Paiement screenshot
    if user.get('awaiting_screenshot') and event.message.photo:
        try:
            await client.forward_messages(ADMIN_ID, event.message)
            buttons = [
                [Button.inline("✅ 24H", data=f"val_{uid}_1d")],
                [Button.inline("✅ 1 Sem", data=f"val_{uid}_1w")],
                [Button.inline("✅ 2 Sem", data=f"val_{uid}_2w")],
                [Button.inline("❌", data=f"rej_{uid}")]
            ]
            await client.send_message(ADMIN_ID, f"🔔 **Paiement**\n👤 {user.get('prenom')} {user.get('nom')}\n🆔 `{uid}`", buttons=buttons)
            await event.respond("📸 Reçu! Validation en cours...")
            update_user(uid, {'awaiting_screenshot': False})
        except Exception as e:
            await event.respond("❌ Erreur, réessayez.")
        return

# ============ COMMANDES ADMIN ============
@client.on(events.NewMessage(pattern='/predict'))
async def cmd_predict(event):
    if event.is_group or event.is_channel or event.sender_id != ADMIN_ID:
        return
    
    if last_known_source_game <= 0:
        await event.respond("⚠️ Non synchronisé.")
        return
    
    admin_predict_state[event.sender_id] = {'step': 'nums'}
    
    info = f"Prochaine auto: #{next_rule1_prediction['target']}" if next_rule1_prediction else "En attente..."
    
    await event.respond(f"""🎯 **PRÉDICTION MANUELLE**

📍 Dernier source: #{last_known_source_game}
📅 {info}

Entrez numéros (6-1436, pairs, fin 2/4/6/8):""")

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

@client.on(events.NewMessage(pattern='/status'))
async def cmd_status(event):
    if event.is_group or event.is_channel or event.sender_id != ADMIN_ID:
        return
    
    info = f"#{next_rule1_prediction['target']}" if next_rule1_prediction else "Aucune"
    eligible = sum(1 for u in users_data if can_receive_predictions(int(u)))
    
    await event.respond(f"""📊 **STATUT**

🎮 Source: #{last_known_source_game}
⏳ R2: {'🔥' if rule2_active else 'Off'} ({len(rule2_predicted_games)} bloqués)
⏱️ R1: {rule1_consecutive_count}/{MAX_RULE1_CONSECUTIVE}
🎯 Cycle: {current_time_cycle_index} ({TIME_CYCLE[current_time_cycle_index]}min)
📅 Prochaine: {info}
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
    global rule2_predicted_games, next_rule1_prediction, admin_predict_state
    
    users_data = {}
    save_users_data()
    pending_predictions.clear()
    queued_predictions.clear()
    processed_messages.clear()
    suit_prediction_counts.clear()
    rule2_predicted_games.clear()
    
    current_game_number = 0
    last_source_game_number = 0
    last_known_source_game = 0
    current_time_cycle_index = 0
    next_rule1_prediction = None
    admin_predict_state.clear()
    
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

🎲 **Prédictions:** 6-1436 (pairs, fin 2/4/6/8)
🔍 **Vérification:** 1-1440 (tous numéros)

💰 **Tarifs:** 500FCFA(24h) | 1500FCFA(1sem) | 2500FCFA(2sem)

📊 **Commandes admin:**
/status - État du bot
/predict - Prédiction manuelle
/bilan - Statistiques
/reset - Reset total
/users - Liste utilisateurs
/msg ID - Envoyer message""")

@client.on(events.NewMessage(pattern='/payer'))
async def cmd_payer(event):
    if event.is_group or event.is_channel:
        return
    
    uid = event.sender_id
    user = get_user(uid)
    
    if not user.get('registered'):
        await event.respond("❌ /start d'abord")
        return
    
    buttons = [
        [Button.url("⚡ 24H - 500 FCFA", PAYMENT_LINK_24H)],
        [Button.url("🔥 1 SEMAINE - 1500 FCFA", PAYMENT_LINK_1W)],
        [Button.url("💎 2 SEMAINES - 2500 FCFA", PAYMENT_LINK_2W)]
    ]
    
    await event.respond(f"""💳 **PAIEMENT**

🎰 {user.get('prenom', 'CHAMPION')}, choisissez:

👇 **VOTRE FORMULE:**""", buttons=buttons)
    update_user(uid, {'awaiting_screenshot': True})

@client.on(events.CallbackQuery(data=re.compile(b'val_(\d+)_(.*)')))
async def handle_val(event):
    if event.sender_id != ADMIN_ID:
        return
    
    uid = int(event.data_match.group(1).decode())
    dur = event.data_match.group(2).decode()
    
    days = {'1d': 1, '1w': 7, '2w': 14}.get(dur, 1)
    end = datetime.now() + timedelta(days=days)
    
    update_user(uid, {
        'subscription_end': end.isoformat(),
        'subscription_type': 'premium'
    })
    
    try:
        await client.send_message(uid, f"🎉 **ACTIVÉ!**\n\n✅ {days} jour(s) confirmé!\n🔥 Bonne chance!")
    except:
        pass
    
    await event.edit(f"✅ {uid} validé")

@client.on(events.CallbackQuery(data=re.compile(b'rej_(\d+)')))
async def handle_rej(event):
    if event.sender_id != ADMIN_ID:
        return
    
    uid = int(event.data_match.group(1).decode())
    try:
        await client.send_message(uid, "❌ Demande rejetée.")
    except:
        pass
    await event.edit(f"❌ {uid} rejeté")

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
    logger.info(f"Serveur web démarré sur port {PORT}")

# ============ RESET QUOTIDIEN ============
async def schedule_daily_reset():
    """Reset quotidien à 00h59 WAT."""
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
        global rule2_predicted_games, next_rule1_prediction
        
        pending_predictions.clear()
        queued_predictions.clear()
        processed_messages.clear()
        rule2_predicted_games.clear()
        
        current_game_number = 0
        last_source_game_number = 0
        last_known_source_game = 0
        current_time_cycle_index = 0
        next_rule1_prediction = None
        
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
