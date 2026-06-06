import os
import sys
import logging
from telethon import TelegramClient
from pyrogram import Client
from dotenv import load_dotenv

logging.basicConfig(
    format="[%(levelname) 5s/%(asctime)s] %(name)s: %(message)s",
    level=logging.WARNING
)

load_dotenv()


def getenv_int(key: str, default=None) -> int | None:
    val = os.getenv(key, default)
    try:
        return int(val) if val is not None else None
    except (ValueError, TypeError):
        return default


def getenv_int_list(key: str, default=None) -> list[int]:
    raw = os.getenv(key)
    if not raw:
        return default or []
    return [int(u.strip()) for u in raw.split() if u.strip().isdigit()]


API_ID = getenv_int("API_ID", 4680197)
API_HASH = os.getenv("API_HASH", "495b0228624028d635bd748b22985f67")
BOT_TOKEN = os.getenv("BOT_TOKEN")
FORCESUB = os.getenv("FORCESUB")
MONGODB_URI = os.getenv("MONGODB_URI")
LOGS = getenv_int("LOGS")
AUTH_USERS = getenv_int_list("AUTH_USERS", [7477152489])

if not all([API_ID, API_HASH, BOT_TOKEN]):
    logging.error("Missing API_ID, API_HASH, or BOT_TOKEN in environment.")
    sys.exit(1)

bot = TelegramClient("bot", API_ID, API_HASH).start(bot_token=BOT_TOKEN)

Bot = Client(
    "save-restricted-bot",
    bot_token=BOT_TOKEN,
    api_id=int(API_ID),
    api_hash=API_HASH
)
