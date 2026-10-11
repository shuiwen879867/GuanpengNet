"""Trust one forwarding hop only when the peer is the local Nginx server."""
from werkzeug.middleware.proxy_fix import ProxyFix


class LocalProxyFix:
    def __init__(self, app):
        self.app = app
        self.proxied_app = ProxyFix(app, x_for=1, x_proto=1, x_host=0, x_port=0, x_prefix=0)

    def __call__(self, environ, start_response):
        if environ.get('REMOTE_ADDR') in ('127.0.0.1', '::1'):
            return self.proxied_app(environ, start_response)
        return self.app(environ, start_response)
