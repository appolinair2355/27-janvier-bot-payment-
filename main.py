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

# Variables Globales d'État
SUIT_CYCLE = ['♥', '♦', '♣', '♠', '♦', '♥', '♠', '♣']
TIME_CYCLE = [2, 4, 7, 5]
current_time_cycle_index = 0
next_prediction_allowed_at = datetime.now()

def get_rule1_suit(game_number: int) -> str | None:
    # Cette fonction est maintenant simplifiée car la logique de cycle est gérée dans process_prediction_logic
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
scp_history = []  # Historique des impositions SCP
already_predicted_games = set()  # Pour éviter de prédire le même numéro deux fois

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
            return

        sent_msg = await client.send_message(user_id, prediction_msg)
        
        # Stockage de l'ID du message privé pour édition ultérieure
        user_id_str = str(user_id)
        if target_game not in pending_predictions:
            pending_predictions[target_game] = {'private_messages': {}}
        
        if 'private_messages' not in pending_predictions[target_game]:
            pending_predictions[target_game]['private_messages'] = {}
            
        pending_predictions[target_game]['private_messages'][user_id_str] = sent_msg.id
        logger.info(f"Prédiction envoyée en privé à {user_id} (Msg ID: {sent_msg.id})")
    except Exception as e:
        logger.error(f"Erreur envoi prédiction privée à {user_id}: {e}")


# --- FONCTIONS DE FORMATAGE DES MESSAGES (NOUVEAU FORMAT) ---

def get_suit_emoji_big(suit: str) -> str:
    """Retourne un gros emoji pour la couleur."""
    big_emojis = {
        '♠': '♠️ 🖤',
        '♥': '❤️ 🔴',
        '♦': '♦️ 🔴',
        '♣': '♣️ 🖤'
    }
    return big_emojis.get(suit, suit)

def get_suit_name(suit: str) -> str:
    """Nom de la couleur."""
    names = {
        '♠': 'PIQUE',
        '♥': 'COEUR', 
        '♦': 'CARREAU',
        '♣': 'TRÈFLE'
    }
    return names.get(suit, suit)

def generate_prediction_message(target_game: int, predicted_suit: str, status: str = '⏳', is_scp: bool = False) -> str:
    """Génère un message de prédiction attractif avec le nouveau format."""
    
    suit_big = get_suit_emoji_big(predicted_suit)
    suit_name = get_suit_name(predicted_suit)
    
    # Bannières décoratives selon le statut
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

{suit_big}
**{suit_name}**

📊 **STATUT:** {status_text}
🕐 {now}

{sub_text}

━━━━━━━━━━━━━━━━━━━━━
💡 Misez intelligemment
"""
    return msg.strip()


# --- Fonctions d'Analyse ---

def extract_game_number(message: str):
    """Extrait le numéro de jeu du message."""
    # Pattern plus flexible pour #N59 ou #N 59
    match = re.search(r"#N\s*(\d+)", message, re.IGNORECASE)
    if match:
        return int(match.group(1))
    return None

def parse_stats_message(message: str):
    """Extrait les statistiques du canal source 2."""
    stats = {}
    # Pattern pour extraire : ♠️ : 9 (23.7 %)
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
    """Extrait le contenu entre parenthèses, y compris les emojis de cartes."""
    # Pattern pour capturer tout ce qui est entre parenthèses, y compris les caractères spéciaux et emojis
    # On cherche spécifiquement après un nombre (score)
    groups = re.findall(r"\d+\(([^)]*)\)", message)
    return groups

def normalize_suits(group_str: str) -> str:
    """Remplace les différentes variantes de symboles par un format unique (important pour la détection)."""
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
    # Normalisation du symbole cible pour comparaison robuste
    target_normalized = normalize_suits(target_suit)
    
    logger.info(f"DEBUG Vérification: Groupe={normalized}, Cible={target_normalized}")
    
    # On vérifie si l'un des caractères de la cible est présent dans le groupe normalisé
    for char in target_normalized:
        if char in normalized:
            logger.info(f"DEBUG Vérification: MATCH TROUVÉ pour {char}")
            return True
    return False

def get_predicted_suit(missing_suit: str) -> str:
    """Applique le mapping personnalisé (couleur manquante -> couleur prédite)."""
    # Ce mapping est maintenant l'inverse : ♠️<->♣️ et ♥️<->♦️
    # Assurez-vous que SUIT_MAPPING dans config.py contient :
    # SUIT_MAPPING = {'♠': '♣', '♣': '♠', '♥': '♦', '♦': '♥'}
    return SUIT_MAPPING.get(missing_suit, missing_suit)

# --- Logique de Prédiction et File d'Attente ---

async def send_prediction_to_channel(target_game: int, predicted_suit: str, base_game: int, rattrapage=0, original_game=None, is_scp=False):
    """Envoie la prédiction au canal de prédiction et l'ajoute aux prédictions actives."""
    try:
        # Le bot lance une nouvelle prédiction dès que le canal source arrive sur le numéro prédit.
        # On vérifie s'il y a une prédiction principale active pour un numéro futur.
        active_auto_predictions = [p for game, p in pending_predictions.items() if p.get('rattrapage', 0) == 0 and game > current_game_number]
        
        if rattrapage == 0 and len(active_auto_predictions) >= 1:
            logger.info(f"Une prédiction automatique pour un numéro futur est déjà active. En attente pour #{target_game}")
            return None

        # Si c'est un rattrapage, on ne crée pas un nouveau message, on garde la trace
        if rattrapage > 0:
            pending_predictions[target_game] = {
                'message_id': 0, # Pas de message pour le rattrapage lui-même
                'suit': predicted_suit,
                'base_game': base_game,
                'status': '🔮',
                'rattrapage': rattrapage,
                'original_game': original_game,
                'created_at': datetime.now().isoformat()
            }
            logger.info(f"Rattrapage {rattrapage} actif pour #{target_game} (Original #{original_game})")
            return 0

        # NOUVEAU FORMAT: Utiliser generate_prediction_message au lieu du format simple
        prediction_msg = generate_prediction_message(target_game, predicted_suit, '⏳', is_scp)

        # Envoi uniquement aux utilisateurs actifs en chat privé (pas de canal de prédiction)
        for user_id_str, user_info in users_data.items():
            try:
                user_id = int(user_id_str)
                # On envoie seulement à ceux qui ont un abonnement actif ou période d'essai active
                if can_receive_predictions(user_id):
                    logger.info(f"Envoi prédiction privée à {user_id}")
                    await send_prediction_to_user(user_id, prediction_msg, target_game)
                else:
                    # Si l'utilisateur est enregistré mais expiré, envoyer notification de blocage
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
            'status': '⏳',
            'check_count': 0,
            'rattrapage': 0,
            'created_at': datetime.now().isoformat(),
            'is_scp': is_scp  # Stocker si c'est une imposition SCP
        })

        logger.info(f"Prédiction active: Jeu #{target_game} - {predicted_suit}")
        return 0

    except Exception as e:
        logger.error(f"Erreur envoi prédiction: {e}")
        return None

def queue_prediction(target_game: int, predicted_suit: str, base_game: int, rattrapage=0, original_game=None, is_scp=False):
    """Met une prédiction en file d'attente pour un envoi différé."""
    # Vérification d'unicité
    if target_game in queued_predictions or (target_game in pending_predictions and rattrapage == 0):
        return False

    queued_predictions[target_game] = {
        'target_game': target_game,
        'predicted_suit': predicted_suit,
        'base_game': base_game,
        'rattrapage': rattrapage,
        'original_game': original_game,
        'queued_at': datetime.now().isoformat(),
        'is_scp': is_scp
    }
    logger.info(f"📋 Prédiction #{target_game} mise en file d'attente (Rattrapage {rattrapage})")
    return True

async def check_and_send_queued_predictions(current_game: int):
    """Vérifie la file d'attente et envoie les prédictions dès que possible."""
    global current_game_number
    current_game_number = current_game

    sorted_queued = sorted(queued_predictions.keys())

    for target_game in sorted_queued:
        # On envoie si le numéro cible est supérieur au numéro actuel
        if target_game >= current_game:
            pred_data = queued_predictions.get(target_game)
            if not pred_data:
                continue
                
            # Tentative d'envoi
            result = await send_prediction_to_channel(
                pred_data['target_game'],
                pred_data['predicted_suit'],
                pred_data['base_game'],
                pred_data.get('rattrapage', 0),
                pred_data.get('original_game'),
                pred_data.get('is_scp', False)
            )
            
            # Si l'envoi a réussi (ou si c'était un rattrapage qui ne crée pas de msg)
            if result is not None:
                queued_predictions.pop(target_game)

async def update_prediction_status(game_number: int, new_status: str):
    """Met à jour le message de prédiction dans le canal et les statistiques."""
    try:
        if game_number not in pending_predictions:
            return False

        pred = pending_predictions[game_number]
        suit = pred['suit']
        is_scp = pred.get('is_scp', False)  # Récupérer si c'était une imposition SCP

        # NOUVEAU FORMAT: Utiliser generate_prediction_message au lieu du format simple
        updated_msg = generate_prediction_message(game_number, suit, new_status, is_scp)

        # Édition des messages privés au lieu d'en renvoyer
        private_msgs = pred.get('private_messages', {})
        for user_id_str, msg_id in private_msgs.items():
            try:
                user_id = int(user_id_str)
                if can_receive_predictions(user_id):
                    logger.info(f"Édition message pour {user_id}: {new_status}")
                    await client.edit_message(user_id, msg_id, updated_msg)
            except Exception as e:
                logger.error(f"Erreur édition message pour {user_id_str}: {e}")

        pred['status'] = new_status
        
        # Mise à jour des statistiques de bilan
        if new_status in ['✅0️⃣', '✅1️⃣', '✅2️⃣', '✅3️⃣']:
            stats_bilan['total'] += 1
            stats_bilan['wins'] += 1
            stats_bilan['win_details'][new_status if new_status != '✅3️⃣' else '✅2️⃣'] += 1
            # On ne supprime pas immédiatement si on a des prédictions en attente
            del pending_predictions[game_number]
            # Dès qu'une prédiction est terminée, on libère pour la suivante
            asyncio.create_task(check_and_send_queued_predictions(current_game_number))
        elif new_status == '❌':
            stats_bilan['total'] += 1
            stats_bilan['losses'] += 1
            stats_bilan['loss_details']['❌'] += 1
            del pending_predictions[game_number]
            # Dès qu'une prédiction est terminée, on libère pour la suivante
            asyncio.create_task(check_and_send_queued_predictions(current_game_number))

        return True
    except Exception as e:
        logger.error(f"Erreur update_status: {e}")
        return False

async def check_prediction_result(game_number: int, first_group: str):
    """Vérifie les résultats selon la séquence ✅0️⃣, ✅1️⃣, ✅2️⃣ ou ❌."""
    # Nettoyage et normalisation du groupe reçu
    first_group = normalize_suits(first_group)
    
    # On parcourt TOUTES les prédictions en attente pour voir si l'une d'elles doit être vérifiée maintenant
    for target_game, pred in list(pending_predictions.items()):
        # Cas 1 : Prédiction initiale (rattrapage 0) sur le numéro actuel
        if target_game == game_number and pred.get('rattrapage', 0) == 0:
            target_suit = pred['suit']
            if has_suit_in_group(first_group, target_suit):
                await update_prediction_status(game_number, '✅0️⃣')
                return
            else:
                # Échec N, on planifie le rattrapage 1 pour N+1
                next_target = game_number + 1
                is_scp = pred.get('is_scp', False)
                queue_prediction(next_target, target_suit, pred['base_game'], rattrapage=1, original_game=game_number, is_scp=is_scp)
                logger.info(f"Échec # {game_number}, Rattrapage 1 planifié pour #{next_target}")
                return # ARRÊT sur cette prédiction pour ce tour
                
        # Cas 2 : Rattrapage (rattrapage 1 ou 2) sur le numéro actuel
        elif target_game == game_number and pred.get('rattrapage', 0) > 0:
            original_game = pred.get('original_game')
            target_suit = pred['suit']
            rattrapage_actuel = pred['rattrapage']
            
            if has_suit_in_group(first_group, target_suit):
                # Trouvé ! On met à jour le statut du message original
                if original_game is not None:
                    await update_prediction_status(original_game, f'✅{rattrapage_actuel}️⃣')
                # On supprime le rattrapage
                if target_game in pending_predictions:
                    del pending_predictions[target_game]
                return # ARRÊT sur cette prédiction
            else:
                # Échec du rattrapage actuel
                if rattrapage_actuel < 2: 
                    # On planifie le rattrapage suivant (+2)
                    next_rattrapage = rattrapage_actuel + 1
                    next_target = game_number + 1
                    is_scp = pred.get('is_scp', False)
                    queue_prediction(next_target, target_suit, pred['base_game'], rattrapage=next_rattrapage, original_game=original_game, is_scp=is_scp)
                    logger.info(f"Échec rattrapage {rattrapage_actuel} sur #{game_number}, Rattrapage {next_rattrapage} planifié pour #{next_target}")
                else:
                    # Échec final après +2
                    if original_game is not None:
                        await update_prediction_status(original_game, '❌')
                    logger.info(f"Échec final pour la prédiction originale #{original_game} après rattrapage +2")
                
                # Dans tous les cas d'échec de rattrapage, on supprime le rattrapage actuel
                if target_game in pending_predictions:
                    del pending_predictions[target_game]
                return # ARRÊT

async def process_stats_message(message_text: str):
    """Traite les statistiques du canal 2 pour l'imposition du Système Central."""
    global rule2_authorized_suit
    stats = parse_stats_message(message_text)
    if not stats:
        rule2_authorized_suit = None
        return

    # Miroirs : ♠️ <-> ♦️ | ❤️ <-> ♣️
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
                # REGLE CORRIGEE : On prédit le plus FAIBLE parmi les miroirs
                selected_target_suit = s1 if v1 < v2 else s2
                
    if selected_target_suit:
        # Ici rule2_authorized_suit stockera directement le costume à prédire (le plus faible)
        rule2_authorized_suit = selected_target_suit
        logger.info(f"Système Central (Imposition) détecté : Écart de {max_diff} sur miroir. Cible faible : {selected_target_suit}")
    else:
        rule2_authorized_suit = None
        logger.info("Système Central (Imposition) : Aucun écart de 6 détecté sur les miroirs.")

async def send_bilan():
    """Envoie le bilan des prédictions."""
    if stats_bilan['total'] == 0:
        return

    win_rate = (stats_bilan['wins'] / stats_bilan['total']) * 100
    loss_rate = (stats_bilan['losses'] / stats_bilan['total']) * 100
    
    msg = (
        "📊 **BILAN DES PRÉDICTIONS**\n\n"
        f"✅ Taux de réussite : {win_rate:.1f}%\n"
        f"❌ Taux de perte : {loss_rate:.1f}%\n\n"
        "**Détails :**\n"
        f"✅0️⃣ : {stats_bilan['win_details']['✅0️⃣']}\n"
        f"✅1️⃣ : {stats_bilan['win_details']['✅1️⃣']}\n"
        f"✅2️⃣ : {stats_bilan['win_details']['✅2️⃣']}\n"
        f"❌ : {stats_bilan['loss_details']['❌']}\n"
        f"\nTotal prédictions : {stats_bilan['total']}"
    )
    
    # Envoi du bilan aux utilisateurs actifs en chat privé
    for user_id_str, user_info in users_data.items():
        try:
            user_id = int(user_id_str)
            if can_receive_predictions(user_id):
                await client.send_message(user_id, msg)
                logger.info(f"✅ Bilan envoyé à {user_id}")
        except Exception as e:
            logger.error(f"❌ Erreur envoi bilan à {user_id_str}: {e}")

async def auto_bilan_task():
    """Tâche périodique pour envoyer le bilan."""
    global last_bilan_time
    logger.info(f"Démarrage de la tâche auto_bilan (Intervalle: {bilan_interval} minutes)")
    while True:
        try:
            await asyncio.sleep(60) # Vérifie chaque minute
            now = datetime.now()
            next_bilan_time = last_bilan_time + timedelta(minutes=bilan_interval)
            
            if now >= next_bilan_time:
                logger.info("Déclenchement automatique du bilan...")
                await send_bilan()
                last_bilan_time = now
        except Exception as e:
            logger.error(f"Erreur dans auto_bilan_task: {e}")
            await asyncio.sleep(10)

def is_message_finalized(message_text: str) -> bool:
    """Vérifie si le message contient le mot 'Finalisé', 🔰 ou ✅."""
    # Un message finalisé contient 🔰 ou ✅. 
    # S'il contient ⏰, il n'est pas encore finalisé, on doit attendre.
    return "Finalisé" in message_text or "🔰" in message_text or "✅" in message_text

async def process_prediction_logic(message_text: str, chat_id: int):
    """Lance la prédiction selon le cycle de temps."""
    global last_source_game_number, current_game_number, scp_cooldown, current_time_cycle_index, next_prediction_allowed_at, already_predicted_games
    if chat_id != SOURCE_CHANNEL_ID:
        return
        
    game_number = extract_game_number(message_text)
    if game_number is None:
        return

    now = datetime.now()
    if now < next_prediction_allowed_at:
        return

    logger.info(f"Cycle de temps : Déclenchement prédiction à {now.strftime('%H:%M:%S')}")
    
    # Mise à jour du prochain créneau
    wait_min = TIME_CYCLE[current_time_cycle_index]
    next_prediction_allowed_at = now + timedelta(minutes=wait_min)
    current_time_cycle_index = (current_time_cycle_index + 1) % len(TIME_CYCLE)
    logger.info(f"Prochaine prédiction autorisée après {wait_min} min (à {next_prediction_allowed_at.strftime('%H:%M:%S')})")
        
    logger.info(f"Analyse SCP pour le message reçu (Jeu #{game_number})")
    
    # On prédit N+2 : si le canal source est sur 10, on lance le numéro 12
    candidate = game_number + 2
    while candidate % 2 != 0 or candidate % 10 == 0:
        candidate += 1
    next_game = candidate

    if next_game > 1436:
        return
    
    # Vérification anti-doublon : ne pas prédire le même numéro deux fois
    if next_game in already_predicted_games:
        logger.info(f"Jeu #{next_game} déjà prédit, ignoré pour éviter doublon.")
        return
    
    # Marquer ce numéro comme prédit
    already_predicted_games.add(next_game)
    logger.info(f"Numéro #{next_game} marqué comme prédit (évite doublon)")
    
    # 1. Calcul de la Règle 1
    # On utilise le cycle direct car la normalisation est gérée ici par l'attente du #4
    rule1_suit = None
    if next_game:
        count_valid = 0
        for n in range(6, next_game + 1, 2):
            if n % 10 != 0:
                count_valid += 1
        if count_valid > 0:
            index = (count_valid - 1) % 8
            rule1_suit = SUIT_CYCLE[index]
            # Forçage spécifique pour le jeu #6 si demandé
            if next_game == 6:
                rule1_suit = '♥'
    
    # 2. Imposition du Système Central (basé sur les stats du canal 2)
    scp_imposition_suit = None
    is_scp = False  # NOUVEAU: tracker si c'est une imposition SCP
    if rule2_authorized_suit:
        if scp_cooldown <= 0:
            # Le Système Central a déjà identifié le costume le plus FAIBLE
            scp_imposition_suit = rule2_authorized_suit
            is_scp = True
            logger.info(f"SCP : Système Central s'impose sur #{next_game}. Cible faible détectée: {scp_imposition_suit}")
        else:
            logger.info(f"SCP : Imposition en pause (Cooldown: {scp_cooldown})")

    # Logique de décision
    final_suit = None
    if scp_imposition_suit:
        # Le Système Central s'impose s'il y a un écart de 6 entre miroirs
        # On vérifie si on a déjà fait une prédiction règle 1 depuis la dernière imposition
        if scp_cooldown <= 0:
            final_suit = scp_imposition_suit
            logger.info(f"SCP : Système Central s'impose pour #{next_game} -> {final_suit}")
            
            # Enregistrement dans l'historique
            scp_history.append({
                'game': next_game,
                'suit': final_suit,
                'time': datetime.now().strftime('%H:%M:%S'),
                'reason': "Écart détecté"
            })
            if len(scp_history) > 10: scp_history.pop(0)

            # On active le cooldown : le Système Central doit attendre que la Règle 1 soit utilisée
            scp_cooldown = 1
            
            # Comparaison avec la règle 1 pour la notification
            if final_suit == rule1_suit:
                logger.info(f"SCP : L'imposition confirme la Règle 1 ({final_suit}). Pas de notification admin.")
            elif ADMIN_ID != 0 and final_suit:
                try:
                    await client.send_message(ADMIN_ID, f"⚠️ **Imposition SCP**\nLe Système Central impose le costume {SUIT_DISPLAY.get(final_suit, final_suit)} pour le jeu #{next_game} (Règle 1 {SUIT_DISPLAY.get(rule1_suit, rule1_suit) if rule1_suit else 'None'} ignorée).")
                except Exception as e:
                    logger.error(f"Erreur notification imposition: {e}")
        else:
            logger.info(f"SCP : Système Central a déjà imposé récemment. Attente d'une prédiction Règle 1.")
    
    # Règle 1 seulement si le Système Central ne s'est PAS imposé pour cette prédiction
    if not final_suit and rule1_suit:
        final_suit = rule1_suit
        logger.info(f"SCP : Règle 1 sélectionnée pour #{next_game} -> {final_suit}")
        # Une fois la Règle 1 utilisée, on réinitialise le cooldown pour permettre une future imposition
        if scp_cooldown > 0:
            scp_cooldown = 0
            logger.info("SCP : Règle 1 utilisée, le Système Central pourra s'imposer à nouveau.")

    if final_suit:
        queue_prediction(next_game, final_suit, game_number, is_scp=is_scp)  # NOUVEAU: passer is_scp
    else:
        logger.info(f"SCP : Aucune règle applicable pour #{next_game}")

    # Envoi immédiat si possible
    await check_and_send_queued_predictions(game_number)

async def process_finalized_message(message_text: str, chat_id: int):
    """Traite uniquement la vérification des résultats quand le message est finalisé."""
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
        first_group = groups[0] if groups else ""

        # Vérification des résultats (seulement quand finalisé)
        if groups:
            await check_prediction_result(game_number, groups[0])

    except Exception as e:
        logger.error(f"Erreur Finalisé: {e}")

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
            
        if chat_id == SOURCE_CHANNEL_ID:
            message_text = event.message.message
            # Prédiction immédiate sans attendre finalisation
            await process_prediction_logic(message_text, chat_id)
            
            # Commande /info pour l'admin
            if message_text.startswith('/info'):
                active_preds = len(pending_predictions)
                history_text = "\n".join([f"🔹 #{h['game']} ({h['suit']}) à {h['time']}" for h in scp_history]) if scp_history else "Aucune imposition récente."
                
                info_msg = (
                    "ℹ️ **ÉTAT DU SYSTÈME**\n\n"
                    f"🎮 Jeu actuel: #{current_game_number}\n"
                    f"🔮 Prédictions actives: {active_preds}\n"
                    f"⏳ Cooldown SCP: {'Actif' if scp_cooldown > 0 else 'Prêt'}\n\n"
                    "📌 **DERNIÈRES IMPOSITIONS SCP :**\n"
                    f"{history_text}\n\n"
                    "📈 Le bot suit le cycle de la Règle 1 par défaut."
                )
                await event.respond(info_msg)
                return

            # Vérification si finalisé
            if is_message_finalized(message_text):
                await process_finalized_message(message_text, chat_id)
        
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
    """Gère les messages édités dans les canaux sources."""
    try:
        chat = await event.get_chat()
        chat_id = chat.id
        if hasattr(chat, 'broadcast') and chat.broadcast:
            if not str(chat_id).startswith('-100'):
                chat_id = int(f"-100{abs(chat_id)}")

        if chat_id == SOURCE_CHANNEL_ID:
            message_text = event.message.message
            # Relancer prédiction si besoin
            await process_prediction_logic(message_text, chat_id)
            
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
    
    # Vérification si l'utilisateur est l'admin pour lui donner accès direct ou après reset
    admin_id = 1190237801
    
    if user.get('registered'):
        if is_user_subscribed(user_id) or user_id == admin_id:
            sub_type = "Premium (prédictions privées)" if get_subscription_type(user_id) == 'premium' or user_id == admin_id else "Standard"
            sub_end = user.get('subscription_end', 'Illimité' if user_id == admin_id else 'N/A')
            # Si l'utilisateur est abonné, on s'assure que expiry_notified est False pour le futur
            update_user(user_id, {'expiry_notified': False})
            await event.respond(
                f"🎯 **Bienvenue {user.get('prenom', 'Admin' if user_id == admin_id else '')}!**\n\n"
                f"✅ Votre accès {sub_type} est actif.\n"
                f"📅 Expire le: {sub_end[:10] if sub_end and user_id != admin_id else sub_end}\n\n"
                "Les prédictions sont envoyées en temps réel ici même dans votre chat privé. 🚀"
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
                "💰 **1000 FCFA** = 1 semaine (prédictions canal)\n"
                "💰 **2000 FCFA** = 2 semaines (prédictions privées)\n\n"
                f"👤 Votre ID: `{user_id}`\n\n"
                "Cliquez sur le bouton ci-dessous pour payer:",
                buttons=buttons
            )
            await asyncio.sleep(2)
            await event.respond(
                "📸 **Après paiement:**\n"
                "1. Envoyez une capture d'écran de votre paiement\n"
                "2. Indiquez le montant payé (1000 ou 2000)"
            )
            update_user(user_id, {'pending_payment': True, 'awaiting_screenshot': True})
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
    
    # Ignorer si c'est une commande (commence par /)
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
            logger.info(f"Nouvel utilisateur inscrit: {user_id} - {user.get('nom')} {user.get('prenom')} ({user.get('pays')})")
        return
    
    if user.get('awaiting_screenshot') and event.message.photo:
        update_user(user_id, {'awaiting_screenshot': False, 'awaiting_amount': True})
        await event.respond(
            f"📸 **Capture d'écran reçue!**\n\n"
            "💰 **Quel montant avez-vous payé?**\n"
            "Répondez avec: `200`, `1000` ou `2000`"
        )
        logger.info(f"Screenshot reçu de l'utilisateur {user_id}")
        return
    
    if user.get('awaiting_amount'):
        message_text = event.message.message.strip()
        if message_text in ['200', '1000', '2000']:
            amount = message_text
            update_user(user_id, {'awaiting_amount': False})
            
            # Notification admin avec bouton de validation
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
                [
                    Button.inline(f"✅ Valider {dur_text}", data=f"valider_{user_id}_{dur_code}")
                ],
                [Button.inline("❌ Rejeter", data=f"rejeter_{user_id}")]
            ]
            
            try:
                # Envoyer la notification à l'admin
                await client.send_message(admin_id, msg_admin, buttons=buttons)
                logger.info(f"Notification d'abonnement envoyée à l'admin pour {user_id}")
            except Exception as e:
                logger.error(f"Erreur notification admin: {e}")

            await event.respond("✅ **Demande envoyée !**\nL'administrateur va vérifier votre paiement. Vous recevrez une notification dès que votre accès sera activé.")
            return
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
        'expiry_notified': False  # Reset notification pour le nouvel abonnement
    })
    
    # Notifier l'utilisateur
    try:
        notif_msg = (
            f"🎉 **Félicitations !**\n\n"
            f"Votre abonnement de {days//7} semaine(s) est activé avec succès ! ✅\n"
            "Vous verrez maintenant les prédictions automatiques ici dans votre chat privé. 🚀"
        )
        await client.send_message(user_id, notif_msg)
    except Exception as e:
        logger.error(f"Erreur notification user {user_id}: {e}")
        
    await event.edit(f"✅ Abonnement de {days//7} semaine(s) activé pour l'utilisateur {user_id}")
    await event.answer("Abonnement activé !")

@client.on(events.CallbackQuery(data=re.compile(b'rejeter_(\d+)')))
async def handle_rejection(event):
    admin_id = 1190237801
    if event.sender_id != admin_id:
        await event.answer("Accès refusé", alert=True)
        return
        
    user_id = int(event.data_match.group(1).decode())
    
    try:
        await client.send_message(user_id, "❌ Votre demande d'abonnement a été rejetée par l'administrateur. Veuillez contacter le support si vous pensez qu'il s'agit d'une erreur.")
    except:
        pass
        
    await event.edit(f"❌ Demande rejetée pour l'utilisateur {user_id}")
    await event.answer("Demande rejetée")

@client.on(events.NewMessage(pattern=r'^/tim (\d+)$'))
async def cmd_set_tim(event):
    if event.is_group or event.is_channel: return
    admin_id = 1190237801
    if event.sender_id != admin_id: return
    
    global bilan_interval
    try:
        bilan_interval = int(event.pattern_match.group(1))
        await event.respond(f"✅ Intervalle de bilan mis à jour : {bilan_interval} minutes\nProchain bilan automatique dans environ {bilan_interval} minutes.")
        logger.info(f"Intervalle de bilan modifié à {bilan_interval} min par l'admin.")
    except Exception as e:
        await event.respond(f"❌ Erreur: {e}")

@client.on(events.NewMessage(pattern='/bilan'))
async def cmd_bilan(event):
    if event.is_group or event.is_channel: return
    admin_id = 1190237801
    if event.sender_id != admin_id: return
    await send_bilan()
    await event.respond("✅ Bilan manuel envoyé au canal.")

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

@client.on(events.NewMessage(pattern='/info'))
async def cmd_info(event):
    if event.is_group or event.is_channel: return
    
    active_preds = len(pending_predictions)
    history_text = "\n".join([f"🔹 #{h['game']} ({h['suit']}) à {h['time']}" for h in scp_history]) if scp_history else "Aucune imposition récente."
    
    info_msg = (
        "ℹ️ **ÉTAT DU SYSTÈME**\n\n"
        f"🎮 Jeu actuel: #{current_game_number}\n"
        f"🔮 Prédictions actives: {active_preds}\n"
        f"⏳ Cooldown SCP: {'Actif' if scp_cooldown > 0 else 'Prêt'}\n\n"
        "📌 **DERNIÈRES IMPOSITIONS SCP :**\n"
        f"{history_text}\n\n"
        "📈 Le bot suit le cycle de la Règle 1 par défaut."
    )
    await event.respond(info_msg)

@client.on(events.NewMessage(pattern='/status'))
async def cmd_status(event):
    if event.is_group or event.is_channel: return
    if event.sender_id != ADMIN_ID and ADMIN_ID != 0:
        await event.respond("Commande réservée à l'administrateur")
        return

    status_msg = f"📊 **État du Bot:**\n\n"
    status_msg += f"🎮 Jeu actuel (Source 1): #{current_game_number}\n\n"
    
    if pending_predictions:
        status_msg += f"**🔮 Actives ({len(pending_predictions)}):**\n"
        for game_num, pred in sorted(pending_predictions.items()):
            distance = game_num - current_game_number
            ratt = f" (R{pred['rattrapage']})" if pred.get('rattrapage', 0) > 0 else ""
            status_msg += f"• #{game_num}{ratt}: {pred['suit']} - {pred['status']} (dans {distance})\n"
    else: status_msg += "**🔮 Aucune prédiction active**\n"

    await event.respond(status_msg)

@client.on(events.NewMessage(pattern='/reset'))
async def cmd_reset_all(event):
    if event.is_group or event.is_channel: return
    admin_id = 1190237801
    if event.sender_id != admin_id:
        await event.respond("❌ Commande réservée à l'administrateur principal.")
        return
    
    global users_data, pending_predictions, queued_predictions, processed_messages, current_game_number, last_source_game_number, stats_bilan, current_time_cycle_index, next_prediction_allowed_at, already_predicted_games
    
    # Réinitialisation des données utilisateurs (efface tous les IDs et abonnements)
    users_data = {}
    save_users_data()
    
    # Réinitialisation des prédictions, stats et cycles
    pending_predictions.clear()
    queued_predictions.clear()
    processed_messages.clear()
    already_predicted_games.clear()
    current_game_number = 0
    last_source_game_number = 0
    current_time_cycle_index = 0
    next_prediction_allowed_at = datetime.now()
    stats_bilan = {
        'total': 0,
        'wins': 0,
        'losses': 0,
        'win_details': {'✅0️⃣': 0, '✅1️⃣': 0, '✅2️⃣': 0},
        'loss_details': {'❌': 0}
    }
    
    logger.warning(f"🚨 RESET TOTAL effectué par l'admin {event.sender_id}")
    await event.respond("🚨 **RÉINITIALISATION TOTALE EFFECTUÉE** 🚨\n\n- Tous les comptes et abonnements ont été supprimés.\n- Même l'administrateur doit se réinscrire et valider son accès pour voir les prédictions.\n- Les statistiques et cycles ont été remis à zéro.")

@client.on(events.NewMessage(pattern='/dif'))
async def cmd_dif(event):
    if event.is_group or event.is_channel: return
    admin_id = 1190237801
    if event.sender_id != admin_id:
        await event.respond("❌ Commande réservée à l'administrateur principal.")
        return
    
    # Extraction du message après /dif
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

@client.on(events.NewMessage(pattern='/help'))
async def cmd_help(event):
    if event.is_group or event.is_channel: return
    await event.respond("""📖 **Aide - Bot de Prédiction Baccarat**

**🎯 Comment ça marche:**
1. Inscrivez-vous avec /start
2. Profitez de 10 minutes d'essai gratuit
3. Abonnez-vous pour continuer

**💰 Tarifs:**
- 1000 FCFA = 1 semaine (prédictions en privé)
- 2000 FCFA = 2 semaines (prédictions en privé)

**📝 Commandes:**
- `/start` - Démarrer / État de l'abonnement
- `/payer` - S'abonner ou renouveler
- `/help` - Cette aide
- `/info` - Informations système
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
        "💰 **200 FCFA** = 24 heures (privé)\n"
        "💰 **1000 FCFA** = 1 semaine (privé)\n"
        "💰 **2000 FCFA** = 2 semaines (privé)\n\n"
        f"👤 Votre ID: `{user_id}`\n\n"
        "Choisissez votre durée et payez via les liens ci-dessous :",
        buttons=buttons
    )
    await asyncio.sleep(2)
    await event.respond(
        "📸 **Après paiement:**\n"
        "1. Envoyez une capture d'écran de votre paiement\n"
        "2. Indiquez le montant payé (200, 1000 ou 2000)"
    )
    update_user(user_id, {'pending_payment': True, 'awaiting_screenshot': True})


# --- Serveur Web et Démarrage ---

async def index(request):
    html = f"""<!DOCTYPE html><html><head><title>Bot Prédiction Baccarat</title></head><body><h1>🎯 Bot de Prédiction Baccarat</h1><p>Le bot est en ligne et surveille les canaux.</p><p><strong>Jeu actuel:</strong> #{current_game_number}</p></body></html>"""
    return web.Response(text=html, content_type='text/html', status=200)

async def health_check(request):
    return web.Response(text="OK", status=200)

async def start_web_server():
    """Démarre le serveur web pour la vérification de l'état (health check)."""
    app = web.Application()
    app.router.add_get('/', index)
    app.router.add_get('/health', health_check)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', PORT)
    await site.start() 

async def schedule_daily_reset():
    """Tâche planifiée pour la réinitialisation quotidienne des stocks de prédiction à 00h59 WAT."""
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
        
        global pending_predictions, queued_predictions, processed_messages, last_transferred_game, current_game_number, last_source_game_number, stats_bilan, already_predicted_games
        
        pending_predictions.clear()
        queued_predictions.clear()
        processed_messages.clear()
        already_predicted_games.clear()
        last_transferred_game = None
        current_game_number = 0
        last_source_game_number = 0
        
                # Reset des statistiques de bilan aussi au reset quotidien
        stats_bilan = {
            'total': 0,
            'wins': 0,
            'losses': 0,
            'win_details': {'✅0️⃣': 0, '✅1️⃣': 0, '✅2️⃣': 0},
            'loss_details': {'❌': 0}
        }
        
        logger.warning("✅ Toutes les données de prédiction ont été effacées.")

async def start_bot():
    """Démarre le client Telegram et les vérifications initiales."""
    global source_channel_ok
    try:
        logger.info("Démarrage du bot...")
        
        # Tentative de connexion avec retry pour gérer les FloodWait
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
                    logger.warning(f"FloodWait détecté: Attente de {wait_seconds} secondes (Essai {attempt + 1}/{max_retries})")
                    await asyncio.sleep(wait_seconds)
                else:
                    raise e
        
        source_channel_ok = True
        logger.info("Bot connecté et prêt pour les chats privés.")
        return True
    except Exception as e:
        logger.error(f"Erreur démarrage du client Telegram: {e}")
        return False

async def main():
    """Fonction principale pour lancer le serveur web, le bot et la tâche de reset."""
    load_users_data()
    try:
        await start_web_server()

        success = await start_bot()
        if not success:
            logger.error("Échec du démarrage du bot")
            return

        # Lancement des tâches en arrière-plan
        asyncio.create_task(schedule_daily_reset())
        asyncio.create_task(auto_bilan_task())
        
        logger.info("Bot complètement opérationnel - En attente de messages...")
        await client.run_until_disconnected()

    except Exception as e:
        logger.error(f"Erreur dans main: {e}")
        import traceback
        logger.error(traceback.format_exc())
    finally:
        if client and client.is_connected():
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
