"""Userbot (user session) management shared by the web app."""

import asyncio
import logging

from pyrogram import Client, raw
from pyrogram.errors import FloodWait

from .config import API_ID, API_HASH, FORCESUB
from .db import db

log = logging.getLogger("webapp.tg")

_clients: dict[int, Client] = {}

# How every session this site creates presents itself to Telegram. Telegram
# shows this in Active Sessions and inside the "new login" alert, so it has to
# read as this app rather than as an anonymous script (CPython/Linux), which is
# what a user would otherwise see and distrust.
SESSION_DEVICE = {
    "device_model": "Telegram Saver",
    "system_version": "Web",
    "app_version": "Telegram Saver",
}


class LoginRequired(Exception):
    """Raised when the user has no usable Telegram session."""


class ForceSubRequired(Exception):
    """Raised when the user has not joined the mandatory channel."""


async def activate(client: Client) -> None:
    """Finish bringing a connected/authorized client online (mirrors Client.start)."""
    try:
        await client.invoke(raw.functions.updates.GetState())
    except Exception:
        pass
    if not client.is_initialized:
        await client.initialize()


def _bind_me(client, me) -> None:
    """Pyrogram fills client.me only inside Client.start(); the web client uses
    connect() instead, so we bind it here. save_file() reads me.is_premium on
    every download/upload and crashes when the attribute is missing."""
    if me is None:
        return
    client.me = me
    client._is_premium = bool(getattr(me, "is_premium", False))


def cache_client(uid: int, client: Client, me=None) -> None:
    _bind_me(client, me)
    _clients[uid] = client


async def drop_userbot(uid: int) -> None:
    client = _clients.pop(uid, None)
    if client:
        try:
            await client.stop()
        except Exception:
            try:
                await client.disconnect()
            except Exception:
                pass


async def get_userbot(uid: int) -> Client:
    """Return a live userbot for this user (from cache or from the stored session)."""
    client = _clients.get(uid)
    if client is not None:
        try:
            _bind_me(client, await client.get_me())
            return client
        except Exception:
            await drop_userbot(uid)

    api_id, api_hash, session = await db.get_credentials(uid)
    if not session:
        raise LoginRequired("Please log in first.")

    client = Client(
        f"web_user_{uid}",
        api_id=int(api_id or API_ID),
        api_hash=api_hash or API_HASH,
        session_string=session,
        in_memory=True,
        **SESSION_DEVICE,
    )
    try:
        authorized = await client.connect()
        if not authorized:
            await client.disconnect()
            raise LoginRequired("Your Telegram session has expired. Please log in again.")
        me = await client.get_me()
        await activate(client)
    except LoginRequired:
        raise
    except Exception as exc:
        try:
            await client.disconnect()
        except Exception:
            pass
        raise LoginRequired(f"Could not start your Telegram session: {exc}")

    _bind_me(client, me)
    _clients[uid] = client
    if me.first_name:
        try:
            await db.set_name(uid, me.first_name)
        except Exception:
            pass
    return client


async def check_force_sub(client: Client, uid: int) -> None:
    if not FORCESUB:
        return
    try:
        await client.get_chat_member(FORCESUB, uid)
    except Exception:
        raise ForceSubRequired(
            f"Please join @{FORCESUB.lstrip('@')} first, then try again."
        )


async def join_invite(client: Client, link: str) -> str:
    try:
        await client.join_chat(link)
        await asyncio.sleep(1)
        return "Joined the channel successfully."
    except FloodWait as fw:
        return f"Rate limited by Telegram. Try again in {fw.value}s."
    except Exception:
        return "Could not join the channel. It may be private, full, or already joined."
