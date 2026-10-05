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

Library (v2.5) -- go-librespot's INTERNAL-API endpoints (the public Web API rate-limits the session
token: every call answered 429 on 2026-10-05, so it is not used for anything):
  GET  /library/playlists?limit=500  {total, items[{uri, name, description, owner_username, length,
                                      image_url|null, collaborative, can_edit, folder}]}
  GET  /context/tracks?uri=<ctx>     {uri, ready, length, cached, tracks[{uri, track|null}]} -- needs
                                      metadata.enabled; first call starts a background enumeration
                                      (ready=false), poll until ready and cached == length. 404 = cache off.
                                      Liked Songs = spotify:collection:tracks
  POST /player/play {uri, skip_to_uri?}  (shuffle_context BEFORE play = start shuffled)
  POST /player/add_to_queue {uri}
  status.context_uri, status.next_track (metadata.enabled)
Search uses the Spotify Web API with the user's OWN developer app (client-credentials, app-only: it
never touches the account) -- spotify.search_client_id/secret in mixer_config.json
(mixer/set_spotify_search.sh). No credentials -> search is off and the page says how to enable it.
"""
import base64
import re
import json
import os
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

UNIT = 'go-librespot.service'
SYSTEMCTL = '/usr/bin/systemctl'      # must match mixer/stage-messenger-spotify.sudoers
ID = r'[A-Za-z0-9]{10,40}'
CONTEXT_URI = re.compile(rf'^(spotify:(playlist|album|artist|show):{ID}|spotify:collection:tracks)$')
ITEM_URI = re.compile(rf'^spotify:(track|episode):{ID}$')
LIKED = 'spotify:collection:tracks'


def _slim_track(t):
    if not isinstance(t, dict):
        return None
    return {'uri': t.get('uri'), 'name': t.get('name') or '', 'artists': t.get('artist_names') or [],
            'album': t.get('album_name') or '', 'album_uri': t.get('album_uri') or '',
            'cover': t.get('album_cover_url'), 'pos': int(t.get('position') or 0), 'dur': int(t.get('duration') or 0)}


class Spotify:
    def __init__(self, cfg, publish, wanted):
        self.api = cfg.get('api', 'http://127.0.0.1:3678').rstrip('/')
        self.config_dir = cfg.get('config_dir', '/home/pi/.config/go-librespot')
        self.aux = int(cfg.get('aux', 1))
        # search: the user's own Spotify developer app (client credentials) -- optional
        self.search_id = (cfg.get('search_client_id') or '').strip()
        self.search_secret = (cfg.get('search_client_secret') or '').strip()
        self.market = (cfg.get('market') or 'US').strip()
        self.accounts_url = cfg.get('accounts_url', 'https://accounts.spotify.com').rstrip('/')
        self.webapi_url = cfg.get('webapi_url', 'https://api.spotify.com').rstrip('/')
        self._stoken, self._stoken_exp, self._slock = None, 0.0, threading.Lock()
        self._pl_cache, self._pl_at = None, 0.0
        self.publish = publish            # fn(dict) -> SSE to every /mixer page
        self.wanted = wanted              # fn() -> True while a page is open
        self.state = {'api': False, 'svc': '?', 'status': None, 'auth': None, 'at': 0, 'aux': self.aux,
                      'note': '', 'search': bool(self.search_id and self.search_secret)}
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
        st = {'api': code == 200, 'status': None, 'auth': None, 'aux': self.aux,
              'search': bool(self.search_id and self.search_secret)}
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
        tr = _slim_track(s.get('track'))
        nx = _slim_track(s.get('next_track'))
        return {'ctx_uri': s.get('context_uri') or '',
                'next': {'uri': nx['uri'], 'name': nx['name'], 'artists': nx['artists']} if nx else None,
                'user': s.get('username') or '', 'device': s.get('device_name') or '',
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
                    s['rep_trk'], s['context'], s['ctx_uri'], (s['next'] or {}).get('uri'), t.get('uri'), t.get('dur'), t.get('pos', 0) // 3000 if s['paused'] or s['stopped'] else None)
        return json.dumps([st['api'], st['svc'], core, st['auth'], st['note'], st['search']], sort_keys=True)

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
    CMDS = ('playpause', 'next', 'prev', 'seek', 'shuffle', 'repeat', 'disconnect', 'play', 'queue')

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
        elif cmd == 'play':                         # val: {uri: context, skip?: track, shuffle?: True|False}
            v = val if isinstance(val, dict) else {}
            uri, skip, shuf = str(v.get('uri') or ''), str(v.get('skip') or ''), v.get('shuffle')
            if not CONTEXT_URI.match(uri):
                return False, 'not a playlist/album URI'
            if skip and not ITEM_URI.match(skip):
                return False, 'not a track URI'
            if shuf in (True, False):               # must be set BEFORE play to start shuffled / in order
                c0, _ = self._req('/player/shuffle_context', {'shuffle_context': bool(shuf)})
                if c0 != 200:
                    return False, f'go-librespot answered {c0 or "nothing"} (shuffle)'
            code, _ = self._req('/player/play', {'uri': uri, **({'skip_to_uri': skip} if skip else {})}, timeout=8)
        elif cmd == 'queue':
            uri = str(val or '')
            if not ITEM_URI.match(uri):
                return False, 'not a track URI'
            code, _ = self._req('/player/add_to_queue', {'uri': uri})
        else:
            code, _ = self._req('/player/' + cmd, {})
        self.kick()
        if code == 200:
            return True, ''
        return False, f'go-librespot answered {code or "nothing"}'

    # ── library (v2.5): go-librespot internal-API endpoints ──
    def playlists(self, fresh=False):
        """-> (ok, data|err). Your playlists in your order, Liked Songs first. Cached 20 s."""
        if not fresh and self._pl_cache and time.time() - self._pl_at < 20:
            return True, self._pl_cache
        if not self.state.get('status'):
            return False, 'Stage Rig is not signed in to Spotify'
        code, j = self._req('/library/playlists?limit=500', timeout=12)
        if code != 200 or not isinstance(j, dict):
            return False, f'go-librespot answered {code or "nothing"}'
        items = [{'uri': LIKED, 'name': 'Liked Songs', 'len': None, 'img': None, 'owner': '', 'folder': '', 'liked': True}]
        for it in j.get('items') or []:
            if not CONTEXT_URI.match(str(it.get('uri') or '')):
                continue
            img = it.get('image_url')
            items.append({'uri': it['uri'], 'name': it.get('name') or '(untitled)', 'len': it.get('length'),
                          'img': img if isinstance(img, str) and img.startswith('https://') else None,
                          'owner': it.get('owner_username') or '', 'folder': ' / '.join(it.get('folder') or [])
                          if isinstance(it.get('folder'), list) else (it.get('folder') or '')})
        data = {'total': j.get('total'), 'items': items}
        self._pl_cache, self._pl_at = data, time.time()
        return True, data

    def tracks(self, uri):
        """-> (ok, data|err) for a context. Poll while not ready or cached < length."""
        if not CONTEXT_URI.match(uri or ''):
            return False, 'not a playlist/album URI'
        if not self.state.get('status'):
            return False, 'Stage Rig is not signed in to Spotify'
        code, j = self._req('/context/tracks?uri=' + urllib.parse.quote(uri, safe=''), timeout=8)
        if code == 404:
            return False, ('go-librespot\'s metadata cache is off -- metadata.enabled must be true in '
                           'mixer/go-librespot.yml (then sudo systemctl restart go-librespot)')
        if code != 200 or not isinstance(j, dict):
            return False, f'go-librespot answered {code or "nothing"}'
        out = []
        for e in j.get('tracks') or []:
            t = _slim_track(e.get('track'))
            if t:
                t.pop('pos', None)
            out.append({'uri': e.get('uri'), 't': t})
        return True, {'uri': uri, 'ready': bool(j.get('ready')), 'length': int(j.get('length') or 0),
                      'cached': int(j.get('cached') or 0), 'tracks': out}

    # ── search (v2.5): Spotify Web API with the user's own app, client-credentials ──
    def _search_token(self):
        with self._slock:
            if self._stoken and time.time() < self._stoken_exp - 60:
                return self._stoken
            basic = base64.b64encode(f'{self.search_id}:{self.search_secret}'.encode()).decode()
            req = urllib.request.Request(self.accounts_url + '/api/token', method='POST',
                                         data=urllib.parse.urlencode({'grant_type': 'client_credentials'}).encode(),
                                         headers={'Authorization': 'Basic ' + basic,
                                                  'Content-Type': 'application/x-www-form-urlencoded'})
            try:
                with urllib.request.urlopen(req, timeout=8) as r:
                    j = json.loads(r.read())
            except urllib.error.HTTPError as e:
                raise RuntimeError('Spotify refused the search app credentials' if e.code in (400, 401)
                                   else f'Spotify accounts answered {e.code}')
            self._stoken = j['access_token']
            self._stoken_exp = time.time() + int(j.get('expires_in') or 3600)
            return self._stoken

    def search(self, q):
        q = (q or '').strip()[:100]
        if not (self.search_id and self.search_secret):
            return False, 'search is not set up (run mixer/set_spotify_search.sh on the Pi)'
        if not q:
            return True, {'tracks': [], 'albums': [], 'playlists': []}
        try:
            tok = self._search_token()
        except Exception as e:
            return False, str(e)
        url = self.webapi_url + '/v1/search?' + urllib.parse.urlencode(
            {'q': q, 'type': 'track,album,playlist', 'limit': 10, 'market': self.market})
        req = urllib.request.Request(url, headers={'Authorization': 'Bearer ' + tok})
        try:
            with urllib.request.urlopen(req, timeout=8) as r:
                j = json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code == 401:
                self._stoken = None
            ra = e.headers.get('Retry-After')
            return False, f'Spotify search answered {e.code}' + (f' (retry in {ra} s)' if ra else '')
        except Exception as e:
            return False, f'Spotify search failed: {e}'
        img = lambda imgs: next((i.get('url') for i in (imgs or []) if isinstance(i, dict)
                                 and str(i.get('url', '')).startswith('https://')), None)
        tracks = [{'uri': t['uri'], 'name': t.get('name') or '', 'artists': [a.get('name') for a in t.get('artists') or []],
                   'album': (t.get('album') or {}).get('name') or '', 'album_uri': (t.get('album') or {}).get('uri') or '',
                   'cover': img((t.get('album') or {}).get('images')), 'dur': int(t.get('duration_ms') or 0)}
                  for t in ((j.get('tracks') or {}).get('items') or []) if t and ITEM_URI.match(str(t.get('uri') or ''))]
        albums = [{'uri': a['uri'], 'name': a.get('name') or '', 'artists': [x.get('name') for x in a.get('artists') or []],
                   'img': img(a.get('images')), 'year': (a.get('release_date') or '')[:4]}
                  for a in ((j.get('albums') or {}).get('items') or []) if a and CONTEXT_URI.match(str(a.get('uri') or ''))]
        playlists = [{'uri': p['uri'], 'name': p.get('name') or '', 'owner': (p.get('owner') or {}).get('display_name') or '',
                      'img': img(p.get('images')), 'len': ((p.get('tracks') or p.get('items') or {}) or {}).get('total')}
                     for p in ((j.get('playlists') or {}).get('items') or []) if p and CONTEXT_URI.match(str(p.get('uri') or ''))]
        return True, {'tracks': tracks, 'albums': albums, 'playlists': playlists}

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
