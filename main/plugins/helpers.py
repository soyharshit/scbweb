import os
import time
import asyncio
import re
import glob
import logging
from pyrogram import Client
from pyrogram.errors import FloodWait
from main.Database.database import Database
from .. import API_ID, API_HASH, MONGODB_URI, DB_NAME


async def login(uid: int, api_id: int, api_hash: str, session: str) -> None:
    db = Database(MONGODB_URI, DB_NAME)
    await asyncio.gather(
        db.update_api_id(uid, api_id),
        db.update_api_hash(uid, api_hash),
        db.update_session(uid, session)
    )


async def logout(uid: int) -> None:
    db = Database(MONGODB_URI, DB_NAME)
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


_ffmpeg_available: bool | None = None


async def is_ffmpeg_available() -> bool:
    global _ffmpeg_available
    if _ffmpeg_available is not None:
        return _ffmpeg_available
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-version",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.communicate()
        _ffmpeg_available = proc.returncode == 0
    except FileNotFoundError:
        _ffmpeg_available = False
    return _ffmpeg_available


async def _get_media_duration(input_path: str) -> float:
    proc = await asyncio.create_subprocess_exec(
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        input_path,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(f"ffprobe failed: {stderr.decode(errors='ignore')[:200]}")
    return float(stdout.decode().strip())


async def split_media_file(input_path: str, max_parts: int = 2) -> list[str]:
    base, ext = os.path.splitext(input_path)
    for old in glob.glob(f"{base}.part.*"):
        try:
            os.remove(old)
        except OSError:
            pass

    duration = await _get_media_duration(input_path)
    if duration <= 0:
        raise RuntimeError("Could not determine media duration.")
    segment_time = duration / max_parts + 1

    output_pattern = f"{base}.part.%03d{ext}"
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-y", "-i", input_path,
        "-c", "copy", "-map", "0",
        "-f", "segment",
        "-segment_time", str(segment_time),
        "-reset_timestamps", "1",
        output_pattern,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        for old in glob.glob(f"{base}.part.*"):
            try:
                os.remove(old)
            except OSError:
                pass
        raise RuntimeError(f"ffmpeg split failed: {stderr.decode(errors='ignore')[:200]}")

    return sorted(glob.glob(f"{base}.part.*"))


_DOWNLOAD_DIRS = ("downloads", "main/downloads")
_PART_GLOBS = ("*.part.*",)
_THUMB_GLOB = "[0-9]*.jpg"


def _remove_if_exists(path: str) -> int:
    try:
        size = os.path.getsize(path)
        os.remove(path)
        return size
    except OSError:
        return 0


def _cleanup_temp_files_sync(
    downloads_max_age_hours: int = 24,
    thumb_max_age_days: int = 7,
) -> tuple[int, int]:
    """Synchronous cleanup. Returns (files_removed, bytes_freed)."""
    import time
    removed = 0
    freed = 0
    now = time.time()
    downloads_cutoff = now - downloads_max_age_hours * 3600
    thumb_cutoff = now - thumb_max_age_days * 86400

    for d in _DOWNLOAD_DIRS:
        if not os.path.isdir(d):
            continue
        for name in os.listdir(d):
            full = os.path.join(d, name)
            if not os.path.isfile(full):
                continue
            try:
                if os.path.getmtime(full) < downloads_cutoff:
                    freed += _remove_if_exists(full)
                    removed += 1
            except OSError:
                continue

    for pattern in _PART_GLOBS:
        for path in glob.glob(pattern):
            freed += _remove_if_exists(path)
            removed += 1

    for path in glob.glob(_THUMB_GLOB):
        try:
            if os.path.getmtime(path) < thumb_cutoff:
                freed += _remove_if_exists(path)
                removed += 1
        except OSError:
            continue

    return removed, freed


async def cleanup_temp_files(
    downloads_max_age_hours: int = 24,
    thumb_max_age_days: int = 7,
) -> tuple[int, int]:
    """Clean orphaned temp files. Returns (files_removed, bytes_freed)."""
    removed, freed = await asyncio.to_thread(
        _cleanup_temp_files_sync, downloads_max_age_hours, thumb_max_age_days
    )
    if removed:
        logging.getLogger(__name__).warning(
            f"Cleanup: removed {removed} files, freed {freed / (1024**2):.1f} MB"
        )
    return removed, freed
