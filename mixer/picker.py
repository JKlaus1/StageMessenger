"""
Channel-pair picker -- runs as its OWN process so the web server's GIL can never stall
audio capture (v1 sliced inside the Flask process; heavy fader traffic caused dropouts).

  usage: picker.py <ctl_file> <capture cmd ...>

stdin : unused
stdout: raw stereo s24le 48 kHz (piped straight into ffmpeg by the parent)
stderr: one status line per ~100 ms ->  "S <peakL_dB> <peakR_dB> <overruns>"
        on capture failure           ->  "E <message>"   then exit 1
ctl   : text file "L R" (zero-based USB channel indices); re-read when its mtime changes.
"""
import math
import os
import subprocess
import sys
import threading
import time

CHANNELS = 48
FRAME = CHANNELS * 3
CHUNK = FRAME * 480                     # 10 ms
FULL = 8388608.0


def read_pair(path, cur):
    try:
        l, r = open(path).read().split()[:2]
        return max(0, min(CHANNELS - 1, int(l))), max(0, min(CHANNELS - 1, int(r)))
    except Exception:
        return cur


def peak_db(buf, off):
    pk = 0
    for i in range(off, len(buf) - 2, 6 * 8):
        v = abs(int.from_bytes(buf[i:i + 3], 'little', signed=True))
        if v > pk:
            pk = v
    return -120.0 if pk == 0 else max(-120.0, 20 * math.log10(pk / FULL))


def main():
    ctl, cmd = sys.argv[1], sys.argv[2:]
    err = sys.stderr
    try:
        cap = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)
    except OSError as e:
        err.write(f'E {e}\n'); err.flush(); sys.exit(1)

    overruns = [0]
    tail = []

    def drain():                         # arecord reports xruns on stderr
        for line in iter(cap.stderr.readline, b''):
            t = line.decode(errors='replace').strip()
            if 'overrun' in t.lower():
                overruns[0] += 1
            elif t:
                tail.append(t); del tail[:-3]
    threading.Thread(target=drain, daemon=True).start()

    out = sys.stdout.buffer
    pair = read_pair(ctl, (0, 1))
    try:
        mtime = os.stat(ctl).st_mtime_ns
    except OSError:
        mtime = 0
    rest = b''
    last = 0.0
    while True:
        data = cap.stdout.read(CHUNK)
        if not data:
            cap.wait()
            err.write('E capture ended' + (': ' + ' | '.join(tail) if tail else '') + '\n')
            err.flush(); sys.exit(1)
        buf = rest + data
        n = len(buf) // FRAME
        rest = buf[n * FRAME:]
        lo, ro = pair[0] * 3, pair[1] * 3
        st = bytearray(n * 6)
        for k in range(3):               # C-speed strided copies, no per-sample Python
            st[k::6] = buf[lo + k:n * FRAME:FRAME]
            st[3 + k::6] = buf[ro + k:n * FRAME:FRAME]
        try:
            out.write(st); out.flush()
        except (BrokenPipeError, OSError):
            cap.kill(); sys.exit(0)
        now = time.time()
        if now - last >= 0.1:
            last = now
            try:
                m = os.stat(ctl).st_mtime_ns
                if m != mtime:
                    mtime = m; pair = read_pair(ctl, pair)
            except OSError:
                pass
            err.write(f'S {peak_db(st, 0):.1f} {peak_db(st, 3):.1f} {overruns[0]}\n'); err.flush()


if __name__ == '__main__':
    main()
