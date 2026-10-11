import unittest

from flask import Flask, jsonify, request
from local_proxy import LocalProxyFix


class LocalProxyTests(unittest.TestCase):
    def setUp(self):
        app = Flask(__name__)
        app.wsgi_app = LocalProxyFix(app.wsgi_app)

        @app.get('/')
        def inspect_request():
            return jsonify(ip=request.remote_addr, secure=request.is_secure)

        self.client = app.test_client()
        self.headers = {'X-Forwarded-For': '198.51.100.25', 'X-Forwarded-Proto': 'https'}

    def test_local_nginx_forwarding(self):
        response = self.client.get('/', headers=self.headers, environ_overrides={'REMOTE_ADDR': '127.0.0.1'})
        self.assertEqual(response.json, {'ip': '198.51.100.25', 'secure': True})

    def test_external_client_cannot_spoof_forwarding(self):
        response = self.client.get('/', headers=self.headers, environ_overrides={'REMOTE_ADDR': '203.0.113.10'})
        self.assertEqual(response.json, {'ip': '203.0.113.10', 'secure': False})

    def test_direct_local_access(self):
        self.assertEqual(self.client.get('/').json, {'ip': '127.0.0.1', 'secure': False})
