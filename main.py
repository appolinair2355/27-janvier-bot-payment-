import os
import asyncio
import re
import logging
import sys
import json
import random
import io
from datetime import datetime, timedelta, timezone, time
from telethon import TelegramClient, events, Button
from telethon.sessions import StringSession
from aiohttp import web
from config import (
    API_ID, API_HASH, BOT_TOKEN, ADMIN_ID,
    SOURCE_CHANNEL_ID, SOURCE_CHANNEL_2_ID, PORT,
    SUIT_MAPPING, ALL_SUITS, SUIT_DISPLAY
)

# === IMPORT EASYOCR ===
try:
    import easyocr
    import numpy as np
    from PIL import Image
    OCR_AVAILABLE = True
    logger.info("✅ EasyOCR importé avec succès")
except ImportError:
    OCR_AVAILABLE = False
    logger.warning("⚠️ EasyOCR non disponible, installation requise: pip install easyocr")

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

# === INITIALISATION EASYOCR ===
ocr_reader = None
if OCR_AVAILABLE:
    try:
        # Initialisation avec anglais et français
        ocr_reader = easyocr.Reader(['en', 'fr'], gpu=False)
        logger.info("✅ EasyOCR initialisé (langues: en, fr)")
    except Exception as e:
        logger.error(f"❌ Erreur initialisation EasyOCR: {e}")
        ocr_reader = None

# Variables Globales d'État
SUIT_CYCLE = ['♥', '♦', '♣', '♠', '♦', '♥', '♠', '♣']
TIME_CYCLE = [5, 7, 10, 6]
current_time_cycle_index = 0
next_prediction_allowed_at = datetime.now()

def get_rule1_suit(game_number: int) -> str | None:
    if game_number < 6 or game_number > 1436 or game_number % 2 != 0 or game_number % 10 == 0:
        return None
    
    count_valid = 0
    for n in range(6, game_number + 1, 2):
        if n % 10 != 0:
            count_valid += 1
            
    if count_valid == 0: return None
    
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

# === NOUVELLES VARIABLES GLOBALES ===
waiting_for_trigger = {}
PREDICTION_DELAY_MINUTES = 4

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
            'awaiting_amount': False,
            'detected_amount': None  # Nouveau: montant détecté par OCR
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

# === FONCTIONS OCR POUR RECONNAISSANCE DE PAIEMENT ===
async def process_payment_screenshot(image_path: str, user_id: int) -> dict:
    """
    Analyse une capture d'écran de paiement avec EasyOCR
    Retourne: {'amount': int|None, 'time': str|None, 'is_valid': bool, 'raw_text': str}
    """
    if not OCR_AVAILABLE or ocr_reader is None:
        logger.error("OCR non disponible")
        return {'amount': None, 'time': None, 'is_valid': False, 'raw_text': ''}
    
    try:
        # Lire l'image
        image = Image.open(image_path)
        # Convertir en numpy array pour EasyOCR
        img_array = np.array(image)
        
        # Effectuer l'OCR
        logger.info(f"🔍 Analyse OCR de l'image pour utilisateur {user_id}...")
        results = ocr_reader.readtext(img_array, detail=0, paragraph=False)
        
        # Concaténer tout le texte
        raw_text = ' '.join(results).lower()
        logger.info(f"Texte OCR détecté: {raw_text[:200]}...")
        
        # Recherche du montant
        amount = None
        
        # Patterns pour détecter les montants
        amount_patterns = [
            r'(\d{3,4})\s*fcfa',
            r'(\d{3,4})\s*xof',
            r'montant[:\s]*(\d{3,4})',
            r'total[:\s]*(\d{3,4})',
            r'payer[:\s]*(\d{3,4})',
            r'(\d{3,4})\s*francs',
            r'(\d{3,4})\s*f',
            r'200|1000|2000',  # Montants spécifiques connus
        ]
        
        for pattern in amount_patterns:
            matches = re.findall(pattern, raw_text)
            for match in matches:
                try:
                    val = int(match)
                    if val in [200, 1000, 2000]:
                        amount = val
                        logger.info(f"✅ Montant détecté: {amount} FCFA")
                        break
                except:
                    continue
            if amount:
                break
        
        # Recherche de l'heure
        time_pattern = r'(\d{1,2})[h:](\d{2})'
        time_match = re.search(time_pattern, raw_text)
        detected_time = f"{time_match.group(1)}h{time_match.group(2)}" if time_match else None
        
        # Vérification si c'est un reçu MoneyFusion valide
        valid_keywords = ['moneyfusion', 'paiement', 'reçu', 'transaction', 'succès', 'confirmé']
        is_valid = any(keyword in raw_text for keyword in valid_keywords)
        
        return {
            'amount': amount,
            'time': detected_time,
            'is_valid': is_valid,
            'raw_text': raw_text
        }
        
    except Exception as e:
        logger.error(f"❌ Erreur OCR: {e}")
        return {'amount': None, 'time': None, 'is_valid': False, 'raw_text': ''}

# === FONCTIONS DE MESSAGE SIMPLIFIÉES ===
def create_beautiful_prediction_message(game_number: int, suit: str) -> str:
    """Crée un message de prédiction simple avec seulement le costume prédit"""
    suit_display = SUIT_DISPLAY.get(suit, suit)
    return f"🔮 **{game_number}** → {suit_display}"

def create_result_message(game_number: int, suit: str, status: str) -> str:
    """Crée le message de résultat avec le statut"""
    suit_display = SUIT_DISPLAY.get(suit, suit)
    return f"🔮 **{game_number}** → {suit_display} : {status}"

async def send_prediction_to_user(user_id: int, prediction_msg: str, target_game: int):
    try:
        user = get_user(user_id)
        
        if not can_receive_predictions(user_id):
            if user.get('subscription_end') and not user.get('expiry_notified', False):
                expiry_msg = (
                    "⚠️ **Votre abonnement a expiré !**\n\n"
                    "Ne laissez pas la chance s'échapper ! 🎰\n"
                    "Réactivez votre accès maintenant ! 💰🎯"
                )
                buttons = [
                    [Button.url("💳 24 HEURES (200 FCFA)", PAYMENT_LINK_24H)],
                    [Button.url("💳 1 SEMAINE (1000 FCFA)", PAYMENT_LINK)],
                    [Button.url("💳 2 SEMAINES (2000 FCFA)", PAYMENT_LINK)]
                ]
                await client.send_message(user_id, expiry_msg, buttons=buttons)
                update_user(user_id, {'expiry_notified': True})
                logger.info(f"Notification d'expiration envoyée à {user_id}")
            return

        sent_msg = await client.send_message(user_id, prediction_msg)
        logger.info(f"Prédiction envoyée à {user_id} pour #{target_game}: {prediction_msg}")
        
        user_id_str = str(user_id)
        if target_game not in pending_predictions:
            pending_predictions[target_game] = {'private_messages': {}}
        
        if 'private_messages' not in pending_predictions[target_game]:
            pending_predictions[target_game]['private_messages'] = {}
            
        pending_predictions[target_game]['private_messages'][user_id_str] = sent_msg.id
        logger.info(f"Message ID stocké: {sent_msg.id} pour utilisateur {user_id}")
        
    except Exception as e:
        logger.error(f"Erreur envoi prédiction privée à {user_id}: {e}")

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
    groups = re.findall(r"\d+\(([^)]*)\)", message)
    return groups

def normalize_suits(group_str: str) -> str:
    normalized = group_str.replace('❤️', '♥').replace('❤', '♥').replace('♥️', '♥')
    normalized = normalized.replace('♠️', '♠').replace('♦️', '♦').replace('♣️', '♣')
    return normalized

def has_suit_in_group(group_str: str, target_suit: str) -> bool:
    normalized = normalize_suits(group_str)
    target_normalized = normalize_suits(target_suit)
    
    for char in target_normalized:
        if char in normalized:
            return True
    return False

def get_predicted_suit(missing_suit: str) -> str:
    return SUIT_MAPPING.get(missing_suit, missing_suit)

# === NOUVELLES FONCTIONS UTILITAIRES ===
def get_next_predictable_number(current_num: int) -> int:
    candidate = current_num + 2
    while candidate <= 1436:
        if candidate % 2 == 0 and candidate % 10 != 0 and candidate > 5:
            return candidate
        candidate += 2
    return None

def get_trigger_number(predict_num: int) -> int:
    trigger = predict_num - 1
    if trigger % 2 == 0:
        trigger -= 1
    return trigger if trigger > 0 else None

# --- Logique de Prédiction et File d'Attente ---

async def send_prediction_to_channel(target_game: int, predicted_suit: str, base_game: int, rattrapage=0, original_game=None):
    try:
        active_auto_predictions = [p for game, p in pending_predictions.items() if p.get('rattrapage', 0) == 0 and game > current_game_number]
        
        if rattrapage == 0 and len(active_auto_predictions) >= 1:
            logger.info(f"Une prédiction automatique pour un numéro futur est déjà active. En attente pour #{target_game}")
            return None

        if rattrapage > 0:
            pending_predictions[target_game] = {
                'message_id': 0,
                'suit': predicted_suit,
                'base_game': base_game,
                'status': '🔮',
                'rattrapage': rattrapage,
                'original_game': original_game,
                'created_at': datetime.now().isoformat()
            }
            logger.info(f"Rattrapage {rattrapage} actif pour #{target_game} (Original #{original_game})")
            return 0

        prediction_msg = create_beautiful_prediction_message(target_game, predicted_suit)

        for user_id_str, user_info in users_data.items():
            try:
                user_id = int(user_id_str)
                if can_receive_predictions(user_id):
                    logger.info(f"Envoi prédiction privée à {user_id}")
                    await send_prediction_to_user(user_id, prediction_msg, target_game)
                else:
                    if user_info.get('registered'):
                        await send_prediction_to_user(user_id, prediction_msg, target_game)
            except Exception as e:
                logger.error(f"Erreur envoi privé à {user_id_str}: {e}")

        if target_game not in pending_predictions:
            pending_predictions[target_game] = {}
            
        pending_predictions[target_game].update({
            'message_id': 0, 
            'suit': predicted_suit,
            'base_game': base_game,
            'status': '🔮',
            'check_count': 0,
            'rattrapage': 0,
            'created_at': datetime.now().isoformat()
        })

        logger.info(f"Prédiction active: Jeu #{target_game} - {predicted_suit}")
        return 0

    except Exception as e:
        logger.error(f"Erreur envoi prédiction: {e}")
        return None

def queue_prediction(target_game: int, predicted_suit: str, base_game: int, rattrapage=0, original_game=None):
    if target_game in queued_predictions or (target_game in pending_predictions and rattrapage == 0):
        return False

    queued_predictions[target_game] = {
        'target_game': target_game,
        'predicted_suit': predicted_suit,
        'base_game': base_game,
        'rattrapage': rattrapage,
        'original_game': original_game,
        'queued_at': datetime.now().isoformat()
    }
    logger.info(f"📋 Prédiction #{target_game} mise en file d'attente (Rattrapage {rattrapage})")
    return True

async def check_and_send_queued_predictions(current_game: int):
    global current_game_number
    current_game_number = current_game

    sorted_queued = sorted(queued_predictions.keys())

    for target_game in sorted_queued:
        if target_game >= current_game:
            pred_data = queued_predictions.get(target_game)
            if not pred_data:
                continue
                
            result = await send_prediction_to_channel(
                pred_data['target_game'],
                pred_data['predicted_suit'],
                pred_data['base_game'],
                pred_data.get('rattrapage', 0),
                pred_data.get('original_game')
            )
            
            if result is not None:
                queued_predictions.pop(target_game)

# === FONCTION DE MISE À JOUR CORRIGÉE ===
async def update_prediction_status(game_number: int, new_status: str):
    """Met à jour le message de prédiction avec les statuts ✅0️⃣, ✅1️⃣, ✅2️⃣ ou ❌."""
    try:
        if game_number not in pending_predictions:
            logger.warning(f"Tentative de mise à jour pour #{game_number} mais pas dans pending_predictions")
            return False

        pred = pending_predictions[game_number]
        suit = pred['suit']
        
        # Vérifier si on n'a pas déjà ce statut
        current_status = pred.get('status', '')
        if current_status == new_status:
            logger.info(f"Statut {new_status} déjà défini pour #{game_number}")
            return True
        
        # Vérifier si on essaie de mettre un statut final alors qu'on en a déjà un autre
        if current_status in ['✅0️⃣', '✅1️⃣', '✅2️⃣', '❌']:
            logger.info(f"#{game_number} a déjà un statut final ({current_status}), ignoré")
            return True

        # Créer le message mis à jour
        updated_msg = create_result_message(game_number, suit, new_status)
        
        logger.info(f"Mise à jour #{game_number}: {new_status}")

        # Édition des messages privés
        private_msgs = pred.get('private_messages', {})
        if not private_msgs:
            logger.warning(f"Aucun message privé trouvé pour #{game_number}")
        
        for user_id_str, msg_id in private_msgs.items():
            try:
                user_id = int(user_id_str)
                # Éditer pour tout le monde, même les non-abonnés
                await client.edit_message(user_id, msg_id, updated_msg)
                logger.info(f"✏️ Message édité pour {user_id}: {updated_msg}")
            except Exception as e:
                logger.error(f"Erreur édition message pour {user_id_str}: {e}")

        # Mettre à jour le statut
        pred['status'] = new_status
        
        # Mise à jour des statistiques et suppression si statut final
        if new_status in ['✅0️⃣', '✅1️⃣', '✅2️⃣']:
            stats_bilan['total'] += 1
            stats_bilan['wins'] += 1
            stats_bilan['win_details'][new_status] += 1
            logger.info(f"✅ Victoire {new_status} pour #{game_number}, suppression")
            del pending_predictions[game_number]
            asyncio.create_task(check_and_send_queued_predictions(current_game_number))
            
        elif new_status == '❌':
            stats_bilan['total'] += 1
            stats_bilan['losses'] += 1
            stats_bilan['loss_details']['❌'] += 1
            logger.info(f"❌ Défaite pour #{game_number}, suppression")
            del pending_predictions[game_number]
            asyncio.create_task(check_and_send_queued_predictions(current_game_number))

        return True
        
    except Exception as e:
        logger.error(f"Erreur update_prediction_status: {e}")
        import traceback
        logger.error(traceback.format_exc())
        return False

# === FONCTION DE VÉRIFICATION CORRIGÉE ===
async def check_prediction_result(game_number: int, first_group: str, is_finalized: bool = False):
    """
    Vérifie les résultats selon la séquence:
    - Jeu N (prédiction N): ✅0️⃣ si trouvé
    - Jeu N+1 (vérification 1): ✅1️⃣ si trouvé (pour prédiction N)
    - Jeu N+2 (vérification 2): ✅2️⃣ si trouvé (pour prédiction N), sinon ❌
    """
    first_group = normalize_suits(first_group)
    
    logger.debug(f"Vérification pour jeu #{game_number}, finalisé={is_finalized}")
    
    # 1. Vérification pour le jeu N (prédiction actuelle) -> ✅0️⃣
    if game_number in pending_predictions:
        pred = pending_predictions[game_number]
        if pred.get('rattrapage', 0) == 0:
            current_status = pred.get('status', '')
            if current_status in ['✅0️⃣', '✅1️⃣', '✅2️⃣', '❌']:
                return False
            
            target_suit = pred['suit']
            logger.info(f"Vérification ✅0️⃣ pour #{game_number}: cherche {target_suit} dans {first_group}")
            
            if has_suit_in_group(first_group, target_suit):
                logger.info(f"✅0️⃣ TROUVÉ pour #{game_number}!")
                await update_prediction_status(game_number, '✅0️⃣')
                return True
            else:
                if 'check_count' not in pred:
                    pred['check_count'] = 0
                pred['check_count'] = 1
                logger.info(f"❌ #{game_number} pas trouvé (check_count=1)")
                return False
    
    # 2. Vérification pour le jeu N-1 (prédiction précédente) -> ✅1️⃣
    prev_game = game_number - 1
    if prev_game in pending_predictions:
        pred = pending_predictions[prev_game]
        if pred.get('rattrapage', 0) == 0:
            current_status = pred.get('status', '')
            if current_status in ['✅0️⃣', '✅1️⃣', '✅2️⃣', '❌']:
                return False
            
            if pred.get('check_count', 0) == 1:
                target_suit = pred['suit']
                logger.info(f"Vérification ✅1️⃣ pour #{prev_game} (actuel #{game_number}): cherche {target_suit}")
                
                if has_suit_in_group(first_group, target_suit):
                    logger.info(f"✅1️⃣ TROUVÉ pour #{prev_game}!")
                    await update_prediction_status(prev_game, '✅1️⃣')
                    return True
                else:
                    pred['check_count'] = 2
                    logger.info(f"❌ #{prev_game} pas trouvé en N+1 (check_count=2)")
                    return False
    
    # 3. Vérification pour le jeu N-2 (prédiction avant-précédente) -> ✅2️⃣ ou ❌
    prev2_game = game_number - 2
    if prev2_game in pending_predictions:
        pred = pending_predictions[prev2_game]
        if pred.get('rattrapage', 0) == 0:
            current_status = pred.get('status', '')
            if current_status in ['✅0️⃣', '✅1️⃣', '✅2️⃣', '❌']:
                return False
            
            if pred.get('check_count', 0) == 2:
                target_suit = pred['suit']
                logger.info(f"Vérification ✅2️⃣/❌ pour #{prev2_game} (actuel #{game_number}): cherche {target_suit}")
                
                if has_suit_in_group(first_group, target_suit):
                    logger.info(f"✅2️⃣ TROUVÉ pour #{prev2_game}!")
                    await update_prediction_status(prev2_game, '✅2️⃣')
                    return True
                else:
                    if is_finalized:
                        logger.info(f"❌ PERDU pour #{prev2_game}!")
                        await update_prediction_status(prev2_game, '❌')
                        return True
                    else:
                        logger.info(f"⏳ #{prev2_game} en attente de finalisation pour ❌")
                        return False
    
    return False

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
        logger.info(f"Système Central: Écart de {max_diff}, cible faible: {selected_target_suit}")
    else:
        rule2_authorized_suit = None

async def send_bilan():
    if stats_bilan['total'] == 0:
        return

    win_rate = (stats_bilan['wins'] / stats_bilan['total']) * 100
    loss_rate = (stats_bilan['losses'] / stats_bilan['total']) * 100
    
    msg = (
        "📊 **BILAN DES PRÉDICTIONS**\n\n"
        f"✅ Taux de réussite : {win_rate:.1f}%\n"
        f"❌ Taux de perte : {loss_rate:.1f}%\n\n"
        "**Détails :**\n"
        f"✅0️⃣ (Immédiat) : {stats_bilan['win_details']['✅0️⃣']}\n"
        f"✅1️⃣ (1 délai) : {stats_bilan['win_details']['✅1️⃣']}\n"
        f"✅2️⃣ (2 délais) : {stats_bilan['win_details']['✅2️⃣']}\n"
        f"❌ (Perdu) : {stats_bilan['loss_details']['❌']}\n"
        f"\nTotal : {stats_bilan['total']}"
    )
    
    for user_id_str, user_info in users_data.items():
        try:
            user_id = int(user_id_str)
            if can_receive_predictions(user_id):
                await client.send_message(user_id, msg)
        except Exception as e:
            logger.error(f"Erreur envoi bilan à {user_id_str}: {e}")

async def auto_bilan_task():
    global last_bilan_time
    logger.info(f"Démarrage auto_bilan (Intervalle: {bilan_interval} min)")
    while True:
        try:
            await asyncio.sleep(60)
            now = datetime.now()
            next_bilan_time = last_bilan_time + timedelta(minutes=bilan_interval)
            
            if now >= next_bilan_time:
                await send_bilan()
                last_bilan_time = now
        except Exception as e:
            logger.error(f"Erreur auto_bilan_task: {e}")
            await asyncio.sleep(10)

def is_message_finalized(message_text: str) -> bool:
    return "Finalisé" in message_text or "🔰" in message_text or "✅" in message_text

async def process_prediction_logic(message_text: str, chat_id: int):
    global current_game_number, scp_cooldown
    global current_time_cycle_index, next_prediction_allowed_at, already_predicted_games
    global waiting_for_trigger
    
    if chat_id != SOURCE_CHANNEL_ID:
        return
        
    game_number = extract_game_number(message_text)
    if game_number is None:
        return

    current_game_number = game_number
    now = datetime.now()
    
    for pred_game, trigger_game in list(waiting_for_trigger.items()):
        if game_number == trigger_game:
            logger.info(f"🎯 Déclencheur atteint! Canal #{game_number}, prédiction #{pred_game}")
            await execute_prediction(pred_game, game_number)
            del waiting_for_trigger[pred_game]
            return
    
    if now < next_prediction_allowed_at:
        return

    logger.info(f"⏰ Cycle temps écoulé à {now.strftime('%H:%M:%S')}")
    
    target_game = get_next_predictable_number(game_number)
    if not target_game or target_game > 1436:
        return
    
    if target_game in already_predicted_games:
        logger.info(f"Jeu #{target_game} déjà prédit, ignoré.")
        return
    
    trigger_game = get_trigger_number(target_game)
    if not trigger_game:
        return
    
    if game_number >= trigger_game:
        logger.info(f"🚀 Prédiction immédiate: Canal #{game_number} >= déclencheur #{trigger_game}")
        await execute_prediction(target_game, game_number)
    else:
        waiting_for_trigger[target_game] = trigger_game
        logger.info(f"⏳ Attente déclencheur #{trigger_game} pour prédire #{target_game}")
    
    wait_min = TIME_CYCLE[current_time_cycle_index]
    next_prediction_allowed_at = now + timedelta(minutes=wait_min)
    current_time_cycle_index = (current_time_cycle_index + 1) % len(TIME_CYCLE)

async def execute_prediction(target_game: int, base_game: int):
    global already_predicted_games, scp_cooldown
    
    already_predicted_games.add(target_game)
    logger.info(f"Numéro #{target_game} marqué comme prédit")
    
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
    
    final_suit = None
    if rule2_authorized_suit and scp_cooldown <= 0:
        final_suit = rule2_authorized_suit
        scp_cooldown = 1
        logger.info(f"SCP impose {final_suit} pour #{target_game}")
        
        if final_suit != rule1_suit and ADMIN_ID != 0:
            try:
                await client.send_message(ADMIN_ID, f"⚠️ SCP impose {SUIT_DISPLAY.get(final_suit, final_suit)} pour #{target_game}")
            except:
                pass
    elif rule1_suit:
        final_suit = rule1_suit
        if scp_cooldown > 0:
            scp_cooldown = 0
    
    if final_suit:
        prediction_msg = create_beautiful_prediction_message(target_game, final_suit)
        
        pending_predictions[target_game] = {
            'message_id': 0,
            'suit': final_suit,
            'base_game': base_game,
            'status': '⏳',
            'check_count': 0,
            'rattrapage': 0,
            'created_at': datetime.now().isoformat(),
            'private_messages': {}
        }
        
        for user_id_str in users_data.keys():
            try:
                user_id = int(user_id_str)
                if can_receive_predictions(user_id):
                    await send_prediction_to_user(user_id, prediction_msg, target_game)
            except Exception as e:
                logger.error(f"Erreur envoi à {user_id_str}: {e}")
        
        logger.info(f"✅ Prédiction lancée: #{target_game} -> {final_suit}")

async def process_finalized_message(message_text: str, chat_id: int):
    global current_game_number
    try:
        if chat_id == SOURCE_CHANNEL_2_ID:
            await process_stats_message(message_text)
            return

        if not is_message_finalized(message_text):
            return

        game_number = extract_game_number(message_text)
        if game_number is None:
            return

        current_game_number = game_number
        groups = extract_parentheses_groups(message_text)

        if groups:
            await check_prediction_result(game_number, groups[0], is_finalized=True)

    except Exception as e:
        logger.error(f"Erreur Finalisé: {e}")

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
            
            await process_prediction_logic(message_text, chat_id)
            
            game_number = extract_game_number(message_text)
            if game_number:
                groups = extract_parentheses_groups(message_text)
                if groups:
                    await check_prediction_result(game_number, groups[0], is_finalized=False)
            
            if message_text.startswith('/info'):
                active_preds = len(pending_predictions)
                history_text = "\n".join([f"🔹 #{h['game']} ({h['suit']}) à {h['time']}" for h in scp_history]) if scp_history else "Aucune"
                
                info_msg = (
                    "ℹ️ **ÉTAT DU SYSTÈME**\n\n"
                    f"🎮 Jeu actuel: #{current_game_number}\n"
                    f"🔮 Prédictions actives: {active_preds}\n"
                    f"⏳ Cooldown SCP: {'Actif' if scp_cooldown > 0 else 'Prêt'}\n\n"
                    "📌 **DERNIÈRES IMPOSITIONS SCP :**\n"
                    f"{history_text}"
                )
                await event.respond(info_msg)
                return

            if is_message_finalized(message_text):
                await process_finalized_message(message_text, chat_id)
        
        elif chat_id == SOURCE_CHANNEL_2_ID:
            message_text = event.message.message
            await process_stats_message(message_text)
            await check_and_send_queued_predictions(current_game_number)
            
        if sender_id == ADMIN_ID:
            if event.message.message.startswith('/'):
                logger.info(f"Commande admin: {event.message.message}")

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
            await process_prediction_logic(message_text, chat_id)
            
            game_number = extract_game_number(message_text)
            if game_number:
                groups = extract_parentheses_groups(message_text)
                if groups:
                    await check_prediction_result(game_number, groups[0], is_finalized=False)
            
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

# --- Commandes Utilisateur ---

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
                f"🎯 **Bienvenue {user.get('prenom', '')}!**\n\n"
                f"✅ Accès {sub_type} actif.\n"
                f"📅 Expire: {sub_end[:10] if sub_end and user_id != admin_id else sub_end}"
            )
        elif is_trial_active(user_id):
            trial_start = datetime.fromisoformat(user['trial_started'])
            remaining = (trial_start + timedelta(minutes=10) - datetime.now()).seconds // 60
            await event.respond(f"⏰ Essai actif: {remaining} min restantes.")
        else:
            update_user(user_id, {'trial_used': True})
            buttons = [[Button.url("💳 PAYER", PAYMENT_LINK)]]
            await event.respond(
                f"⚠️ **Période d'essai terminée!**\n\n"
                "💰 **200 FCFA** = 24h\n"
                "💰 **1000 FCFA** = 1 semaine\n"
                "💰 **2000 FCFA** = 2 semaines\n\n"
                f"👤 ID: `{user_id}`",
                buttons=buttons
            )
            await asyncio.sleep(2)
            await event.respond("📸 Envoyez une capture d'écran de paiement")
            update_user(user_id, {'pending_payment': True, 'awaiting_screenshot': True})
    else:
        user_conversation_state[user_id] = 'awaiting_nom'
        await event.respond("🎰 **Bienvenue!**\n\n📝 **Quel est votre NOM?**")

@client.on(events.NewMessage())
async def handle_registration_and_payment(event):
    if event.is_group or event.is_channel: return
    if event.message.message and event.message.message.startswith('/'): 
        return
    
    user_id = event.sender_id
    user = get_user(user_id)
    
    # Gestion inscription
    if user_id in user_conversation_state:
        state = user_conversation_state[user_id]
        message_text = event.message.message.strip()
        
        if state == 'awaiting_nom':
            update_user(user_id, {'nom': message_text})
            user_conversation_state[user_id] = 'awaiting_prenom'
            await event.respond(f"✅ Nom: **{message_text}**\n\n📝 **Prénom?**")
            return
        
        elif state == 'awaiting_prenom':
            update_user(user_id, {'prenom': message_text})
            user_conversation_state[user_id] = 'awaiting_pays'
            await event.respond(f"✅ Prénom: **{message_text}**\n\n🌍 **Pays?**")
            return
        
        elif state == 'awaiting_pays':
            update_user(user_id, {
                'pays': message_text,
                'registered': True,
                'trial_started': datetime.now().isoformat(),
                'trial_used': False
            })
            del user_conversation_state[user_id]
            await event.respond(f"🎉 **Inscription terminée!**\n\n⏰ **10 min d'essai GRATUIT!**")
            return
    
    # === NOUVEAU: Gestion capture d'écran avec OCR ===
    if user.get('awaiting_screenshot') and event.message.photo:
        logger.info(f"📸 Photo reçue de {user_id}, lancement OCR...")
        
        # Télécharger l'image
        photo_path = await event.message.download_media(file="/tmp/")
        logger.info(f"Image téléchargée: {photo_path}")
        
        if not OCR_AVAILABLE or ocr_reader is None:
            # Fallback si OCR non disponible
            update_user(user_id, {
                'awaiting_screenshot': False, 
                'awaiting_amount': True,
                'screenshot_path': photo_path
            })
            await event.respond(
                "⚠️ **Analyse automatique indisponible**\n\n"
                "💰 **Quel montant avez-vous payé?**\n"
                "Répondez: `200`, `1000` ou `2000`"
            )
            return
        
        # Analyse OCR
        await event.respond("🔍 **Analyse de votre reçu en cours...**")
        ocr_result = await process_payment_screenshot(photo_path, user_id)
        
        detected_amount = ocr_result.get('amount')
        is_valid = ocr_result.get('is_valid', False)
        detected_time = ocr_result.get('time')
        
        logger.info(f"Résultat OCR pour {user_id}: montant={detected_amount}, valide={is_valid}")
        
        if detected_amount and detected_amount in [200, 1000, 2000]:
            # Montant détecté avec succès
            update_user(user_id, {
                'awaiting_screenshot': False,
                'awaiting_amount': False,
                'detected_amount': detected_amount,
                'screenshot_path': photo_path,
                'payment_time': detected_time,
                'payment_valid': is_valid
            })
            
            # Déterminer la durée
            if detected_amount == 200:
                dur_text = "24 heures"
                dur_code = "1d"
            elif detected_amount == 1000:
                dur_text = "1 semaine"
                dur_code = "1w"
            else:
                dur_text = "2 semaines"
                dur_code = "2w"
            
            # Message de confirmation
            conf_msg = (
                f"✅ **Paiement détecté!**\n\n"
                f"💰 Montant: **{detected_amount} FCFA**\n"
                f"⏰ Heure: {detected_time or 'Non détectée'}\n"
                f"📋 Type: {dur_text}\n\n"
                "⏳ Validation par l'administrateur..."
            )
            await event.respond(conf_msg)
            
            # Envoyer à l'admin
            admin_id = 1190237801
            user_info = get_user(user_id)
            
            msg_admin = (
                "🔔 **NOUVELLE DEMANDE D'ABONNEMENT (OCR)**\n\n"
                f"👤 **Utilisateur:** {user_info.get('nom')} {user_info.get('prenom')}\n"
                f"🆔 **ID:** `{user_id}`\n"
                f"💰 **Montant détecté:** {detected_amount} FCFA\n"
                f"⏰ **Heure détectée:** {detected_time or 'N/A'}\n"
                f"📅 **Type:** {dur_text}\n"
                f"✅ **Validation OCR:** {'Reçu valide' if is_valid else 'À vérifier'}\n"
                f"📍 **Pays:** {user_info.get('pays')}\n\n"
                "Vérifier le paiement:"
            )
            
            buttons = [
                [Button.inline(f"✅ Valider {dur_text}", data=f"valider_{user_id}_{dur_code}")],
                [Button.inline("❌ Rejeter", data=f"rejeter_{user_id}")]
            ]
            
            try:
                # Envoyer l'image à l'admin aussi
                await client.send_file(admin_id, photo_path, caption=msg_admin, buttons=buttons)
                logger.info(f"Notification OCR envoyée à l'admin pour {user_id}")
            except Exception as e:
                logger.error(f"Erreur notification admin: {e}")
                await client.send_message(admin_id, msg_admin, buttons=buttons)
            
            return
            
        else:
            # Montant non détecté, demander manuellement
            update_user(user_id, {
                'awaiting_screenshot': False, 
                'awaiting_amount': True,
                'screenshot_path': photo_path,
                'ocr_text': ocr_result.get('raw_text', '')[:500]
            })
            
            await event.respond(
                "⚠️ **Montant non détecté automatiquement**\n\n"
                "💰 **Quel montant avez-vous payé?**\n"
                "Répondez: `200`, `1000` ou `2000`"
            )
            return
    
    # Fallback: saisie manuelle du montant
    if user.get('awaiting_amount'):
        message_text = event.message.message.strip()
        if message_text in ['200', '1000', '2000']:
            amount = int(message_text)
            update_user(user_id, {'awaiting_amount': False, 'detected_amount': amount})
            
            admin_id = 1190237801
            user_info = get_user(user_id)
            
            dur_text = "24 heures" if amount == 200 else "1 semaine" if amount == 1000 else "2 semaines"
            dur_code = "1d" if amount == 200 else "1w" if amount == 1000 else "2w"

            msg_admin = (
                "🔔 **NOUVELLE DEMANDE D'ABONNEMENT (Manuel)**\n\n"
                f"👤 **Utilisateur:** {user_info.get('nom')} {user_info.get('prenom')}\n"
                f"🆔 **ID:** `{user_id}`\n"
                f"💰 **Montant annoncé:** {amount} FCFA\n"
                f"📅 **Type:** {dur_text}\n"
                f"📍 **Pays:** {user_info.get('pays')}\n\n"
                "Vérifier le paiement:"
            )
            
            buttons = [
                [Button.inline(f"✅ Valider {dur_text}", data=f"valider_{user_id}_{dur_code}")],
                [Button.inline("❌ Rejeter", data=f"rejeter_{user_id}")]
            ]
            
            try:
                if user.get('screenshot_path'):
                    await client.send_file(admin_id, user['screenshot_path'], caption=msg_admin, buttons=buttons)
                else:
                    await client.send_message(admin_id, msg_admin, buttons=buttons)
            except Exception as e:
                logger.error(f"Erreur notification admin: {e}")

            await event.respond("✅ **Demande envoyée !** Validation en cours...")
            return
        else:
            await event.respond("❌ Montant invalide. Répondez: `200`, `1000` ou `2000`")
            return

@client.on(events.CallbackQuery(data=re.compile(b'valider_(\d+)_(.*)')))
async def handle_validation(event):
    admin_id = 1190237801
    if event.sender_id != admin_id:
        await event.answer("Accès refusé", alert=True)
        return
        
    user_id = int(event.data_match.group(1).decode())
    duration = event.data_match.group(2).decode()
    
    if duration == '1d':
        days = 1
    elif duration == '1w':
        days = 7
    else:
        days = 14
    
    end_date = datetime.now() + timedelta(days=days)
    update_user(user_id, {
        'subscription_end': end_date.isoformat(),
        'subscription_type': 'premium',
        'expiry_notified': False
    })
    
    try:
        await client.send_message(user_id, f"🎉 **Abonnement activé!** {days//7 or 1} semaine(s) ✅")
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
        await client.send_message(user_id, "❌ Demande rejetée. Contactez le support.")
    except:
        pass
        
    await event.edit(f"❌ Rejeté pour {user_id}")
    await event.answer("Rejeté")

@client.on(events.NewMessage(pattern='/reset'))
async def cmd_reset_all(event):
    if event.is_group or event.is_channel: return
    admin_id = 1190237801
    if event.sender_id != admin_id:
        await event.respond("❌ Réservé à l'admin.")
        return
    
    global users_data, pending_predictions, queued_predictions, processed_messages
    global current_game_number, last_source_game_number, stats_bilan
    global already_predicted_games, waiting_for_trigger
    
    users_data = {}
    save_users_data()
    
    pending_predictions.clear()
    queued_predictions.clear()
    processed_messages.clear()
    already_predicted_games.clear()
    waiting_for_trigger.clear()
    current_game_number = 0
    last_source_game_number = 0
    
    stats_bilan = {
        'total': 0, 'wins': 0, 'losses': 0,
        'win_details': {'✅0️⃣': 0, '✅1️⃣': 0, '✅2️⃣': 0},
        'loss_details': {'❌': 0}
    }
    
    await event.respond("🚨 **RESET EFFECTUÉ**")

@client.on(events.NewMessage(pattern='/payer'))
async def cmd_payer(event):
    if event.is_group or event.is_channel: return
    
    user_id = event.sender_id
    user = get_user(user_id)
    
    if not user.get('registered'):
        await event.respond("❌ Inscrivez-vous d'abord avec /start")
        return
    
    buttons = [
        [Button.url("💳 24H (200 FCFA)", PAYMENT_LINK_24H)],
        [Button.url("💳 1 SEMAINE (1000 FCFA)", PAYMENT_LINK)],
        [Button.url("💳 2 SEMAINES (2000 FCFA)", PAYMENT_LINK)]
    ]
    await event.respond(
        "💳 **ABONNEMENT**\n\n"
        "Envoyez une capture d'écran après paiement:",
        buttons=buttons
    )
    update_user(user_id, {'pending_payment': True, 'awaiting_screenshot': True})

# --- Serveur Web ---

async def index(request):
    html = f"""<!DOCTYPE html>
<html><head><title>Bot Baccarat</title></head>
<body><h1>🎯 Bot Baccarat</h1>
<p>En ligne - Jeu: #{current_game_number}</p>
<p>OCR: {'✅' if OCR_AVAILABLE else '❌'}</p>
</body></html>"""
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

async def schedule_daily_reset():
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
        global already_predicted_games, waiting_for_trigger
        
        pending_predictions.clear()
        queued_predictions.clear()
        processed_messages.clear()
        already_predicted_games.clear()
        waiting_for_trigger.clear()
        current_game_number = 0
        last_source_game_number = 0
        
        stats_bilan = {
            'total': 0, 'wins': 0, 'losses': 0,
            'win_details': {'✅0️⃣': 0, '✅1️⃣': 0, '✅2️⃣': 0},
            'loss_details': {'❌': 0}
        }

async def start_bot():
    global source_channel_ok
    try:
        logger.info("Démarrage du bot...")
        
        max_retries = 5
        for attempt in range(max_retries):
            try:
                await client.connect()
                if not await client.is_user_authorized():
                    await client.sign_in(bot_token=BOT_TOKEN)
                break
            except Exception as e:
                err_str = str(e).lower()
                if "wait of" in err_str:
                    match = re.search(r"wait of (\d+)", err_str)
                    wait_seconds = int(match.group(1)) + 5 if match else 30
                    logger.warning(f"FloodWait: attente {wait_seconds}s")
                    await asyncio.sleep(wait_seconds)
                else:
                    raise e
        
        source_channel_ok = True
        logger.info("Bot connecté!")
        return True
    except Exception as e:
        logger.error(f"Erreur démarrage: {e}")
        return False

async def main():
    load_users_data()
    try:
        await start_web_server()
        success = await start_bot()
        if not success:
            return

        asyncio.create_task(schedule_daily_reset())
        asyncio.create_task(auto_bilan_task())
        
        logger.info("Bot opérationnel!")
        await client.run_until_disconnected()

    except Exception as e:
        logger.error(f"Erreur main: {e}")
        import traceback
        logger.error(traceback.format_exc())
    finally:
        if client and client.is_connected():
            await client.disconnect()

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Bot arrêté")
    except Exception as e:
        logger.error(f"Erreur fatale: {e}")
        import traceback
        logger.error(traceback.format_exc())
