"""
Pi playback (v2.4): go-librespot ("Stage Rig", Spotify Connect) -> WING USB 1/2 -> AUX 1.

go-librespot runs as its own service (mixer/go-librespot.service) with a local control API on
127.0.0.1:3678 (mixer/go-librespot.yml). This module polls it and relays state to /mixer pages over
the existing SSE hub, and forwards whitelisted commands. Stdlib only (no websocket dependency):
/status is polled ~1/s while a page is open, every 10 s otherwise.

go-librespot v0.10.3 API facts used here (from its api-spec.yml / daemon source):
  GET  /           200 when the API is up
  GET  /status     200 + status JSON when signed in, 204 when there is no session
                   top level: username, device_name, stopped, paused, buffering, shuffle_context,
                   repeat_context, repeat_track, context_name, play_origin, volume, volume_steps,
                   track (null or {uri, name, artist_names[], album_name, album_cover_url|null,
                   position ms, duration ms})
  GET  /auth/code  200 {url, code, expires_at} while a device_auth pairing waits, else 204
  POST /player/playpause | next | prev | stop   (no body; stop = stop playback AND disconnect)
  POST /player/seek {position: ms, relative: false}
  POST /player/shuffle_context {shuffle_context: bool}
  POST /player/repeat_context {repeat_context: bool} / repeat_track {repeat_track: bool}
album_cover_url is a public https://i.scdn.co/... URL -- the browser loads it directly.
"""
import json
import os
import subprocess
import threading
import time
import urllib.error
import urllib.request

UNIT = 'go-librespot.service'
SYSTEMCTL = '/usr/bin/systemctl'      # must match mixer/stage-messenger-spotify.sudoers


class Spotify:
    def __init__(self, cfg, publish, wanted):
        self.api = cfg.get('api', 'http://127.0.0.1:3678').rstrip('/')
        self.config_dir = cfg.get('config_dir', '/home/pi/.config/go-librespot')
        self.aux = int(cfg.get('aux', 1))
        self.publish = publish            # fn(dict) -> SSE to every /mixer page
        self.wanted = wanted              # fn() -> True while a page is open
        self.state = {'api': False, 'svc': '?', 'status': None, 'auth': None, 'at': 0, 'aux': self.aux,
                      'note': ''}
        self._key = None
        self._lock = threading.Lock()
        self._busy = False                # a kill/re-pair is running
        self._note_until = 0.0
        self._wake = threading.Event()

    def start(self):
        threading.Thread(target=self._loop, daemon=True, name='spotify-poll').start()

    # ── go-librespot HTTP ──
    def _req(self, path, body=None, timeout=2.0):
        """-> (http status, parsed JSON or None). Status 0 = API unreachable."""
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(self.api + path, data=data, method='GET' if body is None and not
                                     path.startswith('/player/') else 'POST',
                                     headers={'Content-Type': 'application/json'} if data else {})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read()
                return r.status, (json.loads(raw) if raw.strip() else None)
        except urllib.error.HTTPError as e:
            return e.code, None
        except Exception:
            return 0, None

    @staticmethod
    def _svc():
        try:
            return subprocess.run([SYSTEMCTL, 'is-active', UNIT], capture_output=True, text=True,
                                  timeout=3).stdout.strip() or 'unknown'
        except Exception:
            return 'unknown'

    # ── polling ──
    def _loop(self):
        last_svc = 0.0
        while True:
            try:
                self.poll(force_svc=time.time() - last_svc > 10)
                if time.time() - last_svc > 10:
                    last_svc = time.time()
            except Exception as e:                     # never let the poller die
                print(f'[mixer] spotify poll error: {e}', flush=True)
            self._wake.wait(1.0 if self.wanted() else 10.0)
            self._wake.clear()

    def poll(self, force_svc=False):
        code, _ = self._req('/', timeout=1.5)
        st = {'api': code == 200, 'status': None, 'auth': None, 'aux': self.aux}
        if code == 200:
            c, s = self._req('/status')
            if c == 200 and isinstance(s, dict):
                st['status'] = self._slim(s)
            elif c == 204:
                ac, a = self._req('/auth/code')
                if ac == 200 and isinstance(a, dict):
                    st['auth'] = {k: a.get(k) for k in ('url', 'code', 'expires_at')}
        # API down: ask systemd right away if we last saw it up (or never asked), then every ~10 s
        st['svc'] = 'active' if st['api'] else (self._svc() if force_svc or self.state['svc'] in ('?', 'active')
                                                 else self.state['svc'])
        st['at'] = int(time.time() * 1000)
        with self._lock:
            st['note'] = self.state.get('note', '') if time.time() < self._note_until else ''
            key = self._sig(st)
            playing = bool(st['status'] and not st['status']['stopped'] and not st['status']['paused'])
            # publish on any real change; while playing also every 5 s (re-anchors the page's clock)
            due = key != self._key or (playing and time.time() * 1000 - self.state.get('at', 0) > 5000)
            if due:
                self._key = key
                self.state = st
            else:
                return
        self.publish({'t': 'sp', 'v': st})

    @staticmethod
    def _slim(s):
        t = s.get('track') or None
        tr = None
        if isinstance(t, dict):
            tr = {'uri': t.get('uri'), 'name': t.get('name') or '', 'artists': t.get('artist_names') or [],
                  'album': t.get('album_name') or '', 'cover': t.get('album_cover_url'),
                  'pos': int(t.get('position') or 0), 'dur': int(t.get('duration') or 0)}
        return {'user': s.get('username') or '', 'device': s.get('device_name') or '',
                'stopped': bool(s.get('stopped', True)), 'paused': bool(s.get('paused', False)),
                'buffering': bool(s.get('buffering', False)), 'shuffle': bool(s.get('shuffle_context')),
                'rep_ctx': bool(s.get('repeat_context')), 'rep_trk': bool(s.get('repeat_track')),
                'context': s.get('context_name') or '', 'origin': s.get('play_origin') or '', 'track': tr}

    @staticmethod
    def _sig(st):
        """What counts as a change (position excluded -- it moves every poll while playing)."""
        s = st['status']
        core = None
        if s:
            t = s['track'] or {}
            core = (s['user'], s['stopped'], s['paused'], s['buffering'], s['shuffle'], s['rep_ctx'],
                    s['rep_trk'], s['context'], t.get('uri'), t.get('dur'), t.get('pos', 0) // 3000 if s['paused'] or s['stopped'] else None)
        return json.dumps([st['api'], st['svc'], core, st['auth'], st['note']], sort_keys=True)

    def snapshot(self):
        with self._lock:
            return dict(self.state)

    def kick(self):
        """Re-poll now (after a command) and again shortly after (go-librespot settles in ~0.3 s)."""
        def go():
            for d in (0.25, 0.9):
                time.sleep(d)
                self._key = None                  # publish even if only the position moved (a seek)
                try:
                    self.poll()
                except Exception:
                    pass
        threading.Thread(target=go, daemon=True).start()

    def _note(self, txt):
        with self._lock:
            self.state['note'] = txt
            self._note_until = time.time() + 20
        self.kick()

    # ── commands ──
    CMDS = ('playpause', 'next', 'prev', 'seek', 'shuffle', 'repeat', 'disconnect')

    def command(self, cmd, val=None):
        """-> (ok, err). Whitelisted; values clamped."""
        if cmd not in self.CMDS:
            return False, 'bad command'
        s = self.state.get('status')
        if not self.state.get('api'):
            return False, 'go-librespot is not running'
        if cmd != 'disconnect' and not s:
            return False, 'Stage Rig is not signed in to Spotify'
        if cmd == 'seek':
            t = (s or {}).get('track') or {}
            try:
                ms = float(val)
            except (TypeError, ValueError):
                return False, 'bad position'
            if ms != ms:
                return False, 'bad position'
            dur = int(t.get('dur') or 0)
            if not dur:
                return False, 'nothing loaded to seek in'
            code, _ = self._req('/player/seek', {'position': int(max(0, min(dur - 500, ms))), 'relative': False})
        elif cmd == 'shuffle':
            code, _ = self._req('/player/shuffle_context', {'shuffle_context': bool(val)})
        elif cmd == 'repeat':                       # val: 'off' | 'context' | 'track'
            if val not in ('off', 'context', 'track'):
                return False, 'bad repeat mode'
            c1, _ = self._req('/player/repeat_context', {'repeat_context': val in ('context', 'track')})
            c2, _ = self._req('/player/repeat_track', {'repeat_track': val == 'track'})
            code = c1 if c1 != 200 else c2
        elif cmd == 'disconnect':
            code, _ = self._req('/player/stop', {})
        else:
            code, _ = self._req('/player/' + cmd, {})
        self.kick()
        if code == 200:
            return True, ''
        return False, f'go-librespot answered {code or "nothing"}'

    # ── kill switch (service level) -- needs mixer/stage-messenger-spotify.sudoers ──
    def _systemctl(self, verb):
        r = subprocess.run(['sudo', '-n', SYSTEMCTL, verb, UNIT], capture_output=True, text=True, timeout=20)
        if r.returncode != 0:
            raise RuntimeError((r.stderr or r.stdout or f'systemctl {verb} failed').strip().splitlines()[-1])

    def service(self, action):
        """action: 'restart' | 'repair' (stop, forget the saved login, start -> new pairing code)."""
        if action not in ('restart', 'repair'):
            return False, 'bad action'
        if self._busy:
            return False, 'already working on it'
        self._busy = True
        try:
            if action == 'restart':
                self._systemctl('restart')
                self._note('Restarted Stage Rig')
            else:
                self._systemctl('stop')
                p = os.path.join(self.config_dir, 'state.json')
                if os.path.exists(p):
                    os.remove(p)
                self._systemctl('start')
                self._note('Signed out -- approve the new pairing code')
            return True, ''
        except Exception as e:
            return False, f'{e} (is mixer/stage-messenger-spotify.sudoers installed? run mixer/setup_spotify.sh)'
        finally:
            self._busy = False
