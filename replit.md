# Telegram Baccarat Prediction Bot

## Overview
A Telegram bot that monitors source channels and makes predictions for Baccarat games. The bot uses prediction rules based on game numbers and card suit patterns. Includes a complete payment and subscription system.

## Tech Stack
- **Language**: Python 3.11
- **Telegram Library**: Telethon 1.35.0
- **Web Server**: aiohttp (for health checks)
- **Port**: 5000 (web health check server)

## Project Structure
- `main.py` - Main bot application with Telegram client, prediction logic, payment system, and web server
- `config.py` - Configuration (API keys, channel IDs, port, suit mappings)
- `requirements.txt` - Python dependencies
- `users_data.json` - User registration and subscription data (auto-created)

## Configuration
The bot requires the following environment variables (with defaults in config.py):
- `API_ID` - Telegram API ID
- `API_HASH` - Telegram API Hash
- `BOT_TOKEN` - Telegram Bot Token
- `SOURCE_CHANNEL_ID` - Main source channel for game data
- `SOURCE_CHANNEL_2_ID` - Statistics source channel
- `PREDICTION_CHANNEL_ID` - Channel where predictions are posted
- `ADMIN_ID` - Admin user ID for privileged commands
- `TELEGRAM_SESSION` - Optional session string for persistent login

## Payment System
The bot includes a complete subscription system:

### User Flow:
1. User sends /start
2. Bot asks for: Name, Surname, Country
3. After registration, user gets **10 minutes free trial**
4. After trial expires, bot shows payment button (MoneyFusion link)
5. User pays and sends screenshot + amount
6. Bot activates subscription

### Subscription Tiers:
- **1000 FCFA** = 1 week (predictions in channel)
- **2000 FCFA** = 2 weeks (predictions sent via private chat)

### Payment Link:
`https://my.moneyfusion.net/6977f7502181d4ebf722398d`

## Running the Bot
The bot runs via the "Telegram Bot" workflow which executes `python main.py`.

## Health Check
A web server runs on port 5000 with:
- `/` - Status page
- `/health` - Health check endpoint

## Bot Commands
- `/start` - Start registration or show subscription status
- `/payer` - Subscribe or renew subscription
- `/help` - Show help message
- `/info` - Show system information
- `/status` - Show current state (admin only)
- `/bilan` - Send statistics report (admin only)
- `/tim <min>` - Set bilan interval (admin only)

## Recent Changes
- **2026-01-27**: Added complete payment and subscription system with user registration, trial periods, and premium private predictions
