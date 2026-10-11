from flask import Flask, request, jsonify, render_template, redirect
import random
import requests
from collections import defaultdict
import logging
from datetime import datetime, timedelta
import json
import os
import sys
import asyncio
import threading
import time

import aiohttp

# 导入共享对局数据库和职业统计缓存
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "shared_data"))
from match_db import get_shared_db
from class_stats_cache import build_cache, query_cache, get_cache_info

# 配置日志
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)
from local_proxy import LocalProxyFix
app.wsgi_app = LocalProxyFix(app.wsgi_app)




from guanpeng_runtime import API_BASE, API_KEYS, data_dir
class MegaWallsStats:
    def __init__(self):
        self.api_base = API_BASE
        self.api_key = list(API_KEYS)
        self.headers = {"Content-Type": "application/json"}
        self.raw_data = self.load_raw_data()

    def load_raw_data(self):
            """加载原始比赛数据"""
            try:
                if os.path.exists('raw_data.json'):
                    with open('raw_data.json', 'r', encoding='utf-8') as f:
                        return json.load(f)
                else:
                    logger.warning("raw_data.json 文件不存在")
                    return []
            except Exception as e:
                logger.error(f"加载原始数据失败: {str(e)}")
                return []
    
    def get_all_kit_stats(self, days_filter='all'):
        """获取所有职业的统计数据"""
        try:
            filtered_matches = self._filter_matches_by_time(self.raw_data, days_filter)
            kit_stats = {}
            total_matches = len(filtered_matches)
            
            logger.info(f"开始统计职业数据，筛选后比赛场次: {total_matches}")
            
            for match in filtered_matches:
                winner = match.get('winner')
                mvp_player = match.get('mvp')
                
                for player in match.get('playerStatistics', []):
                    kit = player.get('selectedKit', '未知职业')
                    player_name = player.get('realName', '未知玩家')
                    team = player.get('team')
                    
                    if kit not in kit_stats:
                        kit_stats[kit] = {
                            'total_games': 0,
                            'wins': 0,
                            'mvps': 0,
                            'total_final_kills': 0,
                            'total_final_assists': 0,
                            'total_damage': 0,
                            'total_damage_taken': 0,
                            'players': {}
                        }
                    
                    # 更新职业统计数据
                    kit_data = kit_stats[kit]
                    kit_data['total_games'] += 1
                    
                    if team == winner:
                        kit_data['wins'] += 1
                    
                    if player_name == mvp_player:
                        kit_data['mvps'] += 1
                    
                    kit_data['total_final_kills'] += player.get('finalKills', 0)
                    kit_data['total_final_assists'] += player.get('finalAssists', 0)
                    kit_data['total_damage'] += player.get('totalDamage', 0)
                    kit_data['total_damage_taken'] += player.get('takenDamage', 0)
                    
                    # 更新玩家数据
                    if player_name not in kit_data['players']:
                        kit_data['players'][player_name] = {
                            'games': 0,
                            'wins': 0,
                            'mvps': 0,
                            'total_final_kills': 0,
                            'total_final_assists': 0,
                            'total_damage': 0,
                            'total_damage_taken': 0
                        }
                    
                    player_data = kit_data['players'][player_name]
                    player_data['games'] += 1
                    if team == winner:
                        player_data['wins'] += 1
                    if player_name == mvp_player:
                        player_data['mvps'] += 1
                    player_data['total_final_kills'] += player.get('finalKills', 0)
                    player_data['total_final_assists'] += player.get('finalAssists', 0)
                    player_data['total_damage'] += player.get('totalDamage', 0)
                    player_data['total_damage_taken'] += player.get('takenDamage', 0)
            
            logger.info(f"统计完成，找到 {len(kit_stats)} 个职业")
            return kit_stats, total_matches
            
        except Exception as e:
            logger.error(f"获取职业统计数据失败: {str(e)}")
            return {}, 0
    
    def get_kit_leaderboard(self, kit_name, days_filter='all'):
        """获取特定职业的玩家排行榜"""
        try:
            kit_stats, total_matches = self.get_all_kit_stats(days_filter)
            
            logger.info(f"查询职业 {kit_name} 的排行榜，总比赛场次: {total_matches}")
            logger.info(f"可用的职业: {list(kit_stats.keys())}")
            
            if kit_name not in kit_stats:
                logger.warning(f"职业 {kit_name} 未找到")
                return [], total_matches
            
            players_data = []
            kit_data = kit_stats[kit_name]
            
            for player_name, stats in kit_data['players'].items():
                if stats['games'] > 0:  # 只统计有比赛的玩家
                    players_data.append({
                        'name': player_name,
                        'games': stats['games'],
                        'wins': stats['wins'],
                        'win_rate': (stats['wins'] / stats['games'] * 100) if stats['games'] > 0 else 0,
                        'mvps': stats['mvps'],
                        'mvp_rate': (stats['mvps'] / stats['wins'] * 100) if stats['wins'] > 0 else 0,
                        'avg_final_kills': stats['total_final_kills'] / stats['games'] if stats['games'] > 0 else 0,
                        'avg_final_assists': stats['total_final_assists'] / stats['games'] if stats['games'] > 0 else 0,
                        'avg_damage': stats['total_damage'] / stats['games'] if stats['games'] > 0 else 0,
                        'avg_damage_taken': stats['total_damage_taken'] / stats['games'] if stats['games'] > 0 else 0
                    })
            
            # 按场数排序，取前20
            players_data.sort(key=lambda x: x['games'], reverse=True)
            logger.info(f"找到 {len(players_data)} 名使用 {kit_name} 的玩家")
            return players_data[:20], total_matches
            
        except Exception as e:
            logger.error(f"获取职业排行榜失败: {str(e)}")
            return [], 0
    
    def _filter_matches_by_time(self, matches, days_filter):
        """根据时间筛选比赛"""
        if days_filter == 'all':
            return matches
        
        try:
            days = int(days_filter)
            # 获取当前时间戳（毫秒）
            from datetime import datetime, timedelta
            current_time = datetime.now()
            cutoff_time = current_time - timedelta(days=days)
            cutoff_timestamp = cutoff_time.timestamp() * 1000  # 转换为毫秒
            
            filtered_matches = []
            for match in matches:
                match_time = match.get('startTime', 0)
                # 确保时间戳是数值类型且大于筛选时间
                if isinstance(match_time, (int, float)) and match_time >= cutoff_timestamp:
                    filtered_matches.append(match)
            
            logger.info(f"时间筛选: {days}天, 筛选前: {len(matches)}场, 筛选后: {len(filtered_matches)}场")
            return filtered_matches
        except Exception as e:
            logger.error(f"时间筛选失败: {str(e)}")
            return matches

    # 添加新的路由
    @app.route('/kit_stats')
    def kit_stats():
        return redirect('/class_stats')

    def get_match_list_by_name(self, username, limit=20):
        """获取玩家比赛预览列表"""
        if not self.api_key:
            return None
        endpoint = "/match/getMegaWallsMatchList"
        params = {
            "name": username,
            "key": random.choice(self.api_key),
            "limit": limit
        }
        
        try:
            logger.info(f"收到来自IP {request.remote_addr} 的查询: {username}")
            response = requests.get(
                f"{self.api_base}{endpoint}",
                headers=self.headers,
                params=params,
                timeout=10,
                verify=True
            )
            response.raise_for_status()
            response_data = response.json()
            
            if response_data.get('status') == 200 and 'data' in response_data:
                processed_matches = []
                for match in response_data['data']:
                    processed = self._process_match_data(match)
                    processed_matches.append(processed)
                return processed_matches
            else:
                logger.error(f"API返回异常: {response_data.get('message', '未知错误')}")
                return None
                
        except Exception as e:
            logger.error(f"获取比赛列表失败: {str(e)}")
            return None
        
    def get_match_details(self, match_id):
        """获取完整比赛详情"""
        if not self.api_key:
            return None
        endpoint = "/match/megawalls"
        params = {"id": match_id, "key": random.choice(self.api_key)}
        
        try:
            response = requests.get(
                f"{self.api_base}{endpoint}",
                headers=self.headers,
                params=params,
                timeout=10,
                verify=True
            )
            response.raise_for_status()
            match_data = response.json()
            return self._process_match_data(match_data, detailed=True)
            
        except Exception as e:
            logger.error(f"获取比赛详情失败: {str(e)}")
            return None 
    
    def _process_match_data(self, match_data, detailed=False):
        """处理比赛数据，计算额外指标"""
        processed = match_data.copy()

        if 'startTime' in match_data and 'endTime' in match_data:
            duration = (match_data['endTime'] - match_data['startTime']) / 60000
            processed['duration'] = f"{duration:.1f}分钟"
        else:
            processed['duration'] = "未知"

        return processed
    
    def _format_timestamp(self, timestamp):
        """将时间戳转换为可读格式"""
        if not timestamp:
            return "未知时间"
        try:
            return datetime.fromtimestamp(timestamp/1000).strftime('%Y-%m-%d %H:%M')
        except:
            return "时间格式错误"

# ── 职业数据统计（基于对局数据库，支持自定义日期区间） ──

@app.route('/class_stats')
def class_stats_page():
    """职业统计页面（新）"""
    info = get_cache_info(CLASS_STATS_CACHE)
    return render_template('class_stats.html', cache_info=info,
                           kit_image_map=KIT_IMAGE_MAP)


@app.route('/api/class_stats')
def api_class_stats():
    """查询职业统计数据，支持 start 和 end 日期参数。"""
    start = request.args.get('start', '')
    end = request.args.get('end', '')
    results = query_cache(CLASS_STATS_CACHE, start, end)
    info = get_cache_info(CLASS_STATS_CACHE)
    return jsonify({"stats": results, "cache_info": info})




@app.route('/api/kit/leaderboard')
def get_kit_leaderboard():
    try:
        kit_name = request.args.get('kit', '').strip()
        days_filter = request.args.get('days', 'all')
        
        if not kit_name:
            return jsonify({'error': '需要提供职业名称'}), 400
        
        logger.info(f"查询职业排行榜: {kit_name}, 时间筛选: {days_filter}")
        leaderboard, total_matches = mw_stats.get_kit_leaderboard(kit_name, days_filter)
        
        return jsonify({
            'kit_name': kit_name,
            'total_matches': total_matches,
            'leaderboard': leaderboard
        })
    except Exception as e:
        logger.error(f"获取职业排行榜失败: {str(e)}")
        return jsonify({'error': f'服务器错误: {str(e)}'}), 500

# 中文配置
TEAM_COLORS = {
    "GREEN": {"name": "绿队", "color": "#4CAF50", "text_color": "#fff"},
    "RED": {"name": "红队", "color": "#F44336", "text_color": "#fff"},
    "BLUE": {"name": "蓝队", "color": "#2196F3", "text_color": "#fff"},
    "YELLOW": {"name": "黄队", "color": "#FFEB3B", "text_color": "#000"},
    "UNKNOWN": {"name": "未知", "color": "#9E9E9E", "text_color": "#fff"},
    "NONE": {"name": "无", "color": "#9E9E9E", "text_color": "#fff"}
}

KIT_TRANSLATIONS = {
    "Spider": "Spider",
    "Arcanist": "Arcanist",
    "Golem": "Golem",
    "Shark": "Shark",
    "Shaman": "Shaman",
    "HIM": "HIM",
    "Squid": "Squid"
}

# 职业中文名 → 头像文件名映射（API 返回的是中文名，图片也在 static/kit_img/）
KIT_IMAGE_MAP = {
    "蜘蛛": "蜘蛛.png",
    "奥术师": "奥术师.png",
    "傀儡": "傀儡.png",
    "鲨鱼": "鲨鱼.png",
    "萨满": "萨满.png",
    "HIM": "HIM.png",
    "鱿鱼": "鱿鱼.png",
    "僵尸": "僵尸.png",
    "骷髅": "骷髅.png",
    "爬行者": "爬行者.png",
    "末影人": "末影人.png",
    "恐惧魔王": "恐惧魔王.png",
    "烈焰人": "烈焰人.png",
    "猪人": "猪人.png",
    "雪人": "雪人.png",
    "牛": "牛.png",
    "绵羊": "绵羊.png",
    "狼人": "狼人.png",
    "猎人": "猎人.png",
    "海盗": "海盗.png",
    "刺客": "刺客.png",
    "吸血鬼": "吸血鬼.png",
    "天使": "天使.png",
    "机器人": "机器人.png",
    "鼹鼠": "鼹鼠.png",
    "龙": "龙.png",
    "凤凰": "凤凰.png",
    "不法者": "不法者.png",
    "摩尔": "摩尔.png",
    "黑骑士": "黑骑士.png",
    "洞穴蜘蛛": "洞穴蜘蛛.png",
    "凋灵骑士": "凋灵骑士.png",
}

GAME_MODES = {
    "NORMAL": "标准模式",
    "FACE_OFF": "对决模式",
    "PRACTICE": "练习模式"
}

# 初始化服务
mw_stats = MegaWallsStats()
match_db = get_shared_db("web")  # 共享对局数据库

# 职业统计缓存路径
_shared_data_dir = str(data_dir("web"))
CLASS_STATS_CACHE = os.path.join(_shared_data_dir, "class_stats_cache.json")
MATCH_DB_PATH = os.path.join(_shared_data_dir, "match_db.json")
from class_players import register_class_players
operator_cache = register_class_players(app, MATCH_DB_PATH)

@app.route('/wiki.html')
def wiki():
    # If you have a dedicated wiki template:
    return render_template('wiki.html')

@app.route('/')
def index():
    return render_template('index.html', kit_image_map=KIT_IMAGE_MAP)

@app.route('/api/player/matches')
def get_player_matches():
    username = request.args.get('username')
    if not mw_stats.api_key:
        return jsonify({"error": "请在 config.json 中填写 domcer_api_keys 后重启"}), 503
    limit = request.args.get('limit', default=20, type=int)
    
    if not username:
        logger.error(f"来自IP {request.remote_addr} 的请求缺少用户名参数")
        return jsonify({"error": "请输入玩家名称"}), 400
    
    # 正常查询其他玩家
    matches = mw_stats.get_match_list_by_name(username, limit)
    if matches is None:
        return jsonify({"error": "获取比赛列表失败，请稍后再试"}), 500

    # 将未入库的对局 ID 加入待处理队列
    match_db.collect_from_player_matches(matches)

    return jsonify({
        "username": username,
        "matches": matches if matches else []
    })

@app.route('/match')
def view_match():
    match_id = request.args.get('id')
    if not match_id:
        logger.error(f"来自IP {request.remote_addr} 的请求缺少比赛ID参数")
        return render_template('error.html', message="需要提供比赛ID"), 400
    
    match_data = mw_stats.get_match_details(match_id)
    if not match_data:
        logger.error(f"来自IP {request.remote_addr} 获取比赛详情失败，ID: {match_id}")
        return render_template('error.html', message="获取比赛详情失败，请检查比赛ID是否正确"), 500
    
    # 调试：打印match_data结构
    print(f"match_data类型: {type(match_data)}")
    print(f"match_data keys: {match_data.keys() if isinstance(match_data, dict) else 'Not dict'}")
    
    # 检查数据结构并提取玩家数据
    players_data = []
    if isinstance(match_data, dict):
        # 如果match_data包含'data'字段，说明是原始API响应
        if 'data' in match_data:
            actual_match_data = match_data['data']
            players_data = actual_match_data.get('playerStatistics', [])
            # 更新match_data为实际数据，以便模板使用
            match_data = actual_match_data
        else:
            # 如果已经是处理过的数据，直接使用
            players_data = match_data.get('playerStatistics', [])
    
    print(f"处理后match_data keys: {match_data.keys() if isinstance(match_data, dict) else 'Not dict'}")
    print(f"玩家数量: {len(players_data)}")
    
    # 按队伍分组玩家
    players_by_team = defaultdict(list)
    for player in players_data:
        team = player.get('team', 'UNKNOWN')
        player.setdefault('realName', '未知玩家')
        player.setdefault('selectedKit', '未知职业')
        players_by_team[team].append(player)
    
    team_order = ["RED", "BLUE", "GREEN", "YELLOW", "UNKNOWN"]
    sorted_teams = sorted(
        players_by_team.items(),
        key=lambda x: team_order.index(x[0]) if x[0] in team_order else len(team_order)
    )
    
    return render_template(
        'match.html',
        match=match_data,
        teams=sorted_teams,
        team_colors=TEAM_COLORS,
        kit_translations=KIT_TRANSLATIONS,
        kit_image_map=KIT_IMAGE_MAP,
        game_modes=GAME_MODES
    )

def _start_match_processor():
    """Process backlog and refresh both statistics at startup and midnight in China."""
    from operator_cache import CHINA

    async def process_and_refresh():
        try:
            async with aiohttp.ClientSession() as session:
                pending = match_db.pending_count
                if pending > 0:
                    logger.info(f"[MatchProcessor] 处理积压: {pending} 场")
                    await match_db.process_pending(session)
        except Exception:
            logger.exception('[MatchProcessor] 对局更新失败，将刷新已有数据并在下次重试')
        for name, rebuild in (
            ('职业统计', lambda: build_cache(MATCH_DB_PATH, CLASS_STATS_CACHE)),
            ('职业操作者', operator_cache.rebuild),
        ):
            try:
                rebuild()
                logger.info(f'[MatchProcessor] {name}缓存已就绪')
            except Exception:
                logger.exception(f'[MatchProcessor] {name}预处理失败，将在下次重试')

    async def _run():
        await process_and_refresh()
        while True:
            now = datetime.now(CHINA)
            midnight = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
            wait_seconds = (midnight - now).total_seconds()
            logger.info(f"[MatchProcessor] 下次更新（北京时间）: {midnight:%Y-%m-%d %H:%M}")
            await asyncio.sleep(wait_seconds)
            # Refresh even when no pending matches: another process may have updated the source.
            await process_and_refresh()

    threading.Thread(target=lambda: asyncio.run(_run()), daemon=True, name='MatchProcessor').start()
    logger.info('[MatchProcessor] 后台线程已启动')


if os.environ.get("GUANPENG_BACKGROUND", "1") == "1":
    _start_match_processor()

@app.get('/health')
def health():
    return jsonify({"status": "ok", "api_configured": bool(mw_stats.api_key)})


if __name__ == '__main__':
    from waitress import serve
    serve(app, host=os.environ.get("WEB_HOST", "127.0.0.1"),
          port=int(os.environ.get("WEB_PORT", "5000")))
