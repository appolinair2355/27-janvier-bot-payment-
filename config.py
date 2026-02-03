"""
Configuration du bot Telegram de prédiction Baccarat
"""
import os

def parse_channel_id(env_var: str, default: str) -> int:
    value = os.getenv(env_var) or default
    channel_id = int(value)
    # Convertit l'ID positif en format ID de canal Telegram négatif si nécessaire
    if channel_id > 0 and len(str(channel_id)) >= 10:
        channel_id = -channel_id
    return channel_id

# ID du canal source (Résultats Baccarat)
SOURCE_CHANNEL_ID = parse_channel_id('SOURCE_CHANNEL_ID', '-1002682552255')

# ID du canal source 2 (Statistiques)
SOURCE_CHANNEL_2_ID = parse_channel_id('SOURCE_CHANNEL_2_ID', '-1002674389383')

# ID de l'administrateur
ADMIN_ID = int(os.getenv('ADMIN_ID') or '0')

# API Telegram (obtenir sur https://my.telegram.org)
API_ID = int(os.getenv('API_ID') or '0')
API_HASH = os.getenv('API_HASH') or ''
BOT_TOKEN = os.getenv('BOT_TOKEN') or ''

# Port pour le serveur web (Render.com utilise 10000 par défaut)
PORT = int(os.getenv('PORT') or '10000')

# MAPPING : Miroirs selon les instructions utilisateur
SUIT_MAPPING = {
    '♦': '♠',  # Miroir Carreau <-> Pique
    '♠': '♦',
    '♥': '♣',  # Miroir Cœur <-> Trèfle
    '♣': '♥',
}

ALL_SUITS = ['♠', '♥', '♦', '♣']
SUIT_DISPLAY = {
    '♠': '♠️ Pique (Noir)',
    '♥': '❤️ Cœur (Rouge)',
    '♦': '♦️ Carreau (Rouge)',
    '♣': '♣️ Trèfle (Noir)'
}
