"""
WING meter stream (native binary protocol) -- separate from OSC.

Verified against a WING Rack fw 3.1 (Oct 2026): ~21 frames/s, 676-byte frames for
ch1-40 + main1-2, 1/256 dB words.

  TCP 2222, channel 3 (escape 0xdf, select 0xdf 0xd3), payload commands:
    0xd3 <u16 BE udp port>      where the console should send meter frames
    0xd4 <u32 BE report id>     subscription id; re-send < 5 s as keepalive
    0xdc <token idx...>... 0xde collection: token 0xa0 ch, 0xa1 aux, 0xa2 bus,
                                0xa3 main, 0xa4 mtx -- each followed by 0-based indices
  UDP frame: <u32 report id> + int16 BE words, 8 per strip:
    inL, inR, outL, outR, gateKey, gateGain, dynKey, dynGain     (dB = word / 256)

A literal 0xdf in channel payload must be sent as 0xdf 0xde.
"""
import socket
import struct
import threading
import time

TCP_PORT = 2222
ESC = 0xdf
WORDS = 8
REPORT_ID = 0x4C42004D              # no 0xdf bytes

# (key, token, count) in request order -- the frame comes back in this order
GROUPS = [('ch', 0xa0, 40), ('aux', 0xa1, 8), ('bus', 0xa2, 16), ('main', 0xa3, 4), ('mtx', 0xa4, 8)]   # v4.0: Main 1-4 + matrices
FRAME_LEN = 4 + 2 * WORDS * sum(n for _, _, n in GROUPS)


def _escape(b):
    return b.replace(bytes([ESC]), bytes([ESC, 0xde]))


def _collection():
    out = bytearray([0xdc])
    for _, tok, n in GROUPS:
        out.append(tok); out.extend(range(n))
    out.append(0xde)
    return bytes(out)


class Meters:
    """Keeps a meter subscription alive while `wanted()` is true; latest levels in .levels."""

    def __init__(self, ip, wanted=lambda: True, udp_port=14135):
        self.ip = ip
        self.wanted = wanted
        self.udp_port = udp_port
        self.levels = None           # {'ch': [(in, out)...], 'aux': ..., 'bus': ..., 'main': ...}
        self.stamp = 0.0
        self.frames = 0
        self._tcp = None
        self._subscribed = False
        self._stop = threading.Event()
        self.udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            self.udp.bind(('', udp_port))
        except OSError:
            self.udp.bind(('', 0))
            self.udp_port = self.udp.getsockname()[1]
        self.udp.settimeout(0.5)

    def start(self):
        threading.Thread(target=self._rx, daemon=True, name='meter-rx').start()
        threading.Thread(target=self._ctl, daemon=True, name='meter-ctl').start()

    # ── control channel ──
    def _send(self, payload):
        try:
            self._tcp.sendall(_escape(payload))
            return True
        except (OSError, AttributeError):
            self._drop()
            return False

    def _drop(self):
        try:
            if self._tcp:
                self._tcp.close()
        except OSError:
            pass
        self._tcp = None
        self._subscribed = False

    def _connect(self):
        try:
            t = socket.create_connection((self.ip, TCP_PORT), timeout=2)
            t.settimeout(None)
            t.sendall(bytes([ESC, 0xd3]))           # select channel 3 (not escaped)
            self._tcp = t
            threading.Thread(target=self._tcp_drain, args=(t,), daemon=True).start()
            return True
        except OSError:
            self._tcp = None
            return False

    def _tcp_drain(self, t):
        try:
            while t.recv(4096):
                pass
        except OSError:
            pass
        if self._tcp is t:
            self._drop()

    def stop(self):
        self._stop.set()
        self._drop()

    def _ctl(self):
        while not self._stop.is_set():
            if self.wanted() and self.ip:
                if not self._tcp and not self._connect():
                    self._stop.wait(5); continue
                if not self._subscribed:
                    ok = self._send(bytes([0xd3]) + struct.pack('>H', self.udp_port)
                                    + bytes([0xd4]) + struct.pack('>I', REPORT_ID) + _collection())
                    self._subscribed = ok
                else:
                    self._send(bytes([0xd4]) + struct.pack('>I', REPORT_ID))   # keepalive
            else:
                self._subscribed = False             # console drops it after 5 s on its own
                if time.time() - self.stamp > 2:
                    self.levels = None
            self._stop.wait(3)

    # ── data ──
    def _rx(self):
        try:
            self._rx_loop()
        finally:
            self.udp.close()

    def _rx_loop(self):
        while not self._stop.is_set():
            try:
                d, _ = self.udp.recvfrom(4096)
            except socket.timeout:
                if time.time() - self.stamp > 2:
                    self.levels = None
                continue
            except OSError:
                time.sleep(0.5); continue
            if len(d) < FRAME_LEN or struct.unpack('>I', d[:4])[0] != REPORT_ID:
                continue
            w = struct.unpack(f'>{(FRAME_LEN - 4) // 2}h', d[4:FRAME_LEN])
            lv, k = {}, 0
            for key, _, n in GROUPS:
                rows = []
                for _ in range(n):
                    inn = max(w[k], w[k + 1]) / 256.0
                    out = max(w[k + 2], w[k + 3]) / 256.0
                    # gate/dyn: key level in dB; gain reduction as % of the model's full scale
                    # (positive raw word = more reduction; 256 = full scale)
                    rows.append((round(max(-99.0, inn)), round(max(-99.0, out)),
                                 round(max(-99.0, w[k + 4] / 256.0)), max(0, min(100, round(w[k + 5] / 2.56))),
                                 round(max(-99.0, w[k + 6] / 256.0)), max(0, min(100, round(w[k + 7] / 2.56)))))
                    k += WORDS
                lv[key] = rows
            self.levels = lv
            self.stamp = time.time()
            self.frames += 1
