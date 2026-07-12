from typing import Optional, AsyncGenerator
import time
import motor.motor_asyncio
from .. import MONGODB_URI, DB_NAME

SESSION_NAME = DB_NAME


class Database:

    def __init__(self, uri: str = MONGODB_URI, name: str = SESSION_NAME) -> None:
        self._cli = motor.motor_asyncio.AsyncIOMotorClient(uri)
        self._db = self._cli[name]
        self._users = self._db.users

    async def _ensure_indexes(self) -> None:
        pipeline = [
            {'$group': {'_id': '$id', 'dups': {'$addToSet': '$_id'}, 'count': {'$sum': 1}}},
            {'$match': {'count': {'$gt': 1}}}
        ]
        async for dup in self._users.aggregate(pipeline):
            dup_ids = dup['dups'][1:]
            await self._users.delete_many({'_id': {'$in': dup_ids}})
        await self._users.create_index('id', unique=True)

    async def _update(self, uid: int, payload: dict) -> None:
        await self._users.update_one({'id': uid}, {'$set': payload}, upsert=True)

    async def _rem_field(self, uid: int, field: str) -> None:
        await self._users.update_one({'id': uid}, {'$unset': {field: 1}})

    def _new_user(self, uid: int) -> dict:
        return {'id': uid, 'banned': False, 'api_id': None,
                'api_hash': None, 'session': None, 'log': False,
                'daily_download_count': 0, 'daily_download_reset_at': 0}

    async def add_user(self, uid: int) -> None:
        await self._users.update_one(
            {'id': uid}, {'$setOnInsert': self._new_user(uid)}, upsert=True
        )

    async def is_user_exist(self, uid: int) -> bool:
        return bool(await self._users.find_one({'id': uid}))

    async def total_users_count(self) -> int:
        return await self._users.estimated_document_count()

    async def logged_in_count(self) -> int:
        return await self._users.count_documents({'log': True})

    async def get_users(self) -> AsyncGenerator[dict, None]:
        async for doc in self._users.find({}):
            yield doc

    async def banning(self, uid: int) -> None:
        await self._update(uid, {'banned': True})

    async def unbanning(self, uid: int) -> None:
        await self._update(uid, {'banned': False})

    async def is_banned(self, uid: int) -> bool:
        user = await self._users.find_one({'id': uid})
        return user.get('banned', False) if user else False

    async def update_api_id(self, uid: int, api_id: int) -> None:
        await self._update(uid, {'api_id': api_id})

    async def rem_api_id(self, uid: int) -> None:
        await self._rem_field(uid, 'api_id')

    async def update_api_hash(self, uid: int, api_hash: str) -> None:
        await self._update(uid, {'api_hash': api_hash})

    async def rem_api_hash(self, uid: int) -> None:
        await self._rem_field(uid, 'api_hash')

    async def update_session(self, uid: int, session: str) -> None:
        await self._update(uid, {'session': session})

    async def rem_session(self, uid: int) -> None:
        await self._rem_field(uid, 'session')

    async def get_credentials(self, uid: int) -> tuple[Optional[int], Optional[str], Optional[str]]:
        user = await self._users.find_one({'id': uid})
        if not user:
            return None, None, None
        return user.get('api_id'), user.get('api_hash'), user.get('session')

    async def loin(self, uid: int) -> None:
        await self._update(uid, {'log': True})

    async def lout(self, uid: int) -> None:
        await self._update(uid, {'log': False})

    async def is_logged(self, uid: int) -> bool:
        user = await self._users.find_one({'id': uid})
        return user.get('log', False) if user else False

    async def get_download_remaining(self, uid: int) -> tuple[int, int]:
        user = await self._users.find_one({'id': uid})
        if not user:
            return 20, 0
        now = time.time()
        count = user.get('daily_download_count', 0)
        reset_at = user.get('daily_download_reset_at', 0)
        if now >= reset_at:
            await self._update(uid, {'daily_download_count': 0, 'daily_download_reset_at': 0})
            return 20, 0
        remaining = max(0, 20 - count)
        return remaining, int(reset_at)

    async def increment_download_count(self, uid: int) -> None:
        now = time.time()
        user = await self._users.find_one({'id': uid})
        if not user:
            await self.add_user(uid)
            user = {'id': uid, 'daily_download_count': 0, 'daily_download_reset_at': 0}
        count = user.get('daily_download_count', 0)
        reset_at = user.get('daily_download_reset_at', 0)
        if reset_at == 0 or now >= reset_at:
            await self._update(uid, {'daily_download_count': 1, 'daily_download_reset_at': now + 86400})
        else:
            await self._update(uid, {'daily_download_count': count + 1})

    async def set_botCreds(self, uid: int, token: str) -> None:
        await self._users.insert_one({'id': f'bot{uid}', 'bot_token': token})

    async def get_botCreds(self, uid: int) -> Optional[str]:
        doc = await self._users.find_one({'id': f'bot{uid}'})
        return doc.get('bot_token') if doc else None

    async def botLogged(self, uid: int) -> bool:
        return bool(await self._users.find_one({'id': f'bot{uid}'}))

    async def botLogout(self, uid: int) -> None:
        await self._users.delete_many({'id': f'bot{uid}'})


class Stats:
    _db = None

    def __init__(self, uri: str, db_name: str = SESSION_NAME):
        cli = motor.motor_asyncio.AsyncIOMotorClient(uri)
        self._db = cli[db_name]
        self._stats = self._db.stats

    async def increment_forwards(self, n: int = 1) -> None:
        await self._stats.update_one(
            {'_id': 'stats'},
            {'$inc': {'total_forwards': n}},
            upsert=True
        )

    async def get_total_forwards(self) -> int:
        doc = await self._stats.find_one({'_id': 'stats'})
        return doc.get('total_forwards', 0) if doc else 0
