"""Web login flows: phone + OTP (with 2FA) and paste-a-session-string."""

import logging
import re
import secrets
import time
from dataclasses import dataclass, field

from pyrogram import Client
from pyrogram.errors import (
    ApiIdInvalid,
    FloodWait,
    PasswordHashInvalid,
    PhoneCodeExpired,
    PhoneCodeInvalid,
    PhoneNumberInvalid,
    SessionPasswordNeeded,
)

from .config import API_ID, API_HASH, LOGIN_TTL_SECONDS, LOGIN_MAX_ATTEMPTS
from . import tg

log = logging.getLogger("webapp.auth")

PHONE_RE = re.compile(r"^\+?\d{7,15}$")
MIN_SESSION_LEN = 300

_PENDING: dict[str, "Pending"] = {}


class LoginError(Exception):
    """User-facing login error."""


@dataclass
class Pending:
    handle: str
    client: Client
    phone: str
    code_hash: str = ""
    attempts: int = 0
    created: float = field(default_factory=time.time)
    step: str = "code"  # code -> password


def sweep_pending() -> None:
    """Drop expired phone-login attempts and disconnect their clients."""
    now = time.time()
    for handle in [h for h, p in _PENDING.items() if now - p.created > LOGIN_TTL_SECONDS]:
        pending = _PENDING.pop(handle)
        _forget(pending)


def _forget(pending: Pending) -> None:
    try:
        pending.client.loop.create_task(_close(pending.client))
    except Exception:
        pass


async def _close(client: Client) -> None:
    try:
        await client.disconnect()
    except Exception:
        pass


def _get(handle: str) -> Pending:
    sweep_pending()
    pending = _PENDING.get(handle or "")
    if not pending:
        raise LoginError("Login session expired. Please start again.")
    if time.time() - pending.created > LOGIN_TTL_SECONDS:
        _PENDING.pop(handle, None)
        _forget(pending)
        raise LoginError("Login session expired. Please start again.")
    return pending


def _drop(handle: str) -> None:
    pending = _PENDING.pop(handle, None)
    if pending:
        _forget(pending)


async def _finish(pending: Pending):
    """Authorized: export the session, activate the client, return (client, session, me)."""
    session = await pending.client.export_session_string()
    await tg.activate(pending.client)
    me = await pending.client.get_me()
    _PENDING.pop(pending.handle, None)
    return pending.client, session, me


# ---------------------------------------------------------------------------
# Phone + OTP
# ---------------------------------------------------------------------------
async def start_phone_login(phone: str) -> dict:
    phone = (phone or "").strip().replace(" ", "").replace("-", "").replace("(", "").replace(")", "")
    if not PHONE_RE.match(phone):
        raise LoginError(
            "Enter a valid phone number in international format, e.g. +15854346400"
        )

    sweep_pending()
    if len(_PENDING) > 500:
        raise LoginError("Too many pending logins right now. Try again in a minute.")

    client = Client("web_login", api_id=API_ID, api_hash=API_HASH, in_memory=True)
    try:
        await client.connect()
        sent = await client.send_code(phone)
    except FloodWait as fw:
        await _close(client)
        raise LoginError(f"Too many attempts. Try again in {fw.value} seconds.")
    except (PhoneNumberInvalid, ApiIdInvalid):
        await _close(client)
        raise LoginError("Invalid phone number or API credentials.")
    except LoginError:
        raise
    except Exception as exc:
        await _close(client)
        log.warning("send_code failed: %s", exc)
        raise LoginError("Could not send the code. Please try again.")

    handle = secrets.token_hex(8)
    _PENDING[handle] = Pending(handle=handle, client=client, phone=phone, code_hash=sent.phone_code_hash)
    return {"handle": handle, "via": _sent_via(sent)}


def _sent_via(sent) -> str:
    try:
        from pyrogram.enums import SentCodeType

        return {
            SentCodeType.APP: "Telegram app",
            SentCodeType.SMS: "SMS",
            SentCodeType.CALL: "phone call",
            SentCodeType.FLASH_CALL: "phone call",
            SentCodeType.FRAGMENT_SMS: "SMS",
            SentCodeType.EMAIL_CODE: "email",
        }.get(sent.type, "Telegram")
    except Exception:
        return "Telegram"


async def submit_code(handle: str, code: str) -> dict:
    pending = _get(handle)
    code = (code or "").strip().replace(" ", "")
    if not code:
        raise LoginError("Enter the code you received in Telegram.")

    pending.attempts += 1
    if pending.attempts > LOGIN_MAX_ATTEMPTS:
        _drop(handle)
        raise LoginError("Too many wrong codes. Please start again.")

    try:
        await pending.client.sign_in(pending.phone, pending.code_hash, code)
    except SessionPasswordNeeded:
        pending.step = "password"
        return {"need_password": True}
    except PhoneCodeInvalid:
        raise LoginError("Incorrect code. Please try again.")
    except PhoneCodeExpired:
        _drop(handle)
        raise LoginError("The code has expired. Please start again.")
    except FloodWait as fw:
        raise LoginError(f"Too many attempts. Try again in {fw.value} seconds.")
    except LoginError:
        raise
    except Exception as exc:
        log.warning("sign_in failed: %s", exc)
        raise LoginError("Login failed. Please start again.")

    phone = pending.phone
    client, session, me = await _finish(pending)
    return {"client": client, "session": session, "me": me, "phone": phone}


async def submit_password(handle: str, password: str) -> dict:
    pending = _get(handle)
    if pending.step != "password":
        raise LoginError("No password step is pending. Please start again.")
    if not password:
        raise LoginError("Enter your two-step verification password.")

    try:
        await pending.client.check_password(password)
    except PasswordHashInvalid:
        raise LoginError("Wrong password. Please try again.")
    except FloodWait as fw:
        raise LoginError(f"Too many attempts. Try again in {fw.value} seconds.")
    except Exception as exc:
        log.warning("check_password failed: %s", exc)
        raise LoginError("Login failed. Please start again.")

    phone = pending.phone
    client, session, me = await _finish(pending)
    return {"client": client, "session": session, "me": me, "phone": phone}


# ---------------------------------------------------------------------------
# Paste a Pyrogram session string
# ---------------------------------------------------------------------------
async def login_with_session(session: str) -> dict:
    session = (session or "").strip()
    if len(session) < MIN_SESSION_LEN:
        raise LoginError("That session string is too short.")

    client = Client(
        "web_login", api_id=API_ID, api_hash=API_HASH, session_string=session, in_memory=True
    )
    try:
        authorized = await client.connect()
        if not authorized:
            await _close(client)
            raise LoginError("This session is logged out. Generate a new session string.")
        me = await client.get_me()
        if not me:
            await _close(client)
            raise LoginError("This session could not be verified.")
        await tg.activate(client)
    except LoginError:
        raise
    except Exception as exc:
        await _close(client)
        log.warning("session login failed: %s", exc)
        raise LoginError(f"Invalid session string: {exc}")

    return {"client": client, "session": session, "me": me}
