"""Configuration for the web client.

Reads the environment from the .env next to app/ (or from the real process
environment). It does NOT require BOT_TOKEN: the web client only needs a
userbot session, not a bot.
"""

import os
import logging
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent  # the repository root

# Env priority: process env, then the .env next to app/, then the working dir
for _p in (BASE_DIR / ".env", Path.cwd() / ".env"):
    if _p.is_file():
        load_dotenv(_p)

logging.basicConfig(
    format="[%(levelname) 5s/%(asctime)s] %(name)s: %(message)s",
    level=logging.INFO,
)


def getenv_int(key: str, default=None):
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


# --- Telegram API -----------------------------------------------------------
# Read from the environment only (webapp/.env, the repo root .env, or the real
# process env). There is deliberately no fallback value here: baking the
# credentials into the source publishes them with the repository.
API_ID = getenv_int("API_ID")
API_HASH = (os.getenv("API_HASH") or "").strip() or None
if not API_ID or not API_HASH:
    raise RuntimeError(
        "API_ID and API_HASH are not set. Copy .env.example to .env and fill "
        "in the values from https://my.telegram.org/apps"
    )

# --- Optional / bot-side (the bot itself is NOT started by this app) --------
FORCESUB = (os.getenv("FORCESUB") or "").strip() or None
FORCESUB_NAME = FORCESUB.lstrip("@") if FORCESUB else None
AUTH_USERS = getenv_int_list("AUTH_USERS", [7477152489])

# --- Database ---------------------------------------------------------------
MONGODB_URI = (os.getenv("MONGODB_URI") or "").strip() or "mongodb://localhost:27017"
DB_NAME = os.getenv("DB_NAME", "saverestricted")

# --- Web server -------------------------------------------------------------
WEB_HOST = os.getenv("WEB_HOST", "0.0.0.0")
WEB_PORT = getenv_int("WEB_PORT", 8000)
COOKIE_NAME = os.getenv("COOKIE_NAME", "tg_web_session")
COOKIE_SECURE = (os.getenv("COOKIE_SECURE", "") or "").lower() in ("1", "true", "yes")
TOKEN_TTL_SECONDS = getenv_int("TOKEN_TTL_SECONDS", 30 * 86400)
LOGIN_TTL_SECONDS = getenv_int("LOGIN_TTL_SECONDS", 300)
LOGIN_MAX_ATTEMPTS = getenv_int("LOGIN_MAX_ATTEMPTS", 5)

# --- Admin -------------------------------------------------------------------
# The admin signs in on the login page by typing this digit-only password into
# the phone number field. ADMIN_PIN is the first-login default; the admin is
# forced to choose their own password on first use (stored hashed in Mongo).
ADMIN_UID = -1                       # pseudo user id for admin web sessions
ADMIN_PIN = (os.getenv("ADMIN_PIN") or "").strip() or "123456"
ADMIN_PIN_MIN = 6
ADMIN_PIN_MAX = 12

# --- Batch save (one link, keep saving files forward until N) ----------------
BULK_SAVES_PER_DAY = getenv_int("BULK_SAVES_PER_DAY", 1)
BULK_MAX_FILES = getenv_int("BULK_MAX_FILES", 50)

# --- Multiple links saved together (newline separated in the link field) -----
MULTI_SAVES_PER_DAY = getenv_int("MULTI_SAVES_PER_DAY", 2)
MULTI_MAX_LINKS = getenv_int("MULTI_MAX_LINKS", 5)

# --- Limits / jobs ----------------------------------------------------------
DAILY_LIMIT = getenv_int("DAILY_LIMIT", 20)
COOLDOWN_SECONDS = getenv_int("COOLDOWN_SECONDS", 0)
JOB_TTL_SECONDS = getenv_int("JOB_TTL_SECONDS", 3600)
HISTORY_LIMIT = getenv_int("HISTORY_LIMIT", 100)          # records kept per user
HISTORY_TTL_DAYS = getenv_int("HISTORY_TTL_DAYS", 30)     # Mongo purges older records
FREE_SPACE_RESERVE = getenv_int("FREE_SPACE_RESERVE", 2 * 1024 * 1024 * 1024)  # keep 2 GB free
MAX_TEXT_PREVIEW = 4000

DATA_DIR = BASE_DIR / "data"
JOBS_DIR = DATA_DIR / "jobs"
STATIC_DIR = BASE_DIR / "app" / "static"

if not os.path.isdir(JOBS_DIR):
    os.makedirs(JOBS_DIR, exist_ok=True)

if not (os.getenv("MONGODB_URI") or "").strip():
    logging.warning("MONGODB_URI not set, using a local MongoDB at mongodb://localhost:27017")
