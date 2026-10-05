"""
X32 / M32 meters (OSC blobs) -- same .levels shape as meters.Meters, so the meter pump is unchanged.

Verified on an M32C fw 4.06-8 (Oct 2026): '/meters ,s /meters/N' starts ~20 frames/s for 10 s (renew
every 9 s). Each reply is '/meters/N ,b <blob>': int32 LE count, then count float32 LE, linear
(1.0 = 0 dBFS, floor 3.98e-7 = -128 dB). Matched captured audio exactly (-20 dBFS tone = -20.0).
  /meters/0  70: ch 1-32, aux in 1-8, fx rtn 1-8, bus 1-16, mtx 1-6
  /meters/1  96: ch 1-32 input, ch 1-32 gate gain, ch 1-32 dyn gain (gain 1.0 = no reduction)
  /meters/2  49: bus 1-16, mtx 1-6, LR L, LR R, M/C, then dynamics gains
Channel and bus meters are PRE-fader (a channel read the same at fader 0 dB and -inf; a bus read the
same as its pre-fader output tap), so the post-fader "out" level is computed: pre + fader dB, or
silence when muted. LR / M/C are treated the same way (MAIN_PRE) -- flip if the hardware disagrees.
"""
import math
import socket
import struct
import threading
import time

from .wing import osc_msg
from .x32 import PORT, raw_base, fader_db

BANKS = ('/meters/0', '/meters/1', '/meters/2')
MAIN_PRE = True
GATE_FS, DYN_FS = 60.0, 30.0          # dB of gain reduction shown as 100 % (page bars)


def _db(v):
    return -99.0 if not v or v <= 0 else max(-99.0, 20 * math.log10(v))


class X32Meters:
    def __init__(self, driver, wanted=lambda: True):
        self.drv = driver
        self.ip = driver.ip
        self.wanted = wanted
        self._banks = {}               # '/meters/N' -> (stamp, [floats])
        self.frames = 0
        self._stop = threading.Event()
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1 << 20)
        self.sock.bind(('', 0))
        self.sock.settimeout(1.0)

    def start(self):
        threading.Thread(target=self._ctl, daemon=True, name='x32-meters-ctl').start()
        threading.Thread(target=self._rx, daemon=True, name='x32-meters-rx').start()

    def stop(self):
        self._stop.set()

    def _ctl(self):
        """Subscribe within ~1 s of a page opening; renew every 9 s (the console keeps sending 10 s)."""
        last = 0.0
        while not self._stop.is_set():
            if self.wanted() and self.drv.connected:
                if time.time() - last >= 9:
                    last = time.time()
                    for b in BANKS:
                        try:
                            self.sock.sendto(osc_msg('/meters', b), (self.ip, PORT))
                        except OSError:
                            pass
            else:
                last = 0.0
            self._stop.wait(1)

    def _rx(self):
        while not self._stop.is_set():
            try:
                d, _ = self.sock.recvfrom(4096)
            except socket.timeout:
                continue
            except OSError:
                time.sleep(0.5); continue
            try:
                j = d.index(b'\0')
                addr = d[:j].decode()
                if addr not in BANKS or b',b' not in d[:32]:
                    continue
                i = (j // 4 + 1) * 4 + 4                      # skip ',b\0\0'
                n = struct.unpack('>i', d[i:i + 4])[0]
                blob = d[i + 4:i + 4 + n]
                cnt = struct.unpack('<i', blob[:4])[0]
                vals = struct.unpack(f'<{cnt}f', blob[4:4 + 4 * cnt])
            except (ValueError, struct.error, UnicodeDecodeError):
                continue
            self._banks[addr] = (time.time(), vals)
            self.frames += 1

    def _post(self, kind, n, pre):
        rb = raw_base(kind, n)
        if not self.drv.get_raw(rb + '/mix/on', 1):
            return -99.0
        f = fader_db(self.drv.get_raw(rb + '/mix/fader'))
        return -99.0 if f <= -90 else max(-99.0, pre + f)

    @property
    def stamp(self):
        return max((t for t, _ in self._banks.values()), default=0.0)

    @property
    def levels(self):
        """{'ch': [(in, out, gateKey, gateGR%, dynKey, dynGR%)...], 'aux', 'bus', 'main'} or None."""
        now = time.time()
        b0, b1, b2 = (self._banks.get(k) for k in BANKS)
        if not b0 or now - b0[0] > 2:
            return None
        m0 = b0[1]
        m1 = b1[1] if b1 and now - b1[0] < 2 and len(b1[1]) >= 96 else None
        m2 = b2[1] if b2 and now - b2[0] < 2 and len(b2[1]) >= 25 else None
        gr = lambda g, fs: max(0, min(100, round(-_db(g) / fs * 100))) if g else 0
        ch = []
        for i in range(32):
            pre = round(_db(m1[i] if m1 else m0[i]))
            gk = gg = dg = 0
            if m1:
                gg, dg = gr(m1[32 + i], GATE_FS), gr(m1[64 + i], DYN_FS)
            ch.append((pre, round(self._post('ch', i + 1, pre)), pre, gg, pre, dg))
        aux = []
        for i in range(8):
            pre = round(_db(m0[32 + i]))
            aux.append((pre, round(self._post('aux', i + 1, pre)), pre, 0, pre, 0))
        bus = []
        for i in range(16):
            pre = round(_db(m2[i] if m2 else m0[48 + i]))
            bus.append((pre, round(self._post('bus', i + 1, pre)), pre, 0, pre, 0))
        main = []
        if m2:
            for n, pre in ((1, max(_db(m2[22]), _db(m2[23]))), (2, _db(m2[24]))):
                pre = round(pre)
                out = round(self._post('main', n, pre)) if MAIN_PRE else pre
                main.append((pre, out, pre, 0, pre, 0))
        else:
            main = [(-99, -99, -99, 0, -99, 0)] * 2
        return {'ch': ch, 'aux': aux, 'bus': bus, 'main': main}
