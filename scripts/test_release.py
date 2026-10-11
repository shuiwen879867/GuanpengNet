"""Run deployment regression tests without user credentials or production data."""

import asyncio
import importlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
TEST_ROOT = (ROOT / "runtime/tests").resolve()
TEST_ROOT.mkdir(parents=True, exist_ok=True)
tempfile.tempdir = str(TEST_ROOT)
TEMP = tempfile.TemporaryDirectory()
ISOLATED = Path(TEMP.name)
CONFIG = ISOLATED / "config.json"
CONFIG.write_text(json.dumps({"linkage_enabled": True}), encoding="utf-8")
os.environ.update(
    GUANPENG_CONFIG=str(CONFIG),
    GUANPENG_BACKGROUND="0",
    ASTRBOT_ROOT=str(ISOLATED / "astrbot"),
)
sys.path.insert(0, str(ROOT / "shared_data"))
from guanpeng_runtime import CONFIG as SETTINGS
from guanpeng_runtime import command_name, data_dir
from match_db import MatchDatabase


class SharedTests(unittest.TestCase):
    def test_linked_and_independent_paths(self):
        SETTINGS["linkage_enabled"] = True
        self.assertEqual(data_dir("web"), data_dir("bot"))
        SETTINGS["linkage_enabled"] = False
        self.assertNotEqual(data_dir("web"), data_dir("bot"))
        SETTINGS["linkage_enabled"] = True

    def test_command_override(self):
        SETTINGS["commands"] = {"query_megawalls": "战绩"}
        self.assertEqual(command_name("query_megawalls"), "战绩")
        SETTINGS["commands"] = {}

    def test_concurrent_writers_do_not_drop_pending(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "matches.json"
            stores = [MatchDatabase(path), MatchDatabase(path)]
            with ThreadPoolExecutor(max_workers=2) as executor:
                list(
                    executor.map(
                        lambda index: stores[index % 2].add_pending([str(index)]),
                        range(100),
                    )
                )
            self.assertEqual(stores[0].pending_count, 100)
            stores[0].add_pending(["0", "0"])
            self.assertEqual(stores[0].pending_count, 100)

    def test_real_process_writers(self):
        path = ISOLATED / "multiprocess.json"
        code = "import sys;sys.path.insert(0,sys.argv[1]);from match_db import MatchDatabase;db=MatchDatabase(sys.argv[2]);[db.add_pending([sys.argv[3]+str(i)]) for i in range(30)]"
        processes = [
            subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    code,
                    str(ROOT / "shared_data"),
                    str(path),
                    prefix,
                ]
            )
            for prefix in ("a", "b")
        ]
        self.assertEqual([process.wait(timeout=30) for process in processes], [0, 0])
        self.assertEqual(MatchDatabase(path).pending_count, 60)

    def test_failed_fetch_retains_pending(self):
        import match_db

        path = ISOLATED / "retry.json"
        db = MatchDatabase(path)
        db.add_pending(["retry"])

        async def failed(*args):
            return None

        with (
            patch.object(match_db, "API_KEYS", ["test"]),
            patch.object(db, "_fetch_match", failed),
        ):
            asyncio.run(db.process_pending(None))
        self.assertEqual(db.pending_count, 1)


@unittest.skipUnless(
    (ROOT / "Domcer数据查询项目/app.py").exists(), "Standalone bot package"
)
class WebTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, str(ROOT / "Domcer数据查询项目"))
        cls.module = importlib.import_module("app")
        cls.client = cls.module.app.test_client()

    def test_public_pages(self):
        for route in ("/", "/health", "/class_stats", "/wiki.html", "/api/class_stats"):
            self.assertEqual(self.client.get(route).status_code, 200, route)

    def test_removed_admin_routes(self):
        for route in ("/api/class_stats/rebuild", "/ads", "/login"):
            self.assertEqual(self.client.get(route).status_code, 404)

    def test_missing_key_returns_actionable_error(self):
        self.assertEqual(
            self.client.get("/api/player/matches?username=example").status_code, 503
        )

    def test_players_use_same_query_path(self):
        with (
            patch.object(self.module.mw_stats, "api_key", ["test"]),
            patch.object(
                self.module.mw_stats, "get_match_list_by_name", return_value=[]
            ) as query,
        ):
            response = self.client.get("/api/player/matches?username=弓箭神")
            self.assertEqual(response.status_code, 200)
            self.assertNotIn("special_player", response.json)
            query.assert_called_once()

    def test_external_api_integration_with_mock(self):
        from unittest.mock import Mock

        response = Mock()
        response.json.return_value = {
            "status": 200,
            "data": [{"id": "sample", "startTime": 1000, "endTime": 61000}],
        }
        with (
            patch.object(self.module.mw_stats, "api_key", ["test"]),
            patch.object(self.module.requests, "get", return_value=response),
        ):
            result = self.client.get("/api/player/matches?username=example")
            self.assertEqual(result.json["matches"][0]["duration"], "1.0分钟")


@unittest.skipUnless((ROOT / "AstrBot/main.py").exists(), "Standalone web package")
class BotTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, str(ROOT / "AstrBot"))
        (ISOLATED / "astrbot/data").mkdir(parents=True, exist_ok=True)
        cls.plugin = importlib.import_module("data.plugins.guanpeng.main")

    def test_plugins_register(self):
        from astrbot.core.star.star_handler import star_handlers_registry

        handlers = star_handlers_registry.get_handlers_by_module_name(
            self.plugin.__name__
        )
        self.assertGreaterEqual(len(handlers), 17)

    def test_only_public_commands(self):
        import ast

        for module in (self.plugin,):
            source = Path(module.__file__).read_text(encoding="utf-8")
            self.assertNotIn("ADMIN_IDS", source)
            for node in ast.walk(ast.parse(source)):
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "command"
                ):
                    self.assertIsInstance(node.args[0], ast.Call)

    def test_config_has_no_private_sections(self):
        from data.plugins.guanpeng.config_manager import ConfigManager

        with tempfile.TemporaryDirectory() as folder:
            manager = ConfigManager(folder)
            self.assertEqual(manager.get_all_binds(), {})
            self.assertNotIn("l_list", manager._data)
            self.assertNotIn("query_whitelist", manager._data)


if __name__ == "__main__":
    try:
        unittest.main(verbosity=2)
    finally:
        TEMP.cleanup()
