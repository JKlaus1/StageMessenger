"""
Venue WiFi page for Stage Messenger (netwifi).

Ported from Lightboard's wifi_routes.py so a Pi that only runs Stage Messenger can still be moved
onto a new WiFi network. Same NetworkManager logic (scan / connect with auto-revert when there's no
internet / captive-portal hand-off to a kiosk screen if the Pi has one / saved networks / forget).

Served twice, same page and API:
  /wifi        LOCAL ONLY. Anything that came through the Cloudflare tunnel (Cf-Connecting-Ip) is
               refused (the page itself redirects to /mixer/wifi), so the public stage-messenger.com
               side can never reach it. This is what http://<hostname>.local/ lands on (port-80
               redirect, netwifi/redirect80.py).
  /mixer/wifi  Remote-capable. Sits under the existing Cloudflare Access path rule for /mixer and
               follows the same gate as /mixer: tunnel requests need mixer_config.json
               "remote_enabled": true AND a Cloudflare Access JWT.

Needs the polkit rule (netwifi/50-lightboard-nm.rules) so the service user `pi` can drive
NetworkManager without a login session -- netwifi/install_netwifi.sh installs it.
"""

import json
import os
import socket
import subprocess
import threading
import time
import urllib.request

WIFI_DEV = "wlan0"
KIOSK_CMD_FILE = "/tmp/lightboard_kiosk_cmd"   # watched by kiosk_portal_watch.sh in the GUI session
PORTAL_TRIGGER_URL = "http://neverssl.com"     # plain-HTTP URL that trips the captive-portal redirect

# Shared state for the async connect operation. The connect runs in a thread
# because switching networks can drop the very request that started it (when
# you're driving this through the tunnel) — the browser then polls /status.
_connect_state = {"status": "idle", "ssid": None, "detail": "", "ts": 0.0}
_state_lock = threading.Lock()


def _run(args, timeout=20):
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout, p.stderr
    except subprocess.TimeoutExpired:
        return 124, "", "timed out"
    except Exception as e:  # noqa: BLE001
        return 1, "", str(e)


def _split_terse(line):
    """Split an `nmcli -t` line on unescaped ':' (nmcli escapes ':' as '\\:')."""
    out, cur, i = [], "", 0
    while i < len(line):
        c = line[i]
        if c == "\\" and i + 1 < len(line):
            cur += line[i + 1]
            i += 2
            continue
        if c == ":":
            out.append(cur)
            cur = ""
            i += 1
            continue
        cur += c
        i += 1
    out.append(cur)
    return out


def _set_state(status, ssid=None, detail=""):
    with _state_lock:
        _connect_state.update(status=status, ssid=ssid, detail=detail, ts=time.time())


def _kiosk_cmd(value):
    """Signal the in-session kiosk watcher: a URL to display, or 'restore' the touch kiosk."""
    try:
        with open(KIOSK_CMD_FILE, "w") as f:
            f.write(value)
    except Exception:  # noqa: BLE001
        pass


def scan_networks(rescan=True):
    mode = "yes" if rescan else "auto"
    _, out, _ = _run(
        ["nmcli", "-t", "-f", "IN-USE,SSID,SIGNAL,SECURITY",
         "device", "wifi", "list", "ifname", WIFI_DEV, "--rescan", mode], timeout=25)
    nets = {}
    for line in out.splitlines():
        if not line.strip():
            continue
        parts = _split_terse(line)
        if len(parts) < 4:
            continue
        in_use, ssid, signal, security = parts[0], parts[1], parts[2], parts[3]
        if not ssid:  # hidden network
            continue
        try:
            sig = int(signal)
        except ValueError:
            sig = 0
        entry = {
            "ssid": ssid,
            "signal": sig,
            "secure": security not in ("", "--"),
            "in_use": in_use.strip() == "*",
        }
        cur = nets.get(ssid)
        if cur is None or entry["in_use"] or sig > cur["signal"]:
            nets[ssid] = entry
    return sorted(nets.values(), key=lambda n: (0 if n["in_use"] else 1, -n["signal"]))


def _active_wlan_connection():
    _, out, _ = _run(["nmcli", "-t", "-f", "GENERAL.CONNECTION",
                      "device", "show", WIFI_DEV])
    for line in out.splitlines():
        if line.startswith("GENERAL.CONNECTION:"):
            name = line.split(":", 1)[1].strip()
            return name if name and name != "--" else None
    return None


def _wlan_ip():
    _, out, _ = _run(["nmcli", "-t", "-f", "IP4.ADDRESS",
                      "device", "show", WIFI_DEV])
    for line in out.splitlines():
        if line.startswith("IP4.ADDRESS"):
            val = line.split(":", 1)[1].strip()
            return val.split("/")[0] if val else None
    return None


def _nm_connectivity():
    _, out, _ = _run(["nmcli", "networking", "connectivity", "check"], timeout=12)
    return (out.strip() or "unknown")


def _internet_ok():
    """True only if we get a clean 204 (no portal, real internet)."""
    try:
        req = urllib.request.Request(
            "http://connectivitycheck.gstatic.com/generate_204",
            headers={"User-Agent": "stage-messenger-wifi"})
        resp = urllib.request.urlopen(req, timeout=5)
        return getattr(resp, "status", resp.getcode()) == 204
    except Exception:  # noqa: BLE001
        return False


def _kiosk_present():
    """True when this Pi runs the touchscreen kiosk watcher (Lightboard venue/rack builds).
    Headless Stage-Messenger-only Pis have no screen to show a captive portal on."""
    rc, _, _ = _run(["pgrep", "-f", "kiosk_portal_watch"], timeout=5)
    return rc == 0


def get_status():
    with _state_lock:
        connect = dict(_connect_state)
    return {
        "ssid": _active_wlan_connection(),
        "ip": _wlan_ip(),
        "connectivity": _nm_connectivity(),
        "connect": connect,
        "kiosk": _kiosk_present(),
        "hostname": socket.gethostname(),
    }


def _do_connect(ssid, password, prev):
    _set_state("connecting", ssid, "Associating with " + ssid)
    args = ["nmcli", "device", "wifi", "connect", ssid, "ifname", WIFI_DEV]
    if password:
        args += ["password", password]
    rc, out, err = _run(args, timeout=45)

    if rc != 0:
        msg = (err or out or "Could not connect").strip().splitlines()
        _set_state("failed", ssid, (msg[-1] if msg else "Could not connect")[:200])
        _run(["nmcli", "connection", "delete", ssid], timeout=10)  # drop half-made profile
        if prev:
            _run(["nmcli", "connection", "up", prev], timeout=30)
        return

    _set_state("checking", ssid, "Connected — checking for internet")
    ok = False
    for _ in range(6):          # ~18s
        time.sleep(3)
        if _internet_ok():
            ok = True
            break
    if ok:
        _set_state("success", ssid, "Connected to " + ssid + " with internet")
        return

    # No real internet (portal / limited / none) — don't strand: revert.
    label = _nm_connectivity()
    detail = {
        "portal":  "Captive portal detected — no open internet.",
        "limited": "Connected but no internet (limited).",
        "none":    "Connected but no internet.",
    }.get(label, "No internet after connecting.")
    _run(["nmcli", "connection", "modify", ssid, "connection.autoconnect", "no"], timeout=10)
    if prev:
        _run(["nmcli", "connection", "up", prev], timeout=30)
    _set_state("no_internet", ssid, detail + " Reverted to your previous network.")


def _do_portal_login(ssid, password, prev):
    """Connect, then pop the captive portal onto the Pi's touchscreen and wait for
    the user to complete it. Restores the touch kiosk and keeps/reverts accordingly."""
    _set_state("connecting", ssid, "Connecting to " + ssid)
    args = ["nmcli", "device", "wifi", "connect", ssid, "ifname", WIFI_DEV]
    if password:
        args += ["password", password]
    rc, out, err = _run(args, timeout=45)
    if rc != 0:
        msg = (err or out or "Could not connect").strip().splitlines()
        _set_state("failed", ssid, (msg[-1] if msg else "Could not connect")[:200])
        _run(["nmcli", "connection", "delete", ssid], timeout=10)
        if prev:
            _run(["nmcli", "connection", "up", prev], timeout=30)
        return

    # On the venue network now (no internet yet). Show the portal on the Pi screen.
    _kiosk_cmd(PORTAL_TRIGGER_URL)
    _set_state("portal", ssid, "Tap through the venue's login on the Pi's touchscreen.")
    ok = False
    for _ in range(60):                 # up to ~3 min to complete the portal
        time.sleep(3)
        if _internet_ok():
            ok = True
            break
    _kiosk_cmd("restore")               # always put the touch kiosk back

    if ok:
        _set_state("success", ssid, "Portal complete — " + ssid + " is connected with internet.")
        return
    _run(["nmcli", "connection", "modify", ssid, "connection.autoconnect", "no"], timeout=10)
    if prev:
        _run(["nmcli", "connection", "up", prev], timeout=30)
    _set_state("portal_timeout", ssid, "Portal not completed in time — reverted to your previous network.")


# ── Blueprints ─────────────────────────────────────────────────────────────────

from flask import Blueprint, jsonify, redirect, request, send_from_directory, abort  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
MIXER_CONFIG = os.path.join(os.path.dirname(HERE), 'mixer_config.json')


def _remote_enabled():
    try:
        with open(MIXER_CONFIG, encoding='utf-8') as f:
            return bool(json.load(f).get('remote_enabled'))
    except Exception:  # noqa: BLE001
        return False


def _make_bp(name, prefix, local_only):
    bp = Blueprint(name, __name__, url_prefix=prefix)

    @bp.before_request
    def _guard():
        if not request.headers.get('Cf-Connecting-Ip'):
            return None                                   # LAN / localhost: always allowed
        if local_only:
            if request.path.rstrip('/') == prefix:        # someone typed <domain>/wifi: send them to the gated copy
                return redirect('/mixer/wifi', code=302)
            abort(403, 'The local WiFi page is not reachable through the tunnel (use /mixer/wifi).')
        if not _remote_enabled():
            abort(403, 'Remote access is disabled (set remote_enabled in mixer_config.json).')
        if not request.headers.get('Cf-Access-Jwt-Assertion'):
            abort(403, 'Cloudflare Access is not protecting /mixer.')
        return None

    @bp.after_request
    def _no_cache(resp):
        resp.headers['Cache-Control'] = 'no-store'
        return resp

    @bp.route('', strict_slashes=False)
    def page():
        return send_from_directory(HERE, 'wifi.html')

    @bp.route('/api/scan')
    def api_scan():
        rescan = request.args.get('rescan', '1') != '0'
        return jsonify({'networks': scan_networks(rescan)})

    @bp.route('/api/status')
    def api_status():
        return jsonify(get_status())

    def _start(target, busy):
        data = request.get_json(silent=True) or {}
        ssid = (data.get('ssid') or '').strip()
        password = data.get('password') or ''
        if not ssid:
            return jsonify({'ok': False, 'error': 'SSID required'}), 400
        with _state_lock:
            if _connect_state['status'] in busy:
                return jsonify({'ok': False, 'error': 'A connect attempt is already running'}), 409
            _connect_state.update(status='connecting', ssid=ssid, detail='Starting', ts=time.time())
        prev = _active_wlan_connection()
        threading.Thread(target=target, args=(ssid, password, prev), daemon=True).start()
        return jsonify({'ok': True, 'started': True})

    @bp.route('/api/connect', methods=['POST'])
    def api_connect():
        return _start(_do_connect, ('connecting', 'checking', 'portal'))

    @bp.route('/api/portal-login', methods=['POST'])
    def api_portal_login():
        return _start(_do_portal_login, ('connecting', 'checking', 'portal'))

    @bp.route('/api/saved')
    def api_saved():
        _, out, _ = _run(['nmcli', '-t', '-f', 'NAME,TYPE', 'connection', 'show'])
        saved = [p[0] for p in (_split_terse(l) for l in out.splitlines())
                 if len(p) >= 2 and p[1] == '802-11-wireless']
        return jsonify({'saved': saved})

    @bp.route('/api/forget', methods=['POST'])
    def api_forget():
        data = request.get_json(silent=True) or {}
        name = (data.get('name') or '').strip()
        if not name:
            return jsonify({'ok': False, 'error': 'name required'}), 400
        rc, _, err = _run(['nmcli', 'connection', 'delete', name], timeout=15)
        return jsonify({'ok': rc == 0, 'error': (err or '').strip()})

    return bp


def init_netwifi(app):
    app.register_blueprint(_make_bp('netwifi_local', '/wifi', local_only=True))
    app.register_blueprint(_make_bp('netwifi_remote', '/mixer/wifi', local_only=False))
    print('[wifi] page at /wifi (local) and /mixer/wifi (remote, Access-gated)', flush=True)
