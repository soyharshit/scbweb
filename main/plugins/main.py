import os
import time
import asyncio
import cv2
import logging
from pyrogram import Client, filters
from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton
from pyrogram.errors import FloodWait

from .. import bot, API_ID, API_HASH, FORCESUB, Bot
from ..plugins.helpers import (
    get_link, join, _clients, split_file, split_media_file, is_ffmpeg_available
)
from ..plugins.display_progress import progress_for_pyrogram
from main.plugins.dbstuff import db, stats_db

logging.basicConfig(format='[%(levelname) 5s/%(asctime)s] %(name)s: %(message)s', level=logging.WARNING)

COOLDOWN_SECONDS = 300
MAX_FILE_SIZE = 4 * 1024 * 1024 * 1024
SPLIT_THRESHOLD = 2 * 1024 * 1024 * 1024
SPLIT_CHUNK_SIZE = 1900 * 1024 * 1024
MAX_SPLIT_PARTS = 2

user_cooldowns = {}



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
            parts = msg_link.split('/')
            if len(parts) >= 6 and not parts[-2].startswith('c') and not parts[-2].startswith('b'):
                chat_ids.append(parts[-3])
                chat_ids.append(parts[-2])
            else:
                chat_ids.append(parts[-2])
            try:
                msg_id = int(parts[-1].split('?')[0])
            except ValueError:
                await edit.edit('Invalid message link.')
                return
        else:
            await edit.edit('Unsupported link format.')
            return
    except (ValueError, IndexError):
        await edit.edit('Invalid message link.')
        return

    if 't.me/c/' in msg_link or 't.me/b/' in msg_link:
        try:
            user_chat_ids = set()
            async for dialog in userbot.get_dialogs(limit=500):
                cid = getattr(dialog.chat, 'id', None)
                if cid is not None:
                    user_chat_ids.add(cid)
                    user_chat_ids.add(abs(cid))

            int_chat_ids = [cid for cid in chat_ids if isinstance(cid, int)]
            if int_chat_ids and not any(
                cid in user_chat_ids or abs(cid) in user_chat_ids
                for cid in int_chat_ids
            ):
                await edit.edit(
                    "You haven't joined this chat or you don't have access to it.\n"
                    "Please join the chat first, then try again."
                )
                return
        except Exception:
            pass

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
        await edit.edit("Could not fetch the message. Please check the link and make sure you have access to it.")
        return

    if getattr(msg, 'empty', False):
        logging.warning(f"Empty message returned for link: {msg_link} (msg_id={msg_id})")
        await edit.edit(
            "This message is empty or has been deleted.\n"
            "The link may be invalid, or your user session may lack access to this chat."
        )
        return

    if msg.service:
        await edit.edit('This is a service message with no saveable content.')
        return

    if not msg.media and not msg.text and not msg.caption and not msg.sticker and not msg.poll:
        logging.warning(
            f"No saveable content for link: {msg_link} (msg_id={msg_id}), "
            f"type={type(msg).__name__}, media={getattr(msg, 'media', None)}, "
            f"has_text={bool(msg.text)}, has_caption={bool(msg.caption)}"
        )
        await edit.edit(
            "Message has no saveable content.\n"
            "Supported: text, photos, videos, documents, audio, voice, stickers, animations, polls."
        )
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

    size_bytes = os.path.getsize(file)
    size_mib = size_bytes / (1024 * 1024)
    size_gib = size_mib / 1024

    if size_bytes > MAX_FILE_SIZE:
        await edit.edit(
            f"File too large ({size_gib:.2f} GB). Telegram's absolute maximum is 4 GB."
        )
        if os.path.isfile(file):
            os.remove(file)
        return

    is_premium = bool(getattr(userbot, "_is_premium", False))

    if size_bytes > SPLIT_THRESHOLD and not is_premium:
        is_media_file = bool(
            msg.video or msg.video_note or msg.animation or msg.audio or msg.voice
        )
        mime = ""
        if msg.document:
            mime = (getattr(msg.document, "mime_type", "") or "").lower()
            if mime.startswith("video/") or mime.startswith("audio/"):
                is_media_file = True

        is_binary_splittable = bool(msg.document) and not (
            mime.startswith("video/") or mime.startswith("audio/")
        )

        if not (is_media_file or is_binary_splittable):
            await edit.edit(
                f"File is {size_gib:.2f} GB, which exceeds the 2 GB limit for non-Premium accounts.\n"
                f"Photos cannot be split safely.\n"
                f"Upgrade to Telegram Premium to upload files up to 4 GB."
            )
            if os.path.isfile(file):
                os.remove(file)
            return

        await edit.edit('Splitting file...')
        try:
            if is_media_file:
                if not await is_ffmpeg_available():
                    await edit.edit(
                        "ffmpeg is not available on the server, so this media file cannot be split.\n"
                        "Upgrade to Telegram Premium to upload files up to 4 GB without splitting."
                    )
                    if os.path.isfile(file):
                        os.remove(file)
                    return
                parts = await split_media_file(file, MAX_SPLIT_PARTS)
                split_method = "ffmpeg"
            else:
                parts = await split_file(file, SPLIT_CHUNK_SIZE)
                split_method = "binary"
        except Exception as e:
            await edit.edit(f'Split failed: {e}')
            if os.path.isfile(file):
                os.remove(file)
            return

        if len(parts) > MAX_SPLIT_PARTS:
            await edit.edit(
                f"File would split into {len(parts)} parts, exceeding the maximum of {MAX_SPLIT_PARTS}.\n"
                f"Maximum splittable size is ~{(SPLIT_CHUNK_SIZE * MAX_SPLIT_PARTS) / (1024**3):.1f} GB.\n"
                f"Upgrade to Telegram Premium to upload files up to 4 GB."
            )
            for p in parts:
                try:
                    os.remove(p)
                except OSError:
                    pass
            if os.path.isfile(file):
                os.remove(file)
            return

        oversized = [p for p in parts if os.path.getsize(p) > SPLIT_THRESHOLD]
        if oversized:
            await edit.edit(
                f"One or more split parts exceed 2 GB (bitrate too high for safe split).\n"
                f"Upgrade to Telegram Premium to upload files up to 4 GB without splitting."
            )
            for p in parts:
                try:
                    os.remove(p)
                except OSError:
                    pass
            if os.path.isfile(file):
                os.remove(file)
            return

        original_name = os.path.basename(file)
        total = len(parts)
        first_saved_id = None

        if split_method == "binary":
            rejoin_text = (
                f"\n\nTo rejoin:\n"
                f"Linux/Mac: cat '{original_name}.part.*' > '{original_name}'\n"
                f"Windows: copy /b '{original_name}.part.*' '{original_name}'"
            )
        else:
            rejoin_text = ""

        upload_ok = False
        try:
            for idx, part in enumerate(parts, 1):
                try:
                    await edit.edit(f'Uploading part {idx}/{total}...')
                    sent = await userbot.send_document(
                        "me", part,
                        caption=(
                            f"Part {idx}/{total} of {original_name}"
                            f"{rejoin_text}"
                        ),
                        progress=progress_for_pyrogram,
                        progress_args=(userbot, f'Uploading part {idx}/{total}...', edit, time.time())
                    )
                    if idx == 1:
                        first_saved_id = sent.id
                except FloodWait as fw:
                    await asyncio.sleep(fw.value)
                    await edit.edit(
                        f'Rate limited during part {idx}/{total}. Some parts may already be uploaded.'
                    )
                    return
                except Exception as e:
                    await edit.edit(f'Failed at part {idx}/{total}: {e}')
                    return
            upload_ok = True
        finally:
            for p in parts:
                try:
                    os.remove(p)
                except OSError:
                    pass
            if os.path.isfile(file):
                os.remove(file)

        if not upload_ok:
            return

        if split_method == "binary":
            final_text = f'Saved as {total} parts to your Saved Messages.\nRejoin to use.'
        else:
            final_text = f'Saved as {total} parts to your Saved Messages.\nEach part is independently playable.'

        await edit.edit(
            final_text,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton('View Part 1', url=f'tg://openmessage?user_id={user_id}&message_id={first_saved_id}')
            ]])
        )
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
        err = str(e)
        if "2000" in err and ("MiB" in err or "MB" in err or "megab" in err.lower()):
            await edit.edit(
                f"Upload failed: file exceeds the 2 GB limit for non-Premium accounts.\n"
                f"Upgrade to Telegram Premium to upload files up to 4 GB."
            )
        else:
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

    uid = event.from_user.id

    now = time.time()
    cooldown_end = user_cooldowns.get(uid, 0)
    if cooldown_end > now:
        remaining_secs = int(cooldown_end - now)
        mins, secs = divmod(remaining_secs, 60)
        return await event.reply(f'Please wait {mins}m {secs}s before you can forward next message\n\nContact @BotsCraftAdmin to buy premium to remove this waiting time')

    user_cooldowns[uid] = now + COOLDOWN_SECONDS

    init = await event.reply('Processing...')

    if not await is_user_subscribed(uid):
        user_cooldowns.pop(uid, None)
        join_url = f"https://t.me/{FORCESUB}"
        return await init.edit(
            'Please join the channel first to use this bot.',
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton('Join Channel', url=join_url)]
            ])
        )

    creds = await db.get_credentials(uid)
    if not creds or not all(creds):
        user_cooldowns.pop(uid, None)
        return await init.edit('Please log in first using /login.')

    userbot = _clients.get(f"ub_{uid}")
    if userbot:
        try:
            await userbot.get_me()
        except Exception:
            userbot = None
            old = _clients.pop(f"ub_{uid}", None)
            if old:
                try:
                    await old.disconnect()
                except Exception:
                    pass
    if not userbot:
        userbot = Client(
            name=f"user_{uid}",
            api_id=creds[0],
            api_hash=creds[1],
            session_string=creds[2],
            in_memory=True
        )
        try:
            await userbot.start()
            me = await userbot.get_me()
            userbot._is_premium = bool(getattr(me, "is_premium", False))
            _clients[f"ub_{uid}"] = userbot
        except Exception as e:
            _clients.pop(f"ub_{uid}", None)
            user_cooldowns.pop(uid, None)
            return await init.edit(f'User session error: {e}')

    if 't.me/+' in link or 't.me/joinchat/' in link:
        out = await join(userbot, link)
        await init.edit(out)
    else:
        try:
            await get_msg(userbot, uid, link, init)
            await stats_db.increment_forwards()
        except FloodWait as fw:
            await asyncio.sleep(fw.value)
            await init.edit(f'Rate limited. Retry after {fw.value}s.')
        except Exception as e:
            user_cooldowns.pop(uid, None)
            await init.edit(f'Error: {e}')


