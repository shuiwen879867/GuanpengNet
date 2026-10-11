"""Exercise deployed HTTP services and real browser image rendering."""

import argparse
import asyncio
import hashlib
import importlib
import json
import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path


def request_json(url, payload=None):
    body = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        return response.status, json.load(response)


def render(root):
    output = root / "runtime/smoke-images"
    output.mkdir(parents=True, exist_ok=True)
    os.environ["ASTRBOT_ROOT"] = str(root / "runtime/smoke-astrbot")
    (Path(os.environ["ASTRBOT_ROOT"]) / "data").mkdir(parents=True, exist_ok=True)
    sys.path.insert(0, str(root / "AstrBot"))
    sys.path.insert(0, str(root / "shared_data"))
    module = importlib.import_module("data.plugins.guanpeng.main")
    from data.plugins.guanpeng.config_manager import ConfigManager
    from match_db import MatchDatabase

    bot = object.__new__(module.MegaWallsPlugin)
    bot.config = ConfigManager(str(output))
    bot.match_db = MatchDatabase(output / "matches.json")
    bot._get_bg_path = lambda *args: None

    async def matches(*args, **kwargs):
        return [
            {
                "id": "sample",
                "mapName": "Test map",
                "winner": "RED",
                "team": "RED",
                "duration": "10分钟",
                "selectedKit": "僵尸",
                "finalKills": 2,
                "finalAssists": 3,
                "totalDamage": 400,
                "takenDamage": 300,
                "mvp": True,
                "endTime": int(time.time() * 1000),
            }
        ]

    bot.get_match_list_by_name = matches

    class Event:
        def get_sender_id(self):
            return "demo"

        def image_result(self, path):
            return path

        def plain_result(self, text):
            return text

    async def run():
        from PIL import Image

        results = [
            result
            async for result in module.query_megawalls(bot, Event(), "DemoPlayer")
        ]
        assert results, "Query produced no response"
        for result in results:
            path = Path(result)
            assert path.is_file(), f"Image missing: {result}"
            with Image.open(path) as image:
                assert image.width == 400 and image.height > 100
                print(f"Real query image: {image.width}x{image.height}")

    os.chdir(output)
    asyncio.run(run())


def smoke(root, service):
    config = root / "config.json"
    settings = json.loads(config.read_text(encoding="utf-8-sig"))
    services = ["web", "bot"] if service == "all" else [service]
    ports = {"web": settings["web_port"], "bot": settings["bot_dashboard_port"]}
    log = root / "runtime/smoke-startup.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    python = (
        root / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    )
    with log.open("w", encoding="utf-8") as output:
        process = subprocess.Popen(
            [sys.executable, str(root / "deploy.py"), service, "--no-browser"],
            cwd=root,
            stdout=output,
            stderr=subprocess.STDOUT,
            env=dict(os.environ, PYTHONUTF8="1", PYTHONUNBUFFERED="1"),
            start_new_session=os.name != "nt",
        )
        try:
            deadline = time.monotonic() + 150
            pending = set(services)
            while pending and time.monotonic() < deadline:
                if process.poll() is not None:
                    raise RuntimeError(f"Deployment exited with {process.returncode}")
                for item in list(pending):
                    path = "/health" if item == "web" else "/"
                    try:
                        with urllib.request.urlopen(
                            f"http://127.0.0.1:{ports[item]}{path}", timeout=2
                        ) as response:
                            if response.status == 200:
                                pending.remove(item)
                    except (OSError, urllib.error.URLError):
                        pass
                if pending:
                    time.sleep(0.5)
            if pending:
                raise RuntimeError(f"Startup timed out: {pending}")
            if "web" in services:
                status, data = request_json(f"http://127.0.0.1:{ports['web']}/health")
                assert status == 200 and data["status"] == "ok"
                print("Website HTTP health: OK")
            if "bot" in services:
                lines = (
                    (root / "runtime/bot-login.txt")
                    .read_text(encoding="utf-8")
                    .splitlines()
                )
                password = lines[1].split(": ", 1)[1]
                _, data = request_json(
                    f"http://127.0.0.1:{ports['bot']}/api/auth/login",
                    {
                        "username": "astrbot",
                        "password": hashlib.md5(password.encode()).hexdigest(),
                    },
                )
                assert data["status"] == "ok"
                print("Bot dashboard random-password login: OK")
                subprocess.run(
                    [
                        str(python),
                        str(Path(__file__).resolve()),
                        "--package",
                        str(root),
                        "--render",
                    ],
                    cwd=root,
                    check=True,
                    timeout=90,
                )
            print(f"Deployment smoke test passed: {service}")
        except Exception:
            print(log.read_text(encoding="utf-8")[-6000:])
            raise
        finally:
            if process.poll() is None:
                process.send_signal(
                    signal.SIGINT
                ) if os.name != "nt" else process.terminate()
            try:
                process.wait(timeout=20)
            except subprocess.TimeoutExpired:
                if os.name != "nt":
                    os.killpg(process.pid, signal.SIGKILL)
                else:
                    process.kill()
                process.wait()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--package", type=Path, required=True)
    parser.add_argument("--service", choices=("web", "bot", "all"), default="all")
    parser.add_argument("--render", action="store_true")
    options = parser.parse_args()
    package = options.package.resolve()
    render(package) if options.render else smoke(package, options.service)
