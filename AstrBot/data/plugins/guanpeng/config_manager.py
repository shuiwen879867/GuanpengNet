"""
统一配置管理器 - 关鹏插件

将所有分散的配置文件合并为单一 JSON 文件，支持自动迁移旧配置。
"""

import copy
import json
import os
from datetime import datetime
from typing import Any

UNIFIED_CONFIG_FILE = "guanpeng_config.json"

# 旧配置文件列表（用于迁移）
OLD_CONFIG_FILES = {
    "currency": "config_currency.txt",  # format: user_id|last_sign|coins
    "binds": "bind_config.txt",  # format: qq_id|game_id
    "leaderboard": "ld_config.txt",  # JSON: {date: {player_name: count}}
    "sign_backgrounds": "sign_url.txt",  # format: user_id|url
    "query_backgrounds": "query_bg.txt",  # format: user_id|url
}

DEFAULT_CONFIG: dict[str, Any] = {
    "_meta": {
        "version": 1,
        "created_at": "",
        "updated_at": "",
    },
    "currency": {},  # {user_id: [last_sign_date, coins]}
    "binds": {},  # {qq_id: game_id}
    "leaderboard": {},  # {date: {player_name: count}}
    "sign_backgrounds": {},  # {user_id: url}
    "query_backgrounds": {},  # {user_id: url}
}


class ConfigManager:
    """统一配置管理器，支持自动迁移旧配置。

    Args:
        base_dir: 新统一配置文件存放目录（插件目录）
        old_config_dir: 旧配置文件所在目录（AstrBot 根目录），用于迁移
    """

    def __init__(self, base_dir: str = "", old_config_dir: str = ""):
        self._base_dir = base_dir
        self._old_config_dir = old_config_dir or base_dir
        self._config_path = os.path.join(base_dir, UNIFIED_CONFIG_FILE)
        self._data: dict[str, Any] = {}
        self._load()

    # ── 文件读写 ──────────────────────────────────────────

    def _resolve(self, filename: str) -> str:
        return os.path.join(self._base_dir, filename)

    def _resolve_old(self, filename: str) -> str:
        """解析旧配置文件路径（位于 AstrBot 根目录）。"""
        return os.path.join(self._old_config_dir, filename)

    def _load(self) -> None:
        if os.path.exists(self._config_path):
            try:
                with open(self._config_path, encoding="utf-8-sig") as f:
                    self._data = json.load(f)
            except Exception:
                self._data = copy.deepcopy(DEFAULT_CONFIG)
        else:
            # 尝试从旧配置迁移
            self._data = self._migrate_from_old()
            self._touch_meta()
            self._save()

    def _save(self) -> None:
        self._data["_meta"]["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(self._config_path, "w", encoding="utf-8-sig") as f:
            json.dump(self._data, f, ensure_ascii=False, indent=2)

    def _touch_meta(self) -> None:
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        if not self._data["_meta"]["created_at"]:
            self._data["_meta"]["created_at"] = now
        self._data["_meta"]["updated_at"] = now

    # ── 迁移逻辑 ──────────────────────────────────────────

    def _migrate_from_old(self) -> dict[str, Any]:
        """扫描旧配置文件并迁移到新格式。已迁移的文件会被重命名为 .bak。"""
        data = copy.deepcopy(DEFAULT_CONFIG)

        # 1. currency (config_currency.txt)
        cf = self._resolve_old(OLD_CONFIG_FILES["currency"])
        if os.path.exists(cf):
            try:
                with open(cf, encoding="utf-8-sig") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        parts = line.split("|")
                        if len(parts) >= 3:
                            data["currency"][parts[0]] = [parts[1], int(parts[2])]
                self._backup_old(cf)
            except Exception as e:
                print(f"[ConfigManager] 迁移 currency 失败: {e}")

        # 2. binds (bind_config.txt)
        bf = self._resolve_old(OLD_CONFIG_FILES["binds"])
        if os.path.exists(bf):
            try:
                with open(bf, encoding="utf-8-sig") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        parts = line.split("|")
                        if len(parts) >= 2:
                            data["binds"][parts[0]] = parts[1]
                self._backup_old(bf)
            except Exception as e:
                print(f"[ConfigManager] 迁移 binds 失败: {e}")

        # 4. leaderboard (ld_config.txt) - JSON
        lf = self._resolve_old(OLD_CONFIG_FILES["leaderboard"])
        if os.path.exists(lf):
            try:
                with open(lf, encoding="utf-8-sig") as f:
                    data["leaderboard"] = json.load(f)
                self._backup_old(lf)
            except Exception as e:
                print(f"[ConfigManager] 迁移 leaderboard 失败: {e}")

        # 5. sign_backgrounds (sign_url.txt)
        sf = self._resolve_old(OLD_CONFIG_FILES["sign_backgrounds"])
        if os.path.exists(sf):
            try:
                with open(sf, encoding="utf-8-sig") as f:
                    for line in f:
                        line = line.strip()
                        if not line or "|" not in line:
                            continue
                        user_id, url = line.split("|", 1)
                        data["sign_backgrounds"][user_id] = url
                self._backup_old(sf)
            except Exception as e:
                print(f"[ConfigManager] 迁移 sign_backgrounds 失败: {e}")

        # 6. query_backgrounds (query_bg.txt)
        qf = self._resolve_old(OLD_CONFIG_FILES["query_backgrounds"])
        if os.path.exists(qf):
            try:
                with open(qf, encoding="utf-8-sig") as f:
                    for line in f:
                        line = line.strip()
                        if not line or "|" not in line:
                            continue
                        user_id, url = line.split("|", 1)
                        data["query_backgrounds"][user_id] = url
                self._backup_old(qf)
            except Exception as e:
                print(f"[ConfigManager] 迁移 query_backgrounds 失败: {e}")

        return data

    def _backup_old(self, filepath: str) -> None:
        """将旧配置文件重命名为 .bak。"""
        try:
            bak_path = filepath + ".bak"
            if os.path.exists(bak_path):
                os.remove(bak_path)
            os.rename(filepath, bak_path)
            print(f"[ConfigManager] 已备份旧配置: {os.path.basename(filepath)} -> .bak")
        except Exception as e:
            print(f"[ConfigManager] 备份失败 {filepath}: {e}")

    # ── 通用访问接口 ──────────────────────────────────────

    def get(self, section: str, key: str = None, default=None):
        """读取配置节或键值。"""
        if section not in self._data:
            return default
        if key is None:
            return self._data[section]
        return self._data[section].get(key, default)

    def set(self, section: str, key: str, value) -> None:
        """设置键值并自动保存。"""
        if section not in self._data:
            raise KeyError(f"未知配置节: {section}")
        self._data[section][key] = value
        self._save()

    def delete(self, section: str, key: str) -> bool:
        """删除键值并自动保存。"""
        if section in self._data and key in self._data[section]:
            del self._data[section][key]
            self._save()
            return True
        return False

    def set_section(self, section: str, value) -> None:
        """替换整个配置节。"""
        if section not in self._data:
            raise KeyError(f"未知配置节: {section}")
        self._data[section] = value
        self._save()

    # ── 便捷方法 ──────────────────────────────────────────

    # -- currency --
    def get_currency(self, user_id: str) -> tuple[str, int]:
        """返回 (最后签到日期, 关鹏币)"""
        entry = self._data["currency"].get(user_id, None)
        if entry is None:
            return ("从未", 0)
        return (entry[0], entry[1])

    def set_currency(self, user_id: str, last_sign: str, coins: int) -> None:
        self.set("currency", user_id, [last_sign, coins])

    # -- binds --
    def get_bind(self, qq_id: str) -> str | None:
        return self._data["binds"].get(qq_id)

    def set_bind(self, qq_id: str, game_id: str) -> None:
        self.set("binds", qq_id, game_id)

    def remove_bind(self, qq_id: str) -> bool:
        return self.delete("binds", qq_id)

    def get_all_binds(self) -> dict[str, str]:
        return dict(self._data["binds"])

    def get_all_bound_game_ids(self) -> list[str]:
        return list(self._data["binds"].values())

    # -- wives --

    # -- leaderboard --
    def get_leaderboard(self, date: str = None) -> dict[str, int]:
        if date is None:
            date = datetime.now().strftime("%Y-%m-%d")
        return self._data["leaderboard"].get(date, {})

    def set_leaderboard(self, date: str, data: dict[str, int]) -> None:
        self._data["leaderboard"][date] = data
        self._save()

    def get_global_top_players(self, limit: int = 5) -> list[tuple[str, int]]:
        today = datetime.now().strftime("%Y-%m-%d")
        if today not in self._data["leaderboard"]:
            return []
        items = self._data["leaderboard"][today].items()
        return sorted(items, key=lambda x: x[1], reverse=True)[:limit]

    def get_all_monitored_game_ids(self) -> set:
        """获取所有应统计的玩家 ID（绑定玩家 + L列表 + 排行榜中的玩家）。"""
        ids = set(self._data["binds"].values())
        for day_data in self._data["leaderboard"].values():
            ids.update(day_data.keys())
        return ids

    # -- sign_backgrounds --
    def get_sign_bg(self, user_id: str) -> str:
        return self._data["sign_backgrounds"].get(user_id, "")

    def set_sign_bg(self, user_id: str, url: str) -> None:
        self.set("sign_backgrounds", user_id, url)

    # -- query_backgrounds --
    def get_query_bg(self, user_id: str) -> str:
        return self._data["query_backgrounds"].get(str(user_id), "")

    def set_query_bg(self, user_id: str, url: str) -> None:
        self.set("query_backgrounds", user_id, url)

    # -- query_whitelist --

    # -- l_list --

    # -- 获取所有玩家列表（用于老婆抽取等） --
    def get_all_player_names(self) -> list[str]:
        """获取所有已知玩家 ID（来自绑定数据）。"""
        return list(set(self._data["binds"].values()))
