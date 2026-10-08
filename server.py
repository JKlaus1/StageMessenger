import os
import socket
import time
import threading
from flask import Flask, send_from_directory, redirect, request as flask_request
from flask_socketio import SocketIO, emit, join_room, leave_room

app = Flask(__name__, static_folder='public', static_url_path='')
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'stage-messenger-secret')

socketio = SocketIO(app, cors_allowed_origins='*', async_mode='threading')

# ── WING remote mixer + listen-back (/mixer) — optional, never blocks messaging ──
try:
    from mixer import init_mixer
    init_mixer(app)
except Exception as _e:
    print(f'[mixer] disabled: {_e}')

# ── Venue WiFi page (/wifi local, /mixer/wifi remote) — optional, never blocks messaging ──
try:
    from netwifi import init_netwifi
    init_netwifi(app)
except Exception as _e:
    print(f'[wifi] disabled: {_e}')

# ── Device registry ────────────────────────────────────────────────────────────
# { sid: { name, type, role, room, canSend } }
connected_devices = {}

# Control clients (full operator interface)
control_clients = set()

# Quick name uniqueness lookup: { 'NAME_UPPER': sid }
name_registry = {}

# ── Message log ─────────────────────────────────────────────────────────────────
# Held in memory; persists until manually cleared or the Pi is powered off.
message_log = []
log_lock   = threading.Lock()
_log_seq   = 0
LOG_CAP    = 1000

def append_log(kind, frm, message, targets, color):
    global _log_seq
    with log_lock:
        _log_seq += 1
        entry = {
            'id':      _log_seq,
            'ts':      int(time.time() * 1000),
            'kind':    kind,
            'from':    frm,
            'message': message,
            'targets': targets,
            'color':   color,
        }
        message_log.append(entry)
        if len(message_log) > LOG_CAP:
            del message_log[:len(message_log) - LOG_CAP]
    return entry


def device_list():
    """Return serialisable list of connected devices."""
    return [
        {
            'name':    d['name'],
            'type':    d['type'],    # 'display' | 'personal'
            'role':    d['role'],    # 'receiver' | 'sender'
            'room':    d['room'],
            'canSend': d['canSend'],
        }
        for d in connected_devices.values()
    ]


def push_device_list():
    socketio.emit('devices-updated', device_list())


def unregister(sid):
    if sid not in connected_devices:
        return
    device = connected_devices.pop(sid)
    name_upper = device['name'].upper()
    if name_registry.get(name_upper) == sid:
        del name_registry[name_upper]


# ── Routes ─────────────────────────────────────────────────────────────────────

@app.route('/')
def index():
    return send_from_directory('public', 'index.html')

@app.route('/control')
def control():
    return send_from_directory('public', 'control.html')

@app.route('/display')
def display():
    return send_from_directory('public', 'display.html')


# ── Installable app (Android "Install app" / home-screen, full screen) ─────────
# public/pwa.js links each page to /app.webmanifest?start=<that page's path+query>, so whatever
# page you install from (an auto-join link, /mixer?view=console, ...) is what the icon opens.
_PWA = {   # path prefix -> (name, short_name, icon, scope)
    '/mixer':   ('Stage Messenger Mixer', 'Mixer', 'mixer', '/mixer'),
    '/control': ('Stage Messenger Control', 'SM Control', 'control', '/'),
    '/display': ('Stage Monitor', 'Stage Mon', 'messenger', '/'),
    '/':        ('Stage Messenger', 'Messenger', 'messenger', '/'),
}

@app.route('/app.webmanifest')
def app_manifest():
    import json
    from urllib.parse import urlsplit, parse_qs
    start = flask_request.args.get('start', '/')
    u = urlsplit(start)
    if u.scheme or u.netloc or not u.path.startswith('/') or start.startswith('//'):
        start, u = '/', urlsplit('/')
    key = next(k for k in _PWA if k == '/' or u.path == k or u.path.startswith(k + '/'))
    name, short, icon, scope = _PWA[key]
    who = (parse_qs(u.query).get('name') or [''])[0].strip().upper()[:16]
    if key == '/' and who:                      # one icon per auto-join link
        name, short = f'Stage Messenger — {who}', who.title()[:12]
    m = {
        'id': start, 'name': name, 'short_name': short,
        'start_url': start, 'scope': scope,
        'display': 'fullscreen', 'display_override': ['fullscreen', 'standalone'],
        'orientation': 'any',
        'background_color': '#0d0d1a', 'theme_color': '#0d0d1a',
        'icons': [{'src': f'/pwa/{icon}-{px}.png', 'sizes': f'{px}x{px}', 'type': 'image/png',
                   'purpose': p} for px in (192, 512) for p in ('any', 'maskable')],
    }
    resp = app.response_class(json.dumps(m), mimetype='application/manifest+json')
    resp.headers['Cache-Control'] = 'no-cache'
    return resp


# ── Socket events ──────────────────────────────────────────────────────────────

@socketio.on('connect')
def handle_connect():
    pass


@socketio.on('disconnect')
def handle_disconnect():
    sid = flask_request.sid
    control_clients.discard(sid)
    if sid in connected_devices:
        unregister(sid)
        push_device_list()


@socketio.on('register-control')
def handle_register_control():
    control_clients.add(flask_request.sid)
    join_room('control-clients')
    emit('devices-updated', device_list())
    with log_lock:
        emit('log-history', list(message_log))


@socketio.on('register')
def handle_register(data):
    """
    Register a device.
    data = {
      type:      'display' | 'personal',
      role:      'receiver' | 'sender',
      name:      str,
      monitorId: int  (display only),
      canSend:   bool
    }
    """
    sid        = flask_request.sid
    name       = data.get('name', '').strip().upper()
    dtype      = data.get('type', 'personal')
    role       = data.get('role', 'receiver')
    can_send   = bool(data.get('canSend', False))

    # Senders are always personal devices
    if role == 'sender':
        can_send = True

    # ── Validate name ──────────────────────────────────────────────────────────
    if not name:
        emit('register-rejected', {'reason': 'Please enter a name.'})
        return
    if len(name) > 16:
        emit('register-rejected', {'reason': 'Name must be 16 characters or fewer.'})
        return
    if not all(c.isalnum() or c in (' ', '-', '_') for c in name):
        emit('register-rejected', {'reason': 'Letters, numbers, spaces and hyphens only.'})
        return

    # ── Name collision ─────────────────────────────────────────────────────────
    existing_sid = name_registry.get(name)
    if existing_sid and existing_sid != sid:
        emit('register-rejected', {
            'reason': f'"{name}" is already taken — choose a different name.'
        })
        return

    # ── Clean up previous registration ────────────────────────────────────────
    if sid in connected_devices:
        old = connected_devices[sid]
        leave_room(old['room'])
        old_upper = old['name'].upper()
        if name_registry.get(old_upper) == sid:
            del name_registry[old_upper]

    # ── Assign room ────────────────────────────────────────────────────────────
    if dtype == 'display':
        monitor_id = int(data.get('monitorId', 0))
        room = f'mon-{monitor_id}'
    else:
        room = f'personal-{name}'

    join_room(room)
    join_room('all-devices')

    connected_devices[sid] = {
        'name':    name,
        'type':    dtype,
        'role':    role,
        'room':    room,
        'canSend': can_send,
    }
    name_registry[name] = sid

    emit('register-accepted', {'name': name, 'room': room, 'role': role})
    push_device_list()
    print(f'[+] {dtype.upper():8s} {role.upper():8s}  "{name}"  ({room})')


@socketio.on('send-message')
def handle_send_message(data):
    sid = flask_request.sid

    # Only authorised senders and control clients may send
    device = connected_devices.get(sid)
    is_control = sid in control_clients
    is_sender  = device and device.get('canSend')

    if not is_control and not is_sender:
        return

    targets = data.get('targets', ['all'])
    payload = {
        'message':  data.get('message', ''),
        'subtitle': data.get('subtitle', ''),
        'color':    data.get('color', '#cc0000'),
        'duration': data.get('duration', 30000),
        'from':     device['name'] if device else 'Operator',
    }
    if not payload['message']:
        return

    if 'all' in targets:
        emit('message', payload, room='all-devices')
    else:
        for room in targets:
            emit('message', payload, room=room)

    # Also notify control clients
    socketio.emit('message', payload, room='control-clients')

    entry = append_log('sent' if is_control else 'incoming',
                       payload['from'], payload['message'], targets, payload['color'])
    socketio.emit('log-entry', entry, room='control-clients')
    print(f'[M] {payload["from"]} → {targets}: "{payload["message"]}"')


@socketio.on('clear')
def handle_clear(data):
    sid = flask_request.sid
    device = connected_devices.get(sid)
    is_control = sid in control_clients
    is_sender  = device and device.get('canSend')
    if not is_control and not is_sender:
        return

    targets = data.get('targets', ['all'])
    if 'all' in targets:
        emit('clear', room='all-devices')
    else:
        for room in targets:
            emit('clear', room=room)

    frm   = device['name'] if device else 'Operator'
    label = 'All Devices' if 'all' in targets else ', '.join(targets)
    entry = append_log('system', frm, '— CLEAR: ' + label + ' —', targets, '#555')
    socketio.emit('log-entry', entry, room='control-clients')


@socketio.on('clear-log')
def handle_clear_log():
    sid = flask_request.sid
    device = connected_devices.get(sid)
    is_control = sid in control_clients
    is_sender  = device and device.get('canSend')
    if not is_control and not is_sender:
        return
    with log_lock:
        message_log.clear()
    socketio.emit('log-cleared', room='control-clients')
    print('[L] message log cleared')


# ── Startup ────────────────────────────────────────────────────────────────────

def local_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(('8.8.8.8', 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return 'localhost'


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 3000))
    ip   = local_ip()
    print(f'\n✅  Stage Messenger  —  http://{ip}:{port}\n')
    print(f'  Landing:   http://{ip}:{port}/')
    print(f'  Control:   http://{ip}:{port}/control')
    print(f'  Display 1: http://{ip}:{port}/display?monitor=1&name=STAGE+L')
    print(f'  Display 2: http://{ip}:{port}/display?monitor=2&name=CTR')
    print(f'  Display 3: http://{ip}:{port}/display?monitor=3&name=STAGE+R')
    socketio.run(app, host='0.0.0.0', port=port, debug=False, allow_unsafe_werkzeug=True)
