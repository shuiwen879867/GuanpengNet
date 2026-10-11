"""Export source-only distributions from explicit paths, never runtime snapshots."""

import json
import shutil
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "release"
SKIP = {"__pycache__", "node_modules", ".git", ".venv", "venv"}
FILES = set()


def copy_tree(source, destination):
    for path in source.rglob("*"):
        relative = path.relative_to(source)
        if (
            path.is_file()
            and not any(part in SKIP for part in relative.parts)
            and path.suffix != ".pyc"
        ):
            target = destination / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            if (
                not target.exists()
                or target.stat().st_mtime_ns != path.stat().st_mtime_ns
                or target.stat().st_size != path.stat().st_size
            ):
                shutil.copy2(path, target)
            FILES.add(target)


def copy(source, package, destination=None):
    target = package / (destination or source)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(ROOT / source, target)
    FILES.add(target)


def build(name, services, platform):
    package = OUTPUT / name
    FILES.clear()
    package.mkdir(parents=True, exist_ok=True)
    for source in (
        "LICENSE",
        "README.opensource.md",
        "config.example.json",
        "deploy.py",
        ".gitignore",
    ):
        copy(source, package, "README.md" if source == "README.opensource.md" else None)
    (package / "package.json").write_text(
        json.dumps({"services": services, "platform": platform}), encoding="utf-8"
    )
    FILES.add(package / "package.json")
    for source in (
        "guanpeng_runtime.py",
        "match_db.py",
        "class_stats_cache.py",
        "commands_guanpeng.json",
    ):
        copy(f"shared_data/{source}", package)
    for script in (
        "requirements.web.txt",
        "requirements.bot.txt",
    ):
        copy(script, package)
    suffix = "bat" if platform == "Windows" else "sh"
    for service in ("web", "bot", "all"):
        copy(f"start-{service}.{suffix}", package)
    if platform == "Linux":
        copy("start.sh", package)
        copy("install-linux.sh", package)
    copy("scripts/test_release.py", package)
    copy("scripts/smoke_deployment.py", package)
    if "web" in services:
        for source in (
            "app.py",
            "class_players.py",
            "operator_cache.py",
            "local_proxy.py",
            "test_class_players.py",
            "test_local_proxy.py",
        ):
            copy(f"Domcer数据查询项目/{source}", package)
        for source in (
            "index.html",
            "class_stats.html",
            "class_players.html",
            "kit_stats.html",
            "match.html",
            "wiki.html",
        ):
            copy(f"Domcer数据查询项目/templates/{source}", package)
        copy_tree(
            ROOT / "Domcer数据查询项目/static/kit_img",
            package / "Domcer数据查询项目/static/kit_img",
        )
    if "bot" in services:
        for source in (
            "main.py",
            "pyproject.toml",
            "LICENSE",
            "README.md",
            "AGENTS.md",
        ):
            copy(f"AstrBot/{source}", package)
        for directory in ("astrbot", "dashboard", "typings"):
            copy_tree(ROOT / "AstrBot" / directory, package / "AstrBot" / directory)
        copy_tree(ROOT / "AstrBot/data/dist", package / "AstrBot/data/dist")
        for plugin in ("guanpeng",):
            copy(f"AstrBot/data/plugins/{plugin}/main.py", package)
        copy("AstrBot/data/plugins/guanpeng/config_manager.py", package)
        copy_tree(
            ROOT / "AstrBot/data/plugins/guanpeng/kit_img",
            package / "AstrBot/data/plugins/guanpeng/kit_img",
        )
    requirement = (
        "requirements.bot.txt" if "bot" in services else "requirements.web.txt"
    )
    (package / "requirements.txt").write_text(f"-r {requirement}\n", encoding="utf-8")
    FILES.add(package / "requirements.txt")
    with zipfile.ZipFile(
        str(package) + ".zip", "w", compression=zipfile.ZIP_DEFLATED
    ) as archive:
        for filename in sorted(FILES):
            if filename.suffix == ".sh":
                info = zipfile.ZipInfo(
                    str(filename.relative_to(OUTPUT)).replace("\\", "/")
                )
                info.create_system = 3
                info.external_attr = 0o100755 << 16
                info.compress_type = zipfile.ZIP_DEFLATED
                archive.writestr(info, filename.read_bytes())
            else:
                archive.write(filename, str(filename.relative_to(OUTPUT)))
    return package


if __name__ == "__main__":
    OUTPUT.mkdir(exist_ok=True)
    for platform in ("Windows", "Linux"):
        for name, services in (
            ("GuanpengNet", ["web"]),
            ("GuanpengBot", ["bot"]),
            ("GuanpengSuite", ["web", "bot"]),
        ):
            print(build(f"{name}-{platform}", services, platform))
