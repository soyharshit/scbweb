import glob
from pathlib import Path
from main.utils import load_plugins
import logging
from . import bot, LOGS

logging.basicConfig(format='[%(levelname) 5s/%(asctime)s] %(name)s: %(message)s',
                    level=logging.WARNING)

path = "main/plugins/*.py"
files = glob.glob(path)
for name in files:
    with open(name) as a:
        patt = Path(a.name)
        plugin_name = patt.stem
        load_plugins(plugin_name.replace(".py", ""))

async def preload_entities():
    if LOGS:
        logging.warning("LOGS channel configured.")

print("Successfully deployed!")

if __name__ == "__main__":
    bot.loop.run_until_complete(preload_entities())
    bot.run_until_disconnected()
