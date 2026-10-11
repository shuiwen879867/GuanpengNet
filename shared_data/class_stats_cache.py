"""
职业统计缓存 — 预计算每日汇总，查询时按日期区间快速求和

数据流:
    match_db.json → 逐场统计 → daily 汇总 → class_stats_cache.json
    查询时: 筛选日期 → 累加 daily 行 → 计算平均值 → 返回

缓存结构:
{
    "last_match_count": 2458,
    "computed_at": "2026-07-25 03:19:46",
    "daily": {
        "2026-07-20": {
            "蜘蛛": {"apps":10,"wins":5,"mvps":2,"dmg":12345,"dt":9876,"fk":30,"fa":20},
            ...
        },
        ...
    }
}
"""

import json
import os
import time
import tempfile
from collections import defaultdict
from datetime import datetime


# 预计算时忽略的字段（减少缓存体积）
IGNORE_PLAYER_FIELDS = {
    "uuid",
    "kills",
    "deaths",
    "assists",
    "coins",
    "liveInDeathMatch",
    "attckSuccessful",
    "totalAttack",
    "arrowSuccessful",
    "totalArrow",
}


def build_cache(db_path: str, cache_path: str) -> dict:
    """从对局数据库重建职业统计缓存。返回缓存 dict。"""
    if not os.path.exists(db_path):
        return _empty_cache()

    with open(db_path, "r", encoding="utf-8") as f:
        db = json.load(f)

    matches = db.get("matches", {})
    total_count = len(matches)
    daily: dict[str, dict[str, dict]] = defaultdict(
        lambda: defaultdict(
            lambda: {
                "apps": 0,
                "wins": 0,
                "mvps": 0,
                "dmg": 0.0,
                "dt": 0.0,
                "fk": 0,
                "fa": 0,
            }
        )
    )

    for match_id, match in matches.items():
        winner = match.get("winner")
        mvp_name = match.get("mvp")
        start_ts = match.get("startTime", 0)

        try:
            day = datetime.fromtimestamp(int(start_ts) / 1000).strftime("%Y-%m-%d")
        except (ValueError, TypeError, OSError):
            continue

        for player in match.get("playerStatistics", []):
            kit = player.get("selectedKit", "未知")
            if not kit:
                continue

            row = daily[day][kit]
            row["apps"] += 1
            if player.get("team") == winner:
                row["wins"] += 1
            if player.get("realName") == mvp_name:
                row["mvps"] += 1
            row["dmg"] += player.get("totalDamage", 0) or 0
            row["dt"] += player.get("takenDamage", 0) or 0
            row["fk"] += int(player.get("finalKills", 0) or 0)
            row["fa"] += int(player.get("finalAssists", 0) or 0)

    # 转为普通 dict（可 JSON 序列化）
    daily_out = {day: dict(kits) for day, kits in daily.items()}

    cache = {
        "last_match_count": total_count,
        "computed_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "daily": daily_out,
    }

    # 写入缓存文件
    descriptor, temporary = tempfile.mkstemp(
        dir=os.path.dirname(cache_path), suffix=".tmp"
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False)
        os.replace(temporary, cache_path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)

    return cache


def query_cache(
    cache_path: str, start_date: str = "", end_date: str = ""
) -> list[dict]:
    """从缓存查询指定日期区间的职业统计。返回排序后的列表。"""
    if not os.path.exists(cache_path):
        return []

    with open(cache_path, "r", encoding="utf-8") as f:
        cache = json.load(f)

    daily = cache.get("daily", {})
    merged: dict[str, dict] = defaultdict(
        lambda: {
            "apps": 0,
            "wins": 0,
            "mvps": 0,
            "dmg": 0.0,
            "dt": 0.0,
            "fk": 0,
            "fa": 0,
        }
    )

    for day, kits in daily.items():
        if start_date and day < start_date:
            continue
        if end_date and day > end_date:
            continue
        for kit, stats in kits.items():
            m = merged[kit]
            for k in ("apps", "wins", "mvps", "dmg", "dt", "fk", "fa"):
                m[k] = m[k] + stats.get(k, 0)

    # 计算总出场数（用于出场率）
    total_apps = sum(m["apps"] for m in merged.values())

    result = []
    for kit, s in merged.items():
        apps = s["apps"]
        if apps == 0:
            continue
        result.append(
            {
                "kit": kit,
                "apps": apps,
                "appearance_rate": round(apps / total_apps * 100, 1)
                if total_apps
                else 0,
                "win_rate": round(s["wins"] / apps * 100, 1),
                "mvp_rate": round(s["mvps"] / max(s["wins"], 1) * 100, 1),
                "avg_damage": round(s["dmg"] / apps),
                "avg_damage_taken": round(s["dt"] / apps),
                "avg_final_kills": round(s["fk"] / apps, 1),
                "avg_final_assists": round(s["fa"] / apps, 1),
            }
        )

    result.sort(key=lambda x: x["apps"], reverse=True)
    return result


def get_cache_info(cache_path: str) -> dict:
    """获取缓存元信息。"""
    if not os.path.exists(cache_path):
        return {"exists": False, "match_count": 0}
    with open(cache_path, "r", encoding="utf-8") as f:
        cache = json.load(f)
    return {
        "exists": True,
        "match_count": cache.get("last_match_count", 0),
        "computed_at": cache.get("computed_at", ""),
        "days_covered": len(cache.get("daily", {})),
    }


def _empty_cache() -> dict:
    return {"last_match_count": 0, "computed_at": "", "daily": {}}
