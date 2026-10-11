"""All recorded operators for a class, using the shared match database."""
from datetime import date, datetime, timedelta, timezone
import sqlite3
from operator_cache import OperatorCache

from flask import Blueprint, render_template, request

CHINA = timezone(timedelta(hours=8))


def register_class_players(app, db_path):
    cache = OperatorCache(db_path)
    try:
        cache.rebuild()
    except (OSError, ValueError, sqlite3.Error):
        app.logger.exception('操作者统计预处理失败，将保留上次成功缓存')
    app.extensions['operator_cache'] = cache
    bp = Blueprint('class_players', __name__)

    @bp.get('/class_players')
    def page():
        kit = request.args.get('kit', '').strip()
        start, end = request.args.get('start', ''), request.args.get('end', '')
        search = request.args.get('player', '').strip()
        page_number = request.args.get('page', 1, type=int)
        error, rows, total = '', [], 0
        status = 200
        try:
            for value in (start, end):
                if value and date.fromisoformat(value).isoformat() != value:
                    raise ValueError()
            if start and end and start > end:
                raise ValueError()
            if not kit or len(kit) > 60 or len(search) > 100 or page_number < 1:
                raise ValueError()
        except ValueError:
            error, status = '请填写职业名称、有效日期区间和页码。', 400
        if not error:
            try:
                all_rows = cache.query(kit, start, end)
                total = len(all_rows)
                rows = [row for row in all_rows if search.casefold() in row['name'].casefold()]
            except FileNotFoundError:
                error = '尚未收录对局数据。'
            except (OSError, ValueError, AttributeError, TypeError, sqlite3.Error):
                error, status = '统计数据暂时无法读取，请稍后重试。', 503
        count = len(rows)
        pages = max(1, (count + 49) // 50)
        page_number = min(page_number, pages)
        offset = (page_number - 1) * 50
        return render_template('class_players.html', kit=kit, start=start, end=end, search=search,
                               players=rows[offset:offset + 50], total=total, count=count,
                               page=page_number, pages=pages, offset=offset, error=error), status

    app.register_blueprint(bp)
    return cache
