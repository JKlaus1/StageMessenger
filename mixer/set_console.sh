#!/bin/bash
# Point /mixer at a console:  bash ~/stage-messenger/mixer/set_console.sh <wing|x32|auto> <console-ip>
# Edits only mixer_type + mixer_ip in mixer_config.json (every other key -- TURN, Spotify search, remote
# -- is kept), restarts stage-messenger and shows the mixer's startup lines. Run with  ssh -t  (sudo).
#   M32C on the rack network:  bash mixer/set_console.sh x32 192.168.0.43
#   back to the WING:          bash mixer/set_console.sh wing 192.168.0.91
set -e
cd "$(dirname "$0")/.."
TYPE="$1"; IP="$2"
case "$TYPE" in wing|x32|auto) ;; *) echo "usage: $0 wing|x32|auto <console-ip>"; exit 1;; esac
[[ "$IP" =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}$ ]] || { echo "bad IP: $IP"; exit 1; }
python3 - "$TYPE" "$IP" <<'PY'
import json, os, sys
p = 'mixer_config.json'
c = json.load(open(p)) if os.path.exists(p) else {}
print(f"was: mixer_type={c.get('mixer_type', '(default auto)')} mixer_ip={c.get('mixer_ip', '(default)')}")
c['mixer_type'], c['mixer_ip'] = sys.argv[1], sys.argv[2]
tmp = p + '.tmp'
with open(tmp, 'w') as f:
    json.dump(c, f, indent=2)
os.replace(tmp, p)
print(f"now: mixer_type={c['mixer_type']} mixer_ip={c['mixer_ip']}  (other keys kept)")
PY
sudo systemctl restart stage-messenger
echo "restarted -- waiting for the console to load..."
sleep 8
journalctl -u stage-messenger --since "-12s" --no-pager -o cat | grep '\[mixer\]' | tail -8
