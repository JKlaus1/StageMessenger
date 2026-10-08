#!/usr/bin/env bash
# =============================================================================
# Stage Messenger /mixer — one-shot Pi setup (X32 / M32 / WING, auto-detected)
# =============================================================================
# Turns a FRESH Raspberry Pi OS Lite (64-bit, Debian 13 "Trixie") Pi into a
# remote-mixing box: Stage Messenger (:3000) with the /mixer page, console
# auto-detect on eth0, MediaMTX for low-latency listen-back, and a Cloudflare
# tunnel for internet access behind Cloudflare Access.
#
#   Run as user `pi` (NOT with sudo — it calls sudo itself):
#     bash setup_x32_pi.sh <CLOUDFLARE_TUNNEL_TOKEN>
#   Optional (remote listen-back relay through Cloudflare TURN) — pass as env vars,
#   never commit them:
#     TURN_KEY_ID=... TURN_API_TOKEN=... bash setup_x32_pi.sh <TOKEN>
#   Secrets are never printed. Re-running without the TURN vars keeps an existing key.
#   Optional home-screen app name (Chrome "Install app"): APP_NAME="Presley Mixer" bash setup_x32_pi.sh <TOKEN>
#   (later: bash ~/stage-messenger/mixer/set_app_name.sh "New Name")
#
# Safe to re-run: every step is idempotent. Lightboard (the lighting side) is
# deliberately NOT installed — see Lightboard/install.sh for that.
# =============================================================================
set -euo pipefail

TOKEN="${1:-}"
MSG_DIR="/home/pi/stage-messenger"
MSG_REPO="https://github.com/JKlaus1/StageMessenger.git"
VENV="${MSG_DIR}/.venv"
MTX_VER="v1.21.1"

log()  { printf '\n\033[1;36m== %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m!! %s\033[0m\n' "$*"; }
die()  { printf '\033[1;31mxx %s\033[0m\n' "$*" >&2; exit 1; }

[ "$(id -un)" = "pi" ] || die "Run this as user 'pi' (not root, not sudo)."
[ -n "$TOKEN" ] || die "Usage: bash setup_x32_pi.sh <CLOUDFLARE_TUNNEL_TOKEN>"
[ "$(uname -m)" = "aarch64" ] || die "This needs the 64-bit Raspberry Pi OS (uname -m = aarch64)."

log "1/7  Packages"
sudo apt-get update -y
sudo apt-get install -y git curl python3-venv python3-pip ffmpeg alsa-utils \
  network-manager avahi-daemon

log "2/7  Stage Messenger: clone/update + Python venv"
if [ -d "${MSG_DIR}/.git" ]; then
  git -C "$MSG_DIR" pull --ff-only || warn "git pull failed; continuing with the current checkout."
else
  git clone "$MSG_REPO" "$MSG_DIR"
fi
[ -d "$VENV" ] || python3 -m venv "$VENV"
"${VENV}/bin/pip" install --upgrade pip
"${VENV}/bin/pip" install -r "${MSG_DIR}/requirements.txt"

log "3/7  mixer_config.json (console auto-detect, remote enabled, Spotify off, TURN key if given)"
# Merge: keeps anything already there (camera settings, an earlier TURN key, etc.).
TURN_KEY_ID="${TURN_KEY_ID:-}" TURN_API_TOKEN="${TURN_API_TOKEN:-}" APP_NAME="${APP_NAME:-}" \
python3 - "${MSG_DIR}/mixer_config.json" <<'PY'
import json, os, sys
p = sys.argv[1]
c = json.load(open(p)) if os.path.exists(p) else {}
c['mixer_type'] = 'auto'           # whichever console answers on eth0 (WING or X32/M32); hot-swaps
c.pop('mixer_ip', None)            # let auto-detect do it
c['remote_enabled'] = True         # internet access via the tunnel (Cloudflare Access gates it)
c.setdefault('spotify', {})['enabled'] = False   # off until mixer/setup_pi_playback.sh + setup_spotify.sh are run (v4.1: X32 supported)
kid, tok = os.environ.get('TURN_KEY_ID', '').strip(), os.environ.get('TURN_API_TOKEN', '').strip()
if kid and tok:
    c.setdefault('rtc', {}).update(turn_key_id=kid, turn_api_token=tok)
if os.environ.get('APP_NAME', '').strip():
    c['app_name'] = os.environ['APP_NAME'].strip()[:45]
tmp = p + '.tmp'
json.dump(c, open(tmp, 'w'), indent=2); os.replace(tmp, p)
os.chmod(p, 0o600)
shown = json.loads(json.dumps(c))
if shown.get('rtc', {}).get('turn_api_token'):
    shown['rtc']['turn_api_token'] = '(set)'
print(json.dumps(shown, indent=2))
PY

log "4/7  systemd unit: stage-messenger (:3000)"
sudo tee /etc/systemd/system/stage-messenger.service >/dev/null <<UNIT
[Unit]
Description=Stage Messenger
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=pi
WorkingDirectory=${MSG_DIR}
Environment=PORT=3000
ExecStart=${VENV}/bin/python ${MSG_DIR}/server.py
Restart=on-failure
RestartSec=3

[Install]
WantedBy=multi-user.target
UNIT

log "5/7  MediaMTX ${MTX_VER} (low-latency listen-back relay)"
if [ ! -x /home/pi/mediamtx/mediamtx ] || ! /home/pi/mediamtx/mediamtx --version 2>/dev/null | grep -q "$MTX_VER"; then
  mkdir -p /home/pi/mediamtx
  curl -fsSL "https://github.com/bluenviron/mediamtx/releases/download/${MTX_VER}/mediamtx_${MTX_VER}_linux_arm64.tar.gz" \
    | tar -xz -C /home/pi/mediamtx
fi
/home/pi/mediamtx/mediamtx --version
sudo cp "${MSG_DIR}/mixer/mediamtx.service" /etc/systemd/system/mediamtx.service

log "6/7  Network: eth0 = console LAN only (never the internet route), WiFi = internet"
# The console lives on eth0. This profile takes DHCP there but never a default
# route, so a venue/rig network can't hijack the internet uplink (WiFi).
sudo nmcli con delete mixer-network >/dev/null 2>&1 || true
sudo nmcli con add type ethernet con-name mixer-network ifname eth0 \
  ipv4.method auto ipv4.never-default yes ipv4.ignore-auto-dns yes \
  ipv6.method disabled connection.autoconnect yes connection.autoconnect-priority 10
# Make the WiFi profile(s) Imager created retry forever at boot (phone hotspots
# idle their radio when no one is connected) and not power-save.
while IFS= read -r wname; do
  [ -n "$wname" ] || continue
  sudo nmcli con modify "$wname" connection.autoconnect-retries 0 \
    connection.autoconnect-priority 20 802-11-wireless.powersave 2 || true
  echo "  WiFi profile tuned: $wname"
done < <(nmcli -t -f NAME,TYPE con show | awk -F: '$2 ~ /wireless/ {print $1}')

log "7/7  Cloudflare tunnel (cloudflared, dashboard-managed token)"
if ! command -v cloudflared >/dev/null 2>&1; then
  sudo mkdir -p --mode=0755 /usr/share/keyrings
  curl -fsSL https://pkg.cloudflare.com/cloudflare-main.gpg | sudo tee /usr/share/keyrings/cloudflare-main.gpg >/dev/null
  echo "deb [signed-by=/usr/share/keyrings/cloudflare-main.gpg] https://pkg.cloudflare.com/cloudflared any main" \
    | sudo tee /etc/apt/sources.list.d/cloudflared.list >/dev/null
  sudo apt-get update -y && sudo apt-get install -y cloudflared
fi
if systemctl list-unit-files | grep -q '^cloudflared.service'; then
  echo "  cloudflared service already installed — leaving it alone."
else
  sudo cloudflared service install "$TOKEN"
fi

log "WiFi page by hostname (polkit for nmcli, mDNS, port-80 redirect -> /wifi)"
sudo bash "${MSG_DIR}/netwifi/install_netwifi.sh"

log "Enable + start"
sudo systemctl daemon-reload
sudo systemctl enable --now avahi-daemon stage-messenger mediamtx cloudflared
sudo systemctl restart stage-messenger
sleep 6

log "Status"
systemctl is-active stage-messenger mediamtx cloudflared | paste <(printf 'stage-messenger\nmediamtx\ncloudflared\n') -
journalctl -u stage-messenger --since "-20s" --no-pager -o cat | grep '\[mixer\]' | tail -5 || true
echo
echo "Pi hostname: $(hostname)   addresses: $(hostname -I)"
echo
echo "Local:   http://$(hostname).local:3000/mixer   (same network as the Pi)"
echo "WiFi:    http://$(hostname).local/             (set up a new network; same network as the Pi)"
echo "Reboot once so eth0/WiFi come up in their final roles:   sudo reboot"
