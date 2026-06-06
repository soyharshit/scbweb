import asyncio
import glob
from pathlib import Path
from main.utils import load_plugins
import logging
from . import bot, Bot, LOGS

logging.basicConfig(format='[%(levelname) 5s/%(asctime)s] %(name)s: %(message)s',
                    level=logging.WARNING)

path = "main/plugins/*.py"
files = glob.glob(path)
for name in files:
    with open(name) as a:
        patt = Path(a.name)
        plugin_name = patt.stem
        load_plugins(plugin_name.replace(".py", ""))

CLEANUP_INTERVAL_SECONDS = 12 * 3600


async def preload_entities():
    from main.plugins.dbstuff import db
    await db._ensure_indexes()
    if LOGS:
        logging.warning("LOGS channel configured.")


async def start_pyrogram():
    try:
        await Bot.start()
        logging.warning("Pyrogram Bot started successfully.")
    except Exception as e:
        logging.error("Failed to start Pyrogram Bot: %s", e)


async def scheduled_cleanup():
    from main.plugins.helpers import cleanup_temp_files
    while True:
        try:
            await asyncio.sleep(CLEANUP_INTERVAL_SECONDS)
            await cleanup_temp_files()
        except asyncio.CancelledError:
            break
        except Exception as e:
            logging.error(f"Scheduled cleanup error: {e}")


print("Successfully deployed!")

if __name__ == "__main__":
    bot.loop.run_until_complete(preload_entities())
    bot.loop.run_until_complete(start_pyrogram())
    try:
        from main.plugins.helpers import cleanup_temp_files
        bot.loop.run_until_complete(cleanup_temp_files())
    except Exception as e:
        logging.error(f"Startup cleanup error: {e}")
    bot.loop.create_task(scheduled_cleanup())
    bot.run_until_disconnected()
