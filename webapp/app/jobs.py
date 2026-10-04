"""Fetch jobs: resolve a Telegram link and save the message to Saved Messages.

Nothing is served to the browser -- the file only lives in the user's own
Telegram Saved Messages. Local temp files exist solely for the rare fallback
where Telegram refuses a forward/copy and the file has to be re-uploaded.
"""

import asyncio
import io
import logging
import os
import re
import secrets
import shutil
import time
from dataclasses import dataclass, field

from pyrogram.errors import Flood, RPCError

from .config import (
    DAILY_LIMIT,
    HISTORY_TTL_DAYS,
    JOB_TTL_SECONDS,
    JOBS_DIR,
    COOLDOWN_SECONDS,
    FREE_SPACE_RESERVE,
    MAX_TEXT_PREVIEW,
    getenv_int,
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


class FloodBudgetExceeded(JobError):
    """Telegram asks for a longer wait than one job may take.

    Reported to the user straight away instead of holding the job (and the
    screen behind it) hostage for an hour.
    """


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
    wait_left: int = 0        # seconds left of a Telegram rate-limit wait
    text: str | None = None
    items: list[dict] = field(default_factory=list)
    saved_ids: list[int] = field(default_factory=list)   # ids of what landed
    saved_msgs: int = 0      # source messages that made it (a split file adds ids)
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
        "wait_left": job.wait_left,
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
    except Flood as fw:
        job.status = "error"
        job.error = _flood_message(_flood_seconds(fw) or 1)
    except asyncio.CancelledError:
        # the user pressed Stop: keep whatever was saved so far, drop the rest
        job.status = "aborted"
        job.step = "Stopped"
        job.error = None
        try:
            await _persist(job)
        except Exception:
            log.warning("could not persist aborted job %s", job.id)
        raise
    except Exception as exc:  # noqa: BLE001
        log.exception("Job %s failed", job.id)
        job.status = "error"
        job.error = f"Unexpected error: {exc}"
    finally:
        job.speed = 0
        job.percent = 100.0 if job.status == "done" else job.percent
        job.touch()
        await _persist(job)   # final state for the durable history log


def abort_job(job: Job) -> None:
    """Stop a queued or running job (batch scans, multi-link runs, uploads)."""
    if job.status not in ("queued", "running"):
        raise JobError("This job has already finished.")
    job.status = "aborted"
    job.step = "Stopped"
    job.touch()
    if job.task and not job.task.done():
        job.task.cancel()


# ---------------------------------------------------------------------------
# Rate limits: Telegram asks us to wait, so we wait and show the countdown
# ---------------------------------------------------------------------------
MAX_FLOOD_WAIT = 300      # never hold one call longer than this ...
MAX_FLOOD_TOTAL = 900     # ... and never hold a whole job longer than this
FLOOD_MARGIN = 1          # Telegram's number is a minimum, sleep a bit longer
RPC_RETRY_DELAYS = (2, 5) # transient Telegram-side errors: seconds before retry
CALL_TIMEOUT_SECONDS = getenv_int("CALL_TIMEOUT_SECONDS", 900)   # a dead connection
STUCK_JOB_SECONDS = getenv_int("STUCK_JOB_SECONDS", 2700)      # 45 min without progress

# (uid, call) -> the moment that account may call that method again. Learned
# from every FloodWait, so the next call waits *before* it starts instead of
# tripping the same limit a second after the previous wait ended.
_FLOOD_UNTIL: dict[tuple[int, str], float] = {}


def _flood_message(seconds: float) -> str:
    """User-facing wording for a rate-limit wait (translated by the site)."""
    seconds = max(1, int(seconds))
    if seconds >= 60:
        return f"Rate limited by Telegram. Try again in about {seconds // 60} minutes."
    return f"Rate limited by Telegram. Try again in {seconds} seconds."


def _flood_seconds(exc: BaseException) -> int | None:
    """Seconds Telegram asks for, or None when this error is not a wait."""
    if getattr(type(exc), "CODE", 0) != 420:
        return None
    value = getattr(exc, "value", None)
    if not isinstance(value, int) or value <= 0:
        return None
    return value


def _remember_flood(key: tuple[int, str], seconds: float) -> None:
    until = time.time() + max(0.0, seconds)
    if until > _FLOOD_UNTIL.get(key, 0.0):
        _FLOOD_UNTIL[key] = until
    if len(_FLOOD_UNTIL) > 512:
        now = time.time()
        for stale in [k for k, v in _FLOOD_UNTIL.items() if v <= now]:
            _FLOOD_UNTIL.pop(stale, None)


async def _flood_sleep(job: Job, seconds: float) -> None:
    """Sleep through a FloodWait while the UI shows the time left."""
    left = max(1, int(seconds))
    while left > 0:
        job.step = f"Rate limited by Telegram, waiting {left}s"
        job.wait_left = left
        job.touch()
        await asyncio.sleep(1)
        left -= 1
    job.wait_left = 0
    job.step = "Resuming..."
    job.touch()


async def _call(job: Job, what: str, fn):
    """Run one Telegram API call, waiting out rate-limit errors.

    A FloodWait is not a failure: Telegram says exactly how long to hold
    off, so the job waits, shows a live countdown and retries. Four things
    keep that from snowballing:

    * the deadline is remembered per account and per call, so the calls that
      follow wait up front rather than all re-triggering the limit;
    * every wait gets a safety margin, because Telegram's number is a
      minimum and retrying at exactly that moment often fails again;
    * a wait longer than the budget is reported at once instead of holding
      the job for an hour;
    * a transient Telegram-side error (HTTP 5xx) is retried with a short
      backoff instead of failing the whole job.
    """
    key = (job.uid, what)
    spent = 0
    tries = 0
    while True:
        pending = _FLOOD_UNTIL.get(key, 0.0) - time.time()
        if pending > 0:
            if pending > MAX_FLOOD_WAIT or spent + pending > MAX_FLOOD_TOTAL:
                raise FloodBudgetExceeded(_flood_message(pending))
            spent += pending
            log.info("Holding %s for %ss (rate limit seen earlier)", what, int(pending))
            await _flood_sleep(job, pending + FLOOD_MARGIN)
        try:
            return await asyncio.wait_for(fn(), CALL_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            # a dropped connection would otherwise hang the job forever
            raise JobError("Telegram stopped responding. Please try again.") from None
        except Flood as exc:
            delay = _flood_seconds(exc)
            if delay is None:
                raise     # a flood error with no duration: nothing to wait out
            _remember_flood(key, delay + FLOOD_MARGIN)
            if delay > MAX_FLOOD_WAIT or spent + delay > MAX_FLOOD_TOTAL:
                raise FloodBudgetExceeded(_flood_message(delay)) from None
            spent += delay
            log.warning("FloodWait of %ss during %s, waiting", delay, what)
            await _flood_sleep(job, delay + FLOOD_MARGIN)
        except RPCError as exc:
            code = getattr(type(exc), "CODE", 0) or 0
            if code < 500 or tries >= len(RPC_RETRY_DELAYS):
                raise
            delay = RPC_RETRY_DELAYS[tries]
            tries += 1
            log.warning("Telegram failed during %s (%s), retrying in %ss",
                        what, exc, delay)
            job.step = f"Retrying in {delay}s..."
            job.touch()
            await asyncio.sleep(delay)


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
            candidate = await _call(job, "get_messages",
                                   lambda cid=cid: userbot.get_messages(cid, msg_id))
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
                    candidate = await _call(job, "get_messages",
                                           lambda cid=cid: userbot.get_messages(cid, msg_id))
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
    state = {"bytes": 0, "at": time.time()}

    async def _cb(current, total, job: Job):
        now = time.time()
        if now - state["at"] >= 1:
            # show the upload's own speed, not the download's from earlier
            job.speed = max(0, int((current - state["bytes"]) / (now - state["at"])))
            state["bytes"] = current
            state["at"] = now
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


# ---------------------------------------------------------------------------
# Oversized files: Telegram caps one upload, so a big file goes as parts
# ---------------------------------------------------------------------------
# Pyrogram refuses anything above these numbers, which mirror Telegram's own
# limits (2000 MiB without Premium, 4000 MiB with it).
FREE_UPLOAD_BYTES = getenv_int("FREE_UPLOAD_MIB", 2000) * 1024 * 1024
PREMIUM_UPLOAD_BYTES = getenv_int("PREMIUM_UPLOAD_MIB", 4000) * 1024 * 1024


def _upload_limit(userbot) -> int:
    """Bytes Telegram accepts in one upload from this account."""
    me = getattr(userbot, "me", None)
    return PREMIUM_UPLOAD_BYTES if getattr(me, "is_premium", False) else FREE_UPLOAD_BYTES


def _part_name(base: str, index: int) -> str:
    """file.7z -> file.7z.001, so `cat file.7z.* > file.7z` puts it back.

    `_safe_name` keeps `base` at 150 characters, so the suffix always fits
    inside Telegram's file name limit.
    """
    return f"{base}.{index:03d}"


class _PartReader(io.IOBase):
    """A byte range of a local file, handed to Pyrogram as its own file.

    Pyrogram reads a file object with seek/tell/read and takes the upload
    name from `.name`, so a part needs no copy on disk: the slice that fits
    in one Telegram upload is read straight out of the already downloaded
    file and closes after its message has been sent.
    """

    def __init__(self, path: str, offset: int, length: int, name: str):
        super().__init__()
        self._fh = open(path, "rb")
        self._offset = offset
        self._length = length
        self._pos = 0
        self.name = name

    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        if whence == io.SEEK_SET:
            self._pos = offset
        elif whence == io.SEEK_CUR:
            self._pos += offset
        elif whence == io.SEEK_END:
            self._pos = self._length + offset
        else:
            raise ValueError(f"invalid whence ({whence})")
        return self._pos

    def tell(self) -> int:
        return self._pos

    def read(self, size: int = -1) -> bytes:
        if size is None or size < 0:
            size = self._length - self._pos
        size = min(size, max(0, self._length - self._pos))
        if not size:
            return b""
        self._fh.seek(self._offset + self._pos)
        chunk = self._fh.read(size)
        self._pos += len(chunk)
        return chunk

    def readable(self) -> bool:
        return True

    def close(self) -> None:
        try:
            self._fh.close()
        finally:
            super().close()


def _part_progress(label: str, offset: int, size: int, index: int, count: int,
                   state: dict):
    """Progress across every part of one file, with a live upload speed."""
    async def _cb(current, total, job: Job):
        done = offset + current            # bytes of this file sent so far
        now = time.time()
        if now - state["at"] >= 1:
            job.speed = max(0, int((done - state["bytes"]) / (now - state["at"])))
            state["bytes"] = done
            state["at"] = now
        job.percent = min(
            99.9, ((index - 1) + (done / size if size else 0)) / max(count, 1) * 100
        )
        job.downloaded = done
        job.total = size
        job.step = label
        job.touch()

    return _cb


async def _upload_in_parts(userbot, job: Job, path: str, base: str, caption: str | None,
                           index: int, count: int) -> tuple[list[int], int]:
    """Upload one file as several documents, each within Telegram's cap.

    The parts are read straight out of the downloaded file, so splitting
    costs no extra disk space. Each part is its own message
    (file.7z.001, file.7z.002, ...), which is also the only way an account
    without Premium can deliver a file this size. Returns the sent message
    ids and the number of parts.
    """
    size = os.path.getsize(path)
    limit = _upload_limit(userbot)
    parts = max(1, -(-size // limit))      # ceil division
    state = {"bytes": 0, "at": time.time()}
    sent: list[int] = []

    for p in range(parts):
        offset = p * limit
        length = min(limit, size - offset)
        label = f"Uploading part {p + 1}/{parts} to Saved Messages"
        part = _PartReader(path, offset, length, _part_name(base, p + 1))
        progress = _part_progress(label, offset, size, index, count, state)
        try:
            # only the first part keeps the caption: the parts go together
            one = await _call(
                job, "upload",
                lambda: userbot.send_document("me", part, caption=caption if p == 0 else None,
                                              progress=progress, progress_args=(job,)),
            )
        finally:
            part.close()
        sid = _sent_id(one)
        if not sid:
            raise JobError("Could not save to Saved Messages. Please try again.")
        sent.append(sid)
    return sent, parts


async def _download_to_temp(userbot, job: Job, msg, index: int, count: int, job_dir) -> str:
    _check_free_space(_media_size(msg))
    job_dir.mkdir(parents=True, exist_ok=True)
    target = job_dir / f"{index:02d}_{_safe_name(_media_name(msg, index))}"

    start = time.time()
    out = await _call(
        job, "download_media",
        lambda: userbot.download_media(
            msg,
            file_name=str(target),
            progress=_download_progress(index, count, start),
            progress_args=(job,),
        ),
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


def _ids(sent) -> list[int]:
    """Message ids of one send result, as a list (possibly empty)."""
    sid = _sent_id(sent)
    return [sid] if sid else []


# ---------------------------------------------------------------------------
# Saving to Saved Messages
# ---------------------------------------------------------------------------
async def _store_to_saved(userbot, job: Job, msgs: list) -> tuple[list[int], str | None]:
    """forward -> copy -> (download + re-upload) for each message."""
    saved: list[int] = []
    failed = 0
    oversized = False
    split_msgs = 0
    split_parts = 0
    job.saved_msgs = 0        # counted per source message, not per sent part
    job_dir = JOBS_DIR / job.id
    count = len(msgs)

    for i, m in enumerate(msgs, 1):
        job.step = f"Saving to Saved Messages {i}/{count}" if count > 1 else "Saving to Saved Messages..."
        job.touch()

        ids: list[int] = []
        try:  # cheapest: a plain forward keeps the original formatting
            ids = _ids(await _call(job, "forward",
                                   lambda: userbot.forward_messages("me", m.chat.id, [m.id])))
        except FloodBudgetExceeded:
            raise        # the account must cool down; no point trying the rest
        except Exception:
            ids = []
        if not ids:
            try:  # copy behaves the same but leaves no "forwarded from" header
                ids = _ids(await _call(job, "copy",
                                       lambda: userbot.copy_message("me", m.chat.id, m.id)))
            except FloodBudgetExceeded:
                raise
            except Exception:
                ids = []
        if not ids:
            ids, big, parts = await _fallback_save(userbot, job, m, i, count, job_dir)
            oversized = oversized or big
            if parts:
                split_msgs += 1
                split_parts = parts

        if ids:
            saved.extend(ids)
            job.saved_msgs += 1
        else:
            failed += 1
            log.warning("Message %s could not be saved at all (chat=%s)", m.id,
                        getattr(getattr(m, "chat", None), "id", None))

    note = None
    if failed:
        if oversized:
            note = ("One or more files exceed the 2 GB limit for non-Premium accounts. "
                    "Upgrade to Telegram Premium to save larger files.")
        else:
            note = f"{failed} item{'s' if failed != 1 else ''} could not be saved."
    if split_msgs:
        split_note = (
            f"Split into {split_parts} parts; "
            "join them in order to restore the original file."
            if split_msgs == 1 else
            "Split into parts; join each set of parts in order to restore the original file."
        )
        note = f"{note} {split_note}" if note else split_note
    return saved, note


async def _fallback_save(userbot, job: Job, m, index: int, count: int, job_dir):
    """Re-create the message locally and upload it to Saved Messages.

    Returns (sent message ids, over-sized, parts): `parts` is how many
    messages one file was split into to fit Telegram's per-upload limit,
    or 0 when it went in one piece.
    """
    oversized = False

    # Text / polls can be re-sent directly -- no file involved.
    if not _downloadable(m):
        if m.poll:
            try:
                poll = m.poll
                sent = await _call(
                    job, "send_poll",
                    lambda: userbot.send_poll(
                        "me",
                        question=getattr(poll, "question", "") or "Poll",
                        options=[getattr(o, "text", "") for o in
                                 (getattr(poll, "options", None) or [])],
                    ),
                )
                return _ids(sent), False, 0
            except FloodBudgetExceeded:
                raise
            except Exception:
                pass
        text = _text_content(m)
        if text:
            try:
                return _ids(await _call(job, "send_message",
                                        lambda: userbot.send_message("me", text))), False, 0
            except FloodBudgetExceeded:
                raise
            except Exception:
                pass
        return [], False, 0

    path = None
    try:
        path = await _download_to_temp(userbot, job, m, index, count, job_dir)
        caption = m.caption or None
        base = _safe_name(_media_name(m, index))

        if os.path.getsize(path) > _upload_limit(userbot):
            # Too big for a single Telegram upload: send it as numbered parts
            # (file.7z.001, file.7z.002, ...) read straight from this file.
            ids, parts = await _upload_in_parts(userbot, job, path, base, caption,
                                                index, count)
            return ids, False, parts

        label = f"Uploading {index}/{count} to Saved Messages" if count > 1 else "Uploading to Saved Messages"
        progress = _upload_progress(label, index, count)

        if m.photo:
            send = lambda: userbot.send_photo("me", path, caption=caption,
                                              progress=progress, progress_args=(job,))
        elif m.video:
            send = lambda: userbot.send_video("me", path, caption=caption,
                                              progress=progress, progress_args=(job,))
        elif m.animation:
            send = lambda: userbot.send_animation("me", path, caption=caption,
                                                  progress=progress, progress_args=(job,))
        elif m.audio:
            send = lambda: userbot.send_audio("me", path, caption=caption,
                                              progress=progress, progress_args=(job,))
        elif m.voice:
            send = lambda: userbot.send_voice("me", path,
                                              progress=progress, progress_args=(job,))
        elif m.video_note:
            send = lambda: userbot.send_video_note("me", path,
                                                   progress=progress, progress_args=(job,))
        else:
            send = lambda: userbot.send_document("me", path, caption=caption,
                                                 progress=progress, progress_args=(job,))
        sent = await _call(job, "upload", send)
        return _ids(sent), False, 0
    except FloodBudgetExceeded:
        raise        # Telegram wants a cool-down: report it, do not bury it
    except Exception as exc:
        err = str(exc)
        if "2000" in err or "MiB" in err or "MB" in err or "megab" in err.lower():
            oversized = True
        log.warning("Re-upload failed for message %s: %s", m.id, exc)
        return [], oversized, 0
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
            group = await _call(job, "get_media_group",
                                lambda: userbot.get_media_group(msg.chat.id, msg.id))
            msgs = sorted(group, key=lambda m: m.id)
        except Exception:
            log.warning("Album around message %s could not be read", msg.id, exc_info=True)
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
    # `saved_ids` can hold more ids than messages: a split file lands as parts
    if job.saved_msgs >= total:
        job.message = "Saved to your Saved Messages." if total == 1 else \
            f"Saved {total} messages to your Saved Messages."
    else:
        job.message = f"Saved {job.saved_msgs}/{total} messages to your Saved Messages."
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
            for g in await _call(job, "get_media_group",
                                lambda: userbot.get_media_group(base.chat.id, base.id)):
                _add(g)
        except Exception:
            log.warning("Album around message %s could not be read", base.id, exc_info=True)

    cap = min(target * 10, 500)
    scanned = 0
    empty_run = 0
    next_id = base.id or 0
    while len(found) < target and scanned < cap and empty_run < 50:
        scanned += 1
        next_id += 1
        try:
            m = await _call(job, "get_messages",
                            lambda: userbot.get_messages(base.chat.id, next_id))
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
                group = await _call(job, "get_media_group",
                                    lambda: userbot.get_media_group(m.chat.id, m.id))
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

    if job.saved_msgs >= target:
        job.message = f"Saved {job.saved_msgs} files to your Saved Messages."
    else:
        job.message = f"Saved {job.saved_msgs}/{target} files to your Saved Messages."
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
        except FloodBudgetExceeded:
            raise        # rate limited: stop the whole run instead of hammering
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


def sweep_orphan_folders() -> int:
    """Remove job folders whose job no longer exists in memory.

    A normal job deletes its own folder when it finishes; this catches the
    rest (a killed process, a folder left behind by a failed removal) while
    the server keeps running.
    """
    if not JOBS_DIR.exists():
        return 0
    live = set(JOBS.keys())
    removed = 0
    for entry in list(JOBS_DIR.iterdir()):
        if entry.name in live:
            continue          # still being written to by a live job
        try:
            if entry.is_dir():
                shutil.rmtree(entry, ignore_errors=True)
            else:
                entry.unlink()
            removed += 1
            log.warning("Removed orphan job folder %s", entry.name)
        except OSError:
            pass
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
                if job.status in ("queued", "running") and now - job.updated > STUCK_JOB_SECONDS
            ]
            for jid in stale:
                job = JOBS.pop(jid, None)
                if job and job.task and not job.task.done():
                    job.task.cancel()
                shutil.rmtree(JOBS_DIR / jid, ignore_errors=True)
            for uid in [u for u, t in _COOLDOWN_UNTIL.items() if t <= now]:
                _COOLDOWN_UNTIL.pop(uid, None)
            sweep_orphan_folders()   # self-heal: no job, no folder
            from .auth import sweep_pending
            sweep_pending()
        except asyncio.CancelledError:
            break
        except Exception:
            log.exception("cleanup error")
