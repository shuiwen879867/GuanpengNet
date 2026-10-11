import asyncio
import html
import os
import random
import sys
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import quote

import aiohttp
import requests
from jinja2 import Template
from mcstatus import JavaServer
from PIL import Image

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageEventResult, filter
from astrbot.api.event.filter import EventMessageType, event_message_type
from astrbot.api.star import Context, Star, register

from .config_manager import ConfigManager

# 导入共享对局数据库（位于项目根目录 shared_data/）
_plugin_dir = os.path.dirname(os.path.abspath(__file__))
_shared_dir = os.path.abspath(
    os.path.join(_plugin_dir, "..", "..", "..", "..", "shared_data")
)
if _shared_dir not in sys.path:
    sys.path.insert(0, _shared_dir)

# Locate the shared deployment configuration from either bundled plugin.

_runtime_root = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(_runtime_root / "shared_data"))
from class_stats_cache import query_cache as query_class_stats  # noqa: E402
from guanpeng_runtime import API_BASE, API_KEYS, command_name, data_dir  # noqa: E402
from guanpeng_runtime import html_renderer as Html2Image  # noqa: E402
from match_db import get_shared_db  # noqa: E402

# 职业统计缓存路径
_shared_dir = str(data_dir("bot"))
CLASS_STATS_CACHE = os.path.join(_shared_dir, "class_stats_cache.json")

# ── 常量 ──────────────────────────────────────────────────

TEAM_COLORS = {
    "GREEN": {"name": "绿队", "color": "#4CAF50", "text_color": "#fff"},
    "RED": {"name": "红队", "color": "#F44336", "text_color": "#fff"},
    "BLUE": {"name": "蓝队", "color": "#2196F3", "text_color": "#fff"},
    "YELLOW": {"name": "黄队", "color": "#FFEB3B", "text_color": "#000"},
    "UNKNOWN": {"name": "未知", "color": "#9E9E9E", "text_color": "#fff"},
    "NONE": {"name": "无", "color": "#9E9E9E", "text_color": "#fff"},
}

KIT_TRANSLATIONS = {
    "Spider": "蜘蛛",
    "Arcanist": "奥术师",
    "Golem": "傀儡",
    "Shark": "鲨鱼",
    "Shaman": "萨满",
    "HIM": "HIM",
    "Squid": "鱿鱼",
}


def _extract_image_url(chain) -> str | None:
    """从消息链中提取图片 URL。支持多种组件格式。"""
    for i, comp in enumerate(chain):
        comp_type = type(comp).__name__
        logger.info(
            f"[背景设置] chain[{i}] type={comp_type}, hasattr={[a for a in dir(comp) if not a.startswith('_')]}"
        )

        url = None
        # 1) 标准 Image 组件
        if hasattr(comp, "url") and comp.url:
            url = comp.url
        elif hasattr(comp, "file") and comp.file:
            url = comp.file
        # 2) dict 格式
        elif isinstance(comp, dict):
            url = comp.get("url") or comp.get("file") or comp.get("data")
        # 3) QQ 官方 webhook 可能把图片包在嵌套结构里
        if not url and hasattr(comp, "data"):
            d = getattr(comp, "data", None)
            if isinstance(d, dict):
                url = d.get("url") or d.get("file") or d.get("path")
            elif isinstance(d, str) and d:
                url = d
        # 4) 更激进：遍历所有字符串属性
        if not url:
            for attr in (
                "path",
                "src",
                "image",
                "img",
                "raw",
                "content",
                "text",
                "summary",
            ):
                v = getattr(comp, attr, None)
                if isinstance(v, str) and v:
                    url = v
                    break

        if url:
            logger.info(f"[背景设置] 提取到图片 URL: {url[:120]}...")
            return url

    logger.warning(f"[背景设置] 未能从消息链中提取图片 URL，chain 长度: {len(chain)}")
    return None


# ── 主插件类 ──────────────────────────────────────────────


@register("megawalls", "YourName", "Mega Walls战绩查询插件", "1.0.0")
class MegaWallsPlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        self.api_base = API_BASE
        self.api_key = list(API_KEYS)
        self.session: aiohttp.ClientSession | None = None

        # 统一配置管理器（自动从旧配置迁移，位于插件目录下）
        plugin_dir = os.path.dirname(os.path.abspath(__file__))
        # 旧配置文件在 AstrBot 根目录（向上两级：data/plugins/guanpeng -> ../../)
        old_config_dir = os.path.abspath(os.path.join(plugin_dir, "..", ".."))
        self.config = ConfigManager(base_dir=plugin_dir, old_config_dir=old_config_dir)

        # 共享对局数据库
        self.match_db = get_shared_db("bot")

        # 背景图片目录
        self.bg_dir = os.path.join(plugin_dir, "bg_images")
        os.makedirs(self.bg_dir, exist_ok=True)

        # 背景上传等待队列：{user_id: (asyncio.Event, bg_type)}
        self._bg_waiting: dict[str, tuple[asyncio.Event, str]] = {}
        # 每个用户独立的图片 URL 结果（避免多用户竞态）
        self._bg_url_results: dict[str, str] = {}

    # ── 生命周期 ──────────────────────────────────────────

    async def initialize(self):
        # aiohttp 仅用于背景图片下载；MW 查询使用网页端同款 requests 请求。
        self.session = aiohttp.ClientSession()
        self._cache_task = asyncio.create_task(self._refresh_cache_loop())

    async def terminate(self):
        task = getattr(self, "_cache_task", None)
        if task:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        if self.session:
            await self.session.close()

    async def _refresh_cache_loop(self):
        from class_stats_cache import build_cache

        while True:
            try:
                await self.match_db.process_pending(self.session)
                await asyncio.to_thread(
                    build_cache, self.match_db.db_path, CLASS_STATS_CACHE
                )
            except Exception:
                logger.exception("Statistics refresh failed")
            await asyncio.sleep(3600)

    # ── 消息处理 ──────────────────────────────────────────

    @event_message_type(EventMessageType.ALL)
    async def on_message(self, event: AstrMessageEvent) -> MessageEventResult:
        try:
            sender_id = event.get_sender_id()

            # 检查是否在等待背景图片上传
            if sender_id in self._bg_waiting:
                chain = event.get_messages()
                url = _extract_image_url(chain)
                if url:
                    evt, bg_type = self._bg_waiting.pop(sender_id)
                    self._bg_url_results[sender_id] = url
                    evt.set()
                    logger.info(
                        f"[背景设置] 用户 {sender_id} 的 {bg_type} 背景图片已接收，唤醒等待协程"
                    )
                    return None
                else:
                    logger.debug(
                        f"[背景设置] 用户 {sender_id} 在等待背景，但当前消息不含图片"
                    )

        except Exception as e:
            logger.error(f"处理消息时出错: {e}")
        return None

    # ── 发送消息辅助 ──────────────────────────────────────

    # ── API 交互 ──────────────────────────────────────────

    async def _request_domcer(
        self,
        endpoint: str,
        params: dict,
        query_label: str,
        allow_unwrapped_data: bool = False,
    ) -> dict | None:
        """用网页端同样的 requests 请求 Domcer；每次只发起一次请求。"""
        if not self.api_key:
            logger.error("Domcer 查询失败：没有可用 API key")
            return None

        request_params = {**params, "key": random.choice(self.api_key)}

        def fetch_data():
            response = requests.get(
                f"{self.api_base}{endpoint}",
                headers={"Content-Type": "application/json"},
                params=request_params,
                timeout=10,
                verify=True,
            )
            response.raise_for_status()
            return response.json()

        try:
            data = await asyncio.to_thread(fetch_data)
        except requests.RequestException as exc:
            logger.error(
                f"Domcer 查询 {query_label} 网络失败，不重试: {type(exc).__name__}: {exc}"
            )
            return None
        except ValueError as exc:
            logger.error(f"Domcer 查询 {query_label} 返回非 JSON: {exc}")
            return None
        except Exception as exc:
            logger.exception(f"Domcer 查询 {query_label} 未预期异常，不重试: {exc}")
            return None

        if not isinstance(data, dict):
            logger.error(
                f"Domcer 查询 {query_label} 返回类型异常: {type(data).__name__}"
            )
            return None

        # 接口偶尔返回字符串 "200"，不能只用整数比较。
        status = data.get("status")
        if (
            status is None
            and allow_unwrapped_data
            and ("id" in data or "playerStatistics" in data)
        ):
            # 详情接口的部分版本直接返回比赛对象，没有外层
            # {status, data}；统一包装，保持调用方的数据结构不变。
            return {"status": 200, "data": data}
        if str(status) != "200":
            logger.error(
                f"Domcer 查询 {query_label} 返回异常(status={status}, message={data.get('message', '未知错误')})"
            )
            return None

        return data

    async def get_match_list_by_name(
        self, username: str = None, match_id: str = None, limit: int = 20
    ) -> list[dict] | dict | None:
        if match_id:
            endpoint = "/match/megawalls"
            data = await self._request_domcer(
                endpoint,
                {"id": str(match_id).strip()},
                f"比赛 {match_id}",
                allow_unwrapped_data=True,
            )
            if data is not None:
                return self._process_match_data(data, detailed=True)
            logger.error(f"获取比赛详情失败: {match_id}")
            return None

        elif username:
            endpoint = "/match/getMegaWallsMatchList"
            username = str(username).strip()
            data = await self._request_domcer(
                endpoint,
                {"name": username, "limit": limit},
                f"玩家 {username}",
            )
            if data is not None:
                if "data" not in data:
                    logger.error(f"玩家 {username} 的比赛列表缺少 data 字段")
                    return None
                matches = data.get("data")
                if matches is None:
                    # 成功响应但没有 data 时按空结果处理，避免 None 被误认为
                    # 网络失败；列表接口正常的“无记录”通常就是空数组。
                    matches = []
                if isinstance(matches, list):
                    return [
                        self._process_match_data(m)
                        for m in matches
                        if isinstance(m, dict)
                    ]
                logger.error(
                    f"玩家 {username} 的比赛列表格式异常: {type(matches).__name__}"
                )
            return None

        logger.error("必须提供username或match_id参数")
        return None

    def _process_match_data(self, match_data: dict, detailed: bool = False) -> dict:
        processed = match_data.copy()
        if "startTime" in match_data and "endTime" in match_data:
            duration = (match_data["endTime"] - match_data["startTime"]) / 60000
            processed["duration"] = f"{duration:.1f}分钟"
            processed["startTime"] = self._format_timestamp(match_data["startTime"])
        else:
            processed["duration"] = "未知"
            processed["startTime"] = "未知时间"

        if processed.get("totalAttack", 0) > 0:
            processed["attackRate"] = (
                f"{(processed.get('attackSuccessful', 0)) / processed['totalAttack'] * 100:.1f}%"
            )
        else:
            processed["attackRate"] = "0%"

        if processed.get("totalArrow", 0) > 0:
            processed["arrowRate"] = (
                f"{(processed.get('arrowSuccessful', 0)) / processed['totalArrow'] * 100:.1f}%"
            )
        else:
            processed["arrowRate"] = "0%"

        if detailed and "playerStatistics" in processed:
            for player in processed["playerStatistics"]:
                player["attackRate"] = (
                    f"{(player.get('attackSuccessful', 0) / player.get('totalAttack', 1)) * 100:.1f}%"
                    if player.get("totalAttack", 0) > 0
                    else "0%"
                )
                player["arrowRate"] = (
                    f"{(player.get('arrowSuccessful', 0) / player.get('totalArrow', 1)) * 100:.1f}%"
                    if player.get("totalArrow", 0) > 0
                    else "0%"
                )
        return processed

    def _format_timestamp(self, timestamp: int) -> str:
        try:
            return datetime.fromtimestamp(timestamp / 1000).strftime("%Y-%m-%d %H:%M")
        except Exception:
            return "未知时间"

    def crop_image(
        self,
        src_path: str,
        dest_path: str,
        crop_bottom_px: int = 99,
        crop_right_px: int = 0,
    ) -> bool:
        try:
            img = Image.open(src_path)
            width, height = img.size
            img.crop((0, 0, width - crop_right_px, height - crop_bottom_px)).save(
                dest_path
            )
            return True
        except Exception as e:
            print(f"图片裁剪失败: {e}")
            return False

    async def _wait_and_save_bg(self, event: AstrMessageEvent, bg_type: str) -> str:
        """等待用户发送图片（30秒超时），保存为本地背景。返回结果消息。"""
        user_id = event.get_sender_id()
        evt = asyncio.Event()
        self._bg_waiting[user_id] = (evt, bg_type)
        logger.info(
            f"[背景设置] 用户 {user_id} 开始等待 {bg_type} 背景图片（30秒超时）"
        )

        try:
            await asyncio.wait_for(evt.wait(), timeout=30)
            url = self._bg_url_results.pop(user_id, None)
            if url:
                logger.info(f"[背景设置] 用户 {user_id} 开始下载背景图片")
                await self._download_and_save_bg(url, user_id, bg_type)
                return f"{'签到' if bg_type == 'sign' else '查询'}背景设置成功！"
            logger.warning(f"[背景设置] 用户 {user_id} 事件被唤醒但未找到 URL")
            return "未检测到图片，请重试。"
        except asyncio.TimeoutError:
            self._bg_waiting.pop(user_id, None)
            self._bg_url_results.pop(user_id, None)
            logger.warning(f"[背景设置] 用户 {user_id} 等待 {bg_type} 背景超时")
            return "超时未收到图片，请重新发送指令。"

    async def _download_and_save_bg(self, url: str, user_id: str, bg_type: str):
        """下载图片 → 压缩 → 保存到本地 → 删除旧图。"""
        import io

        filename = f"{bg_type}_{user_id}.jpg"
        filepath = os.path.join(self.bg_dir, filename)

        try:
            async with self.session.get(
                url, timeout=aiohttp.ClientTimeout(total=15)
            ) as resp:
                data = await resp.read()
        except Exception:
            return

        # 压缩: 最大宽度800, JPEG 70%质量
        try:
            img = Image.open(io.BytesIO(data))
            if img.mode in ("RGBA", "P"):
                img = img.convert("RGB")
            w, h = img.size
            if w > 800:
                ratio = 800 / w
                img = img.resize((800, int(h * ratio)), Image.LANCZOS)
            img.save(filepath, "JPEG", quality=70, optimize=True)
        except Exception:
            with open(filepath, "wb") as f:
                f.write(data)

        if bg_type == "sign":
            self.config.set_sign_bg(user_id, filepath)
        else:
            self.config.set_query_bg(user_id, filepath)

    def _get_bg_path(self, user_id: str, bg_type: str) -> str:
        """获取背景图片路径，将反斜杠转为正斜杠以兼容 CSS url()。"""
        if bg_type == "sign":
            path = self.config.get_sign_bg(user_id)
        else:
            path = self.config.get_query_bg(user_id)
        if path:
            path = path.replace("\\", "/")
        return path

    def get_most_used_class(self, class_usage: dict) -> str:
        if not class_usage:
            return "无"
        most_used = max(class_usage.items(), key=lambda x: x[1])
        return KIT_TRANSLATIONS.get(most_used[0], most_used[0])

    async def get_recent_matches_stats(self, game_id: str, days: int = 3) -> dict:
        from collections import defaultdict

        matches = await self.get_match_list_by_name(game_id, limit=100)
        if not matches:
            return {
                "total_matches": 0,
                "wins": 0,
                "mvps": 0,
                "class_usage": {},
                "win_rate": 0,
            }
        now = time.time() * 1000
        days_ms = days * 24 * 60 * 60 * 1000
        stats = {
            "total_matches": 0,
            "wins": 0,
            "mvps": 0,
            "class_usage": defaultdict(int),
            "win_rate": 0,
        }
        for match in matches:
            if "endTime" in match and (now - match["endTime"]) <= days_ms:
                stats["total_matches"] += 1
                if match.get("winner") == match.get("team"):
                    stats["wins"] += 1
                if match.get("mvp", False):
                    stats["mvps"] += 1
                stats["class_usage"][match.get("selectedKit", "未知")] += 1
        if stats["total_matches"] > 0:
            stats["win_rate"] = (stats["wins"] / stats["total_matches"]) * 100
        return stats

    def get_server_status(self, server_ip: str) -> dict:
        try:
            if ":" not in server_ip:
                server_ip += ":25565"
            server = JavaServer.lookup(server_ip)
            status = server.status()
            info = {
                "online": True,
                "ip": server_ip,
                "version": status.version.name,
                "players_online": status.players.online,
                "players_max": status.players.max,
                "latency": status.latency,
                "player_list": [],
            }
            if status.players.online > 0 and status.players.sample is not None:
                info["player_list"] = [p.name for p in status.players.sample]
            return info
        except Exception as e:
            return {"online": False, "ip": server_ip, "error": str(e)}

    def get_server_info(self, info: dict) -> str:
        if info["online"]:
            return f"D服:\n 玩家: {info['players_online']}\n 延迟: {info['latency']:.2f} ms\n"
        return f"服务器离线或无法连接\n错误: {info['error']}\n"

    async def _request_game_status(self, endpoint: str = "/api/status") -> dict | None:
        """Fetch a read-only snapshot from the guild game server."""
        base = os.environ.get("GAME_STATUS_URL", "").rstrip("/")
        if not base:
            return None
        url = f"{base}{endpoint}"
        try:
            if self.session is None or self.session.closed:
                self.session = aiohttp.ClientSession()
            async with self.session.get(
                url,
                timeout=aiohttp.ClientTimeout(total=6),
            ) as response:
                if response.status < 200 or response.status >= 300:
                    logger.warning(
                        "Guild server request failed: %s returned HTTP %s",
                        endpoint,
                        response.status,
                    )
                    return None
                payload = await response.json(content_type=None)
                if not isinstance(payload, dict):
                    logger.warning(
                        "Guild server returned non-object JSON: %s", endpoint
                    )
                    return None
                return payload
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
            logger.warning(
                "Guild server request failed: %s: %s",
                endpoint,
                type(exc).__name__,
            )
            return None
        except Exception as exc:
            logger.exception("Unexpected guild server request failure: %s", exc)
            return None

    @staticmethod
    def _game_value(value, fallback="—") -> str:
        if value is None or value == "":
            return fallback
        return html.escape(str(value))

    @staticmethod
    def _game_count(data: dict, *keys: str, default: int = 0) -> int:
        for key in keys:
            value = data.get(key)
            if value is not None:
                try:
                    return int(value)
                except (TypeError, ValueError):
                    continue
        return default

    @staticmethod
    def _game_duration(seconds) -> str:
        try:
            total = max(0, int(seconds))
        except (TypeError, ValueError):
            return "—"
        hours, remainder = divmod(total, 3600)
        minutes, secs = divmod(remainder, 60)
        if hours:
            return f"{hours}小时{minutes:02d}分"
        return f"{minutes}分{secs:02d}秒"

    def _render_game_card(self, title: str, content: str, height: int) -> str | None:
        """Render a deterministic status card and return its local image path."""
        safe_title = html.escape(title)
        height = max(360, min(int(height), 5000))
        page_height = height + 99
        document = f"""<!doctype html><html><head><meta charset="UTF-8"><style>
*{{box-sizing:border-box}}html,body{{margin:0;width:760px;min-height:{height}px}}
body{{padding:24px;font-family:'Microsoft YaHei','Noto Sans CJK SC',sans-serif;color:#eef5ff;
background:radial-gradient(circle at 15% 5%,#274b70 0,transparent 34%),linear-gradient(145deg,#101a2d,#16243d 55%,#101827)}}
.shell{{border:1px solid rgba(125,211,252,.28);border-radius:22px;padding:24px;background:rgba(10,18,32,.78);box-shadow:0 18px 50px rgba(0,0,0,.35)}}
.top{{display:flex;justify-content:space-between;align-items:center;border-bottom:1px solid rgba(255,255,255,.10);padding-bottom:16px;margin-bottom:18px}}
h1{{margin:0;font-size:28px;color:#f8d477;letter-spacing:1px}}.server{{font-size:13px;color:#93c5fd}}
.metrics{{display:grid;grid-template-columns:repeat(3,1fr);gap:12px;margin:14px 0 18px}}
.metric{{padding:15px 12px;border-radius:14px;background:rgba(56,189,248,.09);border:1px solid rgba(56,189,248,.16);text-align:center}}
.num{{font-size:27px;font-weight:800;color:#7dd3fc;line-height:1.2}}.label{{font-size:12px;color:#9fb0c8;margin-top:5px}}
.section{{margin-top:18px}}.section-title{{font-size:16px;font-weight:700;color:#86efac;margin-bottom:9px}}
.list{{display:flex;flex-wrap:wrap;gap:8px}}.tag{{padding:6px 11px;border-radius:999px;background:#223453;color:#dbeafe;font-size:13px}}
.room{{padding:15px;margin-top:10px;border-radius:14px;background:rgba(255,255,255,.055);border-left:4px solid #38bdf8}}
.room-head{{display:flex;justify-content:space-between;gap:12px;align-items:center}}.room-name{{font-size:17px;font-weight:700;color:#f8fafc}}
.room-id{{font:12px Consolas,monospace;color:#93c5fd}}.details{{margin-top:9px;color:#b8c6d9;font-size:13px;line-height:1.75}}
.players{{width:100%;border-collapse:collapse;margin-top:10px;font-size:13px}}.players th,.players td{{padding:8px 9px;text-align:left;border-bottom:1px solid rgba(255,255,255,.07)}}
.players th{{color:#93c5fd;font-weight:600}}.online{{color:#86efac}}.offline{{color:#fca5a5}}.muted{{color:#8290a5}}
.empty{{padding:28px;text-align:center;color:#93a4bb;background:rgba(255,255,255,.04);border-radius:14px}}
.message{{padding:28px 18px;text-align:center;font-size:18px;line-height:1.8;color:#dbeafe;white-space:pre-line}}
.footer{{text-align:right;color:#64748b;font-size:11px;margin-top:18px}}
</style></head><body><div class="shell"><div class="top"><h1>{safe_title}</h1><span class="server">游戏状态</span></div>
{content}<div class="footer">数据来自对战器实时状态接口</div></div></body></html>"""

        output_dir = os.path.abspath("./tmp_guild_game_images")
        os.makedirs(output_dir, exist_ok=True)
        filename = f"guild_{uuid.uuid4().hex}.png"
        renderer = Html2Image(
            output_path=output_dir,
            custom_flags=[
                "--enable-local-file-access",
                "--hide-scrollbars",
                "--width=760",
                f"--height={page_height}",
            ],
        )
        try:
            renderer.screenshot(
                html_str=document,
                save_as=filename,
                size=(760, page_height),
            )
        except Exception as exc:
            logger.exception("Guild game card rendering failed: %s", exc)
            return None
        image_path = os.path.join(output_dir, filename)
        if not os.path.exists(image_path):
            return None
        cropped_path = os.path.join(output_dir, f"cropped_{filename}")
        return (
            cropped_path
            if self.crop_image(image_path, cropped_path, crop_bottom_px=99)
            else image_path
        )

    def _game_message_card(self, title: str, message: str) -> str | None:
        content = f'<div class="message">{html.escape(message)}</div>'
        return self._render_game_card(title, content, 360)

    def _game_overview_content(self, status: dict) -> tuple[str, int]:
        connected = self._game_count(status, "connected_people")
        room_count = self._game_count(status, "active_room_count")
        active_players = self._game_count(status, "active_player_count")
        online_players = self._game_count(status, "active_online_player_count")
        players = (
            status.get("players") if isinstance(status.get("players"), list) else []
        )
        rooms = status.get("rooms") if isinstance(status.get("rooms"), list) else []

        player_tags = (
            "".join(
                f'<span class="tag">{self._game_value(name)}</span>' for name in players
            )
            or '<span class="muted">当前没有进行中的玩家</span>'
        )

        room_cards = []
        for room in rooms:
            if not isinstance(room, dict):
                continue
            room_id = self._game_value(room.get("id"))
            room_name = self._game_value(room.get("name"), "未命名房间")
            room_players = self._game_count(room, "player_count", "active_player_count")
            seats = self._game_count(
                room,
                "total_seats",
                "seat_count",
                "total_player_count",
                default=len(room.get("players", []))
                if isinstance(room.get("players"), list)
                else 0,
            )
            phase = self._game_value(room.get("phase"))
            round_no = self._game_value(room.get("round"))
            room_cards.append(
                '<div class="room"><div class="room-head">'
                f'<span class="room-name">{room_name}</span><span class="room-id">{room_id}</span></div>'
                f'<div class="details">玩家 {room_players}/{seats or "—"}　阶段 {phase}　回合 {round_no}　'
                f"已进行 {self._game_duration(room.get('duration_seconds'))}</div></div>"
            )
        rooms_html = (
            "".join(room_cards) or '<div class="empty">当前没有正在游玩的房间</div>'
        )
        content = f"""
<div class="metrics">
  <div class="metric"><div class="num">{connected}</div><div class="label">在线连接</div></div>
  <div class="metric"><div class="num">{room_count}</div><div class="label">进行中房间</div></div>
  <div class="metric"><div class="num">{online_players}/{active_players}</div><div class="label">房间内在线 / 席位</div></div>
</div>
<div class="section"><div class="section-title">玩家列表</div><div class="list">{player_tags}</div></div>
<div class="section"><div class="section-title">进行中的房间</div>{rooms_html}</div>"""
        return content, 520 + max(0, len(room_cards) - 1) * 110 + max(
            0, len(players) - 8
        ) // 8 * 38

    def _game_room_content(self, room: dict) -> tuple[str, int]:
        room_players = (
            room.get("players") if isinstance(room.get("players"), list) else []
        )
        active_players = self._game_count(room, "player_count", "active_player_count")
        online_players = self._game_count(
            room,
            "online_player_count",
            "active_online_player_count",
            default=sum(
                1
                for player in room_players
                if isinstance(player, dict)
                and player.get("connected")
                and not player.get("withdrawn")
            ),
        )
        seats = self._game_count(
            room,
            "total_seats",
            "seat_count",
            "total_player_count",
            default=len(room_players),
        )
        rows = []
        for player in room_players:
            if not isinstance(player, dict):
                continue
            withdrawn = bool(player.get("withdrawn"))
            connected = bool(player.get("connected")) and not withdrawn
            closed = bool(player.get("closed"))
            if withdrawn:
                state, state_class = "已退出", "muted"
            elif connected:
                state, state_class = "在线", "online"
            else:
                state, state_class = "离线", "offline"
            if closed:
                state += " · 已闭店"
            mascot = (
                player.get("hero")
                or player.get("mascot")
                or player.get("character")
                or player.get("heroine")
                or player.get("kanban_musume")
                or "—"
            )
            rows.append(
                "<tr>"
                f"<td>{self._game_value(player.get('id'))}</td>"
                f"<td>{self._game_value(player.get('name'), '未知玩家')}</td>"
                f"<td>{self._game_value(mascot)}</td>"
                f'<td class="{state_class}">{state}</td></tr>'
            )
        table = (
            '<table class="players"><thead><tr><th>席位</th><th>玩家</th><th>看板娘</th><th>状态</th></tr></thead>'
            f"<tbody>{''.join(rows)}</tbody></table>"
            if rows
            else '<div class="empty">暂无玩家数据</div>'
        )
        content = f"""
<div class="metrics">
  <div class="metric"><div class="num">{active_players}/{seats or "—"}</div><div class="label">当前玩家 / 总席位</div></div>
  <div class="metric"><div class="num">{online_players}</div><div class="label">当前在线</div></div>
  <div class="metric"><div class="num">{self._game_duration(room.get("duration_seconds"))}</div><div class="label">游戏时长</div></div>
</div>
<div class="room"><div class="room-head"><span class="room-name">{self._game_value(room.get("name"), "未命名房间")}</span><span class="room-id">{self._game_value(room.get("id"))}</span></div>
<div class="details">阶段 {self._game_value(room.get("phase"))}　回合 {self._game_value(room.get("round"))}　季节 {self._game_value(room.get("season"))}<br>
文明 {self._game_value(room.get("civilization"))}　瘴气 {self._game_value(room.get("miasma"))}　地图 {self._game_value(room.get("map_type"))}</div></div>
<div class="section"><div class="section-title">玩家详情</div>{table}</div>"""
        return content, 470 + len(rows) * 42


# ════════════════════════════════════════════════════════════
#  命令注册 (通过 filter.command 注册为插件方法)
# ════════════════════════════════════════════════════════════


# ── 对战器状态 ────────────────────────────────────────────


@filter.command(command_name("guild_online_count"))
async def guild_online_count(self, event: AstrMessageEvent):
    """Show the current number of connections as an image card."""
    status = await self._request_game_status()
    if status is None or not status.get("online", False):
        image_path = self._game_message_card("对战器状态", "服务器离线或暂时无法连接")
    else:
        connected = self._game_count(status, "connected_people")
        content = f"""
<div class="metrics" style="grid-template-columns:1fr">
  <div class="metric" style="padding:34px 12px">
    <div class="num" style="font-size:54px">{connected}</div>
    <div class="label" style="font-size:15px">当前在线连接人数</div>
  </div>
</div>"""
        image_path = self._render_game_card("服务器在线人数", content, 360)
    if image_path:
        yield event.image_result(image_path)
    else:
        yield event.plain_result("状态图片生成失败，请稍后重试")


@filter.command(command_name("guild_overview"))
async def guild_overview(self, event: AstrMessageEvent):
    """Show the server overview, rooms and player list as an image card."""
    status = await self._request_game_status()
    if status is None or not status.get("online", False):
        image_path = self._game_message_card("对战器总览", "服务器离线或暂时无法连接")
    else:
        content, height = self._game_overview_content(status)
        image_path = self._render_game_card("对战器总览", content, height)
    if image_path:
        yield event.image_result(image_path)
    else:
        yield event.plain_result("总览图片生成失败，请稍后重试")


@filter.command(command_name("guild_room_details"))
async def guild_room_details(self, event: AstrMessageEvent, room_id: str = ""):
    """Show a room snapshot. With one active room, the ID may be omitted."""
    room_id = str(room_id or "").strip()
    room = None

    if room_id:
        safe_room_id = quote(room_id, safe="")
        room = await self._request_game_status(f"/api/rooms/{safe_room_id}")
        if room is None:
            image_path = self._game_message_card(
                "房间详情",
                f"未找到房间 {room_id}，或服务器暂时无法连接",
            )
        else:
            content, height = self._game_room_content(room)
            image_path = self._render_game_card("房间详情", content, height)
    else:
        status = await self._request_game_status()
        if status is None or not status.get("online", False):
            image_path = self._game_message_card("房间详情", "服务器离线或暂时无法连接")
        else:
            rooms = status.get("rooms") if isinstance(status.get("rooms"), list) else []
            rooms = [item for item in rooms if isinstance(item, dict)]
            if len(rooms) == 1:
                content, height = self._game_room_content(rooms[0])
                image_path = self._render_game_card("房间详情", content, height)
            elif not rooms:
                image_path = self._game_message_card(
                    "房间详情", "当前没有正在游玩的房间"
                )
            else:
                room_ids = "、".join(str(item.get("id", "—")) for item in rooms)
                image_path = self._game_message_card(
                    "房间详情",
                    f"当前有多个房间，请使用 /g详情 房间ID\n可选房间：{room_ids}",
                )

    if image_path:
        yield event.image_result(image_path)
    else:
        yield event.plain_result("房间详情图片生成失败，请稍后重试")


# ── 签到 ──────────────────────────────────────────────────


@filter.command(command_name("daily_sign"))
async def daily_sign(self, event: AstrMessageEvent):
    user_id = event.get_sender_id()
    user_name = event.get_sender_name()
    today = datetime.now().strftime("%Y-%m-%d")

    game_id = self.config.get_bind(user_id)
    if not game_id:
        yield event.plain_result("请先使用/绑定 游戏ID 绑定您的游戏账号")
        return

    matches = await self.get_match_list_by_name(game_id, limit=100)
    if not matches:
        yield event.plain_result("没有找到最近的比赛记录")
        return

    three_days_ago = time.time() * 1000 - 3 * 24 * 60 * 60 * 1000
    recent_matches = [m for m in matches if m.get("endTime", 0) >= three_days_ago]

    best_teammates = None
    worst_teammates = None

    if recent_matches:
        from collections import defaultdict

        teammate_stats = defaultdict(lambda: {"games": 0, "wins": 0})
        for match in recent_matches:
            match_details = await self.get_match_list_by_name(match_id=match["id"])
            if not match_details:
                continue
            match_details = match_details["data"]
            player_team = None
            for player in match_details.get("playerStatistics", []):
                if player.get("realName", "") == game_id:
                    player_team = player.get("team")
                    break
            if not player_team:
                continue
            for player in match_details.get("playerStatistics", []):
                if (
                    player.get("realName", "") != game_id
                    and player.get("team") == player_team
                ):
                    tn = player.get("realName")
                    teammate_stats[tn]["games"] += 1
                    if match_details.get("winner") == player_team:
                        teammate_stats[tn]["wins"] += 1

        teammate_list = []
        for name, s in teammate_stats.items():
            if s["games"] >= 2:
                teammate_list.append((name, (s["wins"] / s["games"]) * 100, s["games"]))
        best_teammates = sorted(teammate_list, key=lambda x: (-x[1], -x[2]))[:3]
        worst_teammates = sorted(teammate_list, key=lambda x: (x[1], -x[2]))[:3]

    stats = await self.get_recent_matches_stats(game_id, 3)
    most_used_class = self.get_most_used_class(stats["class_usage"])

    last_sign, current_coins = self.config.get_currency(user_id)
    has_signed = last_sign == today
    if not has_signed:
        new_coins = current_coins + 30
        self.config.set_currency(user_id, today, new_coins)
    else:
        new_coins = current_coins

    script_dir = os.path.dirname(os.path.abspath(__file__))
    class_img_path = os.path.join(script_dir, "kit_img", f"{most_used_class}.png")
    bg_path = self._get_bg_path(user_id, "sign")
    bg_css = f"url('file://{bg_path}')" if bg_path else "none"

    def fmt_tm(teammates, _best=True):
        if not teammates:
            return "无足够数据"
        parts = []
        for i, (n, wr, g) in enumerate(teammates, 1):
            parts.append(
                f"{i}. {n[:10]}{'...' if len(n) > 10 else ''} {wr:.1f}% ({g}场)"
            )
        return "<br>".join(parts)

    html_content = f"""<html><head><meta charset="UTF-8"><style>
body{{margin:0;padding:20px;background:{bg_css} no-repeat center/cover;font-family:'Microsoft YaHei',sans-serif;display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:20px;align-items:start;}}
.card{{background:rgba(0,0,0,0.75);border-radius:15px;color:#fff;padding:25px;box-shadow:0 5px 15px rgba(0,0,0,0.5);backdrop-filter:blur(5px);border:1px solid rgba(255,255,255,0.1);}}
.header-card{{grid-column:1/-1;display:flex;justify-content:space-between;align-items:center;}}
.title{{font-size:2.5rem;color:#4fc3f7;text-shadow:0 0 10px rgba(79,195,247,0.5);margin:0;}}
.coin-info{{text-align:right;}}
.stats-card{{display:grid;grid-template-columns:1fr 1fr;gap:15px;}}
.stat-item{{background:rgba(50,50,50,0.5);border-radius:10px;padding:15px;}}
.stat-title{{font-size:1.5rem;color:#69f0ae;margin-bottom:10px;border-bottom:1px solid #69f0ae;padding-bottom:5px;}}
.stat-value{{font-size:1.8rem;margin:5px 0;}}
.teammate-card{{min-height:200px;}}
.teammate-header{{display:flex;align-items:center;margin-bottom:15px;}}
.teammate-title{{font-size:1.6rem;margin-left:10px;}}
.best .teammate-title{{color:#69f0ae;}}
.worst .teammate-title{{color:#ff5252;}}
.teammate-list{{font-size:1.4rem;line-height:1.6;}}
.class-card{{display:flex;flex-direction:column;align-items:center;}}
.class-img{{width:150px;height:150px;border-radius:15px;border:3px solid #4fc3f7;box-shadow:0 0 15px rgba(79,195,247,0.5);object-fit:cover;margin-bottom:15px;}}
.class-name{{font-size:1.8rem;color:#ff8a65;text-align:center;margin-bottom:5px;}}
.sign-card{{display:flex;flex-direction:column;align-items:center;justify-content:center;min-height:150px;}}
.sign-message{{font-size:2rem;color:#ff9800;text-align:center;margin-bottom:15px;}}
.sign-date{{font-size:1.2rem;opacity:0.8;}}
.highlight{{color:#FFD700;font-weight:bold;}}
.icon{{font-size:1.8rem;margin-right:5px;}}
</style></head><body>
<div class="card header-card">
<h1 class="title">{user_name}签到成功</h1>
<div class="coin-info">
<div class="stat-value">💰 <span class='highlight'>{new_coins}</span> 关鹏币</div>
{"<div class='sign-date'>今日已签到</div>" if has_signed else "<div class='sign-date'>获得 <span class='highlight'>30</span> 关鹏币</div>"}
</div></div>
<div class="card stats-card">
<div class="stat-item" style="grid-column:1/-1;">
<div class="stat-title">📊 比赛数据</div>
<div class="stat-value"><span class="icon">🕹️</span> {stats["total_matches"]} 场</div>
<div class="stat-value"><span class="icon">🏆</span> {stats["win_rate"]:.1f}% 胜率</div>
<div class="stat-value"><span class="icon">⭐</span> {stats["mvps"]} 次MVP</div>
</div></div>
<div class="card teammate-card best">
<div class="teammate-header"><span class="icon">👍</span><h2 class="teammate-title">最佳队友</h2></div>
<div class="teammate-list">{fmt_tm(best_teammates)}</div></div>
<div class="card teammate-card worst">
<div class="teammate-header"><span class="icon">👎</span><h2 class="teammate-title">近期胜率较低的队友</h2></div>
<div class="teammate-list">{fmt_tm(worst_teammates, False)}</div></div>
<div class="card class-card">
<img class="class-img" src="file://{class_img_path}" onerror="this.style.display='none'">
<h2 class="class-name">{most_used_class}</h2><div>最常使用职业</div></div>
<div class="card sign-card">
{"<div class='sign-message'>今日已签到</div>" if has_signed else "<div class='sign-message'>签到成功</div>"}
<div class="sign-date">当前余额: <span class='highlight'>{new_coins}</span> 关鹏币</div>
</div></body></html>"""

    output_dir = os.path.abspath("./tmp_sign_images")
    os.makedirs(output_dir, exist_ok=True)
    hti = Html2Image(
        output_path=output_dir,
        custom_flags=["--enable-local-file-access", "--width=400", "--height=600"],
    )
    hti.screenshot(html_str=html_content, save_as="sign.png")
    img_path = os.path.join(output_dir, "sign.png")
    cropped_path = os.path.join(output_dir, "sign_cropped.png")
    if os.path.exists(img_path):
        final = (
            cropped_path
            if self.crop_image(img_path, cropped_path, 99, 24)
            else img_path
        )
        yield event.image_result(final)
    else:
        yield event.plain_result("签到卡片生成失败")


# ── 关鹏币 ────────────────────────────────────────────────


@filter.command(command_name("get_currency"))
async def get_currency(self, event: AstrMessageEvent):
    user_id = event.get_sender_id()
    user_name = event.get_sender_name()
    _, coins = self.config.get_currency(user_id)
    yield event.plain_result(f"{user_name},你有{coins}个关鹏币")


# ── mw查询 ────────────────────────────────────────────────


@filter.command(command_name("query_megawalls"))
async def query_megawalls(
    self, event: AstrMessageEvent, arg1: str = "", arg2: str = "", arg3: str = ""
):
    if not arg1:
        yield event.plain_result(
            "请输入玩家名\n例如: /mw查询 玩家名 或 /mw查询 玩家名 职业 僵尸"
        )
        return

    username = arg1
    all_matches = await self.get_match_list_by_name(username)
    if all_matches is None:
        yield event.plain_result(
            f"查询玩家 {username} 失败，接口暂时不可用，请稍后重试"
        )
        return
    if not all_matches:
        yield event.plain_result(f"未找到玩家 {username} 的比赛记录")
        return

    # 将未入库的对局 ID 加入共享数据库的待处理队列
    self.match_db.collect_from_player_matches(all_matches)

    def process_match_data(matches, start_index=1):
        processed = []
        for idx, match in enumerate(matches):
            processed.append(
                {
                    "index": start_index + idx,
                    "map": match.get("mapName", "未知地图"),
                    "result": "胜利"
                    if match.get("winner") == match.get("team")
                    else ("平局" if match.get("winner") == "NONE" else "失败"),
                    "duration": match.get("duration", ""),
                    "class": match.get("selectedKit", "未知"),
                    "final_kills": int(match.get("finalKills", 0)),
                    "final_assists": int(match.get("finalAssists", 0)),
                    "damage": int(match.get("totalDamage", 0)),
                    "takenDamage": int(match.get("takenDamage", 0)),
                    "mvp": match.get("mvp", False),
                    "time": self._format_timestamp(match.get("endTime", 0)),
                    "kit_image": os.path.join(
                        os.path.dirname(os.path.abspath(__file__)),
                        "kit_img",
                        f"{match.get('selectedKit', 'unknown')}.png",
                    ),
                }
            )
        return processed

    def calculate_stats(matches):
        total = len(matches)
        if total == 0:
            return {
                "win_rate": 0,
                "total_final_kills": 0,
                "avg_final_kills": 0,
                "avg_damage": 0,
                "total_matches": 0,
                "avg_ka": 0,
                "avg_taken_damage": 0,
            }
        wins = sum(1 for m in matches if m.get("result") == "胜利")
        win_rate = round((wins / total) * 100, 1) if total > 0 else 0
        total_fk = sum(m.get("final_kills", 0) for m in matches)
        total_fa = sum(m.get("final_assists", 0) for m in matches)
        avg_fk = total_fk / total if total > 0 else 0
        avg_ka = (total_fk + total_fa) / total if total > 0 else 0  # ← 新增：场均KA
        # 所有对局统一统计，不再按模式拆分或过滤伤害数据。
        nc = total or 1
        total_dmg = sum(m.get("damage", 0) for m in matches)
        avg_dmg = total_dmg / nc if nc > 0 else 0
        total_td = sum(m.get("takenDamage", 0) for m in matches)
        avg_td = total_td / total if total > 0 else 0  # ← 新增：场均承受伤害
        return {
            "win_rate": win_rate,
            "total_final_kills": int(total_fk),
            "avg_final_kills": round(avg_fk, 1),
            "avg_damage": int(avg_dmg),
            "total_matches": total,
            "avg_ka": round(avg_ka, 1),
            "avg_taken_damage": int(avg_td),
        }

    filtered_matches = all_matches
    display_title = f"{username} 的超级战墙战绩"
    start_index = 1

    if arg2 == "日期" and arg3:
        try:
            target_date = arg3.replace(".", "-")
            date_parts = target_date.split("-")
            if len(date_parts) == 3:
                target_date = (
                    f"{date_parts[0]}-{date_parts[1].zfill(2)}-{date_parts[2].zfill(2)}"
                )
            date_matches = [
                m
                for m in all_matches
                if self._format_timestamp(m.get("endTime", 0)).startswith(target_date)
            ]
            if not date_matches:
                yield event.plain_result(f"未找到玩家 {username} 在 {arg3} 的比赛记录")
                return
            filtered_matches = date_matches
            display_title = f"{username} 在 {arg3} 的战绩"
        except Exception:
            yield event.plain_result("日期格式错误，请使用 xxxx.xx.xx 格式")
            return

    elif arg2 == "职业" and arg3:
        kit_matches = [m for m in all_matches if m.get("selectedKit") == arg3]
        if not kit_matches:
            yield event.plain_result(f"未找到玩家 {username} 使用 {arg3} 的比赛记录")
            return
        filtered_matches = kit_matches[:5]
        display_title = f"{username} 的 {arg3} 战绩"

    elif arg2 == "翻页" and arg3:
        try:
            page = int(arg3)
            if page < 1:
                raise ValueError
            mpp = 5
            si = (page - 1) * mpp
            if si >= len(all_matches):
                yield event.plain_result(
                    f"页数超出范围，该玩家只有 {len(all_matches)} 场比赛"
                )
                return
            filtered_matches = all_matches[si : si + mpp]
            start_index = si + 1
            display_title = f"{username} 的战绩 (第{page}页)"
        except ValueError:
            yield event.plain_result("页数必须是正整数")
            return
    else:
        filtered_matches = all_matches[:5]

    processed_matches = process_match_data(filtered_matches, start_index)

    if arg2 == "职业" and arg3:
        recent_kit = [m for m in all_matches if m.get("selectedKit") == arg3][:20]
        stats_matches = process_match_data(recent_kit)
    else:
        stats_matches = process_match_data(
            filtered_matches if arg2 == "日期" else all_matches[:20]
        )

    stats = calculate_stats(stats_matches)
    bg_path = self._get_bg_path(event.get_sender_id(), "query")

    TMPL = r"""<meta name="viewport" content="width=400, initial-scale=1.0">
<style>
body,html{margin:0;padding:0;width:400px;background:transparent;font-family:Arial,sans-serif;}
.green-label{color:#00FF7F!important;font-weight:normal;}
.green-text{color:#00FF7F!important;font-weight:normal;text-shadow:0 0 2px rgba(0,0,0,0.3);}
.yellow-text{color:#FFD700!important;font-weight:normal;text-shadow:0 0 3px rgba(0,0,0,0.5);}
.title-outline{color:#FFD700;text-shadow:-1.5px -1.5px 0 #000,1.5px -1.5px 0 #000,-1.5px 1.5px 0 #000,1.5px 1.5px 0 #000;font-size:24px;font-weight:normal;margin:0;}
.mw-container{width:400px;min-height:100vh;background:linear-gradient(135deg,#1a1a2e,#16213e);{% if custom_bg %}background:url('file://{{ custom_bg }}') no-repeat center center/cover;{% endif %}padding:15px;box-sizing:border-box;}
.data-container{background:rgba(50,50,50,0.7);border-radius:8px;padding:12px;margin-bottom:15px;box-shadow:0 2px 5px rgba(0,0,0,0.3);}
.match-container{display:flex;gap:10px;background:rgba(60,60,60,0.7);padding:10px;border-radius:6px;margin-bottom:8px;position:relative;}
.match-index{position:absolute;background:rgba(0,0,0,0.7);color:#FFD700;padding:2px 6px;border-radius:4px;font-size:12px;margin:-5px 0 0 -5px;}
</style>
<div class="mw-container">
<div style="text-align:center;margin-bottom:15px;"><h1 class="title-outline">{{ display_title }}</h1></div>
<div class="data-container">
<div style="display:grid;grid-template-columns:1fr 1fr;gap:10px;">
<div>
<h3 style="margin:0 0 8px 0;color:#4fc3f7;font-size:16px;font-weight:normal;">
{% if arg2 == "职业" %}最近20场{{ arg3 }}数据{% else %}最近{{ stats.total_matches }}场比赛总览{% endif %}
</h3>
<p style="margin:4px 0;"><span class="green-label">胜率: </span><span class="yellow-text">{{ stats.win_rate }}%</span></p>
<p style="margin:4px 0;"><span class="green-label">总击杀: </span><span class="yellow-text">{{ stats.total_final_kills }}</span></p>
<p style="margin:4px 0;"><span class="green-label">场均KA: </span><span class="yellow-text">{{ stats.avg_ka }}</span></p>
</div>
<div>
<h3 style="margin:0 0 8px 0;color:#4fc3f7;font-size:16px;font-weight:normal;">场均</h3>
<p style="margin:4px 0;"><span class="green-label">击杀: </span><span class="yellow-text">{{ stats.avg_final_kills }}</span></p>
<p style="margin:4px 0;"><span class="green-label">伤害: </span><span class="yellow-text">{{ stats.avg_damage }}</span></p>
<p style="margin:4px 0;"><span class="green-label">承伤: </span><span class="yellow-text">{{ stats.avg_taken_damage }}</span></p>
</div>
</div></div>
<h3 style="color:#4fc3f7;border-bottom:2px solid #4fc3f7;padding-bottom:5px;font-size:16px;margin:0 0 10px 0;font-weight:normal;">比赛记录 (共{{ processed_matches|length }}场)</h3>
{% for match in processed_matches %}
<div class="match-container">
<div class="match-index">第{{ match.index }}局</div>
<img class="kit-image" src="file://{{ match.kit_image }}" onerror="this.style.display='none'" style="width:50px;height:50px;border-radius:4px;object-fit:cover;">
<div style="flex:1;">
<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:5px;">
<p style="margin:0;font-size:14px;font-weight:normal;">
<span class="yellow-text">{{ match.map }}</span> |
<span style="color:{% if match.result == '胜利' %}#69f0ae{% elif match.result == '失败' %}#ff5252{% else %}#ffeb3b{% endif %}">{{ match.result }}</span>
{% if match.mvp %}<span style="display:inline-block;background:linear-gradient(135deg,#ffd700,#ff9800);color:#000;padding:2px 6px;border-radius:4px;font-size:12px;margin-left:5px;">MVP</span>{% endif %}
</p>
<p style="margin:0;font-size:12px;color:#b0bec5;">{{ match.time }}</p>
</div>
<p style="margin:0 0 5px 0;font-size:13px;color:#b0bec5;">职业 {{ match.class }} | 时长 {{ match.duration }}</p>
<div style="display:flex;gap:10px;font-size:13px;">
<span class="yellow-text">{{ match.final_kills }} 击杀</span>
<span style="color:#4fc3f7;">{{ match.final_assists }} 助攻</span>
<span class="yellow-text">{{ match.damage }} 伤害</span>
<span style="color:#a5d6a7;">{{ match.takenDamage }} 承伤</span>
</div>
</div></div>
{% endfor %}
</div>"""

    render_data = {
        "username": username,
        "display_title": display_title,
        "custom_bg": bg_path,
        "stats": stats,
        "processed_matches": processed_matches,
        "arg2": arg2,
        "arg3": arg3,
    }
    template = Template(TMPL)
    html_content = template.render(render_data)

    output_dir = os.path.abspath("./tmp_mw_images")
    os.makedirs(output_dir, exist_ok=True)

    base_height = 380
    per_match_height = 85
    total_height = base_height + per_match_height * len(processed_matches)

    hti = Html2Image(
        output_path=output_dir,
        custom_flags=[
            "--disable-smart-width",
            "--disable-smart-height",
            "--width=400",
            f"--height={total_height}",
            "--enable-local-file-access",
            "--hide-scrollbars",
            "--no-sandbox",
            "--force-device-scale-factor=1",
        ],
    )
    img_path = os.path.join(output_dir, "mw_stats.png")
    cropped_path = os.path.join(output_dir, "mw_stats_cropped.png")
    hti.screenshot(
        html_str=html_content, save_as="mw_stats.png", size=(400, total_height + 85)
    )
    if os.path.exists(img_path):
        final = (
            cropped_path if self.crop_image(img_path, cropped_path, 99) else img_path
        )
        yield event.image_result(final)
    else:
        yield event.plain_result("图片生成失败，请检查日志")


# ── mw详情 ────────────────────────────────────────────────


@filter.command(command_name("match_details"))
async def match_details(self, event: AstrMessageEvent, arg1: str = "", arg2: str = ""):
    if not arg1:
        yield event.plain_result(
            "请输入比赛ID或玩家名+比赛序号\n例如: /mw详情 MatchID123 或 /mw详情 玩家名 1"
        )
        return

    if not arg2:
        match_data = await self.get_match_list_by_name(match_id=arg1)
        if match_data:
            match_data = match_data["data"]
    else:
        username = arg1
        try:
            index = int(arg2) - 1
            if index < 0:
                raise ValueError
        except ValueError:
            yield event.plain_result("比赛序号必须是正整数")
            return
        matches = await self.get_match_list_by_name(username)
        if matches is None:
            yield event.plain_result(
                f"查询玩家 {username} 失败，接口暂时不可用，请稍后重试"
            )
            return
        if not matches:
            yield event.plain_result(f"未找到玩家 {username} 的比赛记录")
            return
        if index >= len(matches):
            yield event.plain_result(
                f"比赛序号超出范围，该玩家最近只有 {len(matches)} 场比赛"
            )
            return
        match_data = await self.get_match_list_by_name(match_id=matches[index]["id"])
        if match_data:
            match_data = match_data["data"]

    if not match_data:
        yield event.plain_result("获取比赛详情失败，请检查参数是否正确")
        return

    from collections import defaultdict

    bg_path = self._get_bg_path(event.get_sender_id(), "sign")
    bg_css = f"url('file://{bg_path}')" if bg_path else "none"
    players_by_team = defaultdict(list)
    for player in match_data.get("playerStatistics", []):
        players_by_team[player.get("team", "UNKNOWN")].append(player)

    teams_html = ""
    for team, players in players_by_team.items():
        ti = TEAM_COLORS.get(team, TEAM_COLORS["UNKNOWN"])
        players_sorted = sorted(
            players, key=lambda p: p.get("finalKills", 0), reverse=True
        )
        players_html = ""
        for p in players_sorted:
            players_html += f"""
<div class="player"><div class="player-name">{p.get("realName", "未知玩家")}{"<span class='mvp-badge'>MVP</span>" if p.get("mvp") else ""}</div>
<div class="player-kit">{KIT_TRANSLATIONS.get(p.get("selectedKit"), p.get("selectedKit", "未知"))}</div>
<div class="player-stats"><span class="stat-value">{p.get("finalKills", 0)}</span> <span class="stat-label">击杀</span>
<span class="stat-value">{p.get("finalAssists", 0)}</span> <span class="stat-label">助攻</span>
<span class="stat-value">{int(p.get("totalDamage", 0))}</span> <span class="stat-label">伤害</span></div></div>"""
        teams_html += f"""<div class="team"><div class="team-title" style="background:{ti["color"]}">{ti["name"]} ({len(players)}人)</div>{players_html}</div>"""

    html_content = f"""<html><head><meta charset="UTF-8"><style>
body{{margin:0;padding:20px;background:{bg_css} no-repeat center/cover;font-family:'Microsoft YaHei',sans-serif;color:#fff;}}
.container{{background:rgba(0,0,0,0.75);border-radius:15px;padding:20px;box-shadow:0 5px 15px rgba(0,0,0,0.5);backdrop-filter:blur(5px);border:1px solid rgba(255,255,255,0.1);max-width:500px;margin:0 auto;}}
.header{{text-align:center;margin-bottom:20px;border-bottom:1px solid #4fc3f7;padding-bottom:10px;}}
.title{{font-size:24px;color:#4fc3f7;margin:0;}}
.match-info{{margin-bottom:15px;padding:10px;background:rgba(50,50,50,0.5);border-radius:10px;}}
.team{{margin-bottom:15px;}}
.team-title{{font-size:18px;padding:5px 10px;border-radius:5px;margin-bottom:10px;display:inline-block;}}
.player{{display:flex;align-items:center;margin-bottom:8px;padding:8px;background:rgba(70,70,70,0.5);border-radius:8px;}}
.player-name{{flex:1;font-weight:bold;}}
.player-kit{{width:100px;color:#69f0ae;}}
.player-stats{{width:150px;text-align:right;}}
.stat-label{{color:#b0bec5;font-size:12px;}}
.stat-value{{color:#ffd700;}}
.mvp-badge{{background:linear-gradient(135deg,#ffd700,#ff9800);color:#000;padding:2px 6px;border-radius:4px;font-size:12px;margin-left:5px;}}
</style></head><body>
<div class="container"><div class="header"><h1 class="title">超级战墙比赛详情</h1></div>
<div class="match-info">
<p><strong>比赛ID:</strong> {match_data.get("id", "未知")}</p>
<p><strong>地图:</strong> {match_data.get("mapName", "未知地图")}</p>
<p><strong>时间:</strong> {match_data.get("startTime", "未知时间")} 时长: {match_data.get("duration", "未知")}</p>
<p><strong>MVP:</strong> {match_data.get("mvp", "无")}</p>
<p><strong>胜利队伍:</strong> <span style="color:{TEAM_COLORS.get(match_data.get("winner", "NONE"), TEAM_COLORS["NONE"])["color"]}">{TEAM_COLORS.get(match_data.get("winner", "NONE"), TEAM_COLORS["NONE"])["name"]}</span></p>
</div>{teams_html}</div></body></html>"""

    output_dir = os.path.abspath("./tmp_mw_detail_images")
    os.makedirs(output_dir, exist_ok=True)
    total_players = sum(len(pl) for pl in players_by_team.values())
    total_height = 280 + len(players_by_team) * 50 + total_players * 50
    hti = Html2Image(
        output_path=output_dir,
        custom_flags=[
            "--enable-local-file-access",
            "--width=500",
            "--disable-smart-height",
            "--hide-scrollbars",
        ],
    )
    hti.screenshot(
        html_str=html_content, save_as="mw_detail.png", size=(500, total_height)
    )
    img_path = os.path.join(output_dir, "mw_detail.png")
    cropped_path = os.path.join(output_dir, "mw_detail_cropped.png")
    if os.path.exists(img_path):
        final = (
            cropped_path
            if self.crop_image(img_path, cropped_path, 99, 24)
            else img_path
        )
        yield event.image_result(final)
    else:
        yield event.plain_result("比赛详情卡片生成失败")


# ── Help ──────────────────────────────────────────────────


@filter.command(command_name("guanpeng_help"))
async def guanpeng_help(self, event: AstrMessageEvent):
    """生成关鹏机器人完整使用说明书（图片版）。"""
    sections = [
        (
            "🏰 对战器状态",
            [
                ("/g人数", "查看服务器当前在线连接人数（图片卡片）"),
                ("/g", "查看在线人数、房间人数、玩家与房间总览"),
                ("/g详情 房间ID", "查看指定房间的游戏详情"),
            ],
        ),
        (
            "🎮 战绩查询",
            [
                ("/mw查询 玩家名", "查询最近5场战绩（图片卡片）"),
                ("/mw查询 玩家名 日期 2026.7.1", "查指定日期战绩"),
                ("/mw查询 玩家名 职业 蜘蛛", "按职业筛选"),
                ("/mw查询 玩家名 翻页 2", "翻页（每页5场）"),
                ("/mw详情 比赛ID", "查看比赛详情"),
                ("/mw详情 玩家名 1", "查玩家最近第N场详情"),
                ("/职业查询", "全职业数据统计"),
                ("/职业查询 蜘蛛 2026.7.1 2026.7.20", "按职业+日期区间查询"),
            ],
        ),
        (
            "� 每日数据",
            [
                ("/昨日职业数据", "昨日所有职业数据表格"),
                ("/昨日表现 玩家名", "玩家昨日表现（职业分布+高光对局）"),
                ("/今日表现 玩家名", "玩家今日表现（同上）"),
            ],
        ),
        (
            "�💰 关鹏币 & 签到",
            [
                ("/签到", "每日签到 +30关鹏币，含战绩卡片"),
                ("/关鹏币", "查看余额"),
                ("/绑定 游戏ID", "绑定游戏账号"),
                ("/解绑", "解除绑定"),
            ],
        ),
        (
            "� 个性化",
            [
                ("/签到背景", "发指令后30秒内发一张图→设为签到背景"),
                ("/查询背景", "同上，设为查询背景"),
            ],
        ),
    ]
    rows = ""
    for title, cmds in sections:
        rows += f'<tr class="section"><td colspan="2">{title}</td></tr>'
        for cmd, desc in cmds:
            rows += f"<tr><td><code>{cmd}</code></td><td>{desc}</td></tr>"

    html = f"""<html><head><meta charset="UTF-8"><style>
body{{margin:0;padding:0;font-family:'Microsoft YaHei',sans-serif;background:linear-gradient(135deg,#1a1a2e,#16213e);color:#e0e0e0;}}
.container{{padding:18px;width:520px;}}
h1{{text-align:center;color:#FFD700;font-size:22px;margin:0 0 14px 0;text-shadow:-1px -1px 0 #000,1px -1px 0 #000,-1px 1px 0 #000,1px 1px 0 #000;}}
table{{width:100%;border-collapse:collapse;font-size:13px;}}
td{{padding:6px 8px;border-bottom:1px solid rgba(255,255,255,0.06);vertical-align:top;}}
code{{color:#4fc3f7;font-size:12px;white-space:nowrap;}}
.section td{{color:#69f0ae;font-weight:bold;font-size:14px;padding-top:12px;border-bottom:1px solid rgba(105,240,174,0.3);}}
.footer{{text-align:center;color:#757575;font-size:11px;margin-top:14px;}}
</style></head><body><div class="container">
<h1>📖 关鹏机器人使用说明书</h1>
<table>{rows}</table>
<p class="footer">关鹏机器人 v1.0</p>
</div></body></html>"""

    output_dir = os.path.abspath("./tmp_help_images")
    os.makedirs(output_dir, exist_ok=True)
    hti = Html2Image(
        output_path=output_dir,
        custom_flags=[
            "--enable-local-file-access",
            "--hide-scrollbars",
            "--width=520",
        ],
    )
    hti.screenshot(html_str=html, save_as="help.png")
    img_path = os.path.join(output_dir, "help.png")
    cropped_path = os.path.join(output_dir, "help_cropped.png")
    if os.path.exists(img_path):
        final = (
            cropped_path if self.crop_image(img_path, cropped_path, 99) else img_path
        )
        yield event.image_result(final)
    else:
        yield event.plain_result("说明书生成失败")


# ── 绑定 & 解绑 ──────────────────────────────────────────


@filter.command(command_name("bind_account"))
async def bind_account(self, event: AstrMessageEvent, game_id: str):
    user_id = event.get_sender_id()
    user_name = event.get_sender_name()
    if not game_id:
        yield event.plain_result("请输入要绑定的游戏ID")
        return
    test = await self.get_match_list_by_name(game_id, limit=1)
    if not test:
        yield event.plain_result(
            f"没有找到游戏ID {game_id} 的比赛记录，请检查ID是否正确"
        )
        return
    if self.config.get_bind(user_id):
        yield event.plain_result(
            f"您已经绑定了游戏ID {self.config.get_bind(user_id)}，请先解绑"
        )
        return
    self.config.set_bind(user_id, game_id)
    yield event.plain_result(
        f"{user_name} 成功绑定游戏ID {game_id}\n现在您可以使用签到等功能了"
    )


@filter.command(command_name("unbind_account"))
async def unbind_account(self, event: AstrMessageEvent):
    user_id = event.get_sender_id()
    user_name = event.get_sender_name()
    game_id = self.config.get_bind(user_id)
    if not game_id:
        yield event.plain_result("您还没有绑定任何游戏ID")
        return
    self.config.remove_bind(user_id)
    yield event.plain_result(f"{user_name} 已成功解绑游戏ID {game_id}")


# ── 背景设置（本地图片版，免关鹏币） ──────────────────────


@filter.command(command_name("set_sign_bg"))
async def set_sign_bg(self, event: AstrMessageEvent):
    """设置签到背景：发指令后30秒内发送一张图片即可。"""
    yield event.plain_result("请在 30 秒内发送一张图片作为签到背景")
    result = await self._wait_and_save_bg(event, "sign")
    yield event.plain_result(result)


@filter.command(command_name("set_query_bg"))
async def set_query_bg(self, event: AstrMessageEvent):
    """设置查询背景：发指令后30秒内发送一张图片即可。"""
    yield event.plain_result("请在 30 秒内发送一张图片作为查询背景")
    result = await self._wait_and_save_bg(event, "query")
    yield event.plain_result(result)


# ── 职业查询 ──────────────────────────────────────────────


@filter.command(command_name("query_kit_stats"))
async def query_kit_stats(
    self, event: AstrMessageEvent, arg1: str = "", arg2: str = "", arg3: str = ""
):
    """查询职业统计数据（基于对局数据库缓存）。"""
    import re

    args = [a for a in (arg1, arg2, arg3) if a]
    date_pattern = re.compile(r"^\d{4}\.\d{1,2}\.\d{1,2}$")
    dates = [a for a in args if date_pattern.match(a)]
    kit_parts = [a for a in args if not date_pattern.match(a)]
    kit_name = " ".join(kit_parts) if kit_parts else ""

    start_date = ""
    end_date = ""
    for d in dates:
        parts = d.split(".")
        iso = f"{parts[0]}-{parts[1].zfill(2)}-{parts[2].zfill(2)}"
        if not start_date:
            start_date = iso
        elif not end_date:
            end_date = iso

    all_stats = query_class_stats(CLASS_STATS_CACHE, start_date, end_date)
    if not all_stats:
        yield event.plain_result("职业统计数据为空，请先启动关鹏网生成缓存")
        return

    if kit_name:
        all_stats = [s for s in all_stats if s["kit"] == kit_name]
        if not all_stats:
            yield event.plain_result(f"未找到职业「{kit_name}」的数据")
            return

    title = "全职业数据（全部时间）"
    if kit_name and start_date:
        title = f"{kit_name} ({start_date} ~ {end_date or '至今'})"
    elif kit_name:
        title = f"{kit_name}（全部时间）"
    elif start_date:
        title = f"全职业 ({start_date} ~ {end_date or '至今'})"

    plugin_dir = os.path.dirname(os.path.abspath(__file__))
    kit_img_dir = os.path.join(plugin_dir, "kit_img")

    cards_html = ""
    for s in all_stats:
        img_file = os.path.join(kit_img_dir, f"{s['kit']}.png")
        cards_html += f"""<div class="card">
<div class="card-header"><img src="file://{img_file}" class="card-img" onerror="this.style.display='none'"><span class="card-name">{s["kit"]}</span></div>
<div class="card-stats">
<div class="stat"><span class="stat-num">{s["apps"]}</span><span class="stat-label">出场</span></div>
<div class="stat"><span class="stat-num" style="color:#FFD700">{s["appearance_rate"]}%</span><span class="stat-label">出场率</span></div>
<div class="stat"><span class="stat-num">{s["win_rate"]}%</span><span class="stat-label">胜率</span></div>
<div class="stat"><span class="stat-num">{s["mvp_rate"]}%</span><span class="stat-label">MVP率</span></div>
</div>
<div class="card-stats">
<div class="stat"><span class="stat-num">{s["avg_final_kills"]}</span><span class="stat-label">击杀</span></div>
<div class="stat"><span class="stat-num">{s["avg_final_assists"]}</span><span class="stat-label">助攻</span></div>
<div class="stat"><span class="stat-num">{s["avg_damage"]}</span><span class="stat-label">伤害</span></div>
<div class="stat"><span class="stat-num">{s["avg_damage_taken"]}</span><span class="stat-label">承伤</span></div>
</div></div>"""

    cols = 2
    html = f"""<html><head><meta charset="UTF-8"><style>
body{{margin:0;padding:0;font-family:'Microsoft YaHei',sans-serif;background:linear-gradient(135deg,#1a1a2e,#16213e);color:#fff;}}
.container{{padding:16px;width:580px;box-sizing:border-box;}}
.title{{text-align:center;color:#FFD700;font-size:26px;margin:0 0 18px 0;text-shadow:-1px -1px 0 #000,1px -1px 0 #000,-1px 1px 0 #000,1px 1px 0 #000;}}
.grid{{display:grid;grid-template-columns:repeat({cols},1fr);gap:10px;}}
.card{{background:rgba(255,255,255,0.06);border-radius:10px;padding:12px;border:1px solid rgba(255,255,255,0.1);}}
.card-header{{display:flex;align-items:center;gap:8px;margin-bottom:10px;padding-bottom:8px;border-bottom:1px solid rgba(79,195,247,0.3);}}
.card-img{{width:36px;height:36px;border-radius:5px;border:2px solid #4fc3f7;object-fit:cover;}}
.card-name{{font-size:16px;color:#4fc3f7;font-weight:bold;}}
.card-stats{{display:grid;grid-template-columns:1fr 1fr 1fr 1fr;gap:4px;margin-bottom:6px;}}
.stat{{text-align:center;}}
.stat-num{{display:block;font-size:15px;font-weight:bold;color:#e0e0e0;}}
.stat-label{{display:block;font-size:11px;color:#888;margin-top:1px;}}
</style></head><body><div class="container">
<h1 class="title">{title}</h1>
<div class="grid">{cards_html}</div>
</div></body></html>"""

    output_dir = os.path.abspath("./tmp_kit_stats_images")
    os.makedirs(output_dir, exist_ok=True)
    rows = (len(all_stats) + cols - 1) // cols
    card_h = 130
    h = 80 + card_h * rows
    if h < 250:
        h = 250

    hti = Html2Image(
        output_path=output_dir,
        custom_flags=[
            "--enable-local-file-access",
            "--hide-scrollbars",
            "--width=580",
            f"--height={h}",
            "--force-device-scale-factor=1.5",
        ],
    )
    hti.screenshot(html_str=html, save_as="kit_stats.png", size=(580, h))
    img_path = os.path.join(output_dir, "kit_stats.png")
    cropped_path = os.path.join(output_dir, "kit_stats_cropped.png")
    if os.path.exists(img_path):
        final = (
            cropped_path if self.crop_image(img_path, cropped_path, 20) else img_path
        )
        yield event.image_result(final)
    else:
        yield event.plain_result("图片生成失败")


# ── 昨日职业数据 ──────────────────────────────────────────


@filter.command(command_name("yesterday_class_stats"))
async def yesterday_class_stats(self, event: AstrMessageEvent):
    """查看昨日所有职业的数据表格。"""
    yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    all_stats = query_class_stats(CLASS_STATS_CACHE, yesterday, yesterday)
    if not all_stats:
        yield event.plain_result(f"昨日（{yesterday}）暂无职业数据，请等待缓存更新")
        return

    plugin_dir = os.path.dirname(os.path.abspath(__file__))
    kit_img_dir = os.path.join(plugin_dir, "kit_img")
    bg_path = self._get_bg_path(event.get_sender_id(), "query")
    bg_css = f"url('file://{bg_path}')" if bg_path else "none"

    rows_html = ""
    for s in all_stats:
        img_file = os.path.join(kit_img_dir, f"{s['kit']}.png")
        rows_html += f"""<tr>
<td><div class="kit-cell"><img src="file://{img_file}" onerror="this.style.display='none'"><span>{s["kit"]}</span></div></td>
<td>{s["apps"]}</td><td>{s["appearance_rate"]}%</td><td>{s["win_rate"]}%</td>
<td>{s["mvp_rate"]}%</td><td>{s["avg_final_kills"]}</td><td>{s["avg_final_assists"]}</td>
<td>{s["avg_damage"]}</td><td>{s["avg_damage_taken"]}</td></tr>"""

    html = f"""<html><head><meta charset="UTF-8"><style>
body{{margin:0;padding:16px;font-family:'Microsoft YaHei',sans-serif;background:{bg_css} no-repeat center/cover;background-attachment:fixed;min-height:100vh;}}
.container{{max-width:680px;margin:0 auto;}}
.glass{{background:rgba(20,20,40,0.7);backdrop-filter:blur(14px);-webkit-backdrop-filter:blur(14px);border:1px solid rgba(255,255,255,0.12);border-radius:14px;padding:18px;}}
.title{{text-align:center;font-size:24px;color:#FFD700;margin:0 0 16px 0;text-shadow:-1px -1px 0 #000,1px -1px 0 #000,-1px 1px 0 #000,1px 1px 0 #000;}}
.subtitle{{text-align:center;color:#9e9e9e;font-size:13px;margin:-10px 0 14px 0;}}
.table-wrap{{overflow-x:auto;}}
table{{width:100%;border-collapse:collapse;font-size:13px;color:#e0e0e0;}}
th{{background:rgba(79,195,247,0.2);color:#4fc3f7;padding:8px 6px;text-align:center;font-weight:normal;font-size:12px;white-space:nowrap;border-bottom:1px solid rgba(79,195,247,0.3);}}
td{{padding:7px 6px;text-align:center;border-bottom:1px solid rgba(255,255,255,0.06);white-space:nowrap;}}
tr:hover td{{background:rgba(255,255,255,0.05);}}
.kit-cell{{display:flex;align-items:center;gap:6px;justify-content:center;}}
.kit-cell img{{width:28px;height:28px;border-radius:4px;border:2px solid #4fc3f7;object-fit:cover;}}
.kit-cell span{{color:#69f0ae;font-weight:bold;}}
</style></head><body><div class="container">
<div class="glass"><h1 class="title">昨日职业数据</h1>
<p class="subtitle">{yesterday}</p>
<div class="table-wrap"><table>
<thead><tr><th>职业</th><th>出场</th><th>出场率</th><th>胜率</th><th>MVP率</th><th>场均击杀</th><th>场均助攻</th><th>场均伤害</th><th>场均承伤</th></tr></thead>
<tbody>{rows_html}</tbody></table></div></div>
</div></body></html>"""

    output_dir = os.path.abspath("./tmp_daily_stats")
    os.makedirs(output_dir, exist_ok=True)
    h = (
        380 + len(all_stats) * 62
    )  # 基础 380 + 每行 62px（force-device-scale-factor=1.5 需要更宽裕）
    if h < 600:
        h = 600
    hti = Html2Image(
        output_path=output_dir,
        custom_flags=[
            "--enable-local-file-access",
            "--hide-scrollbars",
            "--width=780",
            f"--height={h}",
            "--force-device-scale-factor=1.5",
        ],
    )
    hti.screenshot(html_str=html, save_as="daily_stats.png", size=(780, h))
    img_path = os.path.join(output_dir, "daily_stats.png")
    cropped_path = os.path.join(output_dir, "daily_stats_cropped.png")
    if os.path.exists(img_path):
        final = (
            cropped_path if self.crop_image(img_path, cropped_path, 22) else img_path
        )
        yield event.image_result(final)
    else:
        yield event.plain_result("图片生成失败")


# ── 昨日表现 / 今日表现（共用核心逻辑） ──


def _render_player_daily_card(
    username: str,
    matches: list[dict],
    date_str: str,
    date_label: str,
    bg_path: str,
) -> str:
    """渲染玩家每日表现 HTML 卡片（matches 来自 API get_match_list_by_name）。"""
    from collections import defaultdict

    total = len(matches)
    wins = sum(1 for m in matches if m.get("team") == m.get("winner"))
    mvps = sum(1 for m in matches if m.get("mvp"))
    total_fk = sum(int(m.get("finalKills", 0) or 0) for m in matches)

    kit_stats: dict[str, dict] = defaultdict(
        lambda: {"apps": 0, "wins": 0, "fk": 0, "fa": 0, "dmg": 0, "dt": 0, "mvps": 0}
    )
    for m in matches:
        kit = m.get("selectedKit", "未知")
        ks = kit_stats[kit]
        ks["apps"] += 1
        if m.get("team") == m.get("winner"):
            ks["wins"] += 1
        if m.get("mvp"):
            ks["mvps"] += 1
        ks["fk"] += int(m.get("finalKills", 0) or 0)
        ks["fa"] += int(m.get("finalAssists", 0) or 0)
        ks["dmg"] += int(m.get("totalDamage", 0) or 0)
        ks["dt"] += int(m.get("takenDamage", 0) or 0)

    kit_rows = ""
    kit_list = sorted(kit_stats.items(), key=lambda x: x[1]["apps"], reverse=True)
    plugin_dir = os.path.dirname(os.path.abspath(__file__))
    kit_img_dir = os.path.join(plugin_dir, "kit_img")
    for kit, ks in kit_list:
        wr = round(ks["wins"] / ks["apps"] * 100, 1) if ks["apps"] else 0
        avg_fk = round(ks["fk"] / ks["apps"], 1) if ks["apps"] else 0
        avg_fa = round(ks["fa"] / ks["apps"], 1) if ks["apps"] else 0
        avg_dmg = round(ks["dmg"] / ks["apps"]) if ks["apps"] else 0
        avg_dt = round(ks["dt"] / ks["apps"]) if ks["apps"] else 0
        mvp_rate = round(ks["mvps"] / ks["wins"] * 100, 1) if ks["wins"] else 0
        img_file = os.path.join(kit_img_dir, f"{kit}.png")
        kit_rows += f"""<tr>
<td><div class="kit-cell"><img src="file://{img_file}" onerror="this.style.display='none'"><span>{kit}</span></div></td>
<td>{ks["apps"]}</td><td>{wr}%</td><td>{ks["mvps"]}</td><td>{mvp_rate}%</td><td>{avg_fk}</td><td>{avg_fa}</td><td>{avg_dmg}</td><td>{avg_dt}</td></tr>"""

    highlights = [
        m
        for m in matches
        if int(m.get("finalKills", 0) or 0) >= 8
        or m.get("mvp")
        or int(m.get("totalDamage", 0) or 0) >= 1000
    ]
    hl_html = ""
    if highlights:
        hl_cards = ""
        for m in highlights[:12]:
            won = m.get("team") == m.get("winner")
            is_draw = m.get("winner") == "NONE"
            result_color = "#69f0ae" if won else ("#ffeb3b" if is_draw else "#ff5252")
            result_text = "胜" if won else ("平" if is_draw else "负")
            mvp_badge = '<span class="mvp-badge">MVP</span>' if m.get("mvp") else ""
            raw_ts = m.get("endTime", 0)
            try:
                time_str = datetime.fromtimestamp(int(raw_ts) / 1000).strftime("%H:%M")
            except (ValueError, TypeError, OSError):
                time_str = ""
            fk = int(m.get("finalKills", 0) or 0)
            fa = int(m.get("finalAssists", 0) or 0)
            dmg = int(m.get("totalDamage", 0) or 0)
            hl_cards += f"""<div class="hl-card">
<div class="hl-top"><span class="hl-map">{m.get("mapName", "?")}</span><span class="hl-time">{time_str}</span><span class="hl-result" style="color:{result_color}">{result_text}</span>{mvp_badge}</div>
<div class="hl-kit">{m.get("selectedKit", "?")}</div>
<div class="hl-nums"><span>⚔{fk}</span><span>🤝{fa}</span><span>💥{dmg}</span></div>
</div>"""
        hl_html = f"""<div class="section-title">⭐ 高光对局</div>
<div class="hl-grid">{hl_cards}</div>"""

    win_rate = round(wins / total * 100, 1) if total > 0 else 0
    mvp_rate_total = round(mvps / wins * 100, 1) if wins else 0
    bg_css = f"url('file://{bg_path}')" if bg_path else "none"

    html = f"""<html><head><meta charset="UTF-8"><style>
body{{margin:0;padding:14px;font-family:'Microsoft YaHei',sans-serif;background:{bg_css} no-repeat center/cover;background-attachment:fixed;min-height:100vh;}}
.container{{max-width:720px;margin:0 auto;}}
.glass{{background:rgba(20,20,40,0.7);backdrop-filter:blur(14px);-webkit-backdrop-filter:blur(14px);border:1px solid rgba(255,255,255,0.12);border-radius:14px;padding:16px;margin-bottom:12px;}}
.title{{text-align:center;font-size:22px;color:#FFD700;margin:0 0 4px 0;text-shadow:-1px -1px 0 #000,1px -1px 0 #000,-1px 1px 0 #000,1px 1px 0 #000;}}
.subtitle{{text-align:center;color:#9e9e9e;font-size:12px;margin:0 0 14px 0;}}
.summary{{display:grid;grid-template-columns:1fr 1fr 1fr 1fr 1fr 1fr;gap:10px;margin-bottom:6px;}}
.sum-item{{text-align:center;}}
.sum-num{{font-size:22px;font-weight:bold;color:#FFD700;}}
.sum-label{{font-size:11px;color:#888;margin-top:2px;}}
.section-title{{color:#4fc3f7;font-size:15px;margin:14px 0 8px 0;padding-bottom:6px;border-bottom:1px solid rgba(79,195,247,0.3);}}
table{{width:100%;border-collapse:collapse;font-size:11px;color:#e0e0e0;}}
th{{background:rgba(79,195,247,0.2);color:#4fc3f7;padding:7px 3px;text-align:center;font-weight:normal;font-size:10px;border-bottom:1px solid rgba(79,195,247,0.3);}}
td{{padding:6px 3px;text-align:center;border-bottom:1px solid rgba(255,255,255,0.06);}}
tr:hover td{{background:rgba(255,255,255,0.05);}}
.kit-cell{{display:flex;align-items:center;gap:4px;justify-content:center;}}
.kit-cell img{{width:24px;height:24px;border-radius:4px;border:2px solid #4fc3f7;object-fit:cover;}}
.kit-cell span{{color:#69f0ae;font-weight:bold;font-size:11px;}}
.hl-grid{{display:grid;grid-template-columns:1fr 1fr;gap:8px;}}
.hl-card{{background:rgba(255,255,255,0.06);border-radius:8px;padding:8px 10px;border:1px solid rgba(255,255,255,0.08);}}
.hl-top{{display:flex;align-items:center;gap:6px;margin-bottom:4px;}}
.hl-map{{color:#FFD700;font-size:12px;flex:1;}}
.hl-time{{color:#888;font-size:10px;}}
.hl-result{{font-weight:bold;font-size:12px;}}
.hl-kit{{color:#69f0ae;font-size:11px;margin-bottom:4px;}}
.hl-nums{{display:flex;gap:10px;font-size:12px;color:#e0e0e0;}}
.mvp-badge{{background:linear-gradient(135deg,#ffd700,#ff9800);color:#000;padding:1px 5px;border-radius:3px;font-size:10px;margin-left:4px;}}
</style></head><body><div class="container">
<div class="glass"><h1 class="title">{username} 的{date_label}表现</h1>
<p class="subtitle">{date_str} · 共 {total} 场</p>
<div class="summary">
<div class="sum-item"><div class="sum-num">{total}</div><div class="sum-label">总场次</div></div>
<div class="sum-item"><div class="sum-num">{win_rate}%</div><div class="sum-label">胜率</div></div>
<div class="sum-item"><div class="sum-num">{wins}</div><div class="sum-label">胜利</div></div>
<div class="sum-item"><div class="sum-num">{mvps}</div><div class="sum-label">MVP</div></div>
<div class="sum-item"><div class="sum-num">{mvp_rate_total}%</div><div class="sum-label">MVP率</div></div>
<div class="sum-item"><div class="sum-num">{total_fk}</div><div class="sum-label">总击杀</div></div>
</div></div>
<div class="glass"><div class="section-title">📊 职业分布</div>
<table><thead><tr><th>职业</th><th>场次</th><th>胜率</th><th>MVP次数</th><th>MVP率</th><th>场均击杀</th><th>场均助攻</th><th>场均伤害</th><th>场均承伤</th></tr></thead>
<tbody>{kit_rows}</tbody></table></div>
<div class="glass">{hl_html}</div>
</div></body></html>"""
    return html


def _calc_height(kit_count: int, hl_count: int) -> int:
    """计算卡片截图高度。"""
    h = 310 + kit_count * 36 + ((hl_count + 1) // 2) * 68 + 100
    return max(h, 480)


@filter.command(command_name("yesterday_performance"))
async def yesterday_performance(self, event: AstrMessageEvent, username: str = ""):
    """查看玩家昨日表现 — 通过 API 实时查询（职业分布 + 高光对局）。"""
    if not username:
        user_id = event.get_sender_id()
        username = self.config.get_bind(user_id)
        if not username:
            yield event.plain_result(
                "请先使用 /绑定 游戏ID 绑定账号，或直接指定玩家名\n例如: /昨日表现 xxx"
            )
            return

    yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")

    all_matches = await self.get_match_list_by_name(username, limit=100)
    if not all_matches:
        yield event.plain_result(f"未找到玩家 {username} 的比赛记录")
        return

    matches = [
        m
        for m in all_matches
        if self._format_timestamp(m.get("endTime", 0)).startswith(yesterday)
    ]
    if not matches:
        yield event.plain_result(f"未找到 {username} 在昨日（{yesterday}）的对局记录")
        return

    self.match_db.collect_from_player_matches(all_matches)

    bg_path = self._get_bg_path(event.get_sender_id(), "query")
    html = _render_player_daily_card(username, matches, yesterday, "昨日", bg_path)

    output_dir = os.path.abspath("./tmp_daily_perf")
    os.makedirs(output_dir, exist_ok=True)
    hl_count = min(
        len(
            [
                m
                for m in matches
                if int(m.get("finalKills", 0) or 0) >= 8
                or m.get("mvp")
                or int(m.get("totalDamage", 0) or 0) >= 1000
            ]
        ),
        12,
    )
    kit_count = len({m.get("selectedKit", "?") for m in matches})
    h = _calc_height(kit_count, hl_count)

    hti = Html2Image(
        output_path=output_dir,
        custom_flags=[
            "--enable-local-file-access",
            "--hide-scrollbars",
            "--width=720",
            f"--height={h}",
            "--force-device-scale-factor=1.5",
        ],
    )
    hti.screenshot(html_str=html, save_as="daily_perf.png", size=(720, h))
    img_path = os.path.join(output_dir, "daily_perf.png")
    cropped_path = os.path.join(output_dir, "daily_perf_cropped.png")
    if os.path.exists(img_path):
        final = (
            cropped_path if self.crop_image(img_path, cropped_path, 20) else img_path
        )
        yield event.image_result(final)
    else:
        yield event.plain_result("图片生成失败")


@filter.command(command_name("today_performance"))
async def today_performance(self, event: AstrMessageEvent, username: str = ""):
    """查看玩家今日表现 — 通过 API 实时查询（职业分布 + 高光对局）。"""
    if not username:
        user_id = event.get_sender_id()
        username = self.config.get_bind(user_id)
        if not username:
            yield event.plain_result(
                "请先使用 /绑定 游戏ID 绑定账号，或直接指定玩家名\n例如: /今日表现 xxx"
            )
            return

    today = datetime.now().strftime("%Y-%m-%d")

    all_matches = await self.get_match_list_by_name(username, limit=100)
    if not all_matches:
        yield event.plain_result(f"未找到玩家 {username} 的比赛记录")
        return

    matches = [
        m
        for m in all_matches
        if self._format_timestamp(m.get("endTime", 0)).startswith(today)
    ]
    if not matches:
        yield event.plain_result(f"未找到 {username} 在今日（{today}）的对局记录")
        return

    self.match_db.collect_from_player_matches(all_matches)

    bg_path = self._get_bg_path(event.get_sender_id(), "query")
    html = _render_player_daily_card(username, matches, today, "今日", bg_path)

    output_dir = os.path.abspath("./tmp_daily_perf")
    os.makedirs(output_dir, exist_ok=True)
    hl_count = min(
        len(
            [
                m
                for m in matches
                if int(m.get("finalKills", 0) or 0) >= 8
                or m.get("mvp")
                or int(m.get("totalDamage", 0) or 0) >= 1000
            ]
        ),
        12,
    )
    kit_count = len({m.get("selectedKit", "?") for m in matches})
    h = _calc_height(kit_count, hl_count)

    hti = Html2Image(
        output_path=output_dir,
        custom_flags=[
            "--enable-local-file-access",
            "--hide-scrollbars",
            "--width=720",
            f"--height={h}",
            "--force-device-scale-factor=1.5",
        ],
    )
    hti.screenshot(html_str=html, save_as="today_perf.png", size=(720, h))
    img_path = os.path.join(output_dir, "today_perf.png")
    cropped_path = os.path.join(output_dir, "today_perf_cropped.png")
    if os.path.exists(img_path):
        final = (
            cropped_path if self.crop_image(img_path, cropped_path, 20) else img_path
        )
        yield event.image_result(final)
    else:
        yield event.plain_result("图片生成失败")
