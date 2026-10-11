from datetime import datetime
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from contextlib import closing
import sqlite3
from urllib.parse import quote

from flask import Flask
from class_players import CHINA, register_class_players
from operator_cache import OperatorCache


class ClassPlayersTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name, 'matches.json')
        self.matches = {'one': {'startTime': int(datetime(2026, 9, 21, tzinfo=CHINA).timestamp() * 1000),
                                'winner': 'RED', 'mvp': 'Player00',
                                'playerStatistics': [dict(realName=f'Player{i:02}', selectedKit='僵尸', team='RED',
                                                          finalKills=2, finalAssists=3, totalDamage=100, takenDamage=40)
                                                     for i in range(55)]}}
        self.save()
        app = Flask(__name__, template_folder=str(Path(__file__).parent / 'templates'))
        app.add_url_rule('/', 'index', lambda: 'home')
        self.cache = register_class_players(app, self.path)
        self.client = app.test_client()

    def save(self):
        self.path.write_text(json.dumps({'matches': self.matches}), encoding='utf-8')
        if hasattr(self, 'cache'):
            self.cache.rebuild()

    def rows(self, start='', end=''):
        return self.cache.query('僵尸', start, end)

    def test_every_operator_is_accessible_with_pagination_and_links(self):
        page1 = self.client.get('/class_players?kit=僵尸').get_data(as_text=True)
        self.assertIn('55</strong>', page1)
        self.assertIn('Player49', page1)
        self.assertNotIn('Player50', page1)
        page2 = self.client.get('/class_players?kit=僵尸&page=2').get_data(as_text=True)
        self.assertIn('Player54', page2)
        self.assertIn('/?username=Player54', page2)
        self.assertIn('职业数据来自关鹏网统计，可能不全', page2)

    def test_stats_dates_duplicates_and_missing_values(self):
        players = self.matches['one']['playerStatistics']
        players.append(dict(players[0], realName='PLAYER00'))
        players.append(dict(players[0], realName='OtherKit', selectedKit='蜘蛛'))
        self.matches['two'] = {'startTime': self.matches['one']['startTime'] - 1,
                               'playerStatistics': [dict(realName='Player00', selectedKit='僵尸', finalKills=None)]}
        self.save()
        row = self.rows()[0]
        self.assertEqual((row['games'], row['wins'], row['mvps'], row['avg_fk']), (2, 1, 1, 1))
        self.assertEqual(row['win_rate'], 50)
        self.assertEqual(self.rows('2026-09-21', '2026-09-21')[0]['games'], 1)
        self.assertEqual(len(self.rows()), 55)

    def test_filter_validation_empty_and_escaping(self):
        html = self.client.get('/class_players?kit=僵尸&player=pLaYeR54').get_data(as_text=True)
        self.assertIn('Player54', html)
        self.assertNotIn('Player00', html)
        for query in ('kit=僵尸&start=bad', 'kit=僵尸&start=2026-09-22&end=2026-09-21', 'kit=僵尸&page=0', ''):
            self.assertEqual(self.client.get('/class_players?' + query).status_code, 400)
        self.assertIn('没有符合条件', self.client.get('/class_players?kit=未收录').get_data(as_text=True))
        self.matches['one']['playerStatistics'][0]['realName'] = '<img src=x onerror=alert(1)>'
        self.save()
        html = self.client.get('/class_players?kit=僵尸').get_data(as_text=True)
        self.assertIn('&lt;img', html)
        self.assertNotIn('<img src=x', html)

    def test_database_changes_invalidate_cache(self):
        self.assertEqual(len(self.rows()), 55)
        self.matches['one']['playerStatistics'].append(dict(realName='NewPlayer', selectedKit='僵尸'))
        self.save()
        self.assertEqual(len(self.rows()), 56)

    def test_queries_and_warm_restart_never_parse_raw_data(self):
        with patch('operator_cache.json.load', side_effect=AssertionError('unexpected raw read')):
            self.assertEqual(len(self.rows()), 55)
            self.assertEqual(len(self.rows('2026-09-21', '2026-09-21')), 55)
            restarted = OperatorCache(self.path)
            self.assertFalse(restarted.rebuild())
            self.assertEqual(len(restarted.query('僵尸')), 55)

    def test_failed_parse_keeps_last_snapshot(self):
        self.path.write_text('{broken json', encoding='utf-8')
        with self.assertRaises(ValueError):
            self.cache.rebuild()
        self.assertEqual(len(self.rows()), 55)

    def test_failed_publish_rolls_back_and_next_refresh_recovers(self):
        with closing(self.cache.connect()) as db:
            db.execute("CREATE TRIGGER fail_publish BEFORE INSERT ON totals BEGIN SELECT RAISE(ABORT,'test failure'); END")
            db.commit()
        self.matches['one']['playerStatistics'].append(dict(realName='NewPlayer', selectedKit='僵尸'))
        self.path.write_text(json.dumps({'matches': self.matches}), encoding='utf-8')
        with self.assertRaises(sqlite3.Error):
            self.cache.rebuild()
        self.assertEqual(len(self.rows()), 55)
        with closing(self.cache.connect()) as db:
            db.execute('DROP TRIGGER fail_publish')
            db.commit()
        self.assertTrue(self.cache.rebuild())
        self.assertEqual(len(self.rows()), 56)


if __name__ == '__main__':
    unittest.main()
