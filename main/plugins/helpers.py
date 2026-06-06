import os
import time
import asyncio
import re
import glob
from pyrogram import Client
from pyrogram.errors import FloodWait
from main.Database.database import Database
from .. import API_ID, API_HASH, MONGODB_URI


async def login(uid: int, api_id: int, api_hash: str, session: str) -> None:
    db = Database(MONGODB_URI, 'saverestricted')
    await asyncio.gather(
        db.update_api_id(uid, api_id),
        db.update_api_hash(uid, api_hash),
        db.update_session(uid, session)
    )


async def logout(uid: int) -> None:
    db = Database(MONGODB_URI, 'saverestricted')
    await asyncio.gather(
        db.rem_api_id(uid),
        db.rem_api_hash(uid),
        db.rem_session(uid)
    )


async def join(client: Client, invite: str) -> str:
    try:
        await client.join_chat(invite)
        await asyncio.sleep(1)
        return "Joined the channel successfully."
    except FloodWait as fw:
        await asyncio.sleep(fw.value)
        return f"FloodWait triggered. Retrying in {fw.value}s."
    except Exception:
        return "Failed to join the channel."


def get_link(text: str | None) -> str | None:
    if not text:
        return None
    pattern = r"(?i)\b((?:https?://)?(?:t\.me|www\.\w+\.\w+)/[^\s]+)"
    match = re.search(pattern, text)
    return match.group(1) if match else None


spam_user, spam_time = [], []
_clients = {}


async def set_timer(bot, uid: int) -> None:
    now = time.time()
    spam_user.append(uid); spam_time.append(now)
    tmp = await bot.send_message(uid, "Cooling down... Please wait 25 seconds.")
    await asyncio.sleep(25)
    await tmp.edit("You can now forward again.")
    spam_user.remove(uid); spam_time.remove(now)


def check_timer(uid: int) -> tuple[bool, str | None]:
    if uid in spam_user:
        left = 24 - round(time.time() - spam_time[spam_user.index(uid)])
        return False, f"Please wait {left}s before forwarding again."
    return True, None


async def screenshot(video: str, ts: int | float, uid: int) -> str | None:
    thumb = f"{uid}.jpg"
    if os.path.exists(thumb):
        return thumb
    cmd = ["ffmpeg", "-ss", str(ts), "-i", video, "-vframes", "1", thumb]
    proc = await asyncio.create_subprocess_exec(*cmd,
                                                stdout=asyncio.subprocess.PIPE,
                                                stderr=asyncio.subprocess.PIPE)
    await proc.communicate()
    return thumb if os.path.isfile(thumb) else None


def _split_file_sync(input_path: str, chunk_size_bytes: int) -> list[str]:
    base = os.path.splitext(input_path)[0]
    for old in glob.glob(f"{base}.part.*"):
        try:
            os.remove(old)
        except OSError:
            pass

    file_size = os.path.getsize(input_path)
    if file_size <= chunk_size_bytes:
        return [input_path]

    num_parts = (file_size + chunk_size_bytes - 1) // chunk_size_bytes
    parts: list[str] = []
    buffer_size = 8 * 1024 * 1024

    with open(input_path, "rb") as f:
        for i in range(num_parts):
            part_path = f"{base}.part.{i:02d}"
            parts.append(part_path)
            with open(part_path, "wb") as pf:
                remaining = chunk_size_bytes
                while remaining > 0:
                    data = f.read(min(remaining, buffer_size))
                    if not data:
                        break
                    pf.write(data)
                    remaining -= len(data)
    return parts


async def split_file(input_path: str, chunk_size_bytes: int) -> list[str]:
    return await asyncio.to_thread(_split_file_sync, input_path, chunk_size_bytes)
