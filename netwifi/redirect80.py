#!/usr/bin/env python3
"""
Port-80 front door for Stage Messenger (stage-wifi-redirect.service).

  http://<hostname>.local/        -> http://<hostname>.local:3000/wifi   (venue WiFi setup page)
  http://<hostname>.local/<path>  -> http://<hostname>.local:3000/<path> (e.g. /mixer)

So setting up a Pi on a new network only needs its hostname -- no IP, no port. Pure redirects
(302, never cached, so changing this later can't leave a browser stuck on an old target); nothing
is proxied. Runs as a sandboxed DynamicUser that may only bind port 80 (see the unit file).
Installed to /usr/local/lib/stage-messenger/ by netwifi/install_netwifi.sh, because /home/pi is 0700.
"""
import os
import socket
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

TARGET_PORT = int(os.environ.get('TARGET_PORT', '3000'))
LANDING = os.environ.get('LANDING', '/wifi')
LISTEN_PORT = int(os.environ.get('LISTEN_PORT', '80'))


class Redirect(BaseHTTPRequestHandler):
    server_version = 'stage-wifi-redirect'

    def _host(self):
        host = (self.headers.get('Host') or '').strip()
        if host.startswith('['):                         # [v6]:port
            host = host[:host.find(']') + 1] if ']' in host else ''
        elif host.count(':') == 1:                       # name:port / v4:port
            host = host.split(':', 1)[0]
        elif host.count(':') > 1:                        # bare v6 without brackets
            host = f'[{host}]'
        if not host:                                     # HTTP/1.0 without Host: use the address we were reached on
            addr = self.connection.getsockname()[0]
            if addr.startswith('::ffff:'):
                addr = addr[7:]
            host = f'[{addr}]' if ':' in addr else addr
        return host

    def _go(self):
        path = self.path if self.path.startswith('/') else '/'
        if path == '/' or path.startswith('/?'):
            path = LANDING + path[1:]
        self.send_response(302)
        self.send_header('Location', f'http://{self._host()}:{TARGET_PORT}{path}')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Content-Length', '0')
        self.end_headers()

    do_GET = do_HEAD = do_POST = _go

    def log_message(self, *a):                           # keep the journal quiet
        pass


class DualStack(ThreadingHTTPServer):
    address_family = socket.AF_INET6
    daemon_threads = True

    def server_bind(self):
        try:
            self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        except OSError:
            pass
        super().server_bind()


if __name__ == '__main__':
    try:
        srv = DualStack(('::', LISTEN_PORT), Redirect)
    except OSError:                                      # no IPv6 on this box
        srv = ThreadingHTTPServer(('0.0.0.0', LISTEN_PORT), Redirect)
    print(f'[wifi-redirect] :{LISTEN_PORT} -> :{TARGET_PORT} (/ -> {LANDING})', flush=True)
    srv.serve_forever()
