#!/bin/bash
# Stage Messenger v2.4 / v4.1 step B -- go-librespot running, signed in, playing to the console USB (WING AUX 1 / X32 Card 1/2). Idempotent.
#   1. ~/.config/go-librespot/config.yml -> mixer/go-librespot.yml
#   2. systemd unit go-librespot (enabled, started) + sudoers rule for the /mixer kill switch
#   3. sign-in: shows the spotify.com pairing link + code and waits for you to approve (first run only)
#   4. playback check: pick "Stage Rig" in the Spotify app and press play -> track + WING USB state
# Needs step A (mixer/setup_pi_playback.sh) first. Run from the laptop with a TTY:
#   ssh -t pi@lights.local "bash ~/stage-messenger/mixer/setup_spotify.sh"
set -u
REPO=$HOME/stage-messenger/mixer
CFG=$HOME/.config/go-librespot
API=http://127.0.0.1:3678
LOG=$HOME/setup_spotify.txt
exec > >(tee "$LOG") 2>&1
say() { echo; echo "=== $*"; }
ok()  { echo "  OK   $*"; }
bad() { echo "  FAIL $*"; FAILS=$((FAILS + 1)); }
FAILS=0
code_of() { curl -s -o /dev/null -w '%{http_code}' --max-time 3 "$API$1"; }
json()    { curl -s --max-time 3 "$API$1"; }

[ "$(id -un)" = "pi" ] || { echo "run as pi"; exit 1; }
[ -x "$HOME/go-librespot/go-librespot" ] || { echo "go-librespot not downloaded -- run mixer/setup_pi_playback.sh first"; exit 1; }
[ -f /etc/asound.conf ] || { echo "/etc/asound.conf missing -- run mixer/setup_pi_playback.sh first"; exit 1; }
for f in go-librespot.yml go-librespot.service; do [ -f "$REPO/$f" ] || { echo "missing $REPO/$f -- git pull first"; exit 1; }; done
echo "go-librespot $(cat "$HOME/go-librespot/VERSION" 2>/dev/null)"

say "0. sudo (asks for the pi password once)"
sudo -v || { echo "sudo failed -- run with ssh -t"; exit 1; }

say "1. config"
mkdir -p "$CFG" && chmod 700 "$CFG"
if [ -e "$CFG/config.yml" ] && [ ! -L "$CFG/config.yml" ]; then
  mv "$CFG/config.yml" "$CFG/config.yml.bak.$(date +%Y%m%d-%H%M%S)"; echo "  moved an existing config.yml aside"
fi
ln -sfn "$REPO/go-librespot.yml" "$CFG/config.yml" && ok "$CFG/config.yml -> $REPO/go-librespot.yml"
[ -f "$CFG/config.yaml" ] && bad "$CFG/config.yaml exists and would be used INSTEAD of config.yml -- remove it"

say "2. systemd unit"
if cmp -s "$REPO/go-librespot.service" /etc/systemd/system/go-librespot.service; then ok "unit up to date"
else sudo install -m 644 "$REPO/go-librespot.service" /etc/systemd/system/ && sudo systemctl daemon-reload && ok "unit installed"; fi
sudo systemctl enable -q go-librespot && ok "enabled at boot"
sudo systemctl restart go-librespot && ok "(re)started"
say "2b. kill-switch permission (/mixer Restart / Sign out & re-pair)"
SUD=/etc/sudoers.d/stage-messenger-spotify
SMU=$(systemctl show -p User --value stage-messenger 2>/dev/null)
[ "${SMU:-root}" = "pi" ] && ok "stage-messenger runs as pi" || echo "  NOTE stage-messenger runs as '${SMU:-root}', not pi -- the rule below is for pi"
if [ -f "$REPO/stage-messenger-spotify.sudoers" ]; then
  if sudo cmp -s "$REPO/stage-messenger-spotify.sudoers" "$SUD"; then ok "sudoers rule up to date"
  elif sudo visudo -cf "$REPO/stage-messenger-spotify.sudoers" >/dev/null; then
    sudo install -m 0440 -o root -g root "$REPO/stage-messenger-spotify.sudoers" "$SUD" && sudo visudo -c >/dev/null \
      && ok "sudoers rule installed ($SUD)" || bad "sudoers install/validation failed"
  else bad "stage-messenger-spotify.sudoers did not validate -- NOT installed"; fi
  sudo -k                                   # drop the cached password: the next check must pass on the rule alone
  sudo -n -l /usr/bin/systemctl restart go-librespot.service >/dev/null 2>&1 \
    && ok "pi may restart/stop/start go-librespot without a password" || bad "passwordless go-librespot control NOT allowed"
else bad "missing $REPO/stage-messenger-spotify.sudoers -- git pull first"; fi

UP=""
for i in $(seq 1 30); do [ "$(code_of /)" = "200" ] && { UP=1; break; }; sleep 1; done
if [ -n "$UP" ]; then ok "control API answering on $API"
else
  bad "control API not answering after 30 s -- journal:"
  journalctl -u go-librespot -n 25 --no-pager
  say "done -- $FAILS failure(s). Log: $LOG"; exit 1
fi

say "3. Spotify sign-in"
user() { json /status | python3 -c "import json,sys; print(json.load(sys.stdin).get('username',''))" 2>/dev/null; }
if [ "$(code_of /status)" = "200" ]; then
  ok "already signed in as $(user)"
else
  SHOWN=""; DEADLINE=$(( $(date +%s) + 600 ))
  while [ "$(date +%s)" -lt "$DEADLINE" ]; do
    if [ "$(code_of /status)" = "200" ]; then break; fi
    if [ "$(code_of /auth/code)" = "200" ]; then
      A=$(json /auth/code)
      KEY=$(printf '%s' "$A" | python3 -c "import json,sys; print(json.load(sys.stdin).get('code',''))" 2>/dev/null)
      if [ "$KEY" != "$SHOWN" ]; then
        SHOWN=$KEY
        printf '%s' "$A" | python3 -c "
import json, sys
a = json.load(sys.stdin)
print()
print('  ==================================================================')
print('   SIGN IN STAGE RIG TO SPOTIFY  (phone signed in to your Premium account)')
print('   1. open this link:')
print()
print('      ' + a.get('url', ''))
print()
print('   2. approve. If it asks for a code, enter:   ' + a.get('code', ''))
print('   (expires ' + a.get('expires_at', '')[:19].replace('T', ' ') + ' UTC -- a fresh code appears here if it does)')
print('  ==================================================================')
print('  waiting for approval (up to 10 min)...')"
      fi
    fi
    sleep 3
  done
  if [ "$(code_of /status)" = "200" ]; then ok "signed in as $(user) -- login saved in $CFG/state.json"
  else bad "not signed in after 10 min -- run this script again for a fresh code"; fi
fi

say "4. playback check"
if [ "$(code_of /status)" = "200" ]; then
  echo "  Open the Spotify app -> devices (speaker icon) -> pick \"Stage Rig\" -> play something."
  echo "  WING: raise AUX 1 (USB 1/2).  X32: it is on Card in 1/2 -- return it to Aux In 1/2 (Routing > Aux In Remap = Card 1-4) or a channel, then raise that fader."
  echo "  waiting up to 3 min for playback..."
  PLAYING=""
  for i in $(seq 1 60); do
    S=$(json /status)
    if printf '%s' "$S" | python3 -c "
import json, sys
s = json.load(sys.stdin)
sys.exit(0 if not s.get('stopped', True) and not s.get('paused', True) else 1)" 2>/dev/null; then PLAYING=1; break; fi
    sleep 3
  done
  if [ -n "$PLAYING" ]; then
    printf '%s' "$S" | python3 -c "
import json, sys
s = json.load(sys.stdin); t = s.get('track') or {}
print('  OK   playing:', t.get('name', '?'), '--', ', '.join(t.get('artist_names') or []), '|', t.get('album_name', ''))
print('       device:', s.get('device_name'), '| volume', s.get('volume'), '/', s.get('volume_steps'), '(fixed: not applied to audio)')"
    sleep 1
    CARD=WING; for c in XLIVE XUSB; do [ -d /proc/asound/$c ] && [ ! -d /proc/asound/WING ] && CARD=$c; done
    if grep -q 'RUNNING' /proc/asound/$CARD/pcm0p/sub0/status 2>/dev/null; then ok "$CARD USB playback stream RUNNING"
    else bad "$CARD USB playback stream not running:"; cat /proc/asound/$CARD/pcm0p/sub0/status 2>&1 | head -5; fi
    echo "  output: $(grep STAGE_RIG_PCM "$CFG/console.env" 2>/dev/null || echo 'STAGE_RIG_PCM unset -> wing_pi (default)')"
  else
    echo "  (nothing played within 3 min -- fine; it is set up. Re-run this script any time to check.)"
  fi
fi

say "journal (last 20 lines)"
journalctl -u go-librespot -n 20 --no-pager -o cat | sed 's/^/  /'
say "done -- $FAILS failure(s). Log: $LOG"
