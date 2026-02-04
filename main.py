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

PAYMENT_LINK_24H = "https://my.moneyfusion.net/6977f7502181d4ebf722398d"
PAYMENT_LINK_1W = "https://my.moneyfusion.net/6977f7502181d4ebf722398d"
PAYMENT_LINK_2W = "https://my.moneyfusion.net/6977f7502181d4ebf722398d"
USERS_FILE = "users_data.json"

ADMIN_NAME = "Sossou Kouamé"
ADMIN_TITLE = "Administrateur et développeur de ce Bot"

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s', handlers=[logging.StreamHandler(sys.stdout)])
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

session_string = os.getenv('TELEGRAM_SESSION', '')
client = TelegramClient(StringSession(session_string), API_ID, API_HASH)

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

last_known_source_game = 0
next_rule1_prediction = None  # {'target_game': 18, 'suit': '♥', 'wait_min': 5, 'base_game': 12}

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

MIN_GAME = 6
MAX_GAME = 1436

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
        users_data[user_id_str] = {'registered': False, 'nom': None, 'prenom': None, 'pays': None, 'trial_started': None, 'trial_used': False, 'subscription_end': None, 'subscription_type': None, 'pending_payment': False, 'awaiting_screenshot': False, 'payment_amount': None}
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

def is_valid_game(n: int) -> bool:
    """Valide si: 6-1436, pair, finit par 2/4/6/8"""
    return MIN_GAME <= n <= MAX_GAME and n % 2 == 0 and n % 10 != 0

def get_next_valid(n: int) -> int:
    """Prochain numéro valide après n"""
    candidate = n + 1
    while candidate <= MAX_GAME:
        if is_valid_game(candidate):
            return candidate
        candidate += 1
    return MAX_GAME

def count_valid_up_to(n: int) -> int:
    """Compte les numéros valides de 6 à n"""
    count = 0
    for i in range(MIN_GAME, min(n + 1, MAX_GAME + 1)):
        if is_valid_game(i):
            count += 1
    return count

def get_suit(n: int) -> str:
    """Costume basé sur le rang du numéro valide"""
    count = count_valid_up_to(n)
    return SUIT_CYCLE[(count - 1) % 8] if count > 0 else '♥'

def calc_next_prediction(base: int, wait: int) -> tuple:
    """Calcule: base + wait = prochain valide"""
    target = base + wait
    while not is_valid_game(target) and target <= MAX_GAME:
        target += 1
    return target, get_suit(target)

async def send_to_all(msg: str, game: int):
    """Envoie à tous les éligibles"""
    sent = {}
    # Admin
    try:
        if ADMIN_ID:
            m = await client.send_message(ADMIN_ID, msg)
            sent[str(ADMIN_ID)] = m.id
    except Exception as e:
        logger.error(f"Erreur admin: {e}")
    
    # Utilisateurs
    for uid_str in users_data:
        try:
            uid = int(uid_str)
            if uid == ADMIN_ID or not can_receive_predictions(uid):
                continue
            m = await client.send_message(uid, msg)
            sent[uid_str] = m.id
        except Exception as e:
            logger.error(f"Erreur user {uid_str}: {e}")
    
    return sent

async def send_prediction(target: int, suit: str, base: int, rattrapage=0, orig=None, rule="R2"):
    """Envoie une prédiction avec signature de la suivante"""
    global rule2_active, rule1_consecutive_count, rule2_predicted_games, next_rule1_prediction, current_time_cycle_index
    
    try:
        # Mode rattrapage
        if rattrapage > 0:
            orig_msgs = pending_predictions.get(orig, {}).get('private_messages', {}).copy() if orig else {}
            pending_predictions[target] = {
                'suit': suit, 'base': base, 'status': '🔮', 'rattrapage': rattrapage,
                'orig': orig, 'rule': rule, 'msgs': orig_msgs, 'time': datetime.now().isoformat()
            }
            if rule == "R2":
                rule2_active = True
                rule2_predicted_games.add(target)
            return True
        
        # Vérif blocage R2
        if rule == "R1":
            if target in rule2_predicted_games:
                logger.info(f"🚫 #{target} bloqué par R2")
                return False
        
        # Calcule prochaine pour signature
        next_idx = (current_time_cycle_index + 1) % len(TIME_CYCLE)
        next_wait = TIME_CYCLE[next_idx]
        next_target, next_suit = calc_next_prediction(target, next_wait)
        
        # Évite blocage R2 pour la signature
        attempts = 0
        while next_target in rule2_predicted_games and attempts < 10:
            next_idx = (next_idx + 1) % len(TIME_CYCLE)
            next_wait = TIME_CYCLE[next_idx]
            next_target, next_suit = calc_next_prediction(next_target, next_wait)
            attempts += 1
        
        # Message simple
        algo = "R2" if rule == "R2" else "R1"
        msg = f"""🎰 **#{target}** → {SUIT_DISPLAY.get(suit, suit)}

🔮 Suivante: #{next_target} ({SUIT_DISPLAY.get(next_suit, next_suit)}) dans {next_wait}min | {algo}"""
        
        msgs = await send_to_all(msg, target)
        if not msgs:
            return False
        
        # Stocke prochaine
        next_rule1_prediction = {
            'target': next_target, 'suit': next_suit, 'wait': next_wait,
            'base': target, 'idx': next_idx
        }
        
        # Stocke prédiction
        pending_predictions[target] = {
            'suit': suit, 'base': base, 'status': '⌛', 'rattrapage': 0,
            'rule': rule, 'msgs': msgs, 'time': datetime.now().isoformat()
        }
        
        if rule == "R2":
            rule2_active = True
            rule2_predicted_games.add(target)
            rule1_consecutive_count = 0
            logger.info(f"🔥 R2: #{target}, prochaine: #{next_target}")
        else:
            rule1_consecutive_count += 1
            logger.info(f"⏱️ R1: #{target}, prochaine: #{next_target}")
        
        return True
        
    except Exception as e:
        logger.error(f"Erreur envoi: {e}")
        return False

def queue_pred(target: int, suit: str, base: int, rattrapage=0, orig=None, rule="R2"):
    """Met en file d'attente"""
    global rule2_active, rule2_predicted_games
    
    if rule == "R2":
        rule2_active = True
        rule2_predicted_games.add(target)
    
    if target in queued_predictions or (target in pending_predictions and rattrapage == 0):
        return False
    
    queued_predictions[target] = {
        'target': target, 'suit': suit, 'base': base,
        'rattrapage': rattrapage, 'orig': orig, 'rule': rule
    }
    return True

async def check_queue(current: int):
    """Envoie les prédictions en attente"""
    global current_game_number
    current_game_number = current
    
    for target in sorted(list(queued_predictions.keys())):
        if target >= current:
            p = queued_predictions.pop(target)
            await send_prediction(p['target'], p['suit'], p['base'], 
                                p.get('rattrapage', 0), p.get('orig'), p.get('rule', 'R2'))

async def update_status(game: int, status: str):
    """Met à jour le statut d'une prédiction"""
    global rule2_active, rule1_consecutive_count
    
    if game not in pending_predictions:
        return False
    
    p = pending_predictions[game]
    suit = p['suit']
    rule = p.get('rule', 'R2')
    r = p.get('rattrapage', 0)
    orig = p.get('orig', game)
    
    # Texte statut
    texts = {
        '✅0️⃣': '✅ GAGNÉ!', '✅1️⃣': '✅ Gagné (2ème)', '✅2️⃣': '✅ Gagné (3ème)',
        '❌': '❌ Perdu', '⏳ R1': '⏳ Rattrapage 1...', '⏳ R2': '⏳ Rattrapage 2...'
    }
    txt = texts.get(status, f'⏳ {status}')
    
    # Nouveau message
    algo = "R2" if rule == "R2" else "R1"
    new_msg = f"""🎰 **#{orig}** → {SUIT_DISPLAY.get(suit, suit)}

📊 {txt} | {algo}"""
    
    # Édite les messages
    for uid_str, mid in list(p.get('msgs', {}).items()):
        try:
            await client.edit_message(int(uid_str), mid, new_msg)
        except:
            pass
    
    p['status'] = status
    
    # Victoire ou défaite
    if status in ['✅0️⃣', '✅1️⃣', '✅2️⃣']:
        stats_bilan['total'] += 1
        stats_bilan['wins'] += 1
        stats_bilan['win_details'][status] = stats_bilan['win_details'].get(status, 0) + 1
        
        if rule == "R2" and r == 0:
            rule2_active = False
        elif rule == "R1":
            rule1_consecutive_count = 0
        
        if game in pending_predictions:
            del pending_predictions[game]
        asyncio.create_task(check_queue(current_game_number))
        
    elif status == '❌':
        stats_bilan['total'] += 1
        stats_bilan['losses'] += 1
        
        if rule == "R2" and r == 0:
            rule2_active = False
        elif rule == "R1":
            rule1_consecutive_count = 0
        
        if game in pending_predictions:
            del pending_predictions[game]
        asyncio.create_task(check_queue(current_game_number))
    
    return True

def extract_game(text: str):
    """Extrait le numéro de jeu"""
    m = re.search(r"#N\s*(\d+)", text, re.I)
    if m:
        n = int(m.group(1))
        return n if is_valid_game(n) else None
    return None

def parse_stats(text: str):
    """Parse les statistiques"""
    stats = {}
    for suit, pat in [('♠', r'♠️?\s*:\s*(\d+)'), ('♥', r'♥️?\s*:\s*(\d+)'), 
                      ('♦', r'♦️?\s*:\s*(\d+)'), ('♣', r'♣️?\s*:\s*(\d+)')]:
        m = re.search(pat, text)
        if m:
            stats[suit] = int(m.group(1))
    return stats

def has_suit(group: str, suit: str) -> bool:
    """Vérifie si le costume est dans le groupe"""
    g = group.replace('❤️', '♥').replace('❤', '♥').replace('♥️', '♥')
    g = g.replace('♠️', '♠').replace('♦️', '♦').replace('♣️', '♣')
    return suit in g

async def check_result(game: int, group: str):
    """Vérifie le résultat d'une prédiction"""
    if game in pending_predictions:
        p = pending_predictions[game]
        if p.get('rattrapage', 0) == 0:
            suit = p['suit']
            rule = p.get('rule', 'R2')
            
            if has_suit(group, suit):
                await update_status(game, '✅0️⃣')
                return
            else:
                # Rattrapage 1
                nxt = get_next_valid(game)
                if queue_pred(nxt, suit, p['base'], 1, game, rule):
                    await update_status(game, '⏳ R1')
                return
    
    # Vérifie rattrapages
    for g, p in list(pending_predictions.items()):
        if g == game and p.get('rattrapage', 0) > 0:
            orig = p.get('orig', g - p['rattrapage'])
            suit = p['suit']
            r = p['rattrapage']
            rule = p.get('rule', 'R2')
            
            if has_suit(group, suit):
                await update_status(orig, f'✅{r}️⃣')
                if g != orig and g in pending_predictions:
                    del pending_predictions[g]
                return
            else:
                if r < 3:
                    nxt = get_next_valid(game)
                    if queue_pred(nxt, suit, p['base'], r+1, orig, rule):
                        await update_status(orig, f'⏳ R{r+1}')
                    if g in pending_predictions:
                        del pending_predictions[g]
                else:
                    await update_status(orig, '❌')
                    if g != orig and g in pending_predictions:
                        del pending_predictions[g]

async def process_stats(text: str):
    """Traite les stats (Règle 2)"""
    global last_source_game_number, suit_prediction_counts, rule2_active, rule2_predicted_games
    
    stats = parse_stats(text)
    if not stats:
        return False
    
    for s1, s2 in [('♦', '♠'), ('♥', '♣')]:
        if s1 in stats and s2 in stats:
            diff = abs(stats[s1] - stats[s2])
            if diff >= 10:
                suit = s1 if stats[s1] < stats[s2] else s2
                
                if suit_prediction_counts.get(suit, 0) >= 3:
                    continue
                
                if last_source_game_number > 0:
                    target = last_source_game_number + USER_A
                    if not is_valid_game(target):
                        target = get_next_valid(last_source_game_number + USER_A - 1)
                    
                    global rule1_consecutive_count
                    rule1_consecutive_count = 0
                    rule2_predicted_games.add(target)
                    
                    if queue_pred(target, suit, last_source_game_number, rule="R2"):
                        suit_prediction_counts[suit] = suit_prediction_counts.get(suit, 0) + 1
                        for s in ALL_SUITS:
                            if s != suit:
                                suit_prediction_counts[s] = 0
                        rule2_active = True
                        logger.info(f"🔥 R2 queue: #{target}")
                        return True
    return False

async def process_rule1(text: str, chat_id: int):
    """Traite Règle 1 - lance quand impair reçu"""
    global last_known_source_game, next_rule1_prediction, current_time_cycle_index, rule1_consecutive_count
    
    if chat_id != SOURCE_CHANNEL_ID:
        return
    
    game = extract_game(text)
    if not game:
        # Vérifie si c'est un impair (pour déclencher)
        m = re.search(r"#N\s*(\d+)", text, re.I)
        if m:
            n = int(m.group(1))
            if n % 2 == 1:  # Impair
                last_known_source_game = n
                logger.info(f"📥 Impair reçu: #{n}")
                
                # Vérifie si c'est le déclencheur
                if next_rule1_prediction and next_rule1_prediction['target'] == n + 1:
                    if (n + 1) in rule2_predicted_games:
                        logger.info(f"🚫 #{n+1} pris par R2")
                        next_rule1_prediction = None
                        return
                    
                    # Envoie la prédiction promise
                    p = next_rule1_prediction
                    logger.info(f"🎯 Déclenché par #{n}: envoi #{p['target']}")
                    
                    if await send_prediction(p['target'], p['suit'], p['base'], rule="R1"):
                        current_time_cycle_index = p['idx']
                    
                    next_rule1_prediction = None
                else:
                    # Crée nouvelle promesse si pas de R2
                    if not rule2_active and rule1_consecutive_count < MAX_RULE1_CONSECUTIVE and not next_rule1_prediction:
                        wait = TIME_CYCLE[current_time_cycle_index]
                        target, suit = calc_next_prediction(n, wait)
                        
                        # Évite R2
                        while target in rule2_predicted_games:
                            current_time_cycle_index = (current_time_cycle_index + 1) % len(TIME_CYCLE)
                            wait = TIME_CYCLE[current_time_cycle_index]
                            target, suit = calc_next_prediction(target, wait)
                        
                        idx = (current_time_cycle_index + 1) % len(TIME_CYCLE)
                        next_rule1_prediction = {
                            'target': target, 'suit': suit, 'wait': wait,
                            'base': n, 'idx': idx
                        }
                        logger.info(f"📝 Promesse: #{target} dans {wait}min (base #{n})")
        return
    
    # Numéro valide reçu
    last_known_source_game = game

def is_finalized(text: str) -> bool:
    return '✅' in text or '🔰' in text or '▶️' in text or 'Finalisé' in text

async def process_finalized(text: str, chat_id: int):
    """Traite les résultats finalisés"""
    global current_game_number, last_source_game_number
    
    if chat_id == SOURCE_CHANNEL_2_ID:
        await process_stats(text)
        await check_queue(current_game_number)
        return
    
    if not is_finalized(text):
        return
    
    game = extract_game(text)
    if not game:
        return
    
    current_game_number = game
    last_source_game_number = game
    
    h = f"{game}_{text[:50]}"
    if h in processed_messages:
        return
    processed_messages.add(h)
    
    # Extrait groupe
    groups = re.findall(r"\(([^)]*)\)", text)
    if groups:
        await check_result(game, groups[0])
        await check_queue(game)

async def handle_msg(event):
    """Gestionnaire principal"""
    global last_known_source_game, current_game_number
    
    try:
        chat = await event.get_chat()
        chat_id = chat.id
        if getattr(chat, 'broadcast', False) and not str(chat_id).startswith('-100'):
            chat_id = int(f"-100{abs(chat_id)}")
        
        text = event.message.message
        
        if chat_id == SOURCE_CHANNEL_ID:
            # Met à jour last_known même si pas valide (pour les impairs)
            m = re.search(r"#N\s*(\d+)", text, re.I)
            if m:
                n = int(m.group(1))
                if is_valid_game(n):
                    last_known_source_game = n
            
            await process_rule1(text, chat_id)
            
            if is_finalized(text):
                g = extract_game(text)
                if g:
                    current_game_number = g
                await process_finalized(text, chat_id)
        
        elif chat_id == SOURCE_CHANNEL_2_ID:
            await process_stats(text)
            await check_queue(current_game_number)
            
    except Exception as e:
        logger.error(f"Erreur: {e}")

async def handle_edit(event):
    """Gestionnaire éditions"""
    try:
        chat = await event.get_chat()
        chat_id = chat.id
        if getattr(chat, 'broadcast', False) and not str(chat_id).startswith('-100'):
            chat_id = int(f"-100{abs(chat_id)}")
        
        text = event.message.message
        
        if chat_id == SOURCE_CHANNEL_ID:
            m = re.search(r"#N\s*(\d+)", text, re.I)
            if m:
                n = int(m.group(1))
                if is_valid_game(n):
                    global last_known_source_game
                    last_known_source_game = n
            
            await process_rule1(text, chat_id)
            
            if is_finalized(text):
                await process_finalized(text, chat_id)
        
        elif chat_id == SOURCE_CHANNEL_2_ID:
            await process_stats(text)
            await check_queue(current_game_number)
            
    except Exception as e:
        logger.error(f"Erreur edit: {e}")

client.add_event_handler(handle_msg, events.NewMessage())
client.add_event_handler(handle_edit, events.MessageEdited())

# ============ COMMANDES ============

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
    
    # Admin message
    if uid in admin_message_state:
        state = admin_message_state[uid]
        if state.get('step') == 'msg':
            try:
                await client.send_message(state['target'], f"📨 **{ADMIN_NAME}**\n\n{event.message.message}")
                await event.respond("✅ Envoyé!")
            except Exception as e:
                await event.respond(f"❌ Erreur: {e}")
            del admin_message_state[uid]
            return
    
    # Admin predict
    if uid in admin_predict_state:
        state = admin_predict_state[uid]
        if state.get('step') == 'nums':
            nums = [int(n) for n in re.findall(r'\d+', event.message.message) if is_valid_game(int(n))]
            if not nums:
                await event.respond("❌ Aucun numéro valide.")
                return
            
            sent = 0
            details = []
            for n in nums:
                suit = get_suit(n)
                if await send_prediction(n, suit, last_known_source_game, rule="R1"):
                    sent += 1
                    details.append(f"#{n} {SUIT_DISPLAY.get(suit, suit)}")
            
            await event.respond(f"✅ **{sent} envoyées**\n\n" + "\n".join(details[:20]))
            del admin_predict_state[uid]
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

async def check_timeout(uid: int):
    await asyncio.sleep(600)
    if uid in pending_screenshots and not pending_screenshots[uid].get('notified'):
        user = get_user(uid)
        if not is_user_subscribed(uid):
            try:
                await client.send_message(uid, f"⏰ **Patience**\n\n{ADMIN_NAME} est occupé. Merci d'attendre 🙏")
                pending_screenshots[uid]['notified'] = True
            except:
                pass

@client.on(events.NewMessage(pattern='/predict'))
async def cmd_predict(event):
    if event.is_group or event.is_channel or event.sender_id != ADMIN_ID:
        return
    
    if last_known_source_game <= 0:
        await event.respond("⚠️ Non synchronisé.")
        return
    
    admin_predict_state[event.sender_id] = {'step': 'nums'}
    
    info = f"Prochaine: #{next_rule1_prediction['target']}" if next_rule1_prediction else "En attente..."
    await event.respond(f"""🎯 **PRÉDICTION MANUELLE**

📍 Source: #{last_known_source_game}
📅 {info}

Entrez numéros ({MIN_GAME}-{MAX_GAME}, finissant par 2/4/6/8):""")

@client.on(events.NewMessage(pattern='/status'))
async def cmd_status(event):
    if event.is_group or event.is_channel or event.sender_id != ADMIN_ID:
        return
    
    info = f"#{next_rule1_prediction['target']}" if next_rule1_prediction else "Aucune"
    
    await event.respond(f"""📊 **STATUT**

🎮 Source: #{last_known_source_game}
⏳ R2: {'🔥' if rule2_active else 'Off'}
⏱️ R1: {rule1_consecutive_count}/{MAX_RULE1_CONSECUTIVE}
🎯 Cycle: {current_time_cycle_index} ({TIME_CYCLE[current_time_cycle_index]}min)
📅 Prochaine: {info}
👥 Users: {len(users_data)} | Éligibles: {sum(1 for u in users_data if can_receive_predictions(int(u)))}
🔒 Bloqués R2: {len(rule2_predicted_games)}
📋 Actives: {len(pending_predictions)}""")

@client.on(events.NewMessage(pattern='/reset'))
async def cmd_reset(event):
    if event.is_group or event.is_channel or event.sender_id != ADMIN_ID:
        return
    
    global users_data, pending_predictions, queued_predictions, processed_messages
    global current_game_number, last_source_game_number, stats_bilan
    global rule1_consecutive_count, rule2_active, suit_prediction_counts
    global last_known_source_game, current_time_cycle_index
    global pending_screenshots, rule2_predicted_games, next_rule1_prediction
    
    users_data = {}
    save_users_data()
    pending_predictions.clear()
    queued_predictions.clear()
    processed_messages.clear()
    suit_prediction_counts.clear()
    pending_screenshots.clear()
    rule2_predicted_games.clear()
    
    current_game_number = 0
    last_source_game_number = 0
    last_known_source_game = 0
    current_time_cycle_index = 0
    next_rule1_prediction = None
    
    rule1_consecutive_count = 0
    rule2_active = False
    
    stats_bilan = {'total': 0, 'wins': 0, 'losses': 0, 'win_details': {'✅0️⃣': 0, '✅1️⃣': 0, '✅2️⃣': 0}, 'loss_details': {'❌': 0}}
    
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
• 3ème: {stats_bilan['win_details'].get('✅2️⃣', 0)}""")

@client.on(events.NewMessage(pattern='/help'))
async def cmd_help(event):
    if event.is_group or event.is_channel:
        return
    
    await event.respond(f"""📖 **AIDE**

🎯 **Utilisation:**
1. /start pour s'inscrire
2. Attendre les prédictions ici
3. Les résultats se mettent à jour auto!

🎲 **Numéros:** {MIN_GAME}-{MAX_GAME} (fins 2,4,6,8)

💰 **Tarifs:** 500FCFA(24h) | 1500FCFA(1sem) | 2500FCFA(2sem)

📊 **Commandes admin:**
/status - État du bot
/predict - Prédiction manuelle
/bilan - Statistiques
/reset - Reset total""")

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

async def daily_reset():
    while True:
        now = datetime.now(timezone(timedelta(hours=1)))
        target = datetime.combine(now.date(), time(0, 59, tzinfo=now.tzinfo)) + timedelta(days=1)
        await asyncio.sleep((target - now).total_seconds())
        
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

async def main():
    load_users_data()
    try:
        app = web.Application()
        app.router.add_get('/', lambda r: web.Response(text=f"OK - #{last_known_source_game}"))
        runner = web.AppRunner(app)
        await runner.setup()
        await web.TCPSite(runner, '0.0.0.0', PORT).start()
        
        await client.start(bot_token=BOT_TOKEN)
        logger.info("✅ Bot démarré")
        
        asyncio.create_task(daily_reset())
        await client.run_until_disconnected()
    except Exception as e:
        logger.error(f"Erreur: {e}")

if __name__ == '__main__':
    asyncio.run(main())
