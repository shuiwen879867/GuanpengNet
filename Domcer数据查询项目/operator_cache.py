"""Persisted per-day and all-time operator aggregates; no raw JSON in page requests."""
from contextlib import closing
from datetime import datetime, timedelta, timezone
import json
import math
import os
from pathlib import Path
import sqlite3
import threading

CHINA = timezone(timedelta(hours=8))
VERSION = 1


def number(value):
    try:
        value = float(value or 0)
        return value if math.isfinite(value) else 0
    except (ValueError, TypeError):
        return 0


class OperatorCache:
    def __init__(self, source, cache_path=None):
        self.source = Path(source)
        self.path = Path(cache_path) if cache_path else self.source.with_name('class_players_cache.sqlite3')
        self.lock = threading.Lock()

    def connect(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        return db

    def rebuild(self, force=False):
        with self.lock:
            stat = self.source.stat()
            signature = f'{VERSION}:{stat.st_mtime_ns}:{stat.st_size}'
            if not force and self.path.exists():
                try:
                    with closing(self.connect()) as db:
                        row = db.execute('SELECT signature FROM metadata').fetchone()
                    if row and row[0] == signature:
                        return False
                except sqlite3.Error:
                    pass
            # Parse once for all classes, entirely outside the publication transaction.
            with self.source.open(encoding='utf-8') as stream:
                stat = os.fstat(stream.fileno())
                signature = f'{VERSION}:{stat.st_mtime_ns}:{stat.st_size}'
                payload = json.load(stream)
                if not isinstance(payload, dict) or not isinstance(payload.get('matches'), dict):
                    raise ValueError('invalid match database')
                matches = payload['matches']
            daily, totals = {}, {}
            for match in matches.values():
                if not isinstance(match, dict):
                    continue
                try:
                    day = datetime.fromtimestamp(float(match.get('startTime', 0)) / 1000, CHINA).date().isoformat()
                except (ValueError, TypeError, OSError, OverflowError):
                    continue
                seen = set()
                for player in match.get('playerStatistics', []):
                    if not isinstance(player, dict):
                        continue
                    kit, name = player.get('selectedKit'), player.get('realName')
                    if not isinstance(kit, str) or not kit or not isinstance(name, str) or not name.strip():
                        continue
                    name = name.strip()
                    identity = name.casefold()
                    key = (kit, identity)
                    if key in seen:
                        continue
                    seen.add(key)
                    winner = match.get('winner')
                    values = [1, int(bool(winner and winner not in ('NONE', 'UNKNOWN') and player.get('team') == winner)),
                              int(bool(match.get('mvp') and str(match['mvp']).casefold() == identity)),
                              number(player.get('totalDamage')), number(player.get('takenDamage')),
                              number(player.get('finalKills')), number(player.get('finalAssists'))]
                    for target, group in ((daily, (kit, day, identity)), (totals, key)):
                        row = target.setdefault(group, [name, 0, 0, 0, 0, 0, 0, 0])
                        for index, value in enumerate(values, 1):
                            row[index] += value
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with closing(self.connect()) as db:
                db.execute('PRAGMA journal_mode=WAL')
                db.executescript('''
                    CREATE TABLE IF NOT EXISTS daily (
                        kit TEXT, day TEXT, identity TEXT, name TEXT,
                        games INTEGER, wins INTEGER, mvps INTEGER,
                        damage REAL, taken REAL, fk REAL, fa REAL,
                        PRIMARY KEY(kit,day,identity));
                    CREATE TABLE IF NOT EXISTS totals (
                        kit TEXT, identity TEXT, name TEXT,
                        games INTEGER, wins INTEGER, mvps INTEGER,
                        damage REAL, taken REAL, fk REAL, fa REAL,
                        PRIMARY KEY(kit,identity));
                    CREATE TABLE IF NOT EXISTS metadata (signature TEXT, updated_at TEXT);
                ''')
                # Readers see the preceding complete snapshot until commit; failure rolls back.
                with db:
                    db.execute('DELETE FROM daily')
                    db.execute('DELETE FROM totals')
                    db.execute('DELETE FROM metadata')
                    db.executemany('INSERT INTO daily VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                                   (key + tuple(row) for key, row in daily.items()))
                    db.executemany('INSERT INTO totals VALUES (?,?,?,?,?,?,?,?,?,?)',
                                   (key + tuple(row) for key, row in totals.items()))
                    db.execute('INSERT INTO metadata VALUES (?,?)', (signature, datetime.now(CHINA).isoformat()))
            return True

    def query(self, kit, start='', end=''):
        if not self.path.exists():
            raise FileNotFoundError(self.path)
        with closing(self.connect()) as db:
            if not start and not end:
                rows = db.execute('SELECT * FROM totals WHERE kit=?', (kit,)).fetchall()
            else:
                clauses, params = ['kit=?'], [kit]
                if start:
                    clauses.append('day>=?')
                    params.append(start)
                if end:
                    clauses.append('day<=?')
                    params.append(end)
                rows = db.execute('''SELECT identity, min(name) AS name, sum(games) AS games,
                    sum(wins) AS wins, sum(mvps) AS mvps, sum(damage) AS damage,
                    sum(taken) AS taken, sum(fk) AS fk, sum(fa) AS fa FROM daily WHERE ''' +
                                  ' AND '.join(clauses) + ' GROUP BY identity', params).fetchall()
        result = []
        for record in rows:
            row = dict(record)
            row['win_rate'] = round(row['wins'] / row['games'] * 100, 1)
            for field in ('damage', 'taken', 'fk', 'fa'):
                row['avg_' + field] = round(row[field] / row['games'], 1)
            result.append(row)
        return sorted(result, key=lambda row: (-row['games'], -row['wins'], row['name'].casefold()))
