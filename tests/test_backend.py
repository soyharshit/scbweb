"""Integration test for the web client: fake DB + fake userbot + real FastAPI app."""

import asyncio
import os
import shutil
import sys
import time

WEBAPP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, WEBAPP_DIR)

from starlette.testclient import TestClient

from app.main import app
from app.db import db as mdb
from app import jobs as jobs_mod
from app import main as main_mod
from app import tg as tg_mod
from app.auth import PHONE_RE, LoginError
from app.config import (BULK_SAVES_PER_DAY, BULK_MAX_FILES,
                        MULTI_SAVES_PER_DAY, MULTI_MAX_LINKS)
from pyrogram.errors import FloodWait

# The real phone login would connect to Telegram; stub it but keep the format
# validation so malformed numbers still fail the same way.
_PHONES: list[str] = []


async def _fake_start_phone(phone):
    if not PHONE_RE.match(phone):
        raise LoginError(
            "Enter a valid phone number in international format, e.g. +15854346400"
        )
    _PHONES.append(phone)
    return {"handle": "handle-1", "via": "Telegram"}


main_mod.start_phone_login = _fake_start_phone

# ---------------------------------------------------------------------------
# Fake DB (MongoDB is unreachable in this sandbox)
# ---------------------------------------------------------------------------
_USERS: dict[int, dict] = {}
_TOKENS: dict[str, int] = {}
_STATS = {"web_fetches": 0}


async def _add_user(uid, phone=None, ip=None):
    _USERS.setdefault(uid, {"id": uid, "banned": False})
    if phone:
        _USERS[uid]["phone"] = phone
    if ip:
        _USERS[uid]["ip"] = ip
async def _is_banned(uid): return bool(_USERS.get(uid, {}).get("banned"))
async def _get_name(uid): return _USERS.get(uid, {}).get("first_name")
async def _store_login(uid, a, b, s, name=None):
    _USERS.setdefault(uid, {"id": uid})
    _USERS[uid].update({"log": True, "session": s, "first_name": name})
async def _clear_login(uid): _USERS.get(uid, {}).pop("session", None)
async def _get_cred(uid):
    u = _USERS.get(uid)
    return (None, None, None) if not u else (1, "h", u.get("session"))
async def _is_logged(uid): return bool(_USERS.get(uid, {}).get("session"))
async def _remaining(uid): return (20, 0)
async def _inc_dl(uid):
    if not _active_unlimited(uid):
        _STATS["dl"] = _STATS.get("dl", 0) + 1
# per-kind buckets (bulk = batch saves, multi = multi-link saves)
_BULK = {"left": None}      # None = fresh bucket (full daily limit)
_MULTI = {"left": None}
async def _bulk_left(uid):
    if _active_unlimited(uid):
        return (BULK_SAVES_PER_DAY, 0)
    return (BULK_SAVES_PER_DAY, 0) if _BULK["left"] is None else (_BULK["left"], 0)
async def _inc_bulk(uid):
    if not _active_unlimited(uid):
        _STATS["bulk"] = _STATS.get("bulk", 0) + 1
async def _multi_left(uid):
    if _active_unlimited(uid):
        return (MULTI_SAVES_PER_DAY, 0)
    return (MULTI_SAVES_PER_DAY, 0) if _MULTI["left"] is None else (_MULTI["left"], 0)
async def _inc_multi(uid):
    if not _active_unlimited(uid):
        _STATS["multi"] = _STATS.get("multi", 0) + 1
async def _inc_fetch(): _STATS["web_fetches"] += 1
_TOKEN_SEQ = [0]

# admin-granted unlimited quota: uid -> the moment it ends
_UNLIMITED: dict[int, float] = {}


def _active_unlimited(uid) -> float:
    until = _UNLIMITED.get(uid, 0)
    return until if until > time.time() else 0


async def _set_unlimited(uid, days) -> float:
    until = time.time() + max(1, int(days)) * 86400
    _UNLIMITED[uid] = until
    _USERS.setdefault(uid, {"id": uid})["unlimited_until"] = until
    return until


async def _clear_unlimited(uid) -> None:
    _UNLIMITED.pop(uid, None)
    _USERS.get(uid, {}).pop("unlimited_until", None)


async def _unlimited_until(uid) -> float:
    return _active_unlimited(uid)


async def _create_token(uid):
    _TOKEN_SEQ[0] += 1
    tok = f"tok_{uid}_{_TOKEN_SEQ[0]}"
    _TOKENS[tok] = uid
    return tok
async def _token_uid(tok): return _TOKENS.get(tok)
async def _revoke(tok): _TOKENS.pop(tok, None)
async def _ensure(): pass
async def _totals(): return {"users": len(_USERS), "logged_in": 1, "web_fetches": _STATS["web_fetches"]}

# site settings (admin password, notice banner, save switch)
_SETTINGS = {
    "admin_pin_hash": None,   # filled below via the real hash helper
    "admin_pin_changed": False,
    "notice": "",
    "saving_enabled": True,
}


async def _get_settings(): return dict(_SETTINGS)
async def _set_settings(values): _SETTINGS.update(dict(values))
async def _check_pin(pin):
    return _SETTINGS["admin_pin_hash"] == mdb._hash_pin(pin)
async def _set_pin(pin):
    _SETTINGS.update({"admin_pin_hash": mdb._hash_pin(pin), "admin_pin_changed": True})
async def _set_banned(uid, banned):
    _USERS.setdefault(uid, {"id": uid})["banned"] = bool(banned)
async def _revoke_user_tokens(uid):
    for t in [k for k, v in _TOKENS.items() if v == uid]:
        _TOKENS.pop(t, None)
async def _record_visit(uid, ip=None):
    _STATS["visits"] = _STATS.get("visits", 0) + 1
    if ip:
        _USERS.setdefault(uid, {"id": uid})["ip"] = ip
async def _record_login(uid, ip=None):
    _STATS["logins"] = _STATS.get("logins", 0) + 1
    if ip:
        _USERS.setdefault(uid, {"id": uid})["ip"] = ip
async def _record_files(n): _STATS["files_saved"] = _STATS.get("files_saved", 0) + n


async def _admin_stats():
    from datetime import date, timedelta
    today = date.today()
    daily = [
        {"day": (today - timedelta(days=i)).isoformat(), "files": i, "visits": i, "logins": 0}
        for i in range(29, -1, -1)
    ]
    return {
        "totals": {
            "users": len(_USERS), "logged_in": 1, "banned": 0,
            "visited": 1, "returning": 0,
            "files_saved": _STATS.get("files_saved", 0),
            "jobs_total": _STATS["web_fetches"],
            "visits_today": 0, "saves_today": 0,
        },
        "daily": daily,
        "users": [{"uid": uid, "name": f"User {uid}", "banned": bool(u.get("banned")),
                   "logged_in": bool(u.get("log")), "visits": 1, "last_seen": 0,
                   "saves_today": 0, "phone": u.get("phone") or "", "ip": u.get("ip") or "",
                   "unlimited_until": _active_unlimited(uid)}
                  for uid, u in _USERS.items()],
    }

# durable history log (in-memory stand-in for Mongo's web_history collection)
_HISTORY: dict[str, dict] = {}


async def _save_history(doc): _HISTORY[doc["id"]] = dict(doc)
async def _get_history(uid, limit=100):
    docs = [dict(d) for d in _HISTORY.values() if d.get("uid") == uid]
    docs.sort(key=lambda d: d.get("created", 0), reverse=True)
    return docs[:limit]
async def _mark_interrupted():
    for d in _HISTORY.values():
        if d.get("status") in ("queued", "running"):
            d.update({"status": "error", "step": "Failed",
                      "error": "Interrupted by a server restart."})

for name, fn in dict(add_user=_add_user, is_banned=_is_banned, get_name=_get_name,
                     store_login=_store_login, clear_login=_clear_login,
                     get_credentials=_get_cred, is_logged=_is_logged,
                     get_download_remaining=_remaining, increment_download_count=_inc_dl,
                     get_bulk_remaining=_bulk_left, increment_bulk_count=_inc_bulk,
                     get_multi_remaining=_multi_left, increment_multi_count=_inc_multi,
                     increment_web_fetch=_inc_fetch, create_token=_create_token,
                     token_uid=_token_uid, revoke_token=_revoke,
                     ensure_indexes=_ensure, totals=_totals,
                     save_history=_save_history, get_history=_get_history,
                     mark_interrupted=_mark_interrupted,
                     get_settings=_get_settings, set_settings=_set_settings,
                     check_admin_pin=_check_pin, set_admin_pin=_set_pin,
                     set_banned=_set_banned, revoke_user_tokens=_revoke_user_tokens,
                     set_unlimited=_set_unlimited, clear_unlimited=_clear_unlimited,
                     unlimited_until=_unlimited_until,
                     record_visit=_record_visit, record_login=_record_login,
                     record_files_saved=_record_files, admin_stats=_admin_stats).items():
    setattr(mdb, name, fn)

_SETTINGS["admin_pin_hash"] = mdb._hash_pin("123456")

# ---------------------------------------------------------------------------
# Fake userbot
# ---------------------------------------------------------------------------
class Obj:
    def __init__(self, **kw): self.__dict__.update(kw)


def make_msg(mid, kind="photo"):
    m = Obj(
        id=mid, empty=False, service=None, text=None, caption=None, sticker=None,
        poll=None, media_group_id=None, photo=None, document=None, video=None,
        audio=None, voice=None, video_note=None, animation=None,
        chat=Obj(id=-1001234), date=time.time(), media=None,
    )
    if kind == "photo":
        m.media = "photo"
        m.photo = Obj(file_name=None, file_size=1234, mime_type="image/jpeg")
        m.caption = "hello caption"
    elif kind == "text":
        m.media = None
        m.text = "just some text\nsecond line"
    elif kind == "poll":
        m.media = None
        m.poll = Obj(question="Best colour?", options=[Obj(text="Red"), Obj(text="Blue")])
    elif kind == "album1":
        m.media = "photo"
        m.photo = Obj(file_name=None, file_size=100, mime_type="image/jpeg")
        m.media_group_id = "group-1"
    elif kind == "album2":
        m.media = "photo"
        m.photo = Obj(file_name=None, file_size=200, mime_type="image/jpeg")
        m.media_group_id = "group-1"
    elif kind == "doc":
        m.media = "document"
        m.document = Obj(file_name="report.pdf", file_size=2048, mime_type="application/pdf")
    return m


class FakeUserbot:
    def __init__(self):
        self.me = Obj(id=111, first_name="Tester", username="tester", is_premium=False)
        self.downloaded = []
        self.joined = []
        self.forwarded = []
        self.copied = []
        self.uploaded = []
        self.parts = []          # (name, bytes) of every split part that was sent
        self.sent_text = []
        self.fail_ids = set()  # message ids where forward+copy are refused
        self.flood_ids = set()  # message ids that answer FloodWait once
        self.slow_ids = set()  # downloads that take a while (abort mid-download)
        self.slow_forward_ids = set()  # forwards that take a while (abort while running)
        self.no_download_ids = set()  # message ids whose download returns nothing
        self.hang_ids = set()  # message ids whose forward never answers

    async def get_me(self): return self.me
    async def get_chat_member(self, chat, uid): return Obj()
    async def get_chat(self, username): return Obj(id=-100999)
    async def get_dialogs(self, limit=500):
        if False:
            yield None

    async def get_messages(self, cid, mid):
        last = mid % 10
        if last == 7:
            return make_msg(mid, "text")
        if last == 8:
            return make_msg(mid, "album1")
        if last == 9:
            return make_msg(mid, "poll")
        if last == 6:
            return make_msg(mid, "doc")
        return make_msg(mid, "photo")

    async def get_media_group(self, cid, mid):
        return [make_msg(mid, "album1"), make_msg(mid + 1, "album2")]

    async def join_chat(self, link):
        self.joined.append(link)
        return Obj(id=-100555)

    async def download_media(self, msg, file_name=None, progress=None, progress_args=()):
        if msg.id in self.no_download_ids:
            return None
        payload = b"X" * (msg.id % 100 + 10)
        with open(file_name, "wb") as fh:
            fh.write(payload)
        if progress:
            await progress(len(payload), len(payload), *progress_args)
        if msg.id in self.slow_ids:
            await asyncio.sleep(2)     # mid-download: long enough to abort
        self.downloaded.append(file_name)
        return file_name

    # --- saving to Saved Messages ---
    async def forward_messages(self, chat, from_chat, msg_ids):
        mid = msg_ids[0] if isinstance(msg_ids, (list, tuple)) else msg_ids
        if mid in self.flood_ids:
            self.flood_ids.discard(mid)   # only the first attempt is rate limited
            raise FloodWait(2)
        if mid in self.slow_forward_ids:
            await asyncio.sleep(2)       # keep the job running long enough to abort
        if mid in self.hang_ids:
            await asyncio.sleep(30)      # never answers: dropped connection
        if mid in self.fail_ids:
            raise RuntimeError("MESSAGE_FORBIDDEN")
        self.forwarded.append((chat, from_chat, mid))
        return [Obj(id=90000 + int(mid))]

    async def copy_message(self, chat, from_chat, msg_id):
        if msg_id in self.fail_ids:
            raise RuntimeError("MESSAGE_FORBIDDEN")
        self.copied.append((chat, from_chat, msg_id))
        return Obj(id=80000 + int(msg_id))

    async def _send(self, kind, path, **kw):
        self.uploaded.append((kind, path))
        if hasattr(path, "read"):        # a split part: keep its name and bytes
            data = path.read()
            path.seek(0)
            self.parts.append((path.name, data))
        return Obj(id=70000 + len(self.uploaded))

    async def send_photo(self, chat, path, **kw): return await self._send("photo", path)
    async def send_video(self, chat, path, **kw): return await self._send("video", path)
    async def send_animation(self, chat, path, **kw): return await self._send("animation", path)
    async def send_audio(self, chat, path, **kw): return await self._send("audio", path)
    async def send_voice(self, chat, path, **kw): return await self._send("voice", path)
    async def send_video_note(self, chat, path, **kw): return await self._send("video_note", path)
    async def send_document(self, chat, path, **kw): return await self._send("document", path)

    async def send_message(self, chat, text, **kw):
        self.sent_text.append(text)
        return Obj(id=60000 + len(self.sent_text))

    async def send_poll(self, chat, question=None, options=None, **kw):
        self.sent_text.append(f"POLL {question} {options}")
        return Obj(id=50000 + len(self.sent_text))


FAKE = FakeUserbot()


async def fake_get_userbot(uid): return FAKE
async def fake_force(client, uid): return None

jobs_mod.get_userbot = fake_get_userbot
jobs_mod.check_force_sub = fake_force

# ---------------------------------------------------------------------------
results = []


def check(name, cond, extra=""):
    results.append((name, bool(cond), extra))
    print(("PASS " if cond else "FAIL ") + name + (f"  {extra}" if extra and not cond else ""))


def wait_job(client, jid, tries=60):
    final = None
    for _ in range(tries):
        time.sleep(0.15)
        r = client.get(f"/api/jobs/{jid}")
        if r.status_code != 200:
            return {"status": "http-error", "raw": r.text}
        final = r.json()["job"]
        if final["status"] in ("done", "error", "aborted"):
            return final
    return final


def fetch(client, link, **body):
    r = client.post("/api/fetch", json={"link": link, **body})
    if r.status_code != 200:
        return r.status_code, {"error": r.json().get("error", r.text)}
    return 200, wait_job(client, r.json()["job"]["id"])


def saved_to_telegram(job, uid=111):
    return (job.get("open_url") or "").startswith(f"tg://openmessage?user_id={uid}&message_id=")


def set_cookie(value):
    """Set the session cookie, dropping every older copy (the admin login
    response adds a server-domain cookie that would otherwise shadow it)."""
    for c in list(client.cookies.jar):
        if c.name == "tg_web_session":
            try:
                client.cookies.jar.clear(c.domain, c.path, c.name)
            except Exception:
                pass
    client.cookies.set("tg_web_session", value)


def get_cookie():
    return next((c.value for c in client.cookies.jar if c.name == "tg_web_session"), None)


with TestClient(app, raise_server_exceptions=False) as client:
    # Pyrogram only fills client.me inside Client.start(); the web client uses
    # connect(), so every path has to bind it or uploads fail on me.is_premium
    plain = Obj(me=None)
    tg_mod._bind_me(plain, Obj(is_premium=False, first_name="Tester"))
    check("client.me bound so uploads can read it",
          plain.me is not None and plain._is_premium is False, str(plain.me))
    prem = Obj(me=None)
    tg_mod._bind_me(prem, Obj(is_premium=True))
    check("premium flag carried over",
          prem.me.is_premium is True and prem._is_premium is True, str(prem.me))
    empty = Obj(me=None)
    tg_mod._bind_me(empty, None)
    check("a missing me leaves the client alone", empty.me is None, str(empty.me))

    # --- static & auth ---
    r = client.get("/")
    check("index served", r.status_code == 200 and "Save Contents" in r.text)
    check("no download checkbox", "saveToggle" not in r.text)
    check("js served", client.get("/app.js").status_code == 200)
    check("css served", client.get("/style.css").status_code == 200)
    check("health probe", client.get("/health").json() == {"status": "ok"})
    out_me = client.get("/api/me").json()
    check("me logged out", out_me.get("logged_in") is False, str(out_me))
    check("logged-out visitors still get the login notice",
          "notice_login" in out_me and "notice_login_color" in out_me, str(out_me))
    check("fetch unauthorised",
          client.post("/api/fetch", json={"link": "https://t.me/x/1"}).status_code == 401)
    r = client.post("/api/login/start", json={"phone": "nope"})
    check("bad phone rejected", r.status_code == 400 and "international" in r.json()["error"])

    # long garbage session string must fail fast (no interactive hang)
    t0 = time.time()
    r = client.post("/api/login/session", json={"session": "A" * 400})
    elapsed = time.time() - t0
    check("garbage session rejected fast", r.status_code == 400 and elapsed < 10,
          f"{r.status_code} {elapsed:.1f}s {r.text[:120]}")

    # --- session cookie ---
    tok = asyncio.get_event_loop().run_until_complete(_create_token(111))
    set_cookie(tok)
    me = client.get("/api/me").json()
    check("me logged in", me.get("logged_in") is True and me.get("uid") == 111, str(me))
    check("quota present", me.get("remaining") == 20 and me.get("daily_limit") == 20, str(me))
    check("bulk/multi quota present",
          me.get("bulk_remaining") == BULK_SAVES_PER_DAY
          and me.get("multi_remaining") == MULTI_SAVES_PER_DAY
          and me.get("bulk_max_files") == BULK_MAX_FILES
          and me.get("multi_max_links") == MULTI_MAX_LINKS, str(me))

    # --- link validation ---
    check("no link rejected",
          client.post("/api/fetch", json={"link": "hello world"}).status_code == 400)
    check("non-telegram rejected",
          client.post("/api/fetch", json={"link": "https://example.com/x/1"}).status_code == 400)
    check("malformed t.me/c rejected",
          client.post("/api/fetch", json={"link": "https://t.me/c/99"}).status_code == 400)

    # --- photo + caption: forwarded to Saved Messages, nothing downloadable ---
    code, job = fetch(client, "https://t.me/sometechnicalchannel/12345")
    check("photo job done", code == 200 and job["status"] == "done", str(job))
    check("photo saved (open url)", saved_to_telegram(job), str(job.get("open_url")))
    check("photo forwarded", any(f[2] == 12345 for f in FAKE.forwarded), str(FAKE.forwarded[-3:]))
    check("photo no re-upload", not FAKE.uploaded, str(FAKE.uploaded))
    check("photo items", len(job.get("items", [])) == 1 and job["items"][0]["kind"] == "photo",
          str(job.get("items")))
    check("no files key", "files" not in job, str(job.keys()))
    check("caption preview", job["text"] == "hello caption", str(job.get("text")))
    check("temp dir cleaned", not (jobs_mod.JOBS_DIR / job["id"]).exists(), job["id"])

    # the file-download endpoint is gone
    r = client.get(f"/api/jobs/{job['id']}/files/0")
    check("no download endpoint", r.status_code == 404, str(r.status_code))

    # --- text-only message ---
    code, job = fetch(client, "https://t.me/sometechnicalchannel/1007")
    check("text job done", code == 200 and job["status"] == "done", str(job))
    check("text preview", job.get("text", "").startswith("just some text"), str(job.get("text")))
    check("text item", len(job.get("items", [])) == 1 and job["items"][0]["kind"] == "text",
          str(job.get("items")))
    check("text saved", saved_to_telegram(job), str(job.get("open_url")))

    # --- poll ---
    code, job = fetch(client, "https://t.me/sometechnicalchannel/1009")
    check("poll job done", code == 200 and job["status"] == "done", str(job))
    check("poll preview", "Best colour?" in (job.get("text") or ""), str(job.get("text")))
    check("poll item", job.get("items", [{}])[0]["kind"] == "poll", str(job.get("items")))
    check("poll saved", saved_to_telegram(job), str(job.get("open_url")))

    # --- document ---
    code, job = fetch(client, "https://t.me/sometechnicalchannel/1006")
    check("doc job done", code == 200 and job["status"] == "done", str(job))
    check("doc name", job["items"][0]["name"].endswith("report.pdf"), str(job["items"]))
    check("doc size", job["items"][0]["size"] == 2048, str(job["items"]))
    check("doc saved", saved_to_telegram(job), str(job.get("open_url")))

    # --- album / media group -> 2 saved messages ---
    code, job = fetch(client, "https://t.me/sometechnicalchannel/1008")
    check("album job done", code == 200 and job["status"] == "done", str(job))
    check("album 2 items", len(job.get("items", [])) == 2, str(job.get("items")))
    check("album 2 saved ids", len(job.get("saved_ids", [])) == 2, str(job.get("saved_ids")))
    check("album message", "Saved 2 messages" in (job.get("message") or ""), str(job.get("message")))

    # --- Telegram stamps ?single on copied album links; we still save all of it ---
    code, job = fetch(client, "https://t.me/sometechnicalchannel/1008?single")
    check("album link with ?single still saves the whole album",
          code == 200 and job["status"] == "done" and len(job.get("items", [])) == 2,
          str(job)[:300])
    check("album ?single saved ids", len(job.get("saved_ids", [])) == 2,
          str(job.get("saved_ids")))

    # a link with a query string still parses (the id is read before it)
    check("query string does not break parsing",
          jobs_mod.parse_link("https://t.me/malakekhanm/79?single")["msg_id"] == 79,
          str(jobs_mod.parse_link("https://t.me/malakekhanm/79?single")))

    # --- protected content: forward+copy refused -> download + re-upload, then cleanup ---
    FAKE.fail_ids.add(12346)
    uploaded_before = len(FAKE.uploaded)
    code, job = fetch(client, "https://t.me/sometechnicalchannel/12346")
    FAKE.fail_ids.clear()
    check("fallback job done", code == 200 and job["status"] == "done", str(job))
    check("fallback re-uploaded", len(FAKE.uploaded) == uploaded_before + 1, str(FAKE.uploaded))
    check("fallback saved", saved_to_telegram(job), str(job.get("open_url")))
    if len(FAKE.uploaded) > uploaded_before:
        kind, path = FAKE.uploaded[-1]
        check("fallback upload type", kind == "document", kind)
        check("temp file deleted", not os.path.exists(path), path)
    check("fallback temp dir cleaned", not (jobs_mod.JOBS_DIR / job["id"]).exists(), job["id"])

    # --- a file bigger than one Telegram upload goes out as numbered parts ---
    jobs_mod.FREE_UPLOAD_BYTES = 40        # pretend Telegram only takes 40 bytes
    FAKE.fail_ids.add(12352)               # forward + copy refused -> re-upload
    parts_before, uploaded_before = len(FAKE.parts), len(FAKE.uploaded)
    code, job = fetch(client, "https://t.me/sometechnicalchannel/12352")
    FAKE.fail_ids.clear()
    jobs_mod.FREE_UPLOAD_BYTES = 2000 * 1024 * 1024
    check("split job done", code == 200 and job["status"] == "done", str(job)[:300])
    split_uploads = FAKE.uploaded[uploaded_before:]
    check("oversized file sent in 2 parts", len(split_uploads) == 2, str(split_uploads))
    check("every part is a document",
          all(kind == "document" for kind, _ in split_uploads), str(split_uploads))
    names = [getattr(p, "name", None) for _, p in split_uploads]
    check("parts are numbered after the file",
          names == ["photo_12352.jpg.001", "photo_12352.jpg.002"], str(names))
    check("part readers are closed",
          all(getattr(p, "closed", False) for _, p in split_uploads), str(names))
    sent_parts = FAKE.parts[parts_before:]
    payload = b"X" * (12352 % 100 + 10)
    check("parts are slices of the downloaded file",
          b"".join(data for _, data in sent_parts) == payload,
          str([(n, len(d)) for n, d in sent_parts]))
    check("no part exceeds the limit",
          all(len(data) <= 40 for _, data in sent_parts),
          str([len(d) for _, d in sent_parts]))
    check("split keeps both message ids",
          len(job.get("saved_ids", [])) == 2, str(job.get("saved_ids")))
    check("split note explains the parts",
          "Split into 2 parts; join them in order to restore the original file."
          in (job.get("message") or ""), str(job.get("message")))
    check("split temp dir cleaned", not (jobs_mod.JOBS_DIR / job["id"]).exists(), job["id"])

    # --- a Premium account uploads in one piece (its limit is much higher) ---
    jobs_mod.FREE_UPLOAD_BYTES = 40
    jobs_mod.PREMIUM_UPLOAD_BYTES = 10_000_000
    FAKE.me.is_premium = True
    FAKE.fail_ids.add(12353)
    uploaded_before = len(FAKE.uploaded)
    code, job = fetch(client, "https://t.me/sometechnicalchannel/12353")
    FAKE.fail_ids.clear()
    FAKE.me.is_premium = False
    jobs_mod.FREE_UPLOAD_BYTES = 2000 * 1024 * 1024
    jobs_mod.PREMIUM_UPLOAD_BYTES = 4000 * 1024 * 1024
    check("premium account sends one message, not parts",
          code == 200 and job["status"] == "done"
          and len(FAKE.uploaded) == uploaded_before + 1,
          str(FAKE.uploaded[uploaded_before:]))
    check("premium job has no split note", "parts" not in (job.get("message") or ""),
          str(job.get("message")))

    # --- invite link -> join ---
    code, job = fetch(client, "https://t.me/+AbCdEfGhIjKlMnOpQr")
    check("invite job done", code == 200 and job["status"] == "done", str(job))
    check("invite message", "Joined the channel successfully" in (job.get("message") or ""), str(job.get("message")))
    check("invite no open url", not job.get("open_url"), str(job.get("open_url")))
    check("join called", FAKE.joined and FAKE.joined[-1].endswith("AbCdEfGhIjKlMnOpQr"), str(FAKE.joined))

    # --- long generated filenames must not break the UI ---
    from app.jobs import _truncate_name
    _long = "AQM_EApKkRJ8Lk7KqqPo4LTiXiw1kpIqpHTw291KBYxpvE19vw28ZWhMJbLLHOB.mp4"
    t = _truncate_name(_long)
    check("long name truncated", len(t) <= 45 and t.endswith(".mp4") and "…" in t,
          f"{len(t)} {t}")
    check("short name untouched", _truncate_name("report.pdf") == "report.pdf")
    check("no ext ok", len(_truncate_name("z" * 300)) <= 45)

    # --- jobs list / ownership / traversal ---
    r = client.get("/api/jobs")
    check("jobs list", r.status_code == 200 and len(r.json()["jobs"]) >= 7, r.text[:200])
    r = client.get("/api/jobs/../../etc/files/0")
    check("traversal blocked", r.status_code in (401, 404), str(r.status_code))
    set_cookie("bogus")
    r = client.get("/api/jobs/whatever")
    check("bad cookie rejected", r.status_code == 401, str(r.status_code))
    set_cookie(tok)

    # --- concurrent job rejected while one is running ---
    r = client.post("/api/fetch", json={"link": "https://t.me/sometechnicalchannel/1008"})
    r2 = client.post("/api/fetch", json={"link": "https://t.me/sometechnicalchannel/1008"})
    if r.status_code == 200 and r2.status_code == 409:
        check("concurrent rejected", True)
        wait_job(client, r.json()["job"]["id"])
    else:
        # first finished before the second started - also acceptable
        check("concurrent sane", r.status_code == 200 and r2.status_code in (200, 409),
              f"{r.status_code}/{r2.status_code}")
        if r2.status_code == 200:
            wait_job(client, r2.json()["job"]["id"])

    # --- no leftover job folders on disk ---
    leftovers = [p.name for p in jobs_mod.JOBS_DIR.iterdir()] if jobs_mod.JOBS_DIR.exists() else []
    check("no leftover job dirs", not leftovers, str(leftovers))

    # --- durable history log (survives a restart of the job cache) ---
    before = {j["id"]: j for j in client.get("/api/jobs").json()["jobs"]}
    done_ids = [i for i, j in before.items() if j["status"] == "done"]
    check("history recorded for finished jobs", len(done_ids) >= 5, f"{len(done_ids)} done")
    check("log holds the final state",
          all(_HISTORY.get(i, {}).get("status") == "done" for i in done_ids),
          str({i: _HISTORY.get(i, {}).get("status") for i in done_ids}))

    jobs_mod.JOBS.clear()   # simulate a server restart: in-memory jobs are gone
    after = {j["id"]: j for j in client.get("/api/jobs").json()["jobs"]}
    missing = [i for i in done_ids if i not in after]
    check("history survives a restart", not missing, f"missing={missing}")
    keep = after.get(done_ids[-1], {}) if done_ids else {}
    check("restarted history keeps details",
          keep.get("items") is not None and keep.get("open_url") and keep.get("link"),
          str(keep)[:200])

    # legacy records (written before batch/multi existed) default to single
    _HISTORY["legacy-1"] = {"id": "legacy-1", "uid": 111,
                            "link": "https://t.me/sometechnicalchannel/1",
                            "status": "done", "step": "Done", "created": time.time() - 5}
    legacy = {j["id"]: j for j in client.get("/api/jobs").json()["jobs"]}.get("legacy-1", {})
    check("legacy history defaults to single", legacy.get("kind") == "single", str(legacy)[:200])

    # ------------------------------------------------------- batch save (mode)
    r = client.post("/api/fetch", json={"link": "https://t.me/sometechnicalchannel/1006",
                                        "mode": "banana"})
    check("unknown mode rejected",
          r.status_code == 400 and "mode" in r.json()["error"].lower(), r.text[:150])
    r = client.post("/api/fetch", json={"link": "https://t.me/sometechnicalchannel/1006",
                                        "mode": "batch"})
    check("batch without count rejected",
          r.status_code == 400 and "how many" in r.json()["error"].lower(), r.text[:150])
    r = client.post("/api/fetch", json={"link": "https://t.me/sometechnicalchannel/1006",
                                        "mode": "batch", "count": 0})
    check("batch zero count rejected", r.status_code == 400, r.text[:150])
    r = client.post("/api/fetch", json={"link": "https://t.me/sometechnicalchannel/1006",
                                        "mode": "batch", "count": BULK_MAX_FILES + 1})
    check("batch over max rejected",
          r.status_code == 400 and str(BULK_MAX_FILES) in r.json()["error"], r.text[:150])
    r = client.post("/api/fetch", json={"link": "https://t.me/+AbCdEfGhIjKlMnOpQr",
                                        "mode": "batch", "count": 5})
    check("batch invite rejected",
          r.status_code == 400 and "message link" in r.json()["error"], r.text[:150])

    # per-kind daily buckets are separate from the normal save limit
    _BULK["left"] = 0
    r = client.post("/api/fetch", json={"link": "https://t.me/sometechnicalchannel/1006",
                                        "mode": "batch", "count": 3})
    check("batch quota 429",
          r.status_code == 429 and "bulk saves" in r.json()["error"], r.text[:150])
    _BULK["left"] = None
    _MULTI["left"] = 0
    r = client.post("/api/fetch", json={"link": "https://t.me/sometechnicalchannel/1\n"
                                                "https://t.me/sometechnicalchannel/2",
                                        "mode": "multi"})
    check("multi quota 429",
          r.status_code == 429 and "multi-link saves" in r.json()["error"], r.text[:150])
    _MULTI["left"] = None

    # ------------------------------------------------------ multi save (mode)
    too_many = "\n".join(f"https://t.me/sometechnicalchannel/{1000 + i}"
                         for i in range(MULTI_MAX_LINKS + 1))
    r = client.post("/api/fetch", json={"link": too_many, "mode": "multi"})
    check("multi over max links rejected",
          r.status_code == 400 and str(MULTI_MAX_LINKS) in r.json()["error"], r.text[:150])
    r = client.post("/api/fetch",
                    json={"link": "https://t.me/sometechnicalchannel/1006\nhello", "mode": "multi"})
    check("multi bad line rejected",
          r.status_code == 400 and "Line 2" in r.json()["error"], r.text[:150])
    r = client.post("/api/fetch",
                    json={"link": "https://t.me/+AbCdEfGhIjKlMnOpQr\n"
                                  "https://t.me/sometechnicalchannel/1006",
                          "mode": "multi"})
    check("multi invite line rejected",
          r.status_code == 400 and "message links" in r.json()["error"], r.text[:150])

    # --- batch job: walks forward from the linked message, collecting files ---
    dl_before = _STATS.get("dl", 0)
    code, job = fetch(client, "https://t.me/sometechnicalchannel/1006", mode="batch", count=3)
    check("batch job done", code == 200 and job["status"] == "done", str(job)[:300])
    check("batch kind flagged", job.get("kind") == "batch" and job.get("count") == 3,
          str(job)[:200])
    check("batch 3 items", len(job.get("items", [])) == 3, str(job.get("items")))
    check("batch saved ids", len(job.get("saved_ids", [])) == 3, str(job.get("saved_ids")))
    check("batch message", (job.get("message") or "").startswith("Saved 3 files"),
          str(job.get("message")))
    check("batch saved (open url)", saved_to_telegram(job), str(job.get("open_url")))
    check("batch counted in bulk bucket", _STATS.get("bulk") == 1, str(_STATS))
    check("batch skips the daily counter", _STATS.get("dl", 0) == dl_before,
          f"{dl_before} -> {_STATS.get('dl', 0)}")
    check("batch temp dir cleaned",
          not (jobs_mod.JOBS_DIR / job["id"]).exists(), job["id"])

    # --- multi job: several links saved together, one quota unit ---
    code, job = fetch(client, "https://t.me/sometechnicalchannel/12345\n"
                              "https://t.me/sometechnicalchannel/1006", mode="multi")
    check("multi job done", code == 200 and job["status"] == "done", str(job)[:300])
    check("multi kind flagged", job.get("kind") == "multi", str(job)[:200])
    check("multi combined items", len(job.get("items", [])) == 2, str(job.get("items")))
    check("multi message",
          (job.get("message") or "") == "Saved 2 links to your Saved Messages.",
          str(job.get("message")))
    check("multi counted once", _STATS.get("multi") == 1, str(_STATS))
    check("multi saved (open url)", saved_to_telegram(job), str(job.get("open_url")))

    # --- a bad link does not spoil the rest of a multi save ---
    orig_resolve = jobs_mod._resolve_message
    async def flaky(userbot, job, parsed, require_content=True):
        if parsed.get("msg_id") in (77777, 77778):
            raise jobs_mod.JobError(
                "Could not fetch the message. Please check the link and make sure "
                "you have access to it.")
        return await orig_resolve(userbot, job, parsed, require_content=require_content)

    jobs_mod._resolve_message = flaky
    code, job = fetch(client, "https://t.me/sometechnicalchannel/77777\n"
                              "https://t.me/sometechnicalchannel/1006", mode="multi")
    jobs_mod._resolve_message = orig_resolve
    check("multi partial done", code == 200 and job["status"] == "done", str(job)[:300])
    check("multi partial message",
          (job.get("message") or "") == "Saved 1/2 links to your Saved Messages.",
          str(job.get("message")))
    check("multi partial counted", _STATS.get("multi") == 2, str(_STATS))

    jobs_mod._resolve_message = flaky
    code, job = fetch(client, "https://t.me/sometechnicalchannel/77777\n"
                              "https://t.me/sometechnicalchannel/77778", mode="multi")
    jobs_mod._resolve_message = orig_resolve
    check("multi all-fail is an error",
          job.get("status") == "error" and "Could not fetch" in (job.get("error") or ""),
          str(job)[:300])
    check("multi all-fail not counted", _STATS.get("multi") == 2, str(_STATS))

    leftovers = [p.name for p in jobs_mod.JOBS_DIR.iterdir()] if jobs_mod.JOBS_DIR.exists() else []
    check("batch/multi leave no job dirs", not leftovers, str(leftovers))

    # a phone number entered without its "+" must still go through
    main_mod._RATE_HITS.clear()
    r = client.post("/api/login/start", json={"phone": "15854346400"})
    check("phone without + proceeds",
          r.status_code == 200 and _PHONES[-1:] == ["+15854346400"],
          f"{r.status_code} {_PHONES[-1:]}")
    r = client.post("/api/login/start", json={"phone": "+15854346400"})
    check("phone with + is left alone",
          r.status_code == 200 and _PHONES[-1:] == ["+15854346400"],
          f"{r.status_code} {_PHONES[-1:]}")
    r = client.post("/api/login/start", json={"phone": "91612345678"})
    check("other country code without + proceeds",
          r.status_code == 200 and _PHONES[-1:] == ["+91612345678"],
          f"{r.status_code} {_PHONES[-1:]}")

    # ------------------------------------------- rate limits and aborting jobs
    # a FloodWait is not a failure: the job waits it out and retries
    FAKE.flood_ids.add(12350)
    code, job = fetch(client, "https://t.me/sometechnicalchannel/12350")
    check("rate limit is waited out, not fatal",
          code == 200 and job["status"] == "done", str(job)[:200])
    check("job saved after waiting", saved_to_telegram(job), str(job.get("open_url")))

    def _sample_wait():
        jb = jobs_mod.Job(id="t", uid=111, link="l")
        loop = asyncio.get_event_loop()

        async def run():
            task = asyncio.ensure_future(jobs_mod._flood_sleep(jb, 2))
            seen = []
            while not task.done():
                seen.append((jb.step, jb.wait_left))
                await asyncio.sleep(0.2)
            await task
            return seen

        return jb, loop.run_until_complete(run())

    jb, samples = _sample_wait()
    check("the countdown is visible while waiting",
          any("Rate limited by Telegram, waiting" in s[0] and s[1] > 0 for s in samples),
          str(samples[:3]))
    check("the countdown clears when the wait is over",
          jb.wait_left == 0 and jb.step == "Resuming...", f"{jb.wait_left} {jb.step}")

    # a call that never answers (dropped connection) gives up instead of
    # hanging the job forever
    real_timeout = jobs_mod.CALL_TIMEOUT_SECONDS
    jobs_mod.CALL_TIMEOUT_SECONDS = 1
    stalled = None

    async def _hang():
        await asyncio.sleep(30)

    try:
        asyncio.get_event_loop().run_until_complete(
            jobs_mod._call(jobs_mod.Job(id="h", uid=111, link="l"), "hang", _hang))
    except jobs_mod.JobError as exc:
        stalled = str(exc)
    finally:
        jobs_mod.CALL_TIMEOUT_SECONDS = real_timeout
        FAKE.hang_ids.discard(12360)
    check("a call that never answers gives up",
          stalled is not None and "stopped responding" in stalled.lower(), str(stalled))

    # --- rate-limit memory, budgets and retries (unit level) ---
    from pyrogram.errors import InternalServerError, MessageIdInvalid

    async def _flood_units():
        out = []

        def add(name, cond, extra=""):
            out.append((name, bool(cond), extra))

        job = jobs_mod.Job(id="f1", uid=777001, link="l")
        calls = {"n": 0}

        async def once_flooded():
            calls["n"] += 1
            if calls["n"] == 1:
                raise FloodWait(1)
            return "ok"

        res = await jobs_mod._call(job, "forward", once_flooded)
        add("a FloodWait is waited out and retried",
            res == "ok" and calls["n"] == 2, f"{res} {calls}")

        # the deadline stays behind, so another call of the same kind waits
        # up front instead of tripping the very same limit again
        jobs_mod._FLOOD_UNTIL[(777002, "copy")] = time.time() + 2
        job2 = jobs_mod.Job(id="f2", uid=777002, link="l")
        seen = []

        async def probe():
            seen.append(time.time())
            return "probed"

        t0 = time.time()
        res = await jobs_mod._call(job2, "copy", probe)
        add("a remembered deadline is waited out before the call",
            res == "probed" and len(seen) == 1 and time.time() - t0 >= 1.5,
            f"{res} {len(seen)} {time.time() - t0:.1f}s")
        add("the countdown clears after that wait", job2.wait_left == 0, str(job2.wait_left))
        jobs_mod._FLOOD_UNTIL.pop((777002, "copy"), None)

        # a wait nobody can sit through is reported instead of hanging the job
        job3 = jobs_mod.Job(id="f3", uid=777003, link="l")
        jobs_mod._FLOOD_UNTIL[(777003, "forward")] = time.time() + 5000
        msg = None
        try:
            await jobs_mod._call(job3, "forward", probe)
        except jobs_mod.FloodBudgetExceeded as exc:
            msg = str(exc)
        jobs_mod._FLOOD_UNTIL.pop((777003, "forward"), None)
        add("an impossible wait is reported in minutes",
            msg == "Rate limited by Telegram. Try again in about 83 minutes.", str(msg))
        add("the call behind an impossible wait never fires", len(seen) == 1, str(len(seen)))
        add("short waits keep the seconds wording",
            jobs_mod._flood_message(30)
            == "Rate limited by Telegram. Try again in 30 seconds.",
            jobs_mod._flood_message(30))

        # a flood error without a duration must not be slept on forever
        raised = None

        async def no_duration():
            raise FloodWait(value=None)

        try:
            await jobs_mod._call(job3, "forward", no_duration)
        except FloodWait as exc:
            raised = exc
        add("a flood error without a duration is raised at once",
            raised is not None, str(raised))

        # a transient Telegram-side error is retried, a permanent one is not
        tries = {"n": 0}

        async def flaky():
            tries["n"] += 1
            if tries["n"] == 1:
                raise InternalServerError(500, "messages.forwardMessages")
            return "recovered"

        res = await jobs_mod._call(job3, "copy", flaky)
        add("a transient Telegram error is retried",
            res == "recovered" and tries["n"] == 2, f"{res} {tries}")

        tries2 = {"n": 0}

        async def denied():
            tries2["n"] += 1
            raise MessageIdInvalid

        raised = None
        try:
            await jobs_mod._call(job3, "upload", denied)
        except MessageIdInvalid as exc:
            raised = exc
        add("a permanent Telegram error is not retried",
            raised is not None and tries2["n"] == 1, f"{raised} {tries2}")
        return out

    for name, cond, extra in asyncio.get_event_loop().run_until_complete(_flood_units()):
        check(name, cond, extra)

    # --- when a link cannot be read, the reason is named instead of guessed ---
    import logging
    from pyrogram.errors import UsernameNotOccupied

    def _resolve(userbot, uid, link, **kw):
        job = jobs_mod.Job(id="resolve-probe", uid=uid, link=link)
        try:
            return asyncio.get_event_loop().run_until_complete(
                jobs_mod._resolve_message(userbot, job, jobs_mod.parse_link(link), **kw))
        except jobs_mod.JobError as exc:
            return str(exc)

    class _NeverOwned(FakeUserbot):
        async def get_chat(self, username):
            raise UsernameNotOccupied

        async def get_messages(self, cid, mid):
            raise UsernameNotOccupied

    res = _resolve(_NeverOwned(), 500111, "https://t.me/senpal_acadmey/115352/115938")
    check("a username Telegram has never heard of is named",
          isinstance(res, str) and "@senpal_acadmey" in res
          and "doesn't exist" in res, str(res))

    # the message is there but holds nothing savable: say that, not "no access"
    class _BlankOnly(FakeUserbot):
        async def get_messages(self, cid, mid):
            m = make_msg(mid, "photo")
            m.media = m.photo = m.caption = m.text = None
            return m

    jobs_mod._DIALOG_CACHE["ub_500112"] = {"chat_ids": {-100999, 100999},
                                            "expires_at": time.time() + 60}
    res = _resolve(_BlankOnly(), 500112, "https://t.me/sometechnicalchannel/12345")
    check("a message with nothing to save is named as such",
          isinstance(res, str) and "no savable content" in res, str(res))
    res = _resolve(_BlankOnly(), 500112, "https://t.me/sometechnicalchannel/12345",
                   require_content=False)
    check("batch base resolution accepts a contentless message",
          res is not None and not isinstance(res, str), str(res))

    # Telegram refuses to answer at all: the public chat is joined, then read
    class _JoinFirst(FakeUserbot):
        def __init__(self):
            super().__init__()
            self.in_chat = False

        async def get_messages(self, cid, mid):
            if not self.in_chat:
                raise RuntimeError("CHANNEL_PRIVATE")
            return make_msg(mid, "photo")

        async def join_chat(self, link):
            self.joined.append(link)
            self.in_chat = True
            return Obj(id=-100999)

    joiner = _JoinFirst()
    res = _resolve(joiner, 500113, "https://t.me/sometechnicalchannel/12345")
    check("a public chat is joined when nothing can be read",
          joiner.joined == ["sometechnicalchannel"], str(joiner.joined))
    check("the message is read after that join",
          res is not None and not isinstance(res, str), str(res))

    class _Capture(logging.Handler):
        def __init__(self):
            super().__init__()
            self.lines = []

        def emit(self, record):
            self.lines.append(record.getMessage())

    cap = _Capture()
    jobs_mod.log.addHandler(cap)
    try:
        _resolve(_NeverOwned(), 500114, "https://t.me/senpal_acadmey/115352/115938")
    finally:
        jobs_mod.log.removeHandler(cap)
    check("the refusals behind a failed link are logged",
          any("refused while resolving" in line for line in cap.lines),
          str(cap.lines[:4]))

    # forward and copy refusals are logged before falling back to a re-upload
    cap = _Capture()
    jobs_mod.log.addHandler(cap)
    try:
        FAKE.fail_ids.add(12356)
        code, job = fetch(client, "https://t.me/sometechnicalchannel/12356")
        FAKE.fail_ids.discard(12356)
    finally:
        jobs_mod.log.removeHandler(cap)
    check("the fallback job still finishes", code == 200 and job["status"] == "done",
          str(job)[:200])
    check("forward and copy refusals are logged before the fallback",
          any("forward refused" in line for line in cap.lines)
          and any("copy refused" in line for line in cap.lines)
          and any("falling back to download" in line for line in cap.lines),
          str(cap.lines[:6]))

    # a running job can be stopped by the user
    dl_before = _STATS.get("dl", 0)
    FAKE.slow_forward_ids.add(12351)
    r = client.post("/api/fetch", json={"link": "https://t.me/sometechnicalchannel/12351"})
    jid = r.json()["job"]["id"]
    time.sleep(0.4)
    running = client.get(f"/api/jobs/{jid}").json()["job"]
    check("job is running before the abort", running["status"] == "running", str(running["status"]))
    r = client.post(f"/api/jobs/{jid}/abort")
    check("abort accepted", r.status_code == 200, r.text[:120])
    final = wait_job(client, jid)
    check("aborted job is marked stopped", final["status"] == "aborted", str(final)[:200])
    check("aborted job keeps no error", not final.get("error"), str(final.get("error")))
    check("aborted job stops showing the stop button state",
          final.get("wait_left", 0) == 0, str(final.get("wait_left")))
    r = client.post(f"/api/jobs/{jid}/abort")
    check("a finished job cannot be aborted again", r.status_code == 409, str(r.status_code))
    check("the durable log records the abort",
          _HISTORY.get(jid, {}).get("status") == "aborted",
          str(_HISTORY.get(jid, {}).get("status")))
    check("abort did not consume the daily counter",
          _STATS.get("dl", 0) == dl_before, f"{dl_before} -> {_STATS.get('dl', 0)}")

    # --- temp files never outlive a job ---
    # a download that cannot even complete: the folder is still removed
    FAKE.fail_ids.add(12354)
    FAKE.no_download_ids.add(12354)
    code, job = fetch(client, "https://t.me/sometechnicalchannel/12354")
    jdir = jobs_mod.JOBS_DIR / job["id"]
    check("a failed download fails the job",
          job["status"] == "error" and "could not be saved" in (job.get("error") or "").lower(),
          str(job.get("error")))
    check("temp folder removed after a failed save", not jdir.exists(), job["id"])
    FAKE.fail_ids.discard(12354)
    FAKE.no_download_ids.discard(12354)

    # aborted while the file was still downloading
    FAKE.fail_ids.add(12355)
    FAKE.slow_ids.add(12355)
    r = client.post("/api/fetch", json={"link": "https://t.me/sometechnicalchannel/12355"})
    jid = r.json()["job"]["id"]
    jdir = jobs_mod.JOBS_DIR / jid
    time.sleep(0.6)
    mid_download = jdir.exists() and any(jdir.iterdir())
    client.post(f"/api/jobs/{jid}/abort")
    wait_job(client, jid)
    check("the temp file exists while downloading", mid_download, jid)
    check("temp folder removed after an abort", not jdir.exists(), jid)
    FAKE.fail_ids.discard(12355)
    FAKE.slow_ids.discard(12355)

    # the periodic sweep clears folders whose job is gone, keeps live ones
    stray = jobs_mod.JOBS_DIR / "stray-folder"
    os.makedirs(stray, exist_ok=True)
    with open(stray / "left.bin", "wb") as fh:
        fh.write(b"x")
    live = jobs_mod.JOBS_DIR / "live-folder"
    os.makedirs(live, exist_ok=True)
    jobs_mod.JOBS["live-folder"] = jobs_mod.Job(id="live-folder", uid=111, link="l")
    removed = jobs_mod.sweep_orphan_folders()
    check("sweep removes folders with no job",
          removed == 1 and not stray.exists(), str(removed))
    check("sweep keeps the folder of a live job", live.exists(), str(live))
    jobs_mod.JOBS.pop("live-folder", None)
    shutil.rmtree(live, ignore_errors=True)

    # ------------------------------------------------------------------ admin
    # a digit-only value in the phone box is the admin password; anything else
    # is treated as a phone number and fails the format check instead
    main_mod._RATE_HITS.clear()
    r = client.post("/api/login/start", json={"phone": "000000"})
    check("wrong digit password falls through to the phone check",
          r.status_code == 400 and "international" in r.json()["error"],
          r.text[:150])

    client.cookies.jar.clear()   # start clean so only the admin cookie remains
    r = client.post("/api/login/start", json={"phone": "123456"})
    adm = r.json()
    check("admin logs in with the default password",
          r.status_code == 200 and adm.get("admin") is True and adm.get("uid") == -1,
          str(adm))
    check("first login forces a password change",
          adm.get("must_change_pin") is True, str(adm))
    admin_tok = get_cookie()

    me = client.get("/api/me").json()
    check("me flags the admin session",
          me.get("is_admin") is True and me.get("must_change_pin") is True, str(me))
    check("me carries notice + save switch",
          me.get("notice") == "" and me.get("saving_enabled") is True, str(me))

    r = client.get("/api/admin/stats")
    st = r.json() if r.status_code == 200 else {}
    check("admin stats reachable",
          r.status_code == 200 and len(st.get("daily", [])) == 30
          and "totals" in st and "users" in st and "active_jobs" in st,
          r.text[:200])
    check("admin stats totals shape",
          all(k in st.get("totals", {}) for k in
              ("users", "logged_in", "banned", "visited", "returning",
               "files_saved", "visits_today", "saves_today")),
          str(st.get("totals")))

    # phone + ip are stored per user and surfaced in the dashboard
    asyncio.get_event_loop().run_until_complete(
        _add_user(111, phone="+15854346400", ip="203.0.113.7"))
    r = client.get("/api/admin/stats")
    st = r.json() if r.status_code == 200 else {}
    u111 = next((u for u in st.get("users", []) if u.get("uid") == 111), {})
    check("user phone + ip in stats",
          u111.get("phone") == "+15854346400" and u111.get("ip") == "203.0.113.7",
          str(u111))

    # forced password change
    r = client.post("/api/admin/pin", json={"new_pin": "123"})
    check("short pin rejected", r.status_code == 400, r.text[:120])
    r = client.post("/api/admin/pin", json={"new_pin": "1234567890123"})
    check("long pin rejected", r.status_code == 400, r.text[:120])
    r = client.post("/api/admin/pin", json={"new_pin": "123456"})
    check("pin equal to the default rejected",
          r.status_code == 400 and "default" in r.json()["error"], r.text[:120])
    r = client.post("/api/admin/pin", json={"new_pin": "654321"})
    check("admin pin changed", r.status_code == 200, r.text[:120])
    me = client.get("/api/me").json()
    check("forced-change flag cleared", me.get("must_change_pin") is False, str(me))
    fresh_tok = get_cookie()
    old_owner = asyncio.get_event_loop().run_until_complete(_token_uid(admin_tok))
    check("password change revokes every other session", old_owner is None, str(old_owner))
    check("the caller keeps working on a fresh token",
          fresh_tok and fresh_tok != admin_tok and client.get("/api/me").status_code == 200,
          f"{fresh_tok} / {admin_tok}")
    r = client.post("/api/login/start", json={"phone": "123456"})
    check("old password no longer works", r.status_code == 400, r.text[:120])

    # the previous cookie is dead now: sign in again with the new password
    client.cookies.jar.clear()
    r = client.post("/api/login/start", json={"phone": "654321"})
    admin_tok = get_cookie()
    check("admin signs in with the new password",
          r.status_code == 200 and r.json().get("admin") is True and bool(admin_tok),
          f"{r.status_code} {admin_tok}")

    # later changes need the current password
    r = client.post("/api/admin/pin", json={"new_pin": "111111"})
    check("current password required after the first change",
          r.status_code == 400 and "current" in r.json()["error"], r.text[:120])
    r = client.post("/api/admin/pin", json={"new_pin": "111111", "current_pin": "0000"})
    check("wrong current password rejected",
          r.status_code == 400 and "current" in r.json()["error"], r.text[:120])
    r = client.post("/api/admin/pin", json={"new_pin": "111111", "current_pin": "654321"})
    check("pin change with the current password", r.status_code == 200, r.text[:120])
    client.cookies.jar.clear()          # the session rotated again
    client.post("/api/login/start", json={"phone": "111111"})
    admin_tok = get_cookie()
    set_cookie(admin_tok)

    # notice banner + save switch
    r = client.post("/api/admin/settings",
                    json={"notice": "Back soon: maintenance tonight.", "saving_enabled": False})
    check("admin saves settings", r.status_code == 200 and r.json().get("ok"), r.text[:120])
    # a colour outside the known set falls back to blue
    r = client.post("/api/admin/settings",
                    json={"notice": "x", "notice_color": "purple", "saving_enabled": False})
    check("unknown notice colour falls back to blue",
          r.status_code == 200 and r.json().get("notice_color") == "blue", r.text[:160])
    # dashboard notice + login page notice, each with its own colour
    r = client.post("/api/admin/settings",
                    json={"notice": "Back soon: maintenance tonight.",
                          "notice_login": "Visitors: the site opens at 8:00.",
                          "notice_color": "yellow", "notice_login_color": "green",
                          "saving_enabled": False})
    check("settings return both colours",
          r.status_code == 200 and r.json().get("notice_color") == "yellow"
          and r.json().get("notice_login_color") == "green", r.text[:200])
    set_cookie(tok)
    me = client.get("/api/me").json()
    check("notice surfaced to users",
          me.get("notice") == "Back soon: maintenance tonight."
          and me.get("notice_color") == "yellow", str(me.get("notice")))
    check("login notice surfaced",
          me.get("notice_login") == "Visitors: the site opens at 8:00."
          and me.get("notice_login_color") == "green", str(me.get("notice_login")))
    check("save switch surfaced", me.get("saving_enabled") is False, str(me))
    r = client.post("/api/fetch", json={"link": "https://t.me/x/1"})
    check("saving disabled blocks fetch",
          r.status_code == 403 and "disabled" in r.json()["error"], r.text[:160])
    r = client.get("/api/admin/stats")
    check("non-admin cannot read admin stats", r.status_code == 403, str(r.status_code))

    # ban / unban
    tok222 = asyncio.get_event_loop().run_until_complete(_create_token(222))
    set_cookie(admin_tok)
    r = client.post("/api/admin/ban", json={"uid": 222, "banned": True})
    check("admin bans a user", r.status_code == 200 and r.json().get("banned") is True,
          r.text[:120])
    check("ban flag stored", _USERS.get(222, {}).get("banned") is True, str(_USERS.get(222)))
    check("banned user's tokens revoked", 222 not in _TOKENS.values(), str(_TOKENS))
    r = client.post("/api/admin/ban", json={"uid": -1, "banned": True})
    check("cannot ban the administrator", r.status_code == 400, r.text[:120])
    set_cookie(tok222)
    check("banned user is signed out",
          client.get("/api/me").json().get("logged_in") is False,
          str(client.get("/api/me").json()))
    set_cookie(admin_tok)
    r = client.post("/api/admin/ban", json={"uid": 222, "banned": False})
    check("admin unbans a user", r.status_code == 200 and r.json().get("banned") is False,
          r.text[:120])
    check("unban flag stored", _USERS.get(222, {}).get("banned") is False, str(_USERS.get(222)))

    # visit beacon (feeds the dashboard)
    set_cookie(tok)
    r = client.post("/api/visit")
    check("visit beacon recorded", r.status_code == 200 and _STATS.get("visits") == 1,
          f"{r.status_code} {_STATS.get('visits')}")
    check("visit beacon stores the ip",
          _USERS.get(111, {}).get("ip") == "testclient", str(_USERS.get(111)))
    set_cookie("bogus")
    check("visit beacon requires login",
          client.post("/api/visit").status_code == 401)
    set_cookie(tok)

    # re-enable saving (leave the site usable) + verify
    set_cookie(admin_tok)
    r = client.post("/api/admin/settings",
                    json={"notice": "", "notice_login": "Visitors: the site opens at 8:00.",
                          "notice_login_color": "green", "saving_enabled": True})
    check("settings restored", r.status_code == 200, r.text[:120])
    set_cookie(tok)
    check("save switch back on",
          client.get("/api/me").json().get("saving_enabled") is True)

    # ------------------------------------------------ unlimited quota (admin)
    tok222b = asyncio.get_event_loop().run_until_complete(_create_token(222))
    set_cookie(admin_tok)
    r = client.post("/api/admin/unlimited", json={"uid": 222, "days": 7})
    check("unlimited granted for N days",
          r.status_code == 200 and r.json().get("unlimited_until", 0) > time.time(),
          r.text[:200])
    left = _USERS.get(222, {}).get("unlimited_until", 0) - time.time()
    check("the days start when the admin grants them",
          7 * 86400 - 10 <= left <= 7 * 86400 + 10, f"{left:.0f}s")
    stats = client.get("/api/admin/stats").json()
    row = next((u for u in stats.get("users", []) if u.get("uid") == 222), None)
    check("dashboard carries the grant",
          row is not None and row.get("unlimited_until", 0) > time.time(), str(row))

    set_cookie(tok222b)
    me = client.get("/api/me").json()
    check("me reports the grant",
          me.get("unlimited") is True and me.get("unlimited_days_left") == 7, str(me))
    check("grant keeps the counter full", me.get("remaining") == 20, str(me))

    # an empty bucket no longer stops an unlimited account ...
    _BULK["left"] = 0
    r = client.post("/api/fetch",
                    json={"link": "https://t.me/sometechnicalchannel/12361",
                          "mode": "batch", "count": 1})
    check("unlimited beats an empty bucket", r.status_code == 200, r.text[:200])
    if r.status_code == 200:
        wait_job(client, r.json()["job"]["id"])
    # ... while an account without the grant is still refused
    set_cookie(tok)
    r = client.post("/api/fetch",
                    json={"link": "https://t.me/sometechnicalchannel/12361",
                          "mode": "batch", "count": 1})
    check("a normal account is still limited", r.status_code == 429, str(r.status_code))
    _BULK["left"] = None

    set_cookie(admin_tok)
    r = client.post("/api/admin/unlimited", json={"uid": 222, "days": 99999})
    check("days are capped",
          r.status_code == 400 and "between 1 and 3650" in r.json().get("error", ""),
          r.text[:200])
    r = client.post("/api/admin/unlimited", json={"uid": -1, "days": 5})
    check("the admin does not need a quota", r.status_code == 400, r.text[:120])
    set_cookie(tok)
    r = client.post("/api/admin/unlimited", json={"uid": 222, "days": 5})
    check("only the admin can grant a quota", r.status_code == 403, str(r.status_code))

    set_cookie(admin_tok)
    r = client.post("/api/admin/unlimited", json={"uid": 222, "days": 0})
    check("the grant can be taken back", r.status_code == 200, r.text[:120])
    check("grant field removed", "unlimited_until" not in _USERS.get(222, {}),
          str(_USERS.get(222)))
    set_cookie(tok222b)
    me = client.get("/api/me").json()
    check("me drops the grant",
          me.get("unlimited") is False and me.get("unlimited_days_left") == 0, str(me))

    # --- the real quota logic (the fakes above only stand in for Mongo) ---
    from app.config import DAILY_LIMIT as _DL

    class _MiniUsers:
        def __init__(self):
            self.docs = {}

        async def find_one(self, flt, proj=None):
            doc = self.docs.get(flt.get("id"))
            return dict(doc) if doc else None

        async def update_one(self, flt, update):
            doc = self.docs.setdefault(flt.get("id"), {"id": flt.get("id")})
            for k, v in (update.get("$set") or {}).items():
                doc[k] = v
            for k in (update.get("$unset") or {}):
                doc.pop(k, None)

    real_users = mdb._users
    mdb._users = _MiniUsers()
    mdb._users.docs[444] = {"id": 444, "daily_download_count": _DL,
                            "daily_download_reset_at": time.time() + 3600}
    # the fakes above stand in for Mongo: drop them so the real methods run
    patched = ("set_unlimited", "clear_unlimited", "unlimited_until")
    saved_methods = {n: getattr(mdb, n) for n in patched}
    for n in patched:
        delattr(mdb, n)

    async def _real_quota():
        res = {}
        res["full"] = (await mdb._quota_remaining(
            444, "daily_download_count", "daily_download_reset_at", _DL))[0]
        await mdb.set_unlimited(444, 3)
        res["open"] = (await mdb._quota_remaining(
            444, "daily_download_count", "daily_download_reset_at", _DL))[0]
        res["until"] = await mdb.unlimited_until(444)
        await mdb._quota_use(444, "daily_download_count", "daily_download_reset_at")
        res["count"] = mdb._users.docs[444]["daily_download_count"]
        res["granted_at"] = mdb._users.docs[444].get("unlimited_granted_at", 0)
        mdb._users.docs[444]["unlimited_until"] = time.time() - 5
        res["expired"] = (await mdb._quota_remaining(
            444, "daily_download_count", "daily_download_reset_at", _DL))[0]
        await mdb.clear_unlimited(444)
        res["cleared"] = await mdb.unlimited_until(444)
        res["field_left"] = "unlimited_until" in mdb._users.docs[444]
        return res

    try:
        real = asyncio.get_event_loop().run_until_complete(_real_quota())
    finally:
        mdb._users = real_users
        for n, fn in saved_methods.items():
            setattr(mdb, n, fn)
    check("real quota refuses a full bucket", real["full"] == 0, str(real))
    check("a grant opens the bucket", real["open"] == _DL, str(real))
    check("the period starts at the grant",
          3 * 86400 - 10 <= real["until"] - time.time() <= 3 * 86400 + 10,
          str(real.get("until")))
    check("the grant records when it began", real["granted_at"] > time.time() - 10,
          str(real.get("granted_at")))
    check("saves inside the window are not counted", real["count"] == _DL,
          str(real.get("count")))
    check("an expired grant counts for nothing", real["expired"] == 0, str(real))
    check("clearing removes the grant",
          real["cleared"] == 0 and real["field_left"] is False, str(real))

    # admin signs out like anyone else
    set_cookie(admin_tok)
    check("admin logout", client.post("/api/logout").status_code == 200)
    signed_out = client.get("/api/me").json()
    check("admin signed out", signed_out.get("logged_in") is False, str(signed_out))
    check("login notice served to signed-out visitors",
          signed_out.get("notice_login") == "Visitors: the site opens at 8:00."
          and signed_out.get("notice_login_color") == "green", str(signed_out))
    set_cookie(tok)

    # --- logout ---
    check("logout", client.post("/api/logout").status_code == 200)
    check("logged out after logout", client.get("/api/me").json().get("logged_in") is False)

failed = [r for r in results if not r[1]]
print(f"\n{len(results) - len(failed)}/{len(results)} passed")
sys.exit(1 if failed else 0)
