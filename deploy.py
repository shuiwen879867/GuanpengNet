"""Create a virtual environment, install dependencies and supervise services."""

import argparse
import hashlib
import json
import os
import secrets
import shutil
import socket
import subprocess
import sys
import time
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "shared_data"))


def bootstrap(services):
    environment = Path(os.environ.get("GUANPENG_VENV", ROOT / ".venv"))
    python = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if not python.exists():
        try:
            subprocess.run([sys.executable, "-m", "venv", str(environment)], check=True)
        except subprocess.CalledProcessError:
            if not python.exists():
                raise
    pip = [str(python), "-m", "pip"]
    if subprocess.run(
        pip + ["--version"],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    ).returncode:
        pip = [sys.executable, "-m", "pip", "--python", str(python)]
        subprocess.run(pip + ["install", "pip"], check=True)
    requirement = (
        "requirements.bot.txt" if "bot" in services else "requirements.web.txt"
    )
    marker = environment / "guanpeng-install.json"
    digest = hashlib.sha256(
        (ROOT / requirement).read_bytes() + (ROOT / "requirements.web.txt").read_bytes()
    ).hexdigest()
    installed = json.loads(marker.read_text()) if marker.exists() else {}
    if installed.get(requirement) != digest:
        print("正在安装依赖，首次启动需要联网，请稍候。", flush=True)
        subprocess.run(
            pip + ["install", "-r", str(ROOT / requirement)], cwd=ROOT, check=True
        )
        installed[requirement] = digest
        marker.write_text(json.dumps(installed), encoding="utf-8")
    return python


def initialize():
    path = Path(os.environ.get("GUANPENG_CONFIG", ROOT / "config.json"))
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / "config.example.json", path)
    from guanpeng_runtime import load_config

    return load_config()


def prepare_bot(config):
    folder = ROOT / "AstrBot/data"
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / "cmd_config.json"
    if not target.exists():
        password = secrets.token_urlsafe(15)
        target.write_text(
            json.dumps(
                {
                    "dashboard": {
                        "enable": True,
                        "username": "astrbot",
                        "password": hashlib.md5(password.encode()).hexdigest(),
                        "jwt_secret": secrets.token_urlsafe(32),
                        "host": config["bot_dashboard_host"],
                        "port": int(config["bot_dashboard_port"]),
                    },
                    "admins_id": [],
                    "disable_builtin_commands": True,
                    "platform": [],
                    "wake_prefix": ["/"],
                    "platform_settings": {"enable_id_white_list": False},
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        credentials = ROOT / "runtime/bot-login.txt"
        credentials.parent.mkdir(parents=True, exist_ok=True)
        credentials.write_text(
            f"Username: astrbot\nPassword: {password}\n", encoding="utf-8"
        )
        print(f"机器人初次登录密码已保存：{credentials}", flush=True)
    else:
        current = json.loads(target.read_text(encoding="utf-8-sig"))
        current.setdefault("dashboard", {}).update(
            host=config["bot_dashboard_host"], port=int(config["bot_dashboard_port"])
        )
        target.write_text(
            json.dumps(current, ensure_ascii=False, indent=2), encoding="utf-8"
        )


def check_port(host, port):
    with socket.socket() as sock:
        try:
            sock.bind((host, int(port)))
        except OSError as error:
            raise RuntimeError(
                f"端口 {port} 已被占用，请修改 config.json 中的端口或关闭已有程序"
            ) from error


def launch(python, services, config, open_browser):
    children = []
    environment = os.environ.copy()
    environment.update(
        PYTHONUTF8="1",
        PYTHONUNBUFFERED="1",
        GAME_STATUS_URL=config["game_status_url"],
        WEB_HOST=config["web_host"],
        WEB_PORT=str(config["web_port"]),
        TZ="Asia/Shanghai",
    )
    try:
        for service in services:
            if service == "web":
                check_port(config["web_host"], config["web_port"])
                folder = ROOT / "Domcer数据查询项目"
                arguments = [str(python), str(folder / "app.py")]
                url = f"http://127.0.0.1:{config['web_port']}"
            else:
                check_port(config["bot_dashboard_host"], config["bot_dashboard_port"])
                prepare_bot(config)
                folder = ROOT / "AstrBot"
                arguments = [
                    str(python),
                    str(folder / "main.py"),
                    "--webui-dir",
                    str(folder / "data/dist"),
                ]
                url = f"http://127.0.0.1:{config['bot_dashboard_port']}"
            print(f"启动 {service}：{url}", flush=True)
            child_env = dict(environment)
            if service == "bot":
                child_env["ASTRBOT_ROOT"] = str(folder)
            children.append(subprocess.Popen(arguments, cwd=folder, env=child_env))
            if open_browser:
                webbrowser.open(url)
        print("按 Ctrl+C 停止所有服务。", flush=True)
        while all(child.poll() is None for child in children):
            time.sleep(0.5)
        codes = [child.returncode for child in children if child.returncode is not None]
        if any(code != 0 for code in codes):
            raise RuntimeError("服务启动失败，请查看上方报错。")
    except KeyboardInterrupt:
        print("正在停止服务…", flush=True)
    finally:
        for child in children:
            if child.poll() is None:
                child.terminate()
        for child in children:
            try:
                child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                child.kill()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "service", choices=["web", "bot", "all"], nargs="?", default="all"
    )
    parser.add_argument("--setup-only", action="store_true")
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()
    if sys.version_info < (3, 10):  # noqa: UP036
        raise RuntimeError("请安装 Python 3.10 或更新版本")
    available = (
        json.loads((ROOT / "package.json").read_text())["services"]
        if (ROOT / "package.json").exists()
        else ["web", "bot"]
    )
    services = available if args.service == "all" else [args.service]
    if any(service not in available for service in services):
        raise RuntimeError("当前独立包未包含此服务，请下载对应包或合并包")
    config = initialize()
    python = bootstrap(services)
    if not args.setup_only:
        launch(python, services, config, not args.no_browser)


if __name__ == "__main__":
    try:
        main()
    except Exception as error:  # noqa: BLE001
        print(f"部署失败：{error}", file=sys.stderr)
        sys.exit(1)
