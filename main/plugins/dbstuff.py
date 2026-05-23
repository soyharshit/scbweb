from asyncio import sleep
from telethon import events, Button
from telethon.errors import FloodWaitError
from .. import bot, AUTH_USERS, MONGODB_URI
from main.Database.database import Database

db = Database(MONGODB_URI, 'saverestricted')


@bot.on(events.NewMessage(incoming=True, func=lambda e: e.is_private))
async def add_new_user(event):
    if not await db.is_user_exist(event.sender_id):
        await db.add_user(event.sender_id)


@bot.on(events.NewMessage(incoming=True, from_users=AUTH_USERS, pattern='/users'))
async def count_users(event):
    msg = await event.reply('Counting users...')
    total = await db.total_users_count()
    await msg.edit(f'Total users: {total}')


@bot.on(events.NewMessage(incoming=True, from_users=AUTH_USERS, pattern='/bcast'))
async def broadcast(event):
    reply = await event.get_reply_message()
    if not reply:
        return await event.reply('Please reply to a message to broadcast.')
    count = await db.total_users_count()
    status = await event.reply(f'Broadcasting to {count} users...')
    sent, failed = 0, 0
    async for user in db.get_users():
        uid = int(user['id'])
        try:
            await event.client.send_message(uid, reply)
            sent += 1
        except FloodWaitError as fw:
            await sleep(fw.seconds + 10)
            try:
                await event.client.send_message(uid, reply)
                sent += 1
            except Exception:
                failed += 1
        except Exception:
            failed += 1
        if (sent + failed) % 10 == 0:
            await status.edit(
                f'Broadcast Progress\n\nSent: {sent}\nFailed: {failed}',
                buttons=[[Button.inline('In Progress...', b'none')]])
    await status.edit(
        f'Broadcast Complete\n\nTotal: {count}\nSent: {sent}\nFailed: {failed}')


@bot.on(events.NewMessage(incoming=True, from_users=AUTH_USERS, pattern=r'^/disallow (\d+)'))
async def ban_user(event):
    uid = int(event.pattern_match.group(1))
    if uid in AUTH_USERS:
        return await event.reply('Cannot ban an authorized user.')
    if await db.is_banned(uid):
        return await event.reply('User is already banned.')
    await db.banning(uid)
    await event.reply(f'User {uid} has been banned.')


@bot.on(events.NewMessage(incoming=True, from_users=AUTH_USERS, pattern=r'^/allow (\d+)'))
async def unban_user(event):
    uid = int(event.pattern_match.group(1))
    if not await db.is_banned(uid):
        return await event.reply('User is not banned.')
    await db.unbanning(uid)
    await event.reply(f'User {uid} has been unbanned.')
