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

async def send_prediction_to_user(user_id: int, prediction_msg: str, target_game: int):
    try:
        if not can_receive_predictions(user_id):
            user = get_user(user_id)
            if user.get('subscription_end') and not user.get('expiry_notified', False):
                expiry_msg = (
                    "⚠️ **Votre abonnement a expiré !**\n\n"
                    "Ne laissez pas la chance s'échapper ! 🎰 Nos algorithmes sont actuellement en pleine performance avec un taux de réussite exceptionnel. 🚀\n\n"
                    "Réactivez votre accès maintenant pour ne rater aucune opportunité de gagner gros aujourd'hui. Votre succès n'attend que vous ! 💰🎯"
                )
                buttons = [
                    [Button.url("💳 24 HEURES (200 FCFA)", PAYMENT_LINK_24H)],
                    [Button.url("💳 1 SEMAINE (1000 FCFA)", PAYMENT_LINK)],
                    [Button.url("💳 2 SEMAINES (2000 FCFA)", PAYMENT_LINK)]
                ]
                await client.send_message(user_id, expiry_msg, buttons=buttons)
                update_user(user_id, {'expiry_notified': True})
                logger.info(f"Notification d'expiration envoyée à {user_id}")
            return None

        sent_msg = await client.send_message(user_id, prediction_msg)
        
        user_id_str = str(user_id)
        if target_game not in pending_predictions:
            pending_predictions[target_game] = {'private_messages': {}}
        
        if 'private_messages' not in pending_predictions[target_game]:
            pending_predictions[target_game]['private_messages'] = {}
            
        pending_predictions[target_game]['private_messages'][user_id_str] = sent_msg.id
        logger.info(f"Prédiction envoyée en privé à {user_id} (Msg ID: {sent_msg.id})")
        return sent_msg.id
    except Exception as e:
        logger.error(f"Erreur envoi prédiction privée à {user_id}: {e}")
        return None

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

async def send_prediction_to_channel(target_game: int, predicted_suit: str, base_game: int, 
                                     rattrapage=0, original_game=None, rule_type="R2"):
    """Envoie la prédiction et l'ajoute aux prédictions actives."""
    global rule2_active, rule1_consecutive_count
    
    try:
        # Si c'est un rattrapage, on ne crée pas un nouveau message mais on référence l'original
        if rattrapage > 0:
            # Récupérer les messages privés de la prédiction originale
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
                'private_messages': original_private_msgs,  # Référence pour mise à jour
                'created_at': datetime.now().isoformat()
            }
            
            if rule_type == "R2":
                rule2_active = True
            logger.info(f"Rattrapage {rattrapage} actif pour #{target_game} (Original #{original_game}, {rule_type})")
            return 0

        # Vérifier si une prédiction Règle 2 est déjà active pour un numéro futur
        if rule_type == "R1":
            active_r2_predictions = [p for game, p in pending_predictions.items() 
                                    if p.get('rule_type') == 'R2' and p.get('rattrapage', 0) == 0 
                                    and game > current_game_number]
            if active_r2_predictions:
                logger.info(f"Règle 2 active, Règle 1 ne peut pas prédire #{target_game}")
                return None
        
        # Format du message selon la règle
        if rule_type == "R2":
            prediction_msg = f"""🌤️ Игра № {target_game}
🔹 Масть Игроку {SUIT_DISPLAY.get(predicted_suit, predicted_suit)}
🤖Statut :⌛
💧 Догон 2 Игры!! (🔰+3 Риск)"""
        else:
            prediction_msg = f"🔵{target_game}  🌀 {SUIT_DISPLAY.get(predicted_suit, predicted_suit)} : ⌛"

        # Envoi aux utilisateurs et stockage des IDs
        private_messages = {}
        for user_id_str, user_info in users_data.items():
            try:
                user_id = int(user_id_str)
                if can_receive_predictions(user_id) or user_info.get('registered'):
                    msg_id = await send_prediction_to_user(user_id, prediction_msg, target_game)
                    if msg_id:
                        private_messages[user_id_str] = msg_id
            except Exception as e:
                logger.error(f"Erreur envoi privé à {user_id_str}: {e}")

        pending_predictions[target_game] = {
            'message_id': 0,
            'suit': predicted_suit,
            'base_game': base_game,
            'status': '⌛',
            'check_count': 0,
            'rattrapage': 0,
            'rule_type': rule_type,
            'private_messages': private_messages,  # Stockage des IDs pour édition future
            'created_at': datetime.now().isoformat()
        }

        # Mise à jour des flags
        if rule_type == "R2":
            rule2_active = True
            rule1_consecutive_count = 0  # Reset compteur Règle 1
            logger.info(f"Règle 2 active: Jeu #{target_game} - {predicted_suit}")
        else:
            rule1_consecutive_count += 1
            logger.info(f"Règle 1 active: Jeu #{target_game} - {predicted_suit} (Consécutif: {rule1_consecutive_count})")

        return 0

    except Exception as e:
        logger.error(f"Erreur envoi prédiction: {e}")
        return None

def queue_prediction(target_game: int, predicted_suit: str, base_game: int, 
                    rattrapage=0, original_game=None, rule_type="R2"):
    """Met une prédiction en file d'attente."""
    global rule2_active
    
    # Si Règle 2 déclenche, on arrête la Règle 1
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

    for target_game in sorted_queued:
        if target_game >= current_game:
            pred_data = queued_predictions.pop(target_game)
            await send_prediction_to_channel(
                pred_data['target_game'],
                pred_data['predicted_suit'],
                pred_data['base_game'],
                pred_data.get('rattrapage', 0),
                pred_data.get('original_game'),
                pred_data.get('rule_type', 'R2')
            )

async def update_prediction_status(game_number: int, new_status: str):
    """Met à jour le statut de la prédiction."""
    global rule2_active, rule1_consecutive_count
    
    try:
        if game_number not in pending_predictions:
            logger.warning(f"Tentative de mise à jour pour jeu #{game_number} non trouvé dans pending_predictions")
            return False

        pred = pending_predictions[game_number]
        suit = pred['suit']
        rule_type = pred.get('rule_type', 'R2')
        rattrapage = pred.get('rattrapage', 0)
        original_game = pred.get('original_game', game_number)

        logger.info(f"Mise à jour statut #{game_number} [{rule_type}] vers {new_status} (rattrapage: {rattrapage})")

        # Format du message mis à jour
        if rule_type == "R2":
            updated_msg = f"""🌤️ Игра № {original_game}
🔹 Масть Игроку {SUIT_DISPLAY.get(suit, suit)}
🤖Statut :{new_status}
💧 Догон 2 Игры!! (🔰+3 Риск)"""
        else:
            updated_msg = f"🔵{original_game}  🌀 {SUIT_DISPLAY.get(suit, suit)} : {new_status}"

        # Édition des messages privés
        private_msgs = pred.get('private_messages', {})
        logger.info(f"Édition de {len(private_msgs)} messages privés pour le statut {new_status}")
        
        for user_id_str, msg_id in private_msgs.items():
            try:
                user_id = int(user_id_str)
                if can_receive_predictions(user_id):
                    await client.edit_message(user_id, msg_id, updated_msg)
                    logger.info(f"✅ Message édité pour {user_id}: {new_status}")
            except Exception as e:
                logger.error(f"❌ Erreur édition message pour {user_id_str}: {e}")

        pred['status'] = new_status
        
        # Mise à jour des statistiques et flags
        if new_status in ['✅0️⃣', '✅1️⃣', '✅2️⃣', '✅3️⃣']:
            stats_bilan['total'] += 1
            stats_bilan['wins'] += 1
            stats_bilan['win_details'][new_status] = (stats_bilan['win_details'].get(new_status, 0) + 1)
            
            # Si c'était une prédiction Règle 2 sans rattrapage, on libère le flag
            if rule_type == "R2" and rattrapage == 0:
                rule2_active = False
                logger.info("Règle 2 terminée (victoire), Règle 1 peut reprendre")
            elif rule_type == "R1":
                rule1_consecutive_count = 0  # Reset si victoire
                
            del pending_predictions[game_number]
            asyncio.create_task(check_and_send_queued_predictions(current_game_number))
            
        elif new_status == '❌':
            stats_bilan['total'] += 1
            stats_bilan['losses'] += 1
            stats_bilan['loss_details']['❌'] += 1
            
            # Si c'était une prédiction Règle 2 sans rattrapage, on libère
            if rule_type == "R2" and rattrapage == 0:
                rule2_active = False
                logger.info("Règle 2 terminée (perte), Règle 1 peut reprendre")
            elif rule_type == "R1":
                rule1_consecutive_count = 0  # Reset si défaite
                
            del pending_predictions[game_number]
            asyncio.create_task(check_and_send_queued_predictions(current_game_number))

        return True
        
    except Exception as e:
        logger.error(f"Erreur update_prediction_status: {e}")
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
                # Échec N, on lance le rattrapage 1 pour N+1
                next_target = game_number + 1
                queue_prediction(next_target, target_suit, pred['base_game'], 
                               rattrapage=1, original_game=game_number, rule_type=rule_type)
                logger.info(f"Échec # {game_number}, Rattrapage 1 planifié pour #{next_target}")

    # 2. Vérification pour les rattrapages (N-1, N-2, N-3)
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
        return False  # Pas de déclencheur Règle 2

    # Miroirs : ♦️<->♠️ et ❤️<->♣️
    pairs = [('♦', '♠'), ('♥', '♣')]
    triggered = False
    
    for s1, s2 in pairs:
        if s1 in stats and s2 in stats:
            v1, v2 = stats[s1], stats[s2]
            diff = abs(v1 - v2)
            
            if diff >= 10:  # Décalage de 10+ requis
                # Prédire le plus faible parmi les deux miroirs
                predicted_suit = s1 if v1 < v2 else s2
                
                # Vérifier la limite de 3 prédictions consécutives pour ce costume
                current_count = suit_prediction_counts.get(predicted_suit, 0)
                if current_count >= 3:
                    logger.info(f"Limite de 3 prédictions atteinte pour {predicted_suit}, ignorée.")
                    continue

                logger.info(f"RÈGLE 2 DÉCLENCHÉE: Décalage {diff} entre {s1}({v1}) et {s2}({v2}). Prédiction: {predicted_suit}")
                
                if last_source_game_number > 0:
                    target_game = last_source_game_number + USER_A
                    
                    # Réinitialiser compteur Règle 1 car Règle 2 prend le relais
                    global rule1_consecutive_count, waiting_for_one_part, cycle_triggered, prediction_target_game
                    rule1_consecutive_count = 0
                    waiting_for_one_part = False
                    cycle_triggered = False
                    prediction_target_game = None
                    
                    if queue_prediction(target_game, predicted_suit, last_source_game_number, rule_type="R2"):
                        suit_prediction_counts[predicted_suit] = current_count + 1
                        # Réinitialiser les autres costumes
                        for s in ALL_SUITS:
                            if s != predicted_suit:
                                suit_prediction_counts[s] = 0
                        triggered = True
                        rule2_active = True
                        return True  # Une seule prédiction par message de stats
    return triggered

# ============================================================
# RÈGLE 1 : Prédiction par Cycle + "1 part" (FALLBACK)
# ============================================================

async def try_launch_prediction_rule1():
    """Tente de lancer la prédiction Règle 1 si condition '1 part' remplie."""
    global waiting_for_one_part, prediction_target_game, cycle_triggered
    global current_time_cycle_index, next_prediction_allowed_at, rule1_consecutive_count
    global rule2_active
    
    # Ne pas lancer si Règle 2 est active ou si on a atteint la limite consécutive
    if rule2_active:
        logger.info("Règle 2 active, Règle 1 en attente")
        return False
        
    if rule1_consecutive_count >= MAX_RULE1_CONSECUTIVE:
        logger.info(f"Limite Règle 1 atteinte ({MAX_RULE1_CONSECUTIVE}), attente Règle 2")
        return False
    
    if not cycle_triggered or prediction_target_game is None:
        return False
    
    # Vérifier la condition "1 part"
    if is_one_part_away(last_known_source_game, prediction_target_game):
        logger.info(f"RÈGLE 1: Condition '1 part' OK: {last_known_source_game} → {prediction_target_game}")
        
        # Calculer le costume selon le cycle
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
        
        # Lancer la prédiction
        success = await send_prediction_to_channel(
            prediction_target_game, 
            predicted_suit, 
            last_known_source_game,
            rule_type="R1"
        )
        
        if success is not None:
            # Réinitialiser les flags et passer au cycle suivant
            waiting_for_one_part = False
            cycle_triggered = False
            prediction_target_game = None
            
            # Consommer le cycle de temps
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

    # Mettre à jour le dernier numéro connu
    last_known_source_game = game_number
    logger.info(f"Règle 1: Dernier numéro source mis à jour: #{game_number}")
    
    # Si on est en attente d'un "1 part", vérifier si c'est maintenant possible
    if waiting_for_one_part and cycle_triggered:
        await try_launch_prediction_rule1()
        return
    
    # Vérifier si le temps cycle est arrivé ET si on peut prédire (pas de Règle 2 active, pas limite atteinte)
    now = datetime.now()
    if now < next_prediction_allowed_at:
        return
        
    if rule2_active:
        logger.info("Temps cycle arrivé mais Règle 2 active, attente")
        return
        
    if rule1_consecutive_count >= MAX_RULE1_CONSECUTIVE:
        logger.info(f"Temps cycle arrivé mais limite Règle 1 atteinte ({rule1_consecutive_count}), attente Règle 2")
        # On reset quand même le timer pour éviter de bloquer indéfiniment
        wait_min = TIME_CYCLE[current_time_cycle_index]
        next_prediction_allowed_at = now + timedelta(minutes=wait_min)
        current_time_cycle_index = (current_time_cycle_index + 1) % len(TIME_CYCLE)
        return
    
    # Le temps cycle est arrivé et on peut prédire !
    logger.info(f"RÈGLE 1: Temps cycle arrivé à {now.strftime('%H:%M:%S')}")
    cycle_triggered = True
    
    # Calculer la cible (N+2 valide)
    candidate = game_number + 2
    while candidate % 2 != 0 or candidate % 10 == 0:
        candidate += 1
    
    prediction_target_game = candidate
    logger.info(f"Règle 1: Cible calculée: #{prediction_target_game}")
    
    # Essayer de lancer immédiatement si condition "1 part" déjà remplie
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
            # Canal 2: Traiter stats (Règle 2) puis vérifier envois
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
        
        # Éviter doublons
        message_hash = f"{game_number}_{message_text[:50]}"
        if message_hash in processed_messages:
            return
        processed_messages.add(message_hash)

        groups = extract_parentheses_groups(message_text)
        if len(groups) < 1:
            return
            
        first_group = groups[0]

        # Vérification des résultats pour toutes les prédictions actives
        await check_prediction_result(game_number, first_group)
        
        # Envoi des files d'attente (pour nouvelles prédictions si place libérée)
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
            
            # 1. D'abord traiter la logique Règle 1 (cycle temps)
            await process_prediction_logic_rule1(message_text, chat_id)
            
            # 2. Puis traiter le message finalisé pour résultats
            if is_message_finalized(message_text):
                await process_finalized_message(message_text, chat_id)
            
            # Commande /info pour l'admin
            if message_text.startswith('/info'):
                active_preds = len(pending_predictions)
                rule1_status = f"Consécutifs: {rule1_consecutive_count}/{MAX_RULE1_CONSECUTIVE}"
                rule2_status = "ACTIVE" if rule2_active else "Inactif"
                
                history_text = "\n".join([f"🔹 #{h['game']} ({h['suit']}) à {h['time']}" for h in scp_history[-5:]]) if scp_history else "Aucune"
                
                info_msg = (
                    "ℹ️ **ÉTAT DU SYSTÈME**\n\n"
                    f"🎮 Jeu actuel: #{current_game_number}\n"
                    f"🔮 Prédictions actives: {active_preds}\n"
                    f"⏳ Règle 2: {rule2_status}\n"
                    f"⏱️ Règle 1: {rule1_status}\n"
                    f"🎯 Cible R1: #{prediction_target_game if prediction_target_game else 'Aucune'}\n"
                    f"📍 Dernier source: #{last_known_source_game}\n\n"
                    "📌 **DERNIÈRES IMPOSITIONS SCP :**\n"
                    f"{history_text}"
                )
                await event.respond(info_msg)
                return
        
        elif chat_id == SOURCE_CHANNEL_2_ID:
            message_text = event.message.message
            # Canal 2: Règle 2 (stats) + vérification envois
            await process_stats_message(message_text)
            await check_and_send_queued_predictions(current_game_number)
            
        # Commandes admin
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
    
    if user.get('registered'):
        if is_user_subscribed(user_id) or user_id == admin_id:
            sub_type = "Premium" if get_subscription_type(user_id) == 'premium' or user_id == admin_id else "Standard"
            sub_end = user.get('subscription_end', 'Illimité' if user_id == admin_id else 'N/A')
            update_user(user_id, {'expiry_notified': False})
            await event.respond(
                f"🎯 **Bienvenue {user.get('prenom', 'Admin' if user_id == admin_id else '')}!**\n\n"
                f"✅ Votre accès {sub_type} est actif.\n"
                f"📅 Expire le: {sub_end[:10] if sub_end and user_id != admin_id else sub_end}\n\n"
                "Les prédictions sont envoyées en temps réel ici même dans votre chat privé. 🚀\n\n"
                "**Système de prédiction:**\n"
                "• Règle 2 (Stats): Prioritaire\n"
                "• Règle 1 (Cycle): Fallback (max 3 consécutifs)"
            )
        elif is_trial_active(user_id):
            trial_start = datetime.fromisoformat(user['trial_started'])
            trial_end = trial_start + timedelta(minutes=10)
            remaining = (trial_end - datetime.now()).seconds // 60
            await event.respond(
                f"🎯 **Bienvenue {user.get('prenom', '')}!**\n\n"
                f"⏰ Période d'essai active: {remaining} minutes restantes.\n"
                "Profitez des prédictions gratuitement!"
            )
        else:
            update_user(user_id, {'trial_used': True})
            buttons = [[Button.url("💳 PAYER", PAYMENT_LINK)]]
            await event.respond(
                f"⚠️ **{user.get('prenom', '')}, votre période d'essai est terminée!**\n\n"
                "Pour continuer à recevoir les prédictions:\n\n"
                "💰 **1000 FCFA** = 1 semaine\n"
                "💰 **2000 FCFA** = 2 semaines\n\n"
                f"👤 Votre ID: `{user_id}`\n\n"
                "Cliquez sur le bouton ci-dessous pour payer:",
                buttons=buttons
            )
    else:
        user_conversation_state[user_id] = 'awaiting_nom'
        await event.respond(
            "🎰 **Bienvenue sur le Bot de Prédiction Baccarat!**\n\n"
            "Pour commencer, je vais vous poser quelques questions.\n\n"
            "📝 **Quel est votre NOM?**"
        )

@client.on(events.NewMessage())
async def handle_registration_and_payment(event):
    if event.is_group or event.is_channel: return
    if event.message.message and event.message.message.startswith('/'): 
        return
    
    user_id = event.sender_id
    user = get_user(user_id)
    
    if user_id in user_conversation_state:
        state = user_conversation_state[user_id]
        message_text = event.message.message.strip()
        
        if state == 'awaiting_nom':
            update_user(user_id, {'nom': message_text})
            user_conversation_state[user_id] = 'awaiting_prenom'
            await event.respond(f"✅ Nom enregistré: **{message_text}**\n\n📝 **Quel est votre PRÉNOM?**")
        
        elif state == 'awaiting_prenom':
            update_user(user_id, {'prenom': message_text})
            user_conversation_state[user_id] = 'awaiting_pays'
            await event.respond(f"✅ Prénom enregistré: **{message_text}**\n\n🌍 **Quel est votre PAYS d'origine?**")
        
        elif state == 'awaiting_pays':
            update_user(user_id, {
                'pays': message_text,
                'registered': True,
                'trial_started': datetime.now().isoformat(),
                'trial_used': False
            })
            del user_conversation_state[user_id]
            
            user = get_user(user_id)
            await event.respond(
                f"🎉 **Inscription terminée!**\n\n"
                f"👤 Nom: {user.get('nom')}\n"
                f"👤 Prénom: {user.get('prenom')}\n"
                f"🌍 Pays: {user.get('pays')}\n\n"
                f"⏰ **Vous avez 10 minutes d'essai GRATUIT!**\n"
                "Les prédictions seront envoyées ici même dans votre chat privé.\n\n"
                "Profitez-en! 🎯"
            )
            logger.info(f"Nouvel utilisateur inscrit: {user_id}")
        return
    
    if user.get('awaiting_screenshot') and event.message.photo:
        update_user(user_id, {'awaiting_screenshot': False, 'awaiting_amount': True})
        await event.respond(
            f"📸 **Capture d'écran reçue!**\n\n"
            "💰 **Quel montant avez-vous payé?**\n"
            "Répondez avec: `200`, `1000` ou `2000`"
        )
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
                f"💰 **Montant annoncé:** {amount} FCFA\n"
                f"📅 **Type souhaité:** {dur_text}\n"
                f"📍 **Pays:** {user_info.get('pays')}\n\n"
                "Veuillez vérifier le paiement et valider l'abonnement."
            )
            
            buttons = [
                [Button.inline(f"✅ Valider {dur_text}", data=f"valider_{user_id}_{dur_code}")],
                [Button.inline("❌ Rejeter", data=f"rejeter_{user_id}")]
            ]
            
            try:
                await client.send_message(admin_id, msg_admin, buttons=buttons)
            except Exception as e:
                logger.error(f"Erreur notification admin: {e}")

            await event.respond("✅ **Demande envoyée !**\nL'administrateur va vérifier votre paiement.")
        else:
            await event.respond("❌ Montant invalide. Répondez avec `200`, `1000` ou `2000`.")
        return

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
        notif_msg = (
            f"🎉 **Félicitations !**\n\n"
            f"Votre abonnement est activé avec succès ! ✅\n"
            "Vous verrez maintenant les prédictions automatiques ici. 🚀"
        )
        await client.send_message(user_id, notif_msg)
    except Exception as e:
        logger.error(f"Erreur notification user {user_id}: {e}")
        
    await event.edit(f"✅ Abonnement activé pour l'utilisateur {user_id}")
    await event.answer("Abonnement activé !")

@client.on(events.CallbackQuery(data=re.compile(b'rejeter_(\d+)')))
async def handle_rejection(event):
    admin_id = 1190237801
    if event.sender_id != admin_id:
        await event.answer("Accès refusé", alert=True)
        return
        
    user_id = int(event.data_match.group(1).decode())
    
    try:
        await client.send_message(user_id, "❌ Votre demande d'abonnement a été rejetée.")
    except:
        pass
        
    await event.edit(f"❌ Demande rejetée pour l'utilisateur {user_id}")
    await event.answer("Demande rejetée")

@client.on(events.NewMessage(pattern=r'^/a (\d+)$'))
async def cmd_set_a_shortcut(event):
    if event.is_group or event.is_channel: return
    if event.sender_id != ADMIN_ID and ADMIN_ID != 0: return
    
    global USER_A
    try:
        val = int(event.pattern_match.group(1))
        USER_A = val
        await event.respond(f"✅ Valeur de 'a' mise à jour : {USER_A}")
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
        await event.respond(f"✅ Valeur de 'a' mise à jour : {USER_A}\nLes prochaines prédictions seront sur le jeu N+{USER_A}")
    except Exception as e:
        await event.respond(f"❌ Erreur: {e}")

@client.on(events.NewMessage(pattern='/status'))
async def cmd_status(event):
    if event.is_group or event.is_channel: return
    if event.sender_id != ADMIN_ID and ADMIN_ID != 0:
        await event.respond("Commande réservée à l'administrateur")
        return

    status_msg = f"📊 **État du Bot:**\n\n"
    status_msg += f"🎮 Jeu actuel: #{current_game_number}\n"
    status_msg += f"🔢 Paramètre 'a': {USER_A}\n"
    status_msg += f"⏳ Règle 2 active: {'Oui' if rule2_active else 'Non'}\n"
    status_msg += f"⏱️ Règle 1 consécutifs: {rule1_consecutive_count}/{MAX_RULE1_CONSECUTIVE}\n\n"
    
    if pending_predictions:
        status_msg += f"**🔮 Actives ({len(pending_predictions)}):**\n"
        for game_num, pred in sorted(pending_predictions.items()):
            distance = game_num - current_game_number
            ratt = f" (R{pred['rattrapage']})" if pred.get('rattrapage', 0) > 0 else ""
            rule = pred.get('rule_type', 'R2')
            status_msg += f"• #{game_num}{ratt}: {pred['suit']} [{rule}] - {pred['status']} (dans {distance})\n"
    else: 
        status_msg += "**🔮 Aucune prédiction active**\n"

    await event.respond(status_msg)

@client.on(events.NewMessage(pattern='/bilan'))
async def cmd_bilan(event):
    if event.is_group or event.is_channel: return
    admin_id = 1190237801
    if event.sender_id != admin_id: return
    
    if stats_bilan['total'] == 0:
        await event.respond("📊 Aucune prédiction n'a encore été effectuée.")
        return

    win_rate = (stats_bilan['wins'] / stats_bilan['total']) * 100 if stats_bilan['total'] > 0 else 0
    loss_rate = (stats_bilan['losses'] / stats_bilan['total']) * 100 if stats_bilan['total'] > 0 else 0
    
    msg = (
        "📊 **BILAN ADMIN**\n\n"
        f"✅ Taux de réussite : {win_rate:.1f}%\n"
        f"❌ Taux de perte : {loss_rate:.1f}%\n\n"
        "**Détails :**\n"
        f"✅0️⃣ (Immédiat) : {stats_bilan['win_details'].get('✅0️⃣', 0)}\n"
        f"✅1️⃣ (1 délai) : {stats_bilan['win_details'].get('✅1️⃣', 0)}\n"
        f"✅2️⃣ (2 délais) : {stats_bilan['win_details'].get('✅2️⃣', 0)}\n"
        f"❌ (Perdu) : {stats_bilan['loss_details'].get('❌', 0)}\n"
        f"\nTotal prédictions : {stats_bilan['total']}"
    )
    
    await event.respond(msg)

@client.on(events.NewMessage(pattern='/reset'))
async def cmd_reset_all(event):
    if event.is_group or event.is_channel: return
    admin_id = 1190237801
    if event.sender_id != admin_id:
        await event.respond("❌ Commande réservée à l'administrateur principal.")
        return
    
    global users_data, pending_predictions, queued_predictions, processed_messages
    global current_game_number, last_source_game_number, stats_bilan
    global rule1_consecutive_count, rule2_active, suit_prediction_counts
    global last_known_source_game, prediction_target_game, waiting_for_one_part, cycle_triggered
    global current_time_cycle_index, next_prediction_allowed_at, already_predicted_games
    
    # Reset complet
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
    
    logger.warning(f"🚨 RESET TOTAL effectué par l'admin {event.sender_id}")
    await event.respond("🚨 **RÉINITIALISATION TOTALE EFFECTUÉE** 🚨")

@client.on(events.NewMessage(pattern='/help'))
async def cmd_help(event):
    if event.is_group or event.is_channel: return
    await event.respond("""📖 **Aide - Bot de Prédiction V2**

**Système de prédiction hybride :**

**Règle 2 (Prioritaire) - Stats:**
• Surveille le Canal Source 2 (statistiques)
• Déclenchement: Décalage ≥10 entre miroirs ♦️<->♠️ ou ❤️<->♣️
• Prédit le plus FAIBLE des deux
• Max 3 prédictions consécutives par costume
• Cible: Dernier numéro Source 1 + a

**Règle 1 (Fallback) - Cycle:**
• S'active si Règle 2 ne trouve pas de déclencheur
• Basée sur cycle de temps + condition "1 part"
• Max 3 prédictions consécutives
• S'arrête immédiatement si Règle 2 se déclenche

**Commandes :**
- `/status` : État du système
- `/set_a <valeur>` : Modifie le paramètre 'a'
- `/info` : Informations détaillées
- `/bilan` : Statistiques (admin)
""")

@client.on(events.NewMessage(pattern='/payer'))
async def cmd_payer(event):
    if event.is_group or event.is_channel: return
    
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
        "💳 **ABONNEMENT - Bot de Prédiction**\n\n"
        "**Tarifs:**\n"
        "💰 **200 FCFA** = 24 heures\n"
        "💰 **1000 FCFA** = 1 semaine\n"
        "💰 **2000 FCFA** = 2 semaines\n\n"
        f"👤 Votre ID: `{user_id}`\n\n"
        "Choisissez votre durée :",
        buttons=buttons
    )
    update_user(user_id, {'pending_payment': True, 'awaiting_screenshot': True})

# --- Serveur Web et Démarrage ---

async def index(request):
    html = f"""<!DOCTYPE html><html><head><title>Bot Prédiction Baccarat</title></head><body>
    <h1>🎯 Bot de Prédiction Baccarat</h1>
    <p>Le bot est en ligne.</p>
    <p><strong>Jeu actuel:</strong> #{current_game_number}</p>
    <p><strong>Règle 2 active:</strong> {'Oui' if rule2_active else 'Non'}</p>
    <p><strong>Règle 1 consécutifs:</strong> {rule1_consecutive_count}/{MAX_RULE1_CONSECUTIVE}</p>
    </body></html>"""
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

    logger.info(f"Tâche de reset planifiée pour {reset_time} WAT.")

    while True:
        now = datetime.now(wat_tz)
        target_datetime = datetime.combine(now.date(), reset_time, tzinfo=wat_tz)
        if now >= target_datetime:
            target_datetime += timedelta(days=1)
            
        time_to_wait = (target_datetime - now).total_seconds()
        logger.info(f"Prochain reset dans {timedelta(seconds=time_to_wait)}")
        await asyncio.sleep(time_to_wait)

        logger.warning("🚨 RESET QUOTIDIEN À 00h59 WAT DÉCLENCHÉ!")
        
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
        logger.info("Bot connecté et prêt.")
        return True
    except Exception as e:
        logger.error(f"Erreur démarrage du client Telegram: {e}")
        return False

async def main():
    """Fonction principale."""
    load_users_data()
    try:
        await start_web_server()
        success = await start_bot()
        if not success:
            logger.error("Échec du démarrage du bot")
            return

        asyncio.create_task(schedule_daily_reset())
        
        logger.info("Bot opérationnel - En attente de messages...")
        await client.run_until_disconnected()

    except Exception as e:
        logger.error(f"Erreur dans main: {e}")
        import traceback
        logger.error(traceback.format_exc())
    finally:
        if client.is_connected():
            await client.disconnect()

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Bot arrêté par l'utilisateur")
    except Exception as e:
        logger.error(f"Erreur fatale: {e}")
        import traceback
        logger.error(traceback.format_exc())
