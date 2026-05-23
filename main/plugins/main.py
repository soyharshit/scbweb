import os
import time
import asyncio
import re
import cv2
import logging
from pyrogram import Client, filters
from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton
from pyrogram.errors import FloodWait

from .. import bot, API_ID, API_HASH, FORCESUB, Bot
from ..plugins.helpers import get_link, join, set_timer, check_timer, _clients
from ..plugins.display_progress import progress_for_pyrogram
from main.plugins.dbstuff import db, stats_db

logging.basicConfig(format='[%(levelname) 5s/%(asctime)s] %(name)s: %(message)s', level=logging.WARNING)

COOLDOWN_SECONDS = 300

process = []
timer = []
user_queues = {}
user_processing = set()
user_cooldowns = {}


async def enqueue_and_process(user_id, link, init_msg):
    if user_id not in user_queues:
        user_queues[user_id] = asyncio.Queue()

    queue = user_queues[user_id]
    is_processing = user_id in user_processing

    if is_processing:
        pos = queue.qsize() + 1
        await init_msg.edit(f'Added to queue. Position: #{pos}')
    else:
        user_processing.add(user_id)

    await queue.put((link, init_msg))

    if not is_processing:
        asyncio.create_task(process_user_queue(user_id))


async def process_user_queue(user_id):
    queue = user_queues.get(user_id)
    if not queue:
        user_processing.discard(user_id)
        return

    while not queue.empty():
        link, init_msg = await queue.get()

        now = time.time()
        cooldown_end = user_cooldowns.get(user_id, 0)
        if cooldown_end > now:
            remaining_secs = int(cooldown_end - now)
            mins, secs = divmod(remaining_secs, 60)
            await init_msg.edit(f'Please wait {mins}m {secs}s before you can forward next message\n\nContact @BotsCraftAdmin to buy premium to remove this waiting time')
            await asyncio.sleep(remaining_secs)

        remaining = list(queue._queue)
        for idx, (_, msg) in enumerate(remaining):
            try:
                await msg.edit(f'Added to queue. Position: #{idx + 1}')
            except Exception:
                pass

        userbot = _clients.get(f"ub_{user_id}")

        if not userbot:
            try:
                await init_msg.edit('Session expired. Please re-login.')
            except Exception:
                pass
            continue

        txt = getattr(init_msg, 'text', '') or ''
        if 'Added to queue' in txt or 'Position' in txt:
            try:
                await init_msg.edit('Processing...')
            except Exception:
                pass

        try:
            await get_msg(userbot, user_id, link, init_msg)
            await stats_db.increment_forwards()
            user_cooldowns[user_id] = time.time() + COOLDOWN_SECONDS
        except FloodWait as fw:
            await asyncio.sleep(fw.value)
            try:
                await init_msg.edit(f'Rate limited. Retry after {fw.value}s.')
            except Exception:
                pass
        except Exception as e:
            try:
                await init_msg.edit(f'Error: {e}')
            except Exception:
                pass

    user_processing.discard(user_id)
    if user_id in user_queues and user_queues[user_id].empty():
        del user_queues[user_id]


async def is_user_subscribed(uid):
    if not FORCESUB:
        return True
    try:
        await bot.get_chat_member(FORCESUB, uid)
        return True
    except:
        return False


def video_meta(path):
    cap = cv2.VideoCapture(path)
    if not cap.isOpened():
        return dict(duration=0, width=0, height=0)
    width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    fps = cap.get(cv2.CAP_PROP_FPS) or 25
    frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    duration = int(frames / fps) if fps else 0
    cap.release()
    return dict(duration=duration, width=width, height=height)


async def gen_thumb(video, out):
    cap = cv2.VideoCapture(video)
    cap.set(cv2.CAP_PROP_POS_FRAMES, int(cap.get(cv2.CAP_PROP_FRAME_COUNT) // 2))
    ret, frame = cap.read()
    cap.release()
    if not ret:
        return None
    h, w = frame.shape[:2]
    if w > 320 or h > 320:
        scale = 320 / max(w, h)
        new_w, new_h = int(w * scale), int(h * scale)
        frame = cv2.resize(frame, (new_w, new_h))
    cv2.imwrite(out, frame)
    if os.path.getsize(out) > 200 * 1024:
        os.remove(out)
        return None
    return out


async def get_msg(userbot, user_id, msg_link, edit):
    msg_id = None
    chat_ids = []

    try:
        if 't.me/c/' in msg_link:
            raw_id = msg_link.split('/')[-2]
            msg_id = int(msg_link.split('/')[-1].split('?')[0])
            if raw_id.startswith('-100'):
                chat_ids.append(int(raw_id))
            elif raw_id.startswith('100'):
                chat_ids.append(int('-' + raw_id))
                chat_ids.append(int(raw_id))
            else:
                chat_ids.append(int('-100' + raw_id))
                chat_ids.append(int(raw_id))
            chat_ids.append(raw_id)
        elif 't.me/b/' in msg_link:
            chat_ids.append(int(msg_link.split('/')[-2]))
            msg_id = int(msg_link.split('/')[-1].split('?')[0])
        elif 't.me/' in msg_link:
            username = msg_link.split('/')[-2]
            msg_id = int(msg_link.split('/')[-1].split('?')[0])
            chat_ids.append(username)
        else:
            await edit.edit('Unsupported link format.')
            return
    except (ValueError, IndexError):
        await edit.edit('Invalid message link.')
        return

    msg = None
    for cid in chat_ids:
        try:
            msg = await userbot.get_messages(cid, msg_id)
            if msg and (msg.media or msg.text or msg.caption):
                break
        except Exception:
            continue

    if not msg:
        try:
            raw_id = str(msg_link.split('/')[-2]).lstrip('-100')
            async for dialog in userbot.get_dialogs():
                cid = getattr(dialog.chat, 'id', None)
                if cid and raw_id in str(abs(cid)):
                    msg = await userbot.get_messages(cid, msg_id)
                    if msg and (msg.media or msg.text or msg.caption):
                        break
        except Exception:
            pass

    if not msg:
        await edit.edit("You have not joined the channel. Please join the channel first.")
        return

    if not msg.media and not msg.text and not msg.caption:
        await edit.edit('Message has no content to save.')
        return

    try:
        if msg.media_group_id:
            sent = await userbot.copy_media_group("me", msg.chat.id, msg.id)
            saved_id = sent[0].id
        else:
            try:
                sent = await userbot.forward_messages("me", msg.chat.id, [msg.id])
            except Exception:
                sent = await userbot.copy_message("me", msg.chat.id, msg.id)
            saved_id = sent[0].id if isinstance(sent, list) else sent.id
        await edit.edit('Saved to your Saved Messages.', reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('View Message', url=f'tg://openmessage?user_id={user_id}&message_id={saved_id}')]]))
        return
    except Exception:
        pass

    file = await userbot.download_media(
        msg,
        progress=progress_for_pyrogram,
        progress_args=(userbot, 'Downloading...', edit, time.time())
    )
    if not file:
        await edit.edit('Failed to download media.')
        return

    caption = msg.caption or ''
    entities = msg.caption_entities
    await edit.edit('Uploading...')
    saved_id = None

    try:
        if msg.video_note:
            meta = video_meta(file)
            thumb = await gen_thumb(file, f'{user_id}.jpg')
            sent = await userbot.send_video_note(
                "me", file, length=meta['height'], duration=meta['duration'],
                thumb=thumb, progress=progress_for_pyrogram,
                progress_args=(userbot, 'Uploading...', edit, time.time())
            )
            saved_id = sent.id
        elif msg.video:
            meta = video_meta(file)
            thumb = await gen_thumb(file, f'{user_id}.jpg')
            sent = await userbot.send_video(
                "me", file, caption=caption, caption_entities=entities,
                duration=meta['duration'], width=meta['width'], height=meta['height'],
                thumb=thumb, progress=progress_for_pyrogram,
                progress_args=(userbot, 'Uploading...', edit, time.time())
            )
            saved_id = sent.id
        elif msg.photo:
            sent = await userbot.send_photo("me", file, caption=caption, caption_entities=entities)
            saved_id = sent.id
        elif msg.audio:
            sent = await userbot.send_audio("me", file, caption=caption, caption_entities=entities,
                                             progress=progress_for_pyrogram,
                                             progress_args=(userbot, 'Uploading...', edit, time.time()))
            saved_id = sent.id
        else:
            sent = await userbot.send_document("me", file, caption=caption, caption_entities=entities,
                                                progress=progress_for_pyrogram,
                                                progress_args=(userbot, 'Uploading...', edit, time.time()))
            saved_id = sent.id
    except FloodWait as fw:
        await asyncio.sleep(fw.value)
        await edit.edit(f'Rate limited. Retry after {fw.value} seconds.')
    except Exception as e:
        await edit.edit(f'Upload failed: {e}')
    else:
        await edit.edit('Saved to your Saved Messages.', reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('View Message', url=f'tg://openmessage?user_id={user_id}&message_id={saved_id}')]]))
    finally:
        if os.path.isfile(file):
            os.remove(file)
        thumb_path = f'{user_id}.jpg'
        if os.path.isfile(thumb_path):
            os.remove(thumb_path)


@Bot.on_message(filters.private & filters.incoming)
async def clone_handler(bot_, event: Message):
    link = get_link(event.text)
    if not link:
        return

    init = await event.reply('Processing...')

    if not await is_user_subscribed(event.from_user.id):
        join_url = f"https://t.me/{FORCESUB}"
        return await init.edit(
            'Please join the channel first to use this bot.',
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton('Join Channel', url=join_url)]
            ])
        )

    creds = await db.get_credentials(event.from_user.id)
    if not creds or not all(creds):
        return await init.edit('Please log in first using /login.')

    userbot = _clients.get(f"ub_{event.from_user.id}")
    if not userbot:
        userbot = Client(
            name=f"user_{event.from_user.id}",
            api_id=creds[0],
            api_hash=creds[1],
            session_string=creds[2],
            in_memory=True
        )
        try:
            await userbot.start()
            await userbot.get_me()
            _clients[f"ub_{event.from_user.id}"] = userbot
        except Exception as e:
            _clients.pop(f"ub_{event.from_user.id}", None)
            return await init.edit(f'User session error: {e}')

    if 't.me/+' in link or 't.me/joinchat/' in link:
        out = await join(userbot, link)
        await init.edit(out)
    else:
        await enqueue_and_process(event.from_user.id, link, init)


