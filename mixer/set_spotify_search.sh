#!/bin/bash
# Stage Messenger v2.5 -- enable Spotify SEARCH on /mixer with your own Spotify developer app.
# The session token go-librespot holds is rate-limited out of the public Web API (429 on every call),
# so search uses an app of your own with the client-credentials flow: app-only, it never touches your
# account -- it can only search the public catalogue.
#
# 1. https://developer.spotify.com/dashboard -> Create app (any name, e.g. "Stage Rig search";
#    Redirect URI http://127.0.0.1:8080 -- never used; tick "Web API").
# 2. Open the app -> Settings -> copy the Client ID and the Client secret.
# 3. From the laptop:  ssh -t pi@lights.local "bash ~/stage-messenger/mixer/set_spotify_search.sh"
# The credentials are tested with a real search BEFORE they are saved, and are stored only in
# ~/stage-messenger/mixer_config.json (gitignored). Remove: run with --remove.
# Renewing (the dashboard shows the secret lasting 180 days): /mixer Library -> Search has a guided form
# (ROTATE on the app page, paste, Test & save) -- or re-run this script with the new secret.
set -u
CFG=$HOME/stage-messenger/mixer_config.json
cd "$HOME/stage-messenger" || exit 1

if [ "${1:-}" = "--remove" ]; then
  python3 - "$CFG" <<'PY'
import json, sys
p = sys.argv[1]
try: c = json.load(open(p))
except FileNotFoundError: c = {}
sp = c.get('spotify') or {}
for k in ('search_client_id', 'search_client_secret', 'search_saved_at'): sp.pop(k, None)
c['spotify'] = sp
json.dump(c, open(p, 'w'), indent=2); print('search credentials removed')
PY
  sudo systemctl restart stage-messenger.service && echo "stage-messenger restarted"
  exit 0
fi

read -r -p 'Spotify app Client ID: ' CID
read -r -s -p 'Spotify app Client secret (not shown): ' CSEC; echo
CID=$(printf '%s' "$CID" | tr -d '[:space:]'); CSEC=$(printf '%s' "$CSEC" | tr -d '[:space:]')
[ -n "$CID" ] && [ -n "$CSEC" ] || { echo "both are needed -- nothing saved"; exit 1; }

echo "testing them with a search..."
CID="$CID" CSEC="$CSEC" python3 - "$CFG" <<'PY' || exit 1
import base64, json, os, sys, urllib.error, urllib.parse, urllib.request
cid, sec, p = os.environ['CID'], os.environ['CSEC'], sys.argv[1]
basic = base64.b64encode(f'{cid}:{sec}'.encode()).decode()
try:
    r = urllib.request.urlopen(urllib.request.Request('https://accounts.spotify.com/api/token', method='POST',
        data=b'grant_type=client_credentials', headers={'Authorization': 'Basic ' + basic,
        'Content-Type': 'application/x-www-form-urlencoded'}), timeout=10)
    tok = json.load(r)['access_token']
except urllib.error.HTTPError as e:
    print(f'  FAIL Spotify refused the credentials (HTTP {e.code}: {e.read()[:160].decode(errors="replace")}) -- nothing saved'); sys.exit(1)
except Exception as e:
    print(f'  FAIL could not reach Spotify: {e} -- nothing saved'); sys.exit(1)
try:
    q = urllib.parse.urlencode({'q': 'gimme shelter', 'type': 'track', 'limit': 3, 'market': 'US'})
    j = json.load(urllib.request.urlopen(urllib.request.Request('https://api.spotify.com/v1/search?' + q,
        headers={'Authorization': 'Bearer ' + tok}), timeout=10))
    names = [f"{t['name']} -- {t['artists'][0]['name']}" for t in j['tracks']['items'] if t]
    print('  OK   search works:', '; '.join(names))
except urllib.error.HTTPError as e:
    print(f'  FAIL token OK but search answered HTTP {e.code}: {e.read()[:200].decode(errors="replace")} -- nothing saved'); sys.exit(1)
except Exception as e:
    print(f'  FAIL token OK but the search request failed: {e!r} -- nothing saved'); sys.exit(1)
try: c = json.load(open(p))
except FileNotFoundError: c = {}
import datetime
c.setdefault('spotify', {}).update(search_client_id=cid, search_client_secret=sec,
                                  search_saved_at=datetime.date.today().isoformat())   # 180-day renewal countdown
json.dump(c, open(p, 'w'), indent=2)
print(f'  OK   saved to {p} (client id {cid[:6]}...)')
PY
sudo systemctl restart stage-messenger.service && echo "  OK   stage-messenger restarted -- hard-refresh /mixer; Library -> Search"
