import sys
import logging
import importlib
from pathlib import Path

def load_plugins(plugin_name):
    name = "main.plugins.{}".format(plugin_name)
    if name in sys.modules:
        return
    path = Path(f"main/plugins/{plugin_name}.py")
    spec = importlib.util.spec_from_file_location(name, path)
    load = importlib.util.module_from_spec(spec)
    load.logger = logging.getLogger(plugin_name)
    spec.loader.exec_module(load)
    sys.modules[name] = load
    print("main has Imported " + plugin_name)
