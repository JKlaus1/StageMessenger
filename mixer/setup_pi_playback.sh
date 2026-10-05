#!/bin/bash
# Stage Messenger v2.4 step A -- Pi -> WING playback, OS side. Idempotent; safe to re-run.
#   1. /etc/asound.conf      <- mixer/asound.conf     (wing_dmix + wing_pi: stereo -> WING USB 1/2)
#   2. WirePlumber rule      <- mixer/51-wing-ignore.conf (kiosk PipeWire keeps off the WING)
#   3. tone test through the system-wide wing_pi (-30 dBFS, 2 players at once)
#   4. go-librespot binary   -> ~/go-librespot/ (downloaded, NOT configured or started yet)
# Run from the laptop with a TTY (sudo asks for the pi password once):
#   ssh -t pi@lights.local "bash ~/stage-messenger/mixer/setup_pi_playback.sh"
# Pin a release:  GOLIBRESPOT_VERSION=v0.x.y bash ~/stage-messenger/mixer/setup_pi_playback.sh
set -u
REPO=$HOME/stage-messenger/mixer
LOG=$HOME/setup_pi_playback.txt
GL=$HOME/go-librespot
exec > >(tee "$LOG") 2>&1
say() { echo; echo "=== $*"; }
ok()  { echo "  OK   $*"; }
bad() { echo "  FAIL $*"; FAILS=$((FAILS + 1)); }
FAILS=0

[ "$(id -un)" = "pi" ] || { echo "run as pi"; exit 1; }
[ -f "$REPO/asound.conf" ] && [ -f "$REPO/51-wing-ignore.conf" ] || { echo "missing files in $REPO -- git pull first"; exit 1; }

say "0. sudo (asks for the pi password once)"
sudo -v || { echo "sudo failed -- run with ssh -t so it can ask for the password"; exit 1; }

say "1. /etc/asound.conf"
if [ -f /etc/asound.conf ] && cmp -s "$REPO/asound.conf" /etc/asound.conf; then
  ok "already up to date"
else
  if [ -f /etc/asound.conf ]; then
    B=/etc/asound.conf.bak.$(date +%Y%m%d-%H%M%S); sudo cp /etc/asound.conf "$B"; echo "  backed up the old one -> $B"
  fi
  sudo install -m 644 "$REPO/asound.conf" /etc/asound.conf && ok "installed" || bad "install failed"
fi
echo "  (wing_pi is proven by the tone test in step 3)"

say "2. WirePlumber: ignore the WING"
export XDG_RUNTIME_DIR=${XDG_RUNTIME_DIR:-/run/user/$(id -u)}
D=$HOME/.config/wireplumber/wireplumber.conf.d
mkdir -p "$D"
if cmp -s "$REPO/51-wing-ignore.conf" "$D/51-wing-ignore.conf"; then ok "rule already in place"
else cp "$REPO/51-wing-ignore.conf" "$D/" && ok "rule installed in $D"; fi
if systemctl --user is-active -q wireplumber; then
  systemctl --user restart wireplumber && sleep 3
  N=$(timeout 5 wpctl status 2>/dev/null | grep -c 'WING')
  [ "$N" = "0" ] && ok "PipeWire no longer lists the WING" || bad "PipeWire still lists the WING ($N lines):"
  [ "$N" = "0" ] || timeout 5 wpctl status 2>&1 | grep 'WING'
else
  echo "  wireplumber not running in this session -- the rule applies at the next desktop login"
fi

say "3. tone test through /etc/asound.conf wing_pi (-30 dBFS; AUX 1 fader is at -oo, so expect silence)"
tone() { python3 -c "
import sys, math, struct
f, n = float(sys.argv[1]), int(48000 * float(sys.argv[2])); a = 0.0316 * 32767
b = bytearray()
for i in range(n):
    v = int(a * math.sin(2 * math.pi * f * i / 48000)); b += struct.pack('<hh', v, v)
sys.stdout.buffer.write(b)" "$1" "$2"; }
play() { tone "$1" "$2" | timeout 10 aplay -q -D wing_pi -t raw -f S16_LE -c 2 -r 48000; echo "${PIPESTATUS[1]}"; }
R1=$(mktemp); play 440 3 > "$R1" &
P1=$!; sleep 0.8
R2=$(play 660 1.5)
wait "$P1"; T=$R1; R1=$(cat "$T"); rm -f "$T"
[ "$R1" = "0" ] && [ "$R2" = "0" ] && ok "two players at once (exit $R1 / $R2)" || bad "players exit $R1 / $R2 (0 = ok, 124 = blocked)"

say "4. go-librespot (download only -- not configured, not started)"
mkdir -p "$GL/releases"
API=https://api.github.com/repos/devgianlu/go-librespot/releases
if [ -n "${GOLIBRESPOT_VERSION:-}" ]; then URL="$API/tags/$GOLIBRESPOT_VERSION"; else URL="$API/latest"; fi
J=$(curl -fsSL --max-time 15 -H 'Accept: application/vnd.github+json' "$URL") || { bad "GitHub API unreachable ($URL)"; J=""; }
if [ -n "$J" ]; then
  read -r TAG PUB ASSET AURL < <(printf '%s' "$J" | python3 -c "
import json, sys
r = json.load(sys.stdin)
names = [a['name'] for a in r.get('assets', [])]
print('  assets:', ', '.join(names), file=sys.stderr)
pick = [a for a in r.get('assets', []) if 'linux' in a['name'].lower()
        and any(k in a['name'].lower() for k in ('arm64', 'aarch64')) and a['name'].endswith(('.tar.gz', '.tgz'))]
a = pick[0] if pick else {'name': '-', 'browser_download_url': '-'}
print(r.get('tag_name', '-'), (r.get('published_at') or '-')[:10], a['name'], a['browser_download_url'])")
  echo "  release: $TAG  (published $PUB)"
  if [ "$ASSET" = "-" ]; then
    bad "no linux arm64 .tar.gz among the assets above -- tell Claude which one to use"
  else
    DIR="$GL/releases/$TAG"
    if [ -x "$DIR/go-librespot" ]; then ok "$TAG already downloaded"
    else
      mkdir -p "$DIR" && curl -fsSL --max-time 120 "$AURL" | tar -xz -C "$DIR" && ok "downloaded $ASSET" || bad "download/extract failed"
      B=$(find "$DIR" -type f -name 'go-librespot' | head -1)
      [ -n "$B" ] && [ "$B" != "$DIR/go-librespot" ] && mv "$B" "$DIR/go-librespot"
      chmod +x "$DIR/go-librespot" 2>/dev/null
    fi
    if [ -x "$DIR/go-librespot" ]; then
      ln -sfn "$DIR/go-librespot" "$GL/go-librespot"; echo "$TAG" > "$GL/VERSION"
      ok "$GL/go-librespot -> $DIR/go-librespot"
      file "$DIR/go-librespot" 2>/dev/null | sed 's/^/  /'
      MISSING=$(ldd "$DIR/go-librespot" 2>/dev/null | grep 'not found')
      [ -z "$MISSING" ] && ok "all shared libraries present" || { bad "missing libraries:"; echo "$MISSING"; }
      echo "  --- go-librespot --help:"
      timeout 5 "$GL/go-librespot" --help 2>&1 | head -40 | sed 's/^/  /'
      # The config keys of THIS release (main-branch docs may be newer) -- step B is written against it.
      curl -fsSL --max-time 15 "https://raw.githubusercontent.com/devgianlu/go-librespot/$TAG/config_schema.json" \
        -o "$DIR/config_schema.json" && ok "saved $TAG config_schema.json" || bad "could not fetch config_schema.json for $TAG"
      [ -f "$DIR/config_schema.json" ] && python3 -c "
import json, sys
s = json.load(open(sys.argv[1]))['definitions']
def walk(p, pre='  '):
    for k, v in (p.get('properties') or {}).items():
        if '\$ref' in v: v = s[v['\$ref'].split('/')[-1]]
        en = v.get('enum'); print(f\"{pre}{k} [{v.get('type')}] default={v.get('default')!r}\" + (f' enum={en}' if en else ''))
        if v.get('type') == 'object': walk(v, pre + '  ')
walk(s['config'])" "$DIR/config_schema.json" 2>&1 | head -70
    fi
  fi
fi
echo "  --- anything already in ~/.config/go-librespot:"
ls -la "$HOME/.config/go-librespot" 2>/dev/null || echo "  (none)"

say "done -- $FAILS failure(s). Log: $LOG"
