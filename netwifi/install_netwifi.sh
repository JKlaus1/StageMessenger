#!/usr/bin/env bash
# =============================================================================
# netwifi — make a Stage Messenger Pi's WiFi page reachable by hostname alone.
#   http://<hostname>.local/  -> venue WiFi page (:3000/wifi)
#
#   Run on the Pi:   sudo bash ~/stage-messenger/netwifi/install_netwifi.sh
#
# Installs (all idempotent, safe to re-run after every git pull):
#   1. polkit rule so the stage-messenger service user (pi) can drive NetworkManager
#      without a login session (/etc/polkit-1/rules.d/50-lightboard-nm.rules -- same
#      file Lightboard installs, so on a Pi with both it is simply rewritten identically)
#   2. avahi-daemon enabled (mDNS: <hostname>.local)
#   3. stage-wifi-redirect.service on port 80 (sandboxed, DynamicUser, bind-80 capability only)
# Then restart stage-messenger yourself so the /wifi page loads.
# =============================================================================
set -euo pipefail
[ "$(id -u)" -eq 0 ] || { echo "Run with sudo: sudo bash $0" >&2; exit 1; }
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "== polkit rule (pi may manage NetworkManager)"
install -D -m 0644 "${HERE}/50-lightboard-nm.rules" /etc/polkit-1/rules.d/50-lightboard-nm.rules

echo "== mDNS (avahi-daemon)"
if ! command -v avahi-daemon >/dev/null 2>&1; then
  apt-get install -y avahi-daemon
fi
systemctl enable --now avahi-daemon >/dev/null

echo "== port-80 redirect (stage-wifi-redirect)"
install -D -m 0644 "${HERE}/redirect80.py" /usr/local/lib/stage-messenger/redirect80.py
install -m 0644 "${HERE}/stage-wifi-redirect.service" /etc/systemd/system/stage-wifi-redirect.service
systemctl daemon-reload
if ss -Hltn 'sport = :80' | grep -q . && ! systemctl is-active --quiet stage-wifi-redirect; then
  echo "!! Something else already listens on port 80 -- not starting the redirect:" >&2
  ss -Hltnp 'sport = :80' >&2 || true
  echo "   The page still works at http://$(hostname).local:3000/wifi" >&2
else
  systemctl enable stage-wifi-redirect >/dev/null
  systemctl restart stage-wifi-redirect
fi

sleep 1
echo
systemctl is-active stage-wifi-redirect avahi-daemon | paste <(printf 'stage-wifi-redirect\navahi-daemon\n') -
echo
echo "WiFi page:  http://$(hostname).local/      (same network as the Pi)"
echo "     also:  http://$(hostname).local:3000/wifi"
echo "Now:        sudo systemctl restart stage-messenger"
