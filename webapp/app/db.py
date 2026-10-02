"""MongoDB layer for the web client.

Uses the same `users` collection as the Telegram bot, so a session created
here is also recognised by the bot (and vice versa).
"""

import hashlib
import secrets
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

import motor.motor_asyncio

from .config import (
    MONGODB_URI,
    DB_NAME,
    TOKEN_TTL_SECONDS,
    DAILY_LIMIT,
    HISTORY_LIMIT,
    ADMIN_PIN,
    ADMIN_UID,
)


class Database:

    def __init__(self, uri: str = MONGODB_URI, name: str = DB_NAME) -> None:
        self._cli = motor.motor_asyncio.AsyncIOMotorClient(uri)
        self._db = self._cli[name]
        self._users = self._db.users
        self._tokens = self._db.web_tokens
        self._stats = self._db.stats
        self._history = self._db.web_history
        self._settings = self._db.web_settings
        self._daily = self._db.web_daily

    # -- setup ---------------------------------------------------------------
    async def ensure_indexes(self) -> None:
        await self._users.create_index("id", unique=True)
        await self._tokens.create_index("hash", unique=True)
        await self._tokens.create_index("uid")
        await self._tokens.create_index("expires")
        await self._users.create_index("web_last_seen")
        await self._history.create_index("id", unique=True)
        await self._history.create_index("uid")
        await self._history.create_index("expires", expireAfterSeconds=0)

    # -- durable save history --------------------------------------------
    async def save_history(self, doc: dict) -> None:
        """Insert or refresh one history record, then trim to the per-user cap.

        Records carry no file data - only the job outcome (link, status, item
        names, timestamp) so the History tab survives server restarts.
        """
        await self._history.update_one({"id": doc["id"]}, {"$set": doc}, upsert=True)
        extra = await (
            self._history.find({"uid": doc["uid"]})
            .sort("created", -1)
            .skip(HISTORY_LIMIT)
            .to_list(length=None)
        )
        stale = [d["id"] for d in extra]
        if stale:
            await self._history.delete_many({"id": {"$in": stale}})

    async def get_history(self, uid: int, limit: int = HISTORY_LIMIT) -> list[dict]:
        try:
            docs = await (
                self._history.find({"uid": uid})
                .sort("created", -1)
                .limit(limit)
                .to_list(length=limit)
            )
        except Exception:
            return []
        for d in docs:
            d.pop("_id", None)
        return docs

    async def mark_interrupted(self) -> None:
        """Flag records that were mid-run when the server last stopped."""
        await self._history.update_many(
            {"status": {"$in": ["queued", "running"]}},
            {"$set": {
                "status": "error",
                "step": "Failed",
                "error": "Interrupted by a server restart.",
                "updated": time.time(),
            }},
        )

    # -- users ---------------------------------------------------------------
    def _new_user(self, uid: int) -> dict:
        return {
            "id": uid,
            "banned": False,
            "api_id": None,
            "api_hash": None,
            "session": None,
            "log": False,
            "first_name": None,
            "daily_download_count": 0,
            "daily_download_reset_at": 0,
        }

    async def add_user(self, uid: int) -> None:
        await self._users.update_one(
            {"id": uid}, {"$setOnInsert": self._new_user(uid)}, upsert=True
        )

    async def is_user_exist(self, uid: int) -> bool:
        return bool(await self._users.find_one({"id": uid}))

    async def is_banned(self, uid: int) -> bool:
        user = await self._users.find_one({"id": uid}, {"banned": 1})
        return bool(user.get("banned")) if user else False

    async def set_name(self, uid: int, name: str) -> None:
        await self._users.update_one({"id": uid}, {"$set": {"first_name": name}}, upsert=True)

    async def get_name(self, uid: int) -> Optional[str]:
        user = await self._users.find_one({"id": uid}, {"first_name": 1})
        return (user or {}).get("first_name")

    # -- sessions ------------------------------------------------------------
    async def store_login(self, uid: int, api_id: int, api_hash: str, session: str, name: str | None = None) -> None:
        payload = {"api_id": api_id, "api_hash": api_hash, "session": session, "log": True}
        if name:
            payload["first_name"] = name
        await self._users.update_one({"id": uid}, {"$set": payload}, upsert=True)

    async def clear_login(self, uid: int) -> None:
        await self._users.update_one(
            {"id": uid},
            {"$unset": {"api_id": 1, "api_hash": 1, "session": 1}, "$set": {"log": False}},
        )

    async def get_credentials(self, uid: int) -> tuple[Optional[int], Optional[str], Optional[str]]:
        user = await self._users.find_one({"id": uid})
        if not user:
            return None, None, None
        return user.get("api_id"), user.get("api_hash"), user.get("session")

    async def is_logged(self, uid: int) -> bool:
        user = await self._users.find_one({"id": uid}, {"log": 1, "session": 1})
        return bool(user and user.get("log") and user.get("session"))

    # -- daily quota ---------------------------------------------------------
    async def get_download_remaining(self, uid: int) -> tuple[int, int]:
        """Returns (remaining, reset_at)."""
        user = await self._users.find_one({"id": uid})
        if not user:
            return DAILY_LIMIT, 0
        now = time.time()
        count = user.get("daily_download_count", 0)
        reset_at = user.get("daily_download_reset_at", 0)
        if now >= reset_at:
            if count or reset_at:
                await self._users.update_one(
                    {"id": uid},
                    {"$set": {"daily_download_count": 0, "daily_download_reset_at": 0}},
                )
            return DAILY_LIMIT, 0
        return max(0, DAILY_LIMIT - count), int(reset_at)

    async def increment_download_count(self, uid: int) -> None:
        now = time.time()
        await self.add_user(uid)
        user = await self._users.find_one({"id": uid}) or {}
        count = user.get("daily_download_count", 0)
        reset_at = user.get("daily_download_reset_at", 0)
        if reset_at == 0 or now >= reset_at:
            await self._users.update_one(
                {"id": uid},
                {"$set": {"daily_download_count": 1, "daily_download_reset_at": now + 86400}},
            )
        else:
            await self._users.update_one({"id": uid}, {"$set": {"daily_download_count": count + 1}})

    # -- web auth tokens -----------------------------------------------------
    @staticmethod
    def _hash(token: str) -> str:
        return hashlib.sha256(token.encode()).hexdigest()

    async def create_token(self, uid: int) -> str:
        token = secrets.token_urlsafe(32)
        now = time.time()
        await self._tokens.update_one(
            {"hash": self._hash(token)},
            {"$set": {"uid": uid, "created": now, "expires": now + TOKEN_TTL_SECONDS}},
            upsert=True,
        )
        return token

    async def token_uid(self, token: str) -> Optional[int]:
        if not token:
            return None
        doc = await self._tokens.find_one(
            {"hash": self._hash(token), "expires": {"$gt": time.time()}}, {"uid": 1}
        )
        return doc["uid"] if doc else None

    async def revoke_token(self, token: str) -> None:
        await self._tokens.delete_one({"hash": self._hash(token)})

    async def revoke_user_tokens(self, uid: int) -> None:
        await self._tokens.delete_many({"uid": uid})

    # -- stats ---------------------------------------------------------------
    async def increment_web_fetch(self) -> None:
        await self._stats.update_one(
            {"_id": "stats"}, {"$inc": {"web_fetches": 1}}, upsert=True
        )

    async def totals(self) -> dict:
        return {
            "users": await self._users.estimated_document_count(),
            "logged_in": await self._users.count_documents({"log": True}),
            "web_fetches": ((await self._stats.find_one({"_id": "stats"})) or {}).get("web_fetches", 0),
        }

    # -- admin settings --------------------------------------------------------
    @staticmethod
    def _hash_pin(pin: str) -> str:
        return hashlib.sha256(("web_admin_pin:" + pin).encode()).hexdigest()

    @staticmethod
    def _today() -> str:
        return datetime.now(timezone.utc).date().isoformat()

    async def get_settings(self) -> dict:
        """Site settings (admin password state, notice banner, save switch)."""
        doc = await self._settings.find_one({"_id": "config"})
        if not doc:
            await self._settings.update_one(
                {"_id": "config"},
                {"$setOnInsert": {
                    "admin_pin_hash": self._hash_pin(ADMIN_PIN),
                    "admin_pin_changed": False,
                    "notice": "",
                    "saving_enabled": True,
                }},
                upsert=True,
            )
            doc = await self._settings.find_one({"_id": "config"}) or {}
        return {
            "admin_pin_hash": doc.get("admin_pin_hash") or self._hash_pin(ADMIN_PIN),
            "admin_pin_changed": bool(doc.get("admin_pin_changed")),
            "notice": doc.get("notice") or "",
            "saving_enabled": bool(doc.get("saving_enabled", True)),
        }

    async def set_settings(self, values: dict) -> None:
        await self._settings.update_one(
            {"_id": "config"}, {"$set": dict(values)}, upsert=True
        )

    async def check_admin_pin(self, pin: str) -> bool:
        settings = await self.get_settings()
        return secrets.compare_digest(settings["admin_pin_hash"], self._hash_pin(pin))

    async def set_admin_pin(self, pin: str) -> None:
        await self.set_settings({
            "admin_pin_hash": self._hash_pin(pin),
            "admin_pin_changed": True,
        })

    async def set_banned(self, uid: int, banned: bool) -> None:
        await self._users.update_one(
            {"id": uid}, {"$set": {"banned": bool(banned)}}, upsert=True
        )

    # -- activity counters (admin dashboard) -----------------------------------
    async def record_visit(self, uid: int) -> None:
        now = time.time()
        await self._users.update_one(
            {"id": uid},
            {"$inc": {"web_visits": 1}, "$set": {"web_last_seen": now}},
        )
        await self._daily.update_one(
            {"_id": self._today()}, {"$inc": {"visits": 1}}, upsert=True
        )

    async def record_login(self, uid: int) -> None:
        await self._daily.update_one(
            {"_id": self._today()}, {"$inc": {"logins": 1}}, upsert=True
        )

    async def record_files_saved(self, n: int) -> None:
        if not n:
            return
        await self._stats.update_one(
            {"_id": "stats"}, {"$inc": {"files_saved": int(n)}}, upsert=True
        )
        await self._daily.update_one(
            {"_id": self._today()}, {"$inc": {"files": int(n)}}, upsert=True
        )

    async def admin_stats(self) -> dict:
        """Everything the admin dashboard shows: totals, 30-day series, users."""
        now = time.time()
        stats_doc = (await self._stats.find_one({"_id": "stats"})) or {}
        users_total = await self._users.estimated_document_count()
        logged_in = await self._users.count_documents({"log": True})
        banned_n = await self._users.count_documents({"banned": True})
        visited = await self._users.count_documents({"web_visits": {"$gte": 1}})
        returning = await self._users.count_documents({"web_visits": {"$gt": 1}})

        today = datetime.now(timezone.utc).date()
        day_ids = [(today - timedelta(days=i)).isoformat() for i in range(29, -1, -1)]
        docs = await self._daily.find({"_id": {"$in": day_ids}}).to_list(length=40)
        by_id = {d["_id"]: d for d in docs}
        daily = []
        for day in day_ids:
            d = by_id.get(day, {})
            daily.append({
                "day": day,
                "files": int(d.get("files", 0)),
                "visits": int(d.get("visits", 0)),
                "logins": int(d.get("logins", 0)),
            })

        users = []
        cursor = self._users.find(
            {},
            {"id": 1, "first_name": 1, "banned": 1, "log": 1, "web_visits": 1,
             "web_last_seen": 1, "daily_download_count": 1,
             "daily_download_reset_at": 1},
        )
        async for u in cursor:
            uid = u.get("id")
            if uid == ADMIN_UID:
                continue
            saves_today = (
                u.get("daily_download_count", 0)
                if u.get("daily_download_reset_at", 0) > now else 0
            )
            users.append({
                "uid": uid,
                "name": u.get("first_name") or f"User {uid}",
                "banned": bool(u.get("banned")),
                "logged_in": bool(u.get("log")),
                "visits": int(u.get("web_visits", 0)),
                "last_seen": u.get("web_last_seen") or 0,
                "saves_today": int(saves_today),
            })
        users.sort(key=lambda x: (x["last_seen"], x["visits"]), reverse=True)

        return {
            "totals": {
                "users": users_total,
                "logged_in": logged_in,
                "banned": banned_n,
                "visited": visited,
                "returning": returning,
                "files_saved": int(stats_doc.get("files_saved", 0)),
                "jobs_total": int(stats_doc.get("web_fetches", 0)),
                "visits_today": daily[-1]["visits"],
                "saves_today": daily[-1]["files"],
            },
            "daily": daily,
            "users": users[:300],
        }


db = Database()
