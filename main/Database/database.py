from typing import Optional, AsyncGenerator
import motor.motor_asyncio
from .. import MONGODB_URI

SESSION_NAME = 'saverestricted'


class Database:

    def __init__(self, uri: str = MONGODB_URI, name: str = SESSION_NAME) -> None:
        self._cli = motor.motor_asyncio.AsyncIOMotorClient(uri)
        self._db = self._cli[name]
        self._users = self._db.users
        self._users.create_index('id', unique=True)

    async def _update(self, uid: int, payload: dict) -> None:
        await self._users.update_one({'id': uid}, {'$set': payload}, upsert=True)

    async def _rem_field(self, uid: int, field: str) -> None:
        await self._users.update_one({'id': uid}, {'$unset': {field: 1}})

    def _new_user(self, uid: int) -> dict:
        return {'id': uid, 'banned': False, 'api_id': None,
                'api_hash': None, 'session': None, 'log': False}

    async def add_user(self, uid: int) -> None:
        await self._users.insert_one(self._new_user(uid))

    async def is_user_exist(self, uid: int) -> bool:
        return bool(await self._users.find_one({'id': uid}))

    async def total_users_count(self) -> int:
        return await self._users.estimated_document_count()

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

    async def set_botCreds(self, uid: int, token: str) -> None:
        await self._users.insert_one({'id': f'bot{uid}', 'bot_token': token})

    async def get_botCreds(self, uid: int) -> Optional[str]:
        doc = await self._users.find_one({'id': f'bot{uid}'})
        return doc.get('bot_token') if doc else None

    async def botLogged(self, uid: int) -> bool:
        return bool(await self._users.find_one({'id': f'bot{uid}'}))

    async def botLogout(self, uid: int) -> None:
        await self._users.delete_many({'id': f'bot{uid}'})
