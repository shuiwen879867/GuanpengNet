"""Shared configuration; neither application needs the other application installed."""

import json
import os
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = Path(os.environ.get("GUANPENG_CONFIG", ROOT / "config.json"))
DEFAULTS = {
    "linkage_enabled": True,
    "shared_data_dir": "runtime/shared",
    "web_data_dir": "runtime/web",
    "bot_data_dir": "runtime/bot",
    "domcer_api_base": "https://api.domcer.com",
    "domcer_api_keys": [],
    "web_host": "127.0.0.1",
    "web_port": 5000,
    "bot_dashboard_host": "127.0.0.1",
    "bot_dashboard_port": 6185,
    "game_status_url": "",
    "browser_executable": "",
    "commands": {},
}


def load_config():
    result = dict(DEFAULTS)
    if CONFIG_PATH.exists():
        with CONFIG_PATH.open(encoding="utf-8-sig") as handle:
            value = json.load(handle)
        if not isinstance(value, dict):
            raise TypeError("config.json must contain an object")
        result.update(value)
    if not isinstance(result["linkage_enabled"], bool):
        raise TypeError("linkage_enabled must be true or false")
    keys = result["domcer_api_keys"]
    if not isinstance(keys, list) or any(not isinstance(key, str) for key in keys):
        raise ValueError("domcer_api_keys must be a list of strings")
    return result


CONFIG = load_config()
API_BASE = os.environ.get("DOMCER_API_BASE", CONFIG["domcer_api_base"]).rstrip("/")
API_KEYS = [
    key.strip()
    for key in os.environ.get("DOMCER_API_KEYS", "").split(",")
    if key.strip()
]
if not API_KEYS:
    API_KEYS = [key.strip() for key in CONFIG["domcer_api_keys"] if key.strip()]


def data_dir(service):
    if service not in {"web", "bot"}:
        raise ValueError("Unknown service")
    key = "shared_data_dir" if CONFIG["linkage_enabled"] else f"{service}_data_dir"
    path = Path(CONFIG[key])
    if not path.is_absolute():
        path = CONFIG_PATH.parent / path
    path.mkdir(parents=True, exist_ok=True)
    return path.resolve()


def command_name(handler):
    commands = {}
    for filename in ("commands_guanpeng.json",):
        with Path(__file__).with_name(filename).open(encoding="utf-8") as handle:
            commands.update(json.load(handle))
    commands.update(CONFIG["commands"])
    name = commands[handler]
    if not isinstance(name, str) or not name.strip() or any(c.isspace() for c in name):
        raise ValueError(f"Invalid command name for {handler}")
    if len(set(commands.values())) != len(commands):
        raise ValueError("Command names must be unique")
    return name


def html_renderer(**kwargs):
    from html2image import Html2Image

    executable = CONFIG.get("browser_executable") or os.environ.get("GUANPENG_BROWSER")
    if not executable:
        candidates = [
            shutil.which(name)
            for name in ("google-chrome", "chromium", "chromium-browser", "msedge")
        ]
        if os.name == "nt":
            for base in (
                os.environ.get("PROGRAMFILES", ""),
                os.environ.get("PROGRAMFILES(X86)", ""),
                os.environ.get("LOCALAPPDATA", ""),
            ):
                for suffix in (
                    "Google/Chrome/Application/chrome.exe",
                    "Microsoft/Edge/Application/msedge.exe",
                ):
                    candidates.append(str(Path(base) / suffix))
        executable = next(
            (item for item in candidates if item and Path(item).is_file()), None
        )
    if executable:
        kwargs["browser_executable"] = executable
    if os.name != "nt" and hasattr(os, "geteuid") and os.geteuid() == 0:
        kwargs["custom_flags"] = [*kwargs.get("custom_flags", []), "--no-sandbox"]
    return Html2Image(**kwargs)
