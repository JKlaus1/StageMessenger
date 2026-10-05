#!/bin/bash
# Console selection for /mixer (v3.3: normally nothing to set -- the Pi finds the console on eth0).
#   bash ~/stage-messenger/mixer/set_console.sh auto            find whichever console is connected (default)
#   bash ~/stage-messenger/mixer/set_console.sh wing|x32        only accept that type, still found automatically
#   bash ~/stage-messenger/mixer/set_console.sh wing|x32 <ip>   pin type + address (old behaviour; no searching)
# Edits only mixer_type + mixer_ip in mixer_config.json (every other key -- TURN, Spotify search, remote
# -- is kept), restarts stage-messenger and shows the mixer's startup lines. Run with  ssh -t  (sudo).
set -e
cd "$(dirname "$0")/.."
TYPE="$1"; IP="$2"
case "$TYPE" in wing|x32|auto) ;; *) echo "usage: $0 auto | wing|x32 [console-ip]"; exit 1;; esac
[[ -z "$IP" || "$IP" =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}$ ]] || { echo "bad IP: $IP"; exit 1; }
[[ "$TYPE" == auto && -n "$IP" ]] && { echo "auto takes no IP (it searches eth0)"; exit 1; }
python3 - "$TYPE" "$IP" <<'PY'
import json, os, sys
p = 'mixer_config.json'
c = json.load(open(p)) if os.path.exists(p) else {}
print(f"was: mixer_type={c.get('mixer_type', '(default auto)')} mixer_ip={c.get('mixer_ip') or '(found automatically)'}")
c['mixer_type'] = sys.argv[1]
if sys.argv[2]:
    c['mixer_ip'] = sys.argv[2]
else:
    c.pop('mixer_ip', None)
tmp = p + '.tmp'
with open(tmp, 'w') as f:
    json.dump(c, f, indent=2)
os.replace(tmp, p)
print(f"now: mixer_type={c['mixer_type']} mixer_ip={c.get('mixer_ip') or '(found automatically)'}  (other keys kept)")
PY
sudo systemctl restart stage-messenger
echo "restarted -- waiting for the console to load..."
sleep 8
journalctl -u stage-messenger --since "-12s" --no-pager -o cat | grep '\[mixer\]' | tail -8
