"""FastAPI web client: login, fetch jobs, download files."""

import asyncio
import contextlib
import logging
import time

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .config import (
    API_HASH,
    API_ID,
    ADMIN_PIN_MAX,
    ADMIN_PIN_MIN,
    ADMIN_UID,
    AUTH_USERS,
    BULK_MAX_FILES,
    BULK_SAVES_PER_DAY,
    COOKIE_NAME,
    COOKIE_SECURE,
    DAILY_LIMIT,
    FORCESUB_NAME,
    MULTI_MAX_LINKS,
    MULTI_SAVES_PER_DAY,
    STATIC_DIR,
    TOKEN_TTL_SECONDS,
    WEB_HOST,
    WEB_PORT,
)
from .db import db
from .auth import (
    LoginError,
    login_with_session,
    start_phone_login,
    submit_code,
    submit_password,
)
from . import jobs as jobs_mod
from . import tg

log = logging.getLogger("webapp")

_NOTICE_COLORS = ("blue", "red", "yellow", "green")


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------
@contextlib.asynccontextmanager
async def lifespan(_: FastAPI):
    try:
        await db.ensure_indexes()
    except Exception:
        log.exception("Could not ensure DB indexes (is MongoDB reachable?)")
    try:
        await db.mark_interrupted()
    except Exception:
        log.exception("Could not reconcile history records")
    jobs_mod.clean_orphan_dirs()
    task = asyncio.create_task(jobs_mod.cleanup_loop())
    log.warning("Web client ready on http://%s:%s", WEB_HOST, WEB_PORT)
    yield
    task.cancel()
    for uid in list(tg._clients):
        await tg.drop_userbot(uid)


app = FastAPI(title="Telegram File Saver (web)", docs_url=None, redoc_url=None, lifespan=lifespan)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
class _ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


async def _uid(request: Request) -> int | None:
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        return None
    try:
        return await db.token_uid(token)
    except Exception:
        log.exception("token lookup failed")
        return None


async def _require_uid(request: Request) -> int:
    uid = await _uid(request)
    if uid is None:
        raise _ApiError(401, "Please log in first.")
    return uid


async def _require_admin(request: Request) -> int:
    uid = await _require_uid(request)
    if uid != ADMIN_UID and uid not in AUTH_USERS:
        raise _ApiError(403, "Not allowed.")
    return uid


def _is_admin(uid: int) -> bool:
    return uid == ADMIN_UID or uid in AUTH_USERS


def _client_ip(request: Request) -> str | None:
    return request.client.host if request.client else None


def _set_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        COOKIE_NAME,
        token,
        max_age=TOKEN_TTL_SECONDS,
        httponly=True,
        samesite="lax",
        secure=COOKIE_SECURE,
        path="/",
    )


def _clear_cookie(response: Response) -> None:
    response.delete_cookie(COOKIE_NAME, path="/")


_RATE_HITS: dict[str, list[float]] = {}
RATE_LIMIT = 10          # allowed hits per window...
RATE_WINDOW = 600        # ...per 10 minutes


def _rate_ok(key: str) -> bool:
    now = time.time()
    hits = [t for t in _RATE_HITS.get(key, []) if now - t < RATE_WINDOW]
    if len(hits) >= RATE_LIMIT:
        _RATE_HITS[key] = hits
        return False
    hits.append(now)
    _RATE_HITS[key] = hits
    if len(_RATE_HITS) > 10_000:  # keep the table bounded
        _RATE_HITS.clear()
    return True


@app.exception_handler(_ApiError)
async def _api_error_handler(request: Request, exc: _ApiError):
    return JSONResponse({"error": exc.message}, status_code=exc.status)


@app.exception_handler(RequestValidationError)
async def _validation_handler(request: Request, exc: RequestValidationError):
    return JSONResponse({"error": "Invalid request."}, status_code=400)


@app.exception_handler(Exception)
async def _generic_handler(request: Request, exc: Exception):
    if isinstance(exc, HTTPException):
        return JSONResponse({"error": exc.detail}, status_code=exc.status_code)
    log.exception("Unhandled error on %s", request.url.path)
    return JSONResponse({"error": "Something went wrong on the server."}, status_code=500)


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------
class PhoneReq(BaseModel):
    phone: str = Field(min_length=1, max_length=32)


class CodeReq(BaseModel):
    handle: str = Field(min_length=1, max_length=64)
    code: str = Field(min_length=1, max_length=16)


class PasswordReq(BaseModel):
    handle: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)


class SessionReq(BaseModel):
    session: str = Field(min_length=1, max_length=4096)


class FetchReq(BaseModel):
    link: str = Field(min_length=1, max_length=8192)
    mode: str = "single"          # single | batch | multi
    count: int | None = None      # batch: how many files to save


class BanReq(BaseModel):
    uid: int
    banned: bool


class SettingsReq(BaseModel):
    notice: str = Field(default="", max_length=1000)
    notice_login: str = Field(default="", max_length=1000)
    notice_color: str = "blue"
    notice_login_color: str = "blue"
    saving_enabled: bool = True


class PinReq(BaseModel):
    new_pin: str = Field(min_length=1, max_length=32)
    current_pin: str | None = Field(default=None, max_length=32)


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------
async def _complete_login(response: Response, me, session: str, client,
                          phone: str | None = None, ip: str | None = None) -> dict:
    """Persist the session, cache the live client, and issue the web cookie."""
    uid = me.id
    await db.add_user(uid, phone=phone, ip=ip)
    await db.store_login(uid, API_ID, API_HASH, session, getattr(me, "first_name", None))
    tg.cache_client(uid, client, me)
    token = await db.create_token(uid)
    _set_cookie(response, token)
    name = getattr(me, "first_name", None) or getattr(me, "username", None) or f"User {uid}"
    try:
        await db.record_login(uid, ip=ip)   # dashboard stat (best-effort)
    except Exception:
        pass
    return {"uid": uid, "name": name}


@app.post("/api/login/start")
async def login_start(req: PhoneReq, request: Request, response: Response):
    # Basic anti-abuse: this endpoint makes Telegram send an SMS/telegram code.
    ip = (request.client.host if request.client else "unknown") or "unknown"
    if not _rate_ok(f"start:{ip}"):
        raise _ApiError(429, "Too many login attempts from this address. Try again later.")

    phone = req.phone.strip()
    if phone.isdigit() and not phone.startswith("+"):
        # Bare digits are either the admin password or a phone number that is
        # missing its "+". Only a real match signs the admin in; anything else
        # falls through and is treated as a phone number.
        if ADMIN_PIN_MIN <= len(phone) <= ADMIN_PIN_MAX and await db.check_admin_pin(phone):
            token = await db.create_token(ADMIN_UID)
            _set_cookie(response, token)
            settings = await db.get_settings()
            return {
                "ok": True,
                "admin": True,
                "uid": ADMIN_UID,
                "name": "Admin",
                "must_change_pin": not settings.get("admin_pin_changed"),
            }
        phone = "+" + phone

    try:
        result = await start_phone_login(phone)
    except LoginError as exc:
        raise _ApiError(400, str(exc))
    return result


@app.post("/api/login/code")
async def login_code(req: CodeReq, request: Request, response: Response):
    ip = _client_ip(request)
    try:
        result = await submit_code(req.handle, req.code)
    except LoginError as exc:
        raise _ApiError(400, str(exc))
    if result.get("need_password"):
        return {"need_password": True, "handle": req.handle}
    info = await _complete_login(response, result["me"], result["session"],
                                 result["client"], phone=result.get("phone"), ip=ip)
    return {"ok": True, **info}


@app.post("/api/login/password")
async def login_password(req: PasswordReq, request: Request, response: Response):
    ip = _client_ip(request)
    try:
        result = await submit_password(req.handle, req.password)
    except LoginError as exc:
        raise _ApiError(400, str(exc))
    info = await _complete_login(response, result["me"], result["session"],
                                 result["client"], phone=result.get("phone"), ip=ip)
    return {"ok": True, **info}


@app.post("/api/login/session")
async def login_session(req: SessionReq, request: Request, response: Response):
    ip = _client_ip(request)
    try:
        result = await login_with_session(req.session)
    except LoginError as exc:
        raise _ApiError(400, str(exc))
    info = await _complete_login(response, result["me"], result["session"],
                                 result["client"], ip=ip)
    return {"ok": True, **info}


@app.post("/api/logout")
async def logout(request: Request, response: Response):
    uid = await _uid(request)
    token = request.cookies.get(COOKIE_NAME)
    if token:
        try:
            await db.revoke_token(token)
        except Exception:
            pass
    if uid is not None:
        await tg.drop_userbot(uid)
        try:
            await db.clear_login(uid)
        except Exception:
            pass
    _clear_cookie(response)
    return {"ok": True}


@app.get("/api/me")
async def me_endpoint(request: Request):
    uid = await _uid(request)
    try:
        settings = await db.get_settings()
    except Exception:
        log.exception("settings lookup failed")
        settings = {}
    if uid is None:
        # Visitors are not signed in, but they still get the public login notice
        return {
            "logged_in": False,
            "notice_login": settings.get("notice_login", ""),
            "notice_login_color": settings.get("notice_login_color", "blue"),
        }
    remaining, reset_at = await db.get_download_remaining(uid)
    try:
        bulk_remaining, _ = await db.get_bulk_remaining(uid)
        multi_remaining, _ = await db.get_multi_remaining(uid)
    except Exception:
        log.exception("quota lookup failed")
        bulk_remaining, multi_remaining = 0, 0
    admin = _is_admin(uid)
    name = (
        "Admin" if uid == ADMIN_UID
        else (await db.get_name(uid) or f"User {uid}")
    )
    return {
        "logged_in": True,
        "uid": uid,
        "name": name,
        "banned": await db.is_banned(uid),
        "daily_limit": DAILY_LIMIT,
        "remaining": remaining,
        "reset_at": reset_at,
        "bulk_limit": BULK_SAVES_PER_DAY,
        "bulk_max_files": BULK_MAX_FILES,
        "bulk_remaining": bulk_remaining,
        "multi_limit": MULTI_SAVES_PER_DAY,
        "multi_max_links": MULTI_MAX_LINKS,
        "multi_remaining": multi_remaining,
        "cooldown_remaining": jobs_mod.cooldown_remaining(uid),
        "force_sub": FORCESUB_NAME,
        "is_admin": admin,
        "must_change_pin": bool(admin and not settings.get("admin_pin_changed")),
        "notice": settings.get("notice", ""),
        "notice_color": settings.get("notice_color", "blue"),
        "notice_login": settings.get("notice_login", ""),
        "notice_login_color": settings.get("notice_login_color", "blue"),
        "saving_enabled": bool(settings.get("saving_enabled", True)),
    }


@app.post("/api/visit")
async def visit_endpoint(request: Request):
    """Beacon fired once per page load; feeds the dashboard's visit stats."""
    uid = await _require_uid(request)
    if uid != ADMIN_UID:
        try:
            await db.record_visit(uid, ip=_client_ip(request))
        except Exception:
            log.exception("visit recording failed")
    return {"ok": True}


# ---------------------------------------------------------------------------
# Fetch / jobs
# ---------------------------------------------------------------------------
@app.post("/api/fetch")
async def fetch_endpoint(req: FetchReq, request: Request):
    uid = await _require_uid(request)

    mode = (req.mode or "single").strip().lower() or "single"
    if mode not in ("single", "batch", "multi"):
        raise _ApiError(400, "Unknown save mode.")

    links: list[str] = []
    count = 0
    if mode == "multi":
        lines = [x.strip() for x in req.link.splitlines() if x.strip()]
        if not lines:
            raise _ApiError(400, "Please paste a Telegram message link (https://t.me/...).")
        if len(lines) > MULTI_MAX_LINKS:
            raise _ApiError(400, f"You can save up to {MULTI_MAX_LINKS} links at once.")
        for i, line in enumerate(lines, 1):
            link = jobs_mod.extract_link(line)
            if not link or ("t.me/" not in link and "telegram.me/" not in link):
                raise _ApiError(400, f"Line {i} is not a valid Telegram link.")
            try:
                parsed = jobs_mod.parse_link(link)
            except jobs_mod.JobError as exc:
                raise _ApiError(400, str(exc))
            if parsed["kind"] != "message":
                raise _ApiError(400, "Multi-link saves need message links (https://t.me/...).")
            links.append(link)
        primary = links[0]
    else:
        link = jobs_mod.extract_link(req.link.strip())
        if not link:
            raise _ApiError(400, "Please paste a Telegram message link (https://t.me/...).")
        if "t.me/" not in link and "telegram.me/" not in link:
            raise _ApiError(400, "Only Telegram links are supported (t.me/...).")
        try:
            parsed = jobs_mod.parse_link(link)
        except jobs_mod.JobError as exc:
            raise _ApiError(400, str(exc))
        if mode == "batch":
            if parsed["kind"] != "message":
                raise _ApiError(400, "Batch save needs a message link (https://t.me/...).")
            if req.count is None or req.count < 1:
                raise _ApiError(400, "Enter how many files to save.")
            if req.count > BULK_MAX_FILES:
                raise _ApiError(400, f"Batch save is limited to {BULK_MAX_FILES} files per run.")
            count = req.count
        primary = link

    if await db.is_banned(uid):
        raise _ApiError(403, "Your account has been banned.")

    if not (await db.get_settings()).get("saving_enabled", True) and uid != ADMIN_UID:
        raise _ApiError(403, "Saving is temporarily disabled by the administrator.")

    if mode == "batch":
        remaining, _ = await db.get_bulk_remaining(uid)
        if remaining <= 0:
            raise _ApiError(429, "You've used all your bulk saves for today. Please try again later.")
    elif mode == "multi":
        remaining, _ = await db.get_multi_remaining(uid)
        if remaining <= 0:
            raise _ApiError(429, "You've used all your multi-link saves for today. Please try again later.")
    else:
        remaining, _ = await db.get_download_remaining(uid)
        if remaining <= 0:
            raise _ApiError(429, "You've used all your daily saves. Please try again later.")

    cool = jobs_mod.cooldown_remaining(uid)
    if cool > 0:
        mins, secs = divmod(cool, 60)
        raise _ApiError(429, f"Please wait {mins}m {secs}s before saving again.")

    try:
        job = jobs_mod.create_job(uid, primary, kind=mode, count=count, links=links)
    except jobs_mod.JobError as exc:
        raise _ApiError(409, str(exc))
    return {"job": jobs_mod.snapshot(job)}


def _with_defaults(d: dict) -> dict:
    """Durable log records may miss transient fields the UI renders."""
    d.setdefault("downloaded", 0)
    d.setdefault("total", 0)
    d.setdefault("speed", 0)
    d.setdefault("wait_left", 0)
    d.setdefault("kind", "single")
    d.setdefault("count", 0)
    d.setdefault("saved_ids", [])
    d.setdefault("items", [])
    d.setdefault("error", None)
    d.setdefault("message", None)
    d.setdefault("text", None)
    d.setdefault("open_url", None)
    d.setdefault("percent", 100.0 if d.get("status") == "done" else 0.0)
    d.setdefault("step", d.get("status", ""))
    return d


@app.get("/api/jobs")
async def jobs_list(request: Request):
    uid = await _require_uid(request)
    # durable history log, with live in-memory jobs layered on top (they win
    # while a job runs, so progress keeps ticking without stale log copies)
    merged = {d["id"]: d for d in await db.get_history(uid)}
    for job in jobs_mod.list_jobs(uid):
        merged[job["id"]] = job
    jobs = sorted(merged.values(), key=lambda d: d.get("created", 0), reverse=True)
    return {"jobs": [_with_defaults(d) for d in jobs]}


@app.get("/api/jobs/{job_id}")
async def job_detail(job_id: str, request: Request):
    uid = await _require_uid(request)
    job = jobs_mod.get_job(job_id, uid)
    if not job:
        raise _ApiError(404, "This job has expired or does not exist.")
    return {"job": jobs_mod.snapshot(job)}


@app.post("/api/jobs/{job_id}/abort")
async def job_abort(job_id: str, request: Request):
    """Stop a running job (batch scan, multi-link run, upload)."""
    uid = await _require_uid(request)
    job = jobs_mod.get_job(job_id, uid)
    if not job:
        raise _ApiError(404, "This job has expired or does not exist.")
    try:
        jobs_mod.abort_job(job)
    except jobs_mod.JobError as exc:
        raise _ApiError(409, str(exc))
    return {"ok": True, "job": jobs_mod.snapshot(job)}


# ---------------------------------------------------------------------------
# Admin
# ---------------------------------------------------------------------------
@app.get("/api/stats")
async def stats(request: Request):
    uid = await _require_uid(request)
    if uid not in AUTH_USERS:
        raise _ApiError(403, "Not allowed.")
    totals = await db.totals()
    totals["active_jobs"] = sum(
        1 for j in jobs_mod.JOBS.values() if j.status in ("queued", "running")
    )
    totals["jobs_in_memory"] = len(jobs_mod.JOBS)
    return totals


@app.get("/api/admin/stats")
async def admin_stats(request: Request):
    await _require_admin(request)
    data = await db.admin_stats()
    data["active_jobs"] = sum(
        1 for j in jobs_mod.JOBS.values() if j.status in ("queued", "running")
    )
    data["jobs_in_memory"] = len(jobs_mod.JOBS)
    return data


@app.post("/api/admin/ban")
async def admin_ban(req: BanReq, request: Request):
    await _require_admin(request)
    if req.uid == ADMIN_UID:
        raise _ApiError(400, "You cannot ban the administrator.")
    await db.set_banned(req.uid, req.banned)
    if req.banned:
        try:
            await db.revoke_user_tokens(req.uid)
        except Exception:
            log.exception("could not revoke tokens for %s", req.uid)
        await tg.drop_userbot(req.uid)
    return {"ok": True, "uid": req.uid, "banned": req.banned}


@app.post("/api/admin/settings")
async def admin_settings(req: SettingsReq, request: Request):
    await _require_admin(request)
    notice = (req.notice or "").strip()[:500]
    notice_login = (req.notice_login or "").strip()[:500]
    color = req.notice_color if req.notice_color in _NOTICE_COLORS else "blue"
    color_login = req.notice_login_color if req.notice_login_color in _NOTICE_COLORS else "blue"
    await db.set_settings({
        "notice": notice,
        "notice_login": notice_login,
        "notice_color": color,
        "notice_login_color": color_login,
        "saving_enabled": bool(req.saving_enabled),
    })
    return {
        "ok": True,
        "notice": notice,
        "notice_login": notice_login,
        "notice_color": color,
        "notice_login_color": color_login,
        "saving_enabled": bool(req.saving_enabled),
    }


@app.post("/api/admin/pin")
async def admin_change_pin(req: PinReq, request: Request, response: Response):
    uid = await _require_admin(request)
    pin = (req.new_pin or "").strip()
    if not pin.isdigit() or not (ADMIN_PIN_MIN <= len(pin) <= ADMIN_PIN_MAX):
        raise _ApiError(
            400,
            f"The password must be {ADMIN_PIN_MIN} to {ADMIN_PIN_MAX} digits.",
        )
    settings = await db.get_settings()
    if settings.get("admin_pin_changed"):
        current = (req.current_pin or "").strip()
        if not current or not await db.check_admin_pin(current):
            raise _ApiError(400, "The current password is incorrect.")
        if await db.check_admin_pin(pin):
            raise _ApiError(400, "The new password must be different.")
    elif await db.check_admin_pin(pin):
        raise _ApiError(400, "Choose a password different from the default one.")
    await db.set_admin_pin(pin)
    # Rotating the password ends every other admin session: an old cookie (for
    # example one obtained with a leaked password) must not keep access.
    try:
        await db.revoke_user_tokens(uid)
        _set_cookie(response, await db.create_token(uid))
    except Exception:
        log.exception("admin session rotation failed")
    log.warning("Admin password changed from %s", _client_ip(request))
    return {"ok": True}


# ---------------------------------------------------------------------------
# Health probe (used by uptime monitors)
# ---------------------------------------------------------------------------
@app.get("/health")
async def health():
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Static frontend (mounted last so /api/* wins)
# ---------------------------------------------------------------------------
# index.html, i18n.js, app.js and style.css ship together (new UI strings and
# markup land at the same time). Without this, browsers heuristically cache the
# assets and can pair a fresh index.html with a stale i18n.js, which makes t()
# print raw keys such as "modal.logout_title". Revalidate every time; the
# existing ETags keep that a cheap 304.
@app.middleware("http")
async def static_no_cache(request: Request, call_next):
    response = await call_next(request)
    if not request.url.path.startswith("/api"):
        response.headers.setdefault("Cache-Control", "no-cache")
    return response


app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")


def main() -> None:
    import uvicorn

    uvicorn.run("app.main:app", host=WEB_HOST, port=WEB_PORT, workers=1, log_level="info")


if __name__ == "__main__":
    main()
