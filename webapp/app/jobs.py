"""Fetch jobs: resolve a Telegram link and save the message to Saved Messages.

Nothing is served to the browser -- the file only lives in the user's own
Telegram Saved Messages. Local temp files exist solely for the rare fallback
where Telegram refuses a forward/copy and the file has to be re-uploaded.
"""

import asyncio
import logging
import os
import re
import secrets
import shutil
import time
from dataclasses import dataclass, field

from pyrogram.errors import FloodWait

from .config import (
    DAILY_LIMIT,
    HISTORY_TTL_DAYS,
    JOB_TTL_SECONDS,
    JOBS_DIR,
    COOLDOWN_SECONDS,
    FREE_SPACE_RESERVE,
    MAX_TEXT_PREVIEW,
)
from .db import db
from .tg import (
    ForceSubRequired,
    LoginRequired,
    check_force_sub,
    get_userbot,
    join_invite,
)

log = logging.getLogger("webapp.jobs")

_DIALOG_CACHE_TTL = 300


class JobError(Exception):
    """User-facing job error."""


@dataclass
class Job:
    id: str
    uid: int
    link: str
    kind: str = "single"   # single | batch (N files forward) | multi (several links)
    count: int = 0         # batch: target number of files
    links: list[str] = field(default_factory=list)  # multi: every submitted link
    status: str = "queued"  # queued | running | done | error
    step: str = "Waiting..."
    percent: float = 0.0
    downloaded: int = 0
    total: int = 0
    speed: int = 0
    error: str | None = None
    message: str | None = None
    text: str | None = None
    items: list[dict] = field(default_factory=list)
    saved_ids: list[int] = field(default_factory=list)
    open_url: str | None = None
    created: float = field(default_factory=time.time)
    updated: float = field(default_factory=time.time)
    task: asyncio.Task | None = None

    def touch(self) -> None:
        self.updated = time.time()


JOBS: dict[str, Job] = {}
_DIALOG_CACHE: dict[str, dict] = {}
_COOLDOWN_UNTIL: dict[int, float] = {}


# ---------------------------------------------------------------------------
# API used by the web layer
# ---------------------------------------------------------------------------
def snapshot(job: Job) -> dict:
    return {
        "id": job.id,
        "link": job.link,
        "kind": job.kind,
        "count": job.count,
        "status": job.status,
        "step": job.step,
        "percent": round(job.percent, 1),
        "downloaded": job.downloaded,
        "total": job.total,
        "speed": job.speed,
        "error": job.error,
        "message": job.message,
        "text": job.text,
        "items": job.items,
        "saved_ids": job.saved_ids,
        "open_url": job.open_url,
        "created": job.created,
    }


def get_job(job_id: str, uid: int) -> Job | None:
    job = JOBS.get(job_id)
    return job if job and job.uid == uid else None


def list_jobs(uid: int) -> list[dict]:
    """Live jobs still held in memory (the merge with the durable log happens
    in /api/jobs; in-memory entries win while a job is running)."""
    jobs = [j for j in JOBS.values() if j.uid == uid]
    jobs.sort(key=lambda j: j.created, reverse=True)
    return [snapshot(j) for j in jobs]


def history_doc(job: Job) -> dict:
    """Compact, JSON-safe record for the durable history log (no file data)."""
    return {
        "id": job.id,
        "uid": job.uid,
        "link": job.link,
        "kind": job.kind,
        "count": job.count,
        "status": job.status,
        "step": job.step,
        "percent": round(float(job.percent), 1),
        "error": job.error,
        "message": job.message,
        "text": (job.text or "")[:2000] or None,
        "items": job.items[:100],
        "open_url": job.open_url,
        "created": job.created,
        "updated": job.updated,
        "expires": job.created + HISTORY_TTL_DAYS * 86400,
    }


async def _persist(job: Job) -> None:
    """Best-effort history write; never let logging failures break a save."""
    try:
        await db.save_history(history_doc(job))
    except Exception as exc:  # noqa: BLE001
        log.warning("History write failed for %s: %s", job.id, exc)


def active_job(uid: int) -> Job | None:
    for job in JOBS.values():
        if job.uid == uid and job.status in ("queued", "running"):
            return job
    return None


def cooldown_remaining(uid: int) -> int:
    if COOLDOWN_SECONDS <= 0:
        return 0
    return max(0, int(_COOLDOWN_UNTIL.get(uid, 0) - time.time()))


def create_job(uid: int, link: str, kind: str = "single", count: int = 0,
               links: list[str] | None = None) -> Job:
    if active_job(uid) is not None:
        raise JobError("Another save is already in progress. Please wait for it to finish.")
    job = Job(
        id=secrets.token_urlsafe(9),
        uid=uid,
        link=link.strip(),
        kind=kind if kind in ("single", "batch", "multi") else "single",
        count=max(0, int(count or 0)),
        links=[l for l in (links or []) if l],
    )
    JOBS[job.id] = job
    job.task = asyncio.get_running_loop().create_task(run_job(job))
    return job


async def run_job(job: Job) -> None:
    job.status = "running"
    job.step = "Connecting..."
    job.touch()
    await _persist(job)   # create the log record up-front so a crash still leaves a trace
    try:
        await _run(job)
        if job.status != "error":
            job.status = "done"
            job.step = "Done"
            job.percent = 100.0
    except JobError as exc:
        job.status = "error"
        job.error = str(exc)
    except (LoginRequired, ForceSubRequired) as exc:
        job.status = "error"
        job.error = str(exc)
    except FloodWait as fw:
        job.status = "error"
        job.error = f"Rate limited by Telegram. Try again in {fw.value} seconds."
    except Exception as exc:  # noqa: BLE001
        log.exception("Job %s failed", job.id)
        job.status = "error"
        job.error = f"Unexpected error: {exc}"
    finally:
        job.speed = 0
        job.percent = 100.0 if job.status == "done" else job.percent
        job.touch()
        await _persist(job)   # final state for the durable history log


# ---------------------------------------------------------------------------
# Link parsing / message resolution (ported from the bot's get_msg)
# ---------------------------------------------------------------------------
def extract_link(text: str | None) -> str | None:
    if not text:
        return None
    match = re.search(r"(?i)\b((?:https?://)?(?:t\.me|telegram\.me|www\.\S+\.\S+)/\S+)", text)
    return match.group(1) if match else None


def parse_link(raw_link: str) -> dict:
    link = raw_link.strip().replace("telegram.me/", "t.me/")
    if "t.me/+" in link or "t.me/joinchat/" in link or "/joinchat/" in link:
        return {"kind": "invite", "link": link}
    if not re.match(r"(?i)^https?://", link):
        link = "https://" + link
    try:
        if "t.me/c/" in link:
            path = link.split("t.me/c/", 1)[1].split("?", 1)[0]
            segments = [s for s in path.split("/") if s]
            if len(segments) >= 3:
                raw_id, msg_id = segments[0], int(segments[2])
            elif len(segments) >= 2:
                raw_id, msg_id = segments[0], int(segments[1])
            else:
                raise JobError("Invalid message link.")
            chat_ids: list = []
            if raw_id.startswith("-100"):
                chat_ids.append(int(raw_id))
            elif raw_id.startswith("100"):
                chat_ids.append(int("-" + raw_id))
                chat_ids.append(int(raw_id))
            else:
                chat_ids.append(int("-100" + raw_id))
                chat_ids.append(int(raw_id))
            chat_ids.append(raw_id)
            return {"kind": "message", "chat_ids": chat_ids, "msg_id": msg_id,
                    "single": "?single" in link}

        if "t.me/b/" in link:
            parts = link.split("/")
            return {"kind": "message", "chat_ids": [int(parts[-2])],
                    "msg_id": int(parts[-1].split("?")[0]), "single": "?single" in link}

        if "t.me/" in link:
            parts = link.split("/")
            if len(parts) >= 6 and not parts[-2].startswith(("c", "b")):
                chat_ids = [parts[-3], parts[-2]]
            else:
                chat_ids = [parts[-2]]
            msg_id = int(parts[-1].split("?")[0])
            return {"kind": "message", "chat_ids": chat_ids, "msg_id": msg_id,
                    "single": "?single" in link}
    except JobError:
        raise
    except (ValueError, IndexError):
        raise JobError("Invalid message link.")
    raise JobError("Unsupported link format. Please use a t.me link.")


async def _cached_dialogs(userbot, uid: int) -> set:
    key = f"ub_{uid}"
    now = time.time()
    cached = _DIALOG_CACHE.get(key)
    if cached and now < cached["expires_at"]:
        return cached["chat_ids"]
    chat_ids: set = set()
    async for dialog in userbot.get_dialogs(limit=500):
        cid = getattr(dialog.chat, "id", None)
        if cid is not None:
            chat_ids.add(cid)
            chat_ids.add(abs(cid))
    _DIALOG_CACHE[key] = {"chat_ids": chat_ids, "expires_at": now + _DIALOG_CACHE_TTL}
    return chat_ids


def _has_content(msg) -> bool:
    return bool(
        msg
        and not getattr(msg, "empty", False)
        and (msg.media or msg.text or msg.caption or msg.sticker or msg.poll)
    )


def _downloadable(msg) -> bool:
    return bool(
        msg.photo or msg.document or msg.video or msg.audio
        or msg.voice or msg.video_note or msg.animation or msg.sticker
    )


async def _resolve_message(userbot, job: Job, parsed: dict, require_content: bool = True):
    msg_id = parsed["msg_id"]
    chat_ids = list(parsed["chat_ids"])

    job.step = "Finding the message..."
    job.touch()

    for username in [c for c in chat_ids if isinstance(c, str) and not c.lstrip("-").isdigit()]:
        try:
            resolved = await userbot.get_chat(username)
            rid = getattr(resolved, "id", None)
            if rid and rid not in chat_ids:
                chat_ids.append(rid)
        except Exception:
            pass

    # For private-format links (t.me/c/, t.me/b/) the user must have the chat
    # in their dialogs, otherwise there is nothing to try. For public username
    # links we don't block upfront -- Telegram may serve the message anyway;
    # if it can't be fetched we fall back to the "join first" hint below.
    not_in_dialogs = False
    needs_dialogs = "t.me/c/" in job.link or "t.me/b/" in job.link or any(
        isinstance(c, str) and not c.lstrip("-").isdigit() for c in chat_ids
    )
    if needs_dialogs:
        try:
            known = await _cached_dialogs(userbot, job.uid)
            int_ids = [cid for cid in chat_ids if isinstance(cid, int)]
            if int_ids and not any(cid in known or abs(cid) in known for cid in int_ids):
                if "t.me/c/" in job.link or "t.me/b/" in job.link:
                    raise JobError(
                        "You haven't joined this chat or you don't have access to it.\n"
                        "Please join the chat first, then try again."
                    )
                not_in_dialogs = True
        except JobError:
            raise
        except Exception:
            pass

    join_hint = (
        "You haven't joined this chat or you don't have access to it.\n"
        "Please join the chat first, then try again."
    )

    msg = None
    for cid in chat_ids:
        try:
            candidate = await userbot.get_messages(cid, msg_id)
            if _has_content(candidate):
                msg = candidate
                break
        except Exception:
            continue

    if not msg:
        for cid in chat_ids:
            if not isinstance(cid, int):
                continue
            try:
                async for m in userbot.get_chat_history(cid, limit=1, offset_id=msg_id + 1):
                    if m.id == msg_id and _has_content(m):
                        msg = m
                        break
                if msg:
                    break
            except Exception:
                continue

    if not msg:
        try:
            raw_id = str(job.link.split("/")[-2]).lstrip("-100")
            known = await _cached_dialogs(userbot, job.uid)
            for cid in known:
                if raw_id in str(abs(cid)):
                    candidate = await userbot.get_messages(cid, msg_id)
                    if _has_content(candidate):
                        msg = candidate
                        break
        except Exception:
            pass

    if not msg:
        raise JobError(
            join_hint if not_in_dialogs else
            "Could not fetch the message. Please check the link and make sure you have access to it."
        )

    if getattr(msg, "empty", False):
        raise JobError(
            "This message is empty or has been deleted.\n"
            "The link may be invalid, or your session may lack access to this chat."
        )

    if msg.service:
        raise JobError("This is a service message with nothing to save.")

    if require_content and not _has_content(msg):
        raise JobError(
            "Message has no savable content.\n"
            "Supported: text, photos, videos, documents, audio, voice, stickers, animations, polls."
        )

    return msg


# ---------------------------------------------------------------------------
# Message metadata helpers
# ---------------------------------------------------------------------------
def _media_name(msg, index: int) -> str:
    for attr in ("document", "video", "audio", "animation", "voice"):
        obj = getattr(msg, attr, None)
        name = getattr(obj, "file_name", None) if obj else None
        if name:
            return name
    if msg.photo:
        return f"photo_{msg.id}.jpg"
    if msg.video_note:
        return f"round_{msg.id}.mp4"
    if msg.voice:
        return f"voice_{msg.id}.ogg"
    if msg.sticker:
        return f"sticker_{msg.id}.webp"
    if msg.audio:
        return f"audio_{msg.id}.mp3"
    if msg.animation:
        return f"animation_{msg.id}.mp4"
    if msg.video:
        return f"video_{msg.id}.mp4"
    if msg.document:
        return f"file_{msg.id}"
    return f"file_{index}"


def _media_kind(msg) -> str:
    if msg.photo:
        return "photo"
    if msg.video or msg.video_note or msg.animation:
        return "video"
    if msg.audio or msg.voice:
        return "audio"
    return "doc"


def _media_size(msg) -> int:
    for attr in ("document", "video", "audio", "animation", "voice", "sticker", "photo", "video_note"):
        obj = getattr(msg, attr, None)
        size = getattr(obj, "file_size", None) if obj else None
        if size:
            return int(size)
    return 0


def _text_content(msg) -> str | None:
    if msg.text:
        return msg.text
    if msg.caption:
        return msg.caption
    if msg.poll:
        poll = msg.poll
        lines = [getattr(poll, "question", "") or ""]
        for opt in getattr(poll, "options", None) or []:
            lines.append(f" - {getattr(opt, 'text', '')}")
        return "\n".join(lines).strip() or None
    return None


def _truncate_name(name: str, limit: int = 44) -> str:
    """Shorten long generated filenames for display, keeping the extension.

    Telegram sometimes hands back long random names
    (e.g. ``AQM_EApKkRJ8Lk7KqqPo4LTiXiw1kpIqpHTw...mp4``); unbroken tokens like
    that must not be able to blow up the layout on narrow screens.
    """
    name = (name or "").strip()
    if len(name) <= limit:
        return name
    root, dot, ext = name.rpartition(".")
    if dot and root and 0 < len(ext) <= 8:
        keep = max(limit - len(ext) - 2, 1)
        return f"{root[:keep]}….{ext}"
    return name[: limit - 1] + "…"


def _build_items(msgs: list) -> list[dict]:
    """Human-readable list of what is being saved (from metadata, no download)."""
    items = []
    for idx, m in enumerate(msgs, 1):
        if _downloadable(m):
            items.append({
                "name": _truncate_name(_media_name(m, idx)),
                "kind": _media_kind(m),
                "size": _media_size(m),
            })
        elif m.poll:
            q = _truncate_name(getattr(m.poll, "question", "") or "Poll", 40)
            items.append({"name": f"Poll · {q}", "kind": "poll", "size": 0})
        else:
            text = _text_content(m) or f"Message {m.id}"
            short = text if len(text) <= 70 else text[:70] + "…"
            items.append({"name": short, "kind": "text", "size": 0})
    return items


def _safe_name(name: str) -> str:
    name = os.path.basename((name or "").replace("\\", "/")).strip()
    name = re.sub(r'[\r\n\t<>:"|?*]', "_", name)
    return name[:150] or "file"


# ---------------------------------------------------------------------------
# Progress + temp download (used only by the re-upload fallback)
# ---------------------------------------------------------------------------
def _download_progress(index: int, count: int, start: float):
    async def _cb(current, total, job: Job):
        now = time.time()
        fraction = (current / total) if total else 0
        job.percent = min(99.9, ((index - 1) + fraction) / max(count, 1) * 100)
        job.downloaded = current
        job.total = total
        job.speed = int(current / (now - start)) if now > start else 0
        job.step = f"Downloading {index}/{count}" if count > 1 else "Downloading..."
        job.touch()

    return _cb


def _upload_progress(label: str, index: int, count: int):
    async def _cb(current, total, job: Job):
        job.percent = min(99.9, ((index - 1) + (current / total if total else 0)) / max(count, 1) * 100)
        job.downloaded = current
        job.total = total
        job.step = label
        job.touch()

    return _cb


def _check_free_space(expected: int = 0) -> None:
    free = shutil.disk_usage(JOBS_DIR).free
    if free < FREE_SPACE_RESERVE:
        raise JobError("The server is low on disk space. Please try again later.")
    if expected and free < expected + FREE_SPACE_RESERVE:
        raise JobError(
            "Not enough free disk space on the server for this file.\n"
            "Please try again later or pick a smaller file."
        )


async def _download_to_temp(userbot, job: Job, msg, index: int, count: int, job_dir) -> str:
    _check_free_space(_media_size(msg))
    job_dir.mkdir(parents=True, exist_ok=True)
    target = job_dir / f"{index:02d}_{_safe_name(_media_name(msg, index))}"

    start = time.time()
    out = await userbot.download_media(
        msg,
        file_name=str(target),
        progress=_download_progress(index, count, start),
        progress_args=(job,),
    )
    if not out:
        raise JobError("Failed to download the media for re-upload.")
    return str(out)


def _sent_id(sent) -> int | None:
    if not sent:
        return None
    if isinstance(sent, list):
        sent = sent[0] if sent else None
    return getattr(sent, "id", None) if sent else None


# ---------------------------------------------------------------------------
# Saving to Saved Messages
# ---------------------------------------------------------------------------
async def _store_to_saved(userbot, job: Job, msgs: list) -> tuple[list[int], str | None]:
    """forward -> copy -> (download + re-upload) for each message."""
    saved: list[int] = []
    failed = 0
    oversized = False
    job_dir = JOBS_DIR / job.id
    count = len(msgs)

    for i, m in enumerate(msgs, 1):
        job.step = f"Saving to Saved Messages {i}/{count}" if count > 1 else "Saving to Saved Messages..."
        job.touch()

        sid = None
        try:  # cheapest: a plain forward keeps the original formatting
            sid = _sent_id(await userbot.forward_messages("me", m.chat.id, [m.id]))
        except Exception:
            sid = None
        if sid is None:
            try:  # copy behaves the same but leaves no "forwarded from" header
                sid = _sent_id(await userbot.copy_message("me", m.chat.id, m.id))
            except Exception:
                sid = None
        if sid is None:
            sid, big = await _fallback_save(userbot, job, m, i, count, job_dir)
            oversized = oversized or big

        if sid:
            saved.append(sid)
        else:
            failed += 1

    note = None
    if failed:
        if oversized:
            note = ("One or more files exceed the 2 GB limit for non-Premium accounts. "
                    "Upgrade to Telegram Premium to save larger files.")
        else:
            note = f"{failed} item{'s' if failed != 1 else ''} could not be saved."
    return saved, note


async def _fallback_save(userbot, job: Job, m, index: int, count: int, job_dir):
    """Re-create the message locally and upload it to Saved Messages."""
    oversized = False

    # Text / polls can be re-sent directly -- no file involved.
    if not _downloadable(m):
        if m.poll:
            try:
                poll = m.poll
                sent = await userbot.send_poll(
                    "me",
                    question=getattr(poll, "question", "") or "Poll",
                    options=[getattr(o, "text", "") for o in (getattr(poll, "options", None) or [])],
                )
                return _sent_id(sent), False
            except Exception:
                pass
        text = _text_content(m)
        if text:
            try:
                return _sent_id(await userbot.send_message("me", text)), False
            except Exception:
                pass
        return None, False

    path = None
    try:
        path = await _download_to_temp(userbot, job, m, index, count, job_dir)
        label = f"Uploading {index}/{count} to Saved Messages" if count > 1 else "Uploading to Saved Messages"
        progress = _upload_progress(label, index, count)
        caption = m.caption or None

        if m.photo:
            sent = await userbot.send_photo("me", path, caption=caption,
                                            progress=progress, progress_args=(job,))
        elif m.video:
            sent = await userbot.send_video("me", path, caption=caption,
                                            progress=progress, progress_args=(job,))
        elif m.animation:
            sent = await userbot.send_animation("me", path, caption=caption,
                                                progress=progress, progress_args=(job,))
        elif m.audio:
            sent = await userbot.send_audio("me", path, caption=caption,
                                            progress=progress, progress_args=(job,))
        elif m.voice:
            sent = await userbot.send_voice("me", path,
                                            progress=progress, progress_args=(job,))
        elif m.video_note:
            sent = await userbot.send_video_note("me", path,
                                                 progress=progress, progress_args=(job,))
        else:
            sent = await userbot.send_document("me", path, caption=caption,
                                               progress=progress, progress_args=(job,))
        return _sent_id(sent), False
    except Exception as exc:
        err = str(exc)
        if "2000" in err or "MiB" in err or "MB" in err or "megab" in err.lower():
            oversized = True
        log.warning("Re-upload failed for message %s: %s", m.id, exc)
        return None, oversized
    finally:
        if path:
            try:
                os.remove(path)
            except OSError:
                pass


# ---------------------------------------------------------------------------
# Job body
# ---------------------------------------------------------------------------
async def _run(job: Job) -> None:
    userbot = await get_userbot(job.uid)
    me = await userbot.get_me()

    job.step = "Checking access..."
    job.touch()
    await check_force_sub(userbot, me.id)

    try:
        if job.kind == "batch":
            await _run_batch(userbot, job)
        elif job.kind == "multi":
            await _run_multi(userbot, job)
        else:
            await _run_single(userbot, job)
    finally:
        # Temp files are only needed while a re-upload is in flight.
        shutil.rmtree(JOBS_DIR / job.id, ignore_errors=True)


# ---------------------------------------------------------------------------
# Shared pieces of every save mode
# ---------------------------------------------------------------------------
async def _quota_or_die(uid: int, getter, exhausted: str) -> None:
    """Raise when this mode's rolling daily bucket is empty."""
    remaining, reset_at = await getter(uid)
    if remaining <= 0:
        hrs = int((reset_at - time.time()) // 3600)
        mins = int((reset_at - time.time()) % 3600 // 60)
        raise JobError(f"{exhausted}\nResets in {hrs}h {mins}m.")


async def _finish_save(job: Job, files: int) -> None:
    """Site counters + cooldown shared by every successful save."""
    try:
        await db.increment_web_fetch()
        await db.record_files_saved(files)
    except Exception:
        pass
    if COOLDOWN_SECONDS > 0:
        _COOLDOWN_UNTIL[job.uid] = time.time() + COOLDOWN_SECONDS


def _store_result(job: Job, saved_ids: list[int], items: list[dict]) -> None:
    job.items = items
    job.saved_ids = saved_ids
    job.open_url = f"tg://openmessage?user_id={job.uid}&message_id={saved_ids[0]}"


def _no_content_error() -> JobError:
    return JobError(
        "Message has no savable content.\n"
        "Supported: text, photos, videos, documents, audio, voice, stickers, animations, polls."
    )


async def _save_msg(userbot, job: Job, msg, parsed: dict):
    """Forward one resolved message (and its album) to Saved Messages."""
    msgs = [msg]
    if msg.media_group_id and not parsed.get("single"):
        try:
            group = await userbot.get_media_group(msg.chat.id, msg.id)
            msgs = sorted(group, key=lambda m: m.id)
        except Exception:
            msgs = [msg]
    saved_ids, items, note = await _save_list(userbot, job, msgs)
    return saved_ids, items, note, msgs


async def _save_list(userbot, job: Job, msgs: list):
    """Forward an already assembled message list to Saved Messages."""
    items = _build_items(msgs)
    if not items:
        raise _no_content_error()
    saved_ids, note = await _store_to_saved(userbot, job, msgs)
    if not saved_ids:
        raise JobError(note or "Could not save to Saved Messages. Please try again.")
    return saved_ids, items, note


# ---------------------------------------------------------------------------
# Single save (invite join or one message / album)
# ---------------------------------------------------------------------------
async def _run_single(userbot, job: Job) -> None:
    parsed = parse_link(job.link)

    if parsed["kind"] == "invite":
        job.step = "Joining channel..."
        job.touch()
        job.message = await join_invite(userbot, parsed["link"])
        return

    await _quota_or_die(job.uid, db.get_download_remaining,
                        f"You've used all {DAILY_LIMIT} daily saves.")

    msg = await _resolve_message(userbot, job, parsed)
    saved_ids, items, note, msgs = await _save_msg(userbot, job, msg, parsed)

    text = _text_content(msg)
    if text:
        job.text = text[:MAX_TEXT_PREVIEW]
    _store_result(job, saved_ids, items)

    await db.increment_download_count(job.uid)
    await _finish_save(job, len(items))

    total = len(msgs)
    if len(saved_ids) == total:
        job.message = "Saved to your Saved Messages." if total == 1 else \
            f"Saved {total} messages to your Saved Messages."
    else:
        job.message = f"Saved {len(saved_ids)}/{total} messages to your Saved Messages."
    if note:
        job.message += f" {note}"


# ---------------------------------------------------------------------------
# Batch save (one link, keep saving files forward until N)
# ---------------------------------------------------------------------------
async def _collect_batch(userbot, job: Job, base) -> list:
    """Walk messages newer than `base`, collecting downloadable media files."""
    target = max(1, job.count or 1)
    found: list = []
    seen_ids: set = set()
    seen_groups: set = set()

    def _add(m) -> None:
        if m is not None and not getattr(m, "empty", False) \
                and m.id not in seen_ids and _downloadable(m):
            seen_ids.add(m.id)
            found.append(m)

    _add(base)
    if base.media_group_id:
        seen_groups.add(base.media_group_id)
        try:
            for g in await userbot.get_media_group(base.chat.id, base.id):
                _add(g)
        except Exception:
            pass

    cap = min(target * 10, 500)
    scanned = 0
    empty_run = 0
    next_id = base.id or 0
    while len(found) < target and scanned < cap and empty_run < 50:
        scanned += 1
        next_id += 1
        try:
            m = await userbot.get_messages(base.chat.id, next_id)
        except Exception:
            m = None
        if m is None or getattr(m, "empty", False) or not _has_content(m):
            empty_run += 1
            continue
        empty_run = 0
        if m.service:
            continue
        gid = getattr(m, "media_group_id", None)
        if gid:
            if gid in seen_groups:
                continue
            seen_groups.add(gid)
            added = False
            try:
                group = await userbot.get_media_group(m.chat.id, m.id)
                for g in sorted(group, key=lambda x: x.id):
                    if _downloadable(g):
                        _add(g)
                        added = True
            except Exception:
                added = False
            if not added:
                _add(m)
        else:
            _add(m)
        job.percent = min(50.0, len(found) / target * 50.0)
        job.step = f"Finding files {len(found)}/{target}..."
        job.touch()
    return found[:target]


async def _run_batch(userbot, job: Job) -> None:
    parsed = parse_link(job.link)
    if parsed["kind"] != "message":
        raise JobError("Batch save needs a message link (https://t.me/...).")

    await _quota_or_die(job.uid, db.get_bulk_remaining,
                        "You've used all your bulk saves for today.")

    base = await _resolve_message(userbot, job, parsed, require_content=False)

    target = max(1, job.count or 1)
    found = await _collect_batch(userbot, job, base)
    if not found:
        raise JobError("No savable files were found after this link.")

    job.percent = 50.0
    saved_ids, items, note = await _save_list(userbot, job, found)

    text = _text_content(found[0])
    if text:
        job.text = text[:MAX_TEXT_PREVIEW]
    _store_result(job, saved_ids, items)

    await db.increment_bulk_count(job.uid)
    await _finish_save(job, len(items))

    if len(saved_ids) >= target:
        job.message = f"Saved {len(saved_ids)} files to your Saved Messages."
    else:
        job.message = f"Saved {len(saved_ids)}/{target} files to your Saved Messages."
    if note:
        job.message += f" {note}"


# ---------------------------------------------------------------------------
# Multi save (several links, newline separated, saved together)
# ---------------------------------------------------------------------------
async def _run_multi(userbot, job: Job) -> None:
    links = list(job.links) or [job.link]
    total = len(links)

    await _quota_or_die(job.uid, db.get_multi_remaining,
                        "You've used all your multi-link saves for today.")

    saved_all: list[int] = []
    all_items: list[dict] = []
    notes: list[str] = []
    first_error: str | None = None
    first_text: str | None = None
    ok = 0

    for i, link in enumerate(links, 1):
        job.step = f"Saving link {i}/{total}..."
        job.percent = (i - 1) / max(total, 1) * 100
        job.touch()
        try:
            parsed = parse_link(link)
            if parsed["kind"] != "message":
                raise JobError("Multi-link saves need message links (https://t.me/...).")
            msg = await _resolve_message(userbot, job, parsed)
            saved_ids, items, note, _msgs = await _save_msg(userbot, job, msg, parsed)
        except JobError as exc:
            if first_error is None:
                first_error = str(exc)
            continue
        ok += 1
        saved_all.extend(saved_ids)
        all_items.extend(items)
        if note:
            notes.append(note)
        if first_text is None:
            first_text = _text_content(msg)

    if not saved_all:
        raise JobError(first_error or "Could not save to Saved Messages. Please try again.")

    if first_text:
        job.text = first_text[:MAX_TEXT_PREVIEW]
    _store_result(job, saved_all, all_items)

    await db.increment_multi_count(job.uid)
    await _finish_save(job, len(all_items))

    job.message = (
        f"Saved {total} links to your Saved Messages." if ok >= total
        else f"Saved {ok}/{total} links to your Saved Messages."
    )
    if notes:
        job.message += " " + " ".join(dict.fromkeys(notes))


# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------
def clean_orphan_dirs() -> int:
    """Remove leftover job folders (nothing is kept after a job finishes)."""
    removed = 0
    for entry in list(JOBS_DIR.iterdir()):
        try:
            if entry.is_dir():
                shutil.rmtree(entry, ignore_errors=True)
            else:
                entry.unlink()
            removed += 1
        except OSError:
            pass
    if removed:
        log.info("Startup cleanup removed %s leftover job folder(s)", removed)
    return removed


async def cleanup_loop(interval: int = 60) -> None:
    while True:
        try:
            await asyncio.sleep(interval)
            now = time.time()
            stale = [
                jid for jid, job in JOBS.items()
                if job.status not in ("queued", "running") and now - job.updated > JOB_TTL_SECONDS
            ]
            # Safety net for jobs stuck running (e.g. the server died mid-flight)
            stale += [
                jid for jid, job in JOBS.items()
                if job.status in ("queued", "running") and now - job.updated > 6 * 3600
            ]
            for jid in stale:
                job = JOBS.pop(jid, None)
                if job and job.task and not job.task.done():
                    job.task.cancel()
                shutil.rmtree(JOBS_DIR / jid, ignore_errors=True)
            for uid in [u for u, t in _COOLDOWN_UNTIL.items() if t <= now]:
                _COOLDOWN_UNTIL.pop(uid, None)
            from .auth import sweep_pending
            sweep_pending()
        except asyncio.CancelledError:
            break
        except Exception:
            log.exception("cleanup error")
