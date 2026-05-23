import os
from asyncio import sleep
from telethon import events, Button
from telethon.errors import FloodWaitError
from .. import bot, AUTH_USERS, MONGODB_URI
from main.Database.database import Database, Stats

db = Database(MONGODB_URI, 'saverestricted')
stats_db = Stats(MONGODB_URI, 'saverestricted')


@bot.on(events.NewMessage(incoming=True, func=lambda e: e.is_private))
async def add_new_user(event):
    msg_id = event.id
    if msg_id in _processed:
        return
    _processed.add(msg_id)
    if not await db.is_user_exist(event.sender_id):
        await db.add_user(event.sender_id)

    if event.photo:
        img = await event.download_media()
        if os.path.exists(f"{event.sender_id}.jpg"):
            os.remove(f"{event.sender_id}.jpg")
        os.rename(img, f"{event.sender_id}.jpg")
        await event.reply("Your thumbnail image has been saved.")


_processed = set()

async def clean_processed():
    while True:
        await sleep(600)
        _processed.clear()

bot.loop.create_task(clean_processed())

@bot.on(events.NewMessage(incoming=True, from_users=AUTH_USERS, pattern='/users'))
async def count_users(event):
    msg_id = event.id
    if msg_id in _processed:
        return
    _processed.add(msg_id)
    msg = await event.reply('Counting users...')
    total = await db.total_users_count()
    await msg.edit(f'Total users: {total}')


@bot.on(events.NewMessage(incoming=True, from_users=AUTH_USERS, pattern='/stats'))
async def stats_handler(event):
    msg_id = event.id
    if msg_id in _processed:
        return
    _processed.add(msg_id)
    total_users = await db.total_users_count()
    logged_in = await db.logged_in_count()
    total_forwards = await stats_db.get_total_forwards()
    await event.reply(
        f"**Bot Statistics**\n\n"
        f"👥 Total users: `{total_users}`\n"
        f"🔑 Logged in: `{logged_in}`\n"
        f"📤 Total forwards: `{total_forwards}`"
    )


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
