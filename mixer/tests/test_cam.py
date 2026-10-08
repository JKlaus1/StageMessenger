"""
Cam feed (v3.4-v3.7): picker cam output + delay line (off-hardware).

The picker slices a second stereo pair out of the same capture for the video encoder, delays it, and
writes it to a fifo through a bounded queue. The listen-back output must stay bit-exact and never wait
on the cam side. A fake 48-channel capture encodes (channel, frame) into every sample so each output
frame can be traced back to where it came from.

    cd ~/stage-messenger && python3 -m mixer.tests.test_cam
"""
import json
import os
import re
import select
import subprocess
import sys
import tempfile
import threading
import time

from mixer.tests.test_x32_api import check, wait_for, setup, FAILS
from mixer.tests.test_wing_regression import FakeWing
from mixer import picker
from mixer.cam import Cam
from mixer.listen import Listener

HERE = os.path.dirname(os.path.abspath(__file__))
PICKER = os.path.join(os.path.dirname(HERE), 'picker.py')
FR = 6                                           # bytes per stereo s24 frame


def val(c, f):
    return (c + 1) * 100000 + f                  # < 2^23 for 48 channels and 20000 frames


# Emits N frames of 48 channels (channel c, frame f -> val(c, f)), then lingers so the picker's
# writer thread can drain before the capture "ends" and the picker exits.
FAKE_RAMP = r'''
import sys, time
N, CH = int(sys.argv[1]), 48
w = sys.stdout.buffer
for s in range(0, N, 480):
    b = bytearray()
    for f in range(s, min(N, s + 480)):
        for c in range(CH):
            b += ((c + 1) * 100000 + f).to_bytes(3, 'little')
    w.write(b); w.flush()
time.sleep(float(sys.argv[2]))
'''

# Cheap constant audio, lots of it (for the stalled-reader test).
FAKE_BULK = r'''
import sys, time
frames = int(sys.argv[1])
blob = b''.join(((c + 1) * 1000).to_bytes(3, 'little') for c in range(48)) * 480
w = sys.stdout.buffer
for _ in range(frames // 480):
    w.write(blob); w.flush()
time.sleep(0.3)
'''


def frames_of(b):
    return [(int.from_bytes(b[i:i + 3], 'little'), int.from_bytes(b[i + 3:i + 6], 'little'))
            for i in range(0, len(b) - FR + 1, FR)]


def pump(fd, sink, stop):
    while not stop.is_set():
        r, _, _ = select.select([fd], [], [], 0.1)
        if r:
            try:
                d = os.read(fd, 65536)
            except BlockingIOError:
                continue
            if d:
                sink.append(d)


def run_picker(tmp, script, args, cam_line=None, read_cam=True, timeout=60):
    """-> (listen_bytes, cam_bytes, seconds, stderr_text)"""
    ctl = os.path.join(tmp, 'pair'); camctl = os.path.join(tmp, 'pair.cam'); fifo = os.path.join(tmp, 'cam.pcm')
    if os.path.exists(fifo):
        os.remove(fifo)
    os.mkfifo(fifo)
    open(ctl, 'w').write('0 1\n')
    if cam_line is not None:
        open(camctl, 'w').write(cam_line.replace('FIFO', fifo) + '\n')
    elif os.path.exists(camctl):
        os.remove(camctl)
    hold = os.open(fifo, os.O_RDWR | os.O_NONBLOCK)                       # the manager's reader-keeper
    env = dict(os.environ, PICKER_CAM_CTL=camctl) if cam_line is not None else dict(os.environ)
    env.pop('PICKER_CAM_CTL', None) if cam_line is None else None
    cam, stop = [], threading.Event()
    if read_cam:
        threading.Thread(target=pump, args=(hold, cam, stop), daemon=True).start()
    t0 = time.time()
    p = subprocess.Popen([sys.executable, PICKER, ctl, sys.executable, '-c', script] + args,
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
    out, err = p.communicate(timeout=timeout)
    dt = time.time() - t0
    time.sleep(0.2)
    stop.set()
    time.sleep(0.15)
    os.close(hold)
    return out, b''.join(cam), dt, err.decode(errors='replace')


def test_delay_line():
    print('delay line')
    DL = picker.DelayLine
    n_fr, D = 40, 7
    x = [bytes([i % 250 + 1]) * 0 + (i + 1).to_bytes(3, 'little') * 2 for i in range(n_fr * 5)]   # frame i -> value i+1
    dl, out = DL(), []
    sizes = [3, 1, 7, 2, 11, 5, 13, 4]
    pos, k = 0, 0
    while pos < len(x):
        m = sizes[k % len(sizes)]; k += 1
        chunk = b''.join(x[pos:pos + m]); pos += m
        out.append(dl.process(chunk, D * FR))
    y = frames_of(b''.join(out))
    check('delay: same length as input', len(y) == len(x), (len(y), len(x)))
    check('delay: first D frames silent', all(v == (0, 0) for v in y[:D]))
    check('delay: exactly D frames late', all(y[i + D][0] == i + 1 for i in range(len(x) - D)))

    # raise the delay mid-stream: silence is inserted, nothing repeats, nothing is lost
    dl, out, pos = DL(), [], 0
    for i in range(0, 60, 2):
        d = 4 if i < 30 else 9
        out.append(dl.process(b''.join(x[i:i + 2]), d * FR))
    y = [a for a, _ in frames_of(b''.join(out))]
    nz = [v for v in y if v]
    check('raise: no repeated audio', nz == sorted(set(nz)), nz[:40])
    check('raise: nothing lost', nz == list(range(1, len(nz) + 1)), nz[:40])
    check('raise: now 9 frames late at the end', y[-1] == 60 - 9, y[-3:])

    # lower the delay: the surplus is skipped, order kept, ends 3 frames late
    dl, out = DL(), []
    for i in range(0, 60, 2):
        d = 12 if i < 30 else 3
        out.append(dl.process(b''.join(x[i:i + 2]), d * FR))
    y = [a for a, _ in frames_of(b''.join(out))]
    nz = [v for v in y if v]
    check('lower: order kept (no repeats)', nz == sorted(set(nz)), nz[:40])
    check('lower: now 3 frames late at the end', y[-1] == 60 - 3, y[-3:])
    check('lower: same length', len(y) == 60)


def test_camout_never_blocks():
    print('cam writer')
    with tempfile.TemporaryDirectory() as tmp:
        fifo = os.path.join(tmp, 'f'); os.mkfifo(fifo)
        hold = os.open(fifo, os.O_RDWR | os.O_NONBLOCK)                    # reader exists, never reads
        co = picker.CamOut(fifo)
        chunk = bytes(2880)
        t0 = time.time()
        for _ in range(2000):                                              # 20 s of audio into a stalled reader
            co.put(chunk)
        dt = time.time() - t0
        check('put() never blocks on a stalled reader', dt < 1.0, dt)
        time.sleep(0.3)
        check('queue stays bounded (~2 s)', co.qbytes <= picker.CAM_QUEUE_S * picker.BYTES_PER_S + 4096, co.qbytes)
        check('older audio was dropped', co.dropped > 0, co.dropped)
        co.close(); time.sleep(0.4); os.close(hold)


def test_picker_end_to_end():
    print('picker: listen untouched, cam delayed')
    N = 20000
    with tempfile.TemporaryDirectory() as tmp:
        d_ms = 100
        out, cam, dt, err = run_picker(tmp, FAKE_RAMP, [str(N), '0.8'], cam_line=f'5 6 {d_ms} FIFO')
        L = frames_of(out)
        check('listen: every frame delivered', len(L) == N, len(L))
        check('listen: channel 0/1 bit-exact', all(L[f] == (val(0, f), val(1, f)) for f in range(0, N, 97)) and L[-1] == (val(0, N - 1), val(1, N - 1)))
        C = frames_of(cam)
        D = d_ms * 48
        check('cam: delivered at the same rate', abs(len(C) - N) <= 480, (len(C), N))
        check('cam: first 100 ms silent', all(v == (0, 0) for v in C[:D]))
        ok = all(C[f + D] == (val(5, f), val(6, f)) for f in range(0, min(len(C) - D, N - D), 53))
        check('cam: channel 5/6 exactly 100 ms (4800 frames) late', ok, C[D:D + 3])
        check('picker exit is the capture ending, not a crash', 'E capture ended' in err and 'Traceback' not in err, err[-200:])

        out0, cam0, _, err0 = run_picker(tmp, FAKE_RAMP, [str(N), '0.3'], cam_line='off')
        check('cam off: no cam audio, listen unchanged', not cam0 and out0 == out, (len(cam0), len(out0), len(out)))

        out1, cam1, _, err1 = run_picker(tmp, FAKE_RAMP, [str(N), '0.3'], cam_line=None)
        check('no cam control at all: listen unchanged', not cam1 and out1 == out)

        out2, cam2, _, err2 = run_picker(tmp, FAKE_RAMP, [str(N), '0.8'], cam_line='2 3 0 FIFO')
        C2 = frames_of(cam2)
        check('zero delay: straight copy of the pair', all(C2[f] == (val(2, f), val(3, f)) for f in range(0, min(len(C2), N), 61)) and len(C2) >= N - 480, len(C2))


def test_picker_stalled_cam_reader():
    print('picker: a stuck video encoder must not hold up listen')
    frames = 48000 * 5                              # 5 s of 48-channel audio, ~35 MB, as fast as the pipe takes it
    with tempfile.TemporaryDirectory() as tmp:
        out, cam, dt, err = run_picker(tmp, FAKE_BULK, [str(frames)], cam_line='5 6 250 FIFO', read_cam=False, timeout=90)
        got = len(out) // FR
        check('listen: all 5 s delivered while the cam reader never read', got == frames, (got, frames))
        check('listen: finished promptly (not throttled by cam)', dt < 25, dt)
        check('no tracebacks', 'Traceback' not in err, err[-300:])


# Real-time 48-channel capture: silence with ONE click on USB channels 5/6 (zero-based 4/5) at 1.2 s.
FAKE_RT = r"""
import sys, time
secs = float(sys.argv[1])
CH, click_at = 48, 1.2
sil = bytes(CH * 3 * 480)
fr = bytearray(CH * 3 * 480)
click = bytearray(sil)
for f in range(5):
    for c in (4, 5):
        click[f * CH * 3 + c * 3: f * CH * 3 + c * 3 + 3] = (6000000).to_bytes(3, 'little')
w = sys.stdout.buffer
t0 = time.monotonic(); n = 0
while n * 0.01 < secs:
    w.write(bytes(click) if n == int(click_at / 0.01) else sil); w.flush()
    n += 1
    d = t0 + n * 0.01 - time.monotonic()
    if d > 0:
        time.sleep(d)
"""

LAVFI = ['-re', '-use_wallclock_as_timestamps', '1', '-f', 'lavfi', '-i', 'testsrc2=size=640x360:rate=30']


def ffprobe_start(path, kind):
    r = subprocess.run(['ffprobe', '-v', 'error', '-select_streams', kind, '-show_entries', 'stream=start_time',
                        '-of', 'csv=p=0', path], capture_output=True, text=True)
    try:
        return float(r.stdout.split()[0])
    except (IndexError, ValueError):
        return None


def click_time(path):
    """Seconds (on the file's timeline) of the click in the audio stream."""
    raw = subprocess.run(['ffmpeg', '-v', 'error', '-i', path, '-map', '0:a:0', '-f', 's16le', '-ar', '48000', '-ac', '1', '-'],
                         capture_output=True).stdout
    pk = max(range(0, len(raw) - 1, 2), key=lambda i: abs(int.from_bytes(raw[i:i + 2], 'little', signed=True)), default=None)
    if pk is None or abs(int.from_bytes(raw[pk:pk + 2], 'little', signed=True)) < 3000:
        return None
    return (ffprobe_start(path, 'a:0') or 0.0) + (pk // 2) / 48000.0


def test_cam_sub_blend():
    """v4.2: the Listen "+ Subs" also goes into the video's console sound (picker 'blend' line)."""
    print('cam: + Subs blend (picker + cam control line + Mixer wiring)')
    with tempfile.TemporaryDirectory() as tmp:
        f = os.path.join(tmp, 'c')
        open(f, 'w').write('4 5 120 /dev/shm/x y.pcm\nblend 10 11 0.5\n')
        check('parse: pair, delay, fifo (spaces ok) + blend', picker.parse_cam_ctl(f) == (4, 5, 120 * 48, '/dev/shm/x y.pcm', (10, 11, 0.5)),
              picker.parse_cam_ctl(f))
        open(f, 'w').write('4 5 0 /p.pcm\n')
        check('parse: no 2nd line -> blend None (old format)', picker.parse_cam_ctl(f) == (4, 5, 0, '/p.pcm', None))
        open(f, 'w').write('4 5 0 /p.pcm\nblend 1 2 x\nblend 1 2 0\n')
        check('parse: bad / zero gain blend ignored', picker.parse_cam_ctl(f)[4] is None)
        open(f, 'w').write('off\nblend 1 2 1\n')
        check('parse: off stays off', picker.parse_cam_ctl(f) is None)

        N, g = 9600, 0.5
        out, cam, dt, err = run_picker(tmp, FAKE_RAMP, [str(N), '0.8'], cam_line=f'2 3 0 FIFO\nblend 10 11 {g}')
        L, C = frames_of(out), frames_of(cam)
        check('blend: listen pair untouched', len(L) == N and all(L[i] == (val(0, i), val(1, i)) for i in range(0, N, 97)))
        exp = lambda i: tuple(int(round(val(ch, i) + g * (val(10, i) + val(11, i)) / 2)) for ch in (2, 3))
        ok = len(C) >= N - 480 and all(abs(C[i][0] - exp(i)[0]) <= 1 and abs(C[i][1] - exp(i)[1]) <= 1
                                       for i in range(480, min(len(C), N), 59))
        check('blend: cam = pair + gain*(SL+SR)/2 after the 10 ms ramp', ok, (C[480:482], [exp(480), exp(481)]))
        check('blend: no picker errors', 'Traceback' not in err and 'blend failed' not in err, err[-200:])

        ctl = os.path.join(tmp, 'cam.ctl')
        lst = Listener(capture_cmd=['false'], bitrate='64k', cam_ctl=ctl)
        feeds = lambda: [{'id': 'main1', 'usb': [1, 2]}, {'id': 'main2', 'usb': [3, 4]}, {'id': 'bus1', 'usb': [5, 6]}]
        want = {'v': (2, 3, 0.5)}
        c = Cam({'enabled': True, 'feed': 'main1'}, lst, feeds, probe=lambda: (False, False, 0), ctl_path=ctl,
                video_input=LAVFI, blend=lambda fid: want['v'] if fid == 'main1' else None)
        c.running, c.audio_kind, c._fifo = True, 'console', '/tmp/v.pcm'
        c._write_ctl()
        check('cam ctl: Main LR + subs -> blend line', open(ctl).read() == '0 1 0 /tmp/v.pcm\nblend 2 3 0.500000\n', open(ctl).read())
        check('cam status says subs on', c.status()['subs'] is True)
        want['v'] = None; c.blend_changed()
        check('cam ctl: subs off -> plain line', open(ctl).read() == '0 1 0 /tmp/v.pcm\n', open(ctl).read())
        want['v'] = (2, 3, 2.0); c.set_feed('bus1')
        check('cam ctl: another feed -> no blend', open(ctl).read() == '4 5 0 /tmp/v.pcm\n', open(ctl).read())
        c.running = False; c._write_ctl()
        check('cam ctl: off when not running', open(ctl).read() == 'off\n')


def make_cam(tmp, delay_ms, out_file, feeds, secs_capture=10, **cfg):
    ctl = os.path.join(tmp, 'cam.ctl')
    lst = Listener(capture_cmd=[sys.executable, '-c', FAKE_RT, str(secs_capture)], bitrate='64k', cam_ctl=ctl)
    c = Cam({'enabled': True, 'delay_ms': delay_ms, 'feed': 'main1', 'idle_s': 1.0, **cfg}, lst, feeds,
            probe=lambda: (False, False, 0), ctl_path=ctl, video_input=LAVFI,
            output=['-y', '-f', 'matroska', out_file])
    return lst, c, ctl


def test_cam_pipeline():
    print('cam: audio delay shows up in the encoded stream')
    feeds = lambda: [{'id': 'main1', 'usb': [5, 6]}]
    times = {}
    with tempfile.TemporaryDirectory() as tmp:
        for d in (0, 500):
            out = os.path.join(tmp, f'out{d}.mkv')
            lst, c, ctl = make_cam(tmp, d, out, feeds)
            check(f'delay {d}: camera counts as available', c.available())
            c.hold(60)
            time.sleep(0.8)
            check(f'delay {d}: encoder running with audio', c.running and c.audio and c.error == '', c.status())
            line = open(ctl).read().split()
            check(f'delay {d}: picker told pair 4/5 (USB 5/6), delay, fifo', line[:3] == ['4', '5', str(d)] and line[3].endswith('.pcm'), line)
            check(f'delay {d}: listen capture started by the cam', lst.running)
            time.sleep(4.2)
            c._stop('test')
            check(f'delay {d}: ctl back to off after stop', open(ctl).read().strip() == 'off')
            check(f'delay {d}: fifo cleaned up', not [f for f in os.listdir('/dev/shm') if f.startswith(f'stage_cam_{os.getpid()}') and f.endswith('.pcm')] if os.path.isdir('/dev/shm') else True)
            lst._teardown()
            t = click_time(out)
            times[d] = t
            check(f'delay {d}: file has H.264 video + Opus audio',
                  ffprobe_start(out, 'v:0') is not None and ffprobe_start(out, 'a:0') is not None)
            check(f'delay {d}: click found in the encoded audio', t is not None, t)
        if times.get(0) is not None and times.get(500) is not None:
            diff = times[500] - times[0]
            check('encoded click moved ~500 ms later with delay_ms=500', 0.40 <= diff <= 0.60, f'{diff:.3f}s ({times})')
            check('click lands ~1.2 s + startup after video start (sane timeline)', 0.5 < times[0] < 4.0, times[0])


def test_cam_video_only_and_idle():
    print('cam: video-only (no console audio) and idle shutdown')
    with tempfile.TemporaryDirectory() as tmp:
        out = os.path.join(tmp, 'v.mkv')
        lst, c, ctl = make_cam(tmp, 0, out, lambda: [])
        c.probe = lambda: (True, True, 0)                       # API up, stream ready, nobody watching
        check('no feeds -> status says no audio', c.status()['audio'] is False)
        c.hold(1.0)
        time.sleep(0.7)
        check('video-only: running without audio', c.running and not c.audio, c.status())
        check('video-only: listen capture NOT started for it', not lst.running)
        check('video-only: picker cam output stays off', open(ctl).read().strip() == 'off')
        t0 = time.time()
        while c.running and time.time() - t0 < 8:
            time.sleep(0.25)
        check('stops by itself after the last viewer + idle_s', not c.running, c.status())
        time.sleep(0.3)
        check('video-only file has no audio stream', ffprobe_start(out, 'v:0') is not None and ffprobe_start(out, 'a:0') is None)
        check('keepalive released', not lst.keepalive())
        lst._teardown()


def test_cam_capture_unavailable():
    print('cam: console audio not there (no WING on USB) -> picture still comes up, says why')
    with tempfile.TemporaryDirectory() as tmp:
        out = os.path.join(tmp, 'noaudio.mkv')
        ctl = os.path.join(tmp, 'cam.ctl')
        lst = Listener(capture_cmd=['false'], bitrate='64k', cam_ctl=ctl)          # capture dies at once
        c = Cam({'enabled': True, 'delay_ms': 0, 'feed': 'main1', 'idle_s': 1.0}, lst,
                lambda: [{'id': 'main1', 'usb': [1, 2]}], probe=lambda: (False, False, 0), ctl_path=ctl,
                video_input=LAVFI, output=['-y', '-f', 'matroska', out])
        c.hold(60)
        check('starts expecting audio', c.running and c.audio)
        t0 = time.time()
        while time.time() - t0 < 10 and (c.audio or not c.running):
            time.sleep(0.25)
        took = time.time() - t0
        check('gives up on audio within a few seconds', not c.audio and c.running and took < 6, (c.status(), took))
        st = c.status()
        check('status explains why it is picture-only', bool(st['audio_issue']) and st['running'], st)
        check('picker cam output stays off', open(ctl).read().strip() == 'off')
        time.sleep(1.2)
        c._stop('test')
        lst._teardown()
        check('output has video and no audio', ffprobe_start(out, 'v:0') is not None and ffprobe_start(out, 'a:0') is None)
        check('capture is not kept alive for a cam without audio', not lst.keepalive())


def test_cam_failure():
    print('cam: a camera that cannot start gives up with a reason')
    with tempfile.TemporaryDirectory() as tmp:
        out = os.path.join(tmp, 'f.mkv')
        lst, c, ctl = make_cam(tmp, 0, out, lambda: [])
        c._video_input = ['-f', 'lavfi', '-i', 'nosuchsource=1']
        c.hold(60)
        t0 = time.time()
        while time.time() - t0 < 25 and (c.running or not c.error):
            time.sleep(0.25)
        check('gives up instead of looping forever', not c.running, c.status())
        check('error text reports what ffmpeg said', bool(c.error), c.status())
        check('restarts were bounded', 1 <= c.restarts <= 6, c.restarts)
        lst._teardown()


# A stand-in for a phone running IP Webcam: multipart MJPEG at /video, 800x600 (4:3, so the pad path runs).
class FakePhone:
    def __init__(self, size='800x600', rate=15, user=None, ipw=False, port=0, orientation='landscape'):
        import http.server, socketserver
        outer = self
        self.size, self.rate, self.hits, self.procs = size, rate, 0, []
        self.ipw, self.orientation, self.status_hits, self.audio_hits = ipw, orientation, 0, 0

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                if outer.ipw and self.path == '/status.json':        # IP Webcam's status (v3.8 probe)
                    outer.status_hits += 1
                    body = json.dumps({'curvals': {'orientation': outer.orientation, 'video_size': '1280x720'}}).encode()
                    self.send_response(200); self.send_header('Content-Type', 'application/json')
                    self.send_header('Content-Length', str(len(body))); self.end_headers(); self.wfile.write(body)
                    return
                if outer.ipw and self.path == '/audio.wav':          # IP Webcam's mic: 44.1 kHz mono wav stream
                    outer.audio_hits += 1
                    self.send_response(200); self.send_header('Content-Type', 'audio/x-wav'); self.end_headers()
                    p = subprocess.Popen(['ffmpeg', '-v', 'error', '-re', '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=44100',
                                          '-ac', '1', '-c:a', 'pcm_s16le', '-f', 'wav', '-'], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
                    outer.procs.append(p)
                    try:
                        while True:
                            b = p.stdout.read(4096)
                            if not b:
                                break
                            self.wfile.write(b)
                    except OSError:
                        pass
                    finally:
                        p.kill()
                    return
                if self.path != '/video':
                    self.send_error(404); return
                outer.hits += 1
                self.send_response(200)
                self.send_header('Content-Type', 'multipart/x-mixed-replace;boundary=ffmpeg')
                self.end_headers()
                p = subprocess.Popen(['ffmpeg', '-v', 'error', '-re', '-f', 'lavfi', '-i',
                                      f'testsrc2=size={outer.size}:rate={outer.rate}', '-c:v', 'mjpeg', '-q:v', '8',
                                      '-f', 'mpjpeg', '-boundary_tag', 'ffmpeg', '-'],
                                     stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
                outer.procs.append(p)
                try:
                    while True:
                        b = p.stdout.read(4096)
                        if not b:
                            break
                        self.wfile.write(b)
                except OSError:
                    pass
                finally:
                    p.kill()

        class S(socketserver.ThreadingMixIn, http.server.HTTPServer):
            daemon_threads = True
            allow_reuse_address = True
        self.srv = S(('127.0.0.1', port), H)
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def stop(self):
        for p in self.procs:
            p.kill()
        self.srv.shutdown(); self.srv.server_close()


def video_size(path):
    r = subprocess.run(['ffprobe', '-v', 'error', '-select_streams', 'v:0', '-show_entries', 'stream=width,height',
                        '-of', 'csv=p=0', path], capture_output=True, text=True)
    try:
        w, h = r.stdout.strip().split(',')[:2]
        return int(w), int(h)
    except ValueError:
        return None


def net_cam(tmp, url, out_file=None, **cfg):
    ctl = os.path.join(tmp, 'cam.ctl')
    lst = Listener(capture_cmd=['false'], bitrate='64k', cam_ctl=ctl)
    c = Cam({'enabled': True, 'idle_s': 1.0, 'video_url': url, **cfg}, lst, lambda: [],
            probe=lambda: (False, False, 0), ctl_path=ctl,
            output=['-y', '-f', 'matroska', out_file] if out_file else None)
    return lst, c


def test_cam_net_source_setup():
    print('cam: network camera (video_url) -- config, command line, redaction')
    from mixer.cam import clean_url, redact
    with tempfile.TemporaryDirectory() as tmp:
        lst, c = net_cam(tmp, 'http://192.168.1.50:8080/video', quality='low')
        cmd = c._cmd(False)
        check('http url: available without a USB camera', c.available() and c.status()['source'] == 'network', c.status())
        check('http url: ffmpeg reads the url, not v4l2', '-i' in cmd and 'http://192.168.1.50:8080/video' in cmd and 'v4l2' not in cmd, cmd)
        check('http url: wall-clock stamps + reconnect + read timeout',
              '-use_wallclock_as_timestamps' in cmd and '-reconnect' in cmd and '-rw_timeout' in cmd, cmd)
        vf = cmd[cmd.index('-vf') + 1]
        check('picture fitted to the tier (low -> 854x480, 10 fps) with even dims',
              vf.startswith('fps=10,scale=854:480:force_original_aspect_ratio=decrease:force_divisible_by=2:flags=bilinear,pad=854:480:'), vf)
        check('url never appears in status', 'video_url' not in str(c.status()) and '192.168' not in str(c.status()), c.status())
        lst._teardown()
        lst, c = net_cam(tmp, 'rtsp://u:p@192.168.1.50:8554/live', rtsp_transport='udp')
        cmd = c._cmd(False)
        check('rtsp url: transport + socket timeout, no http reconnect flags',
              cmd[cmd.index('-rtsp_transport') + 1] == 'udp' and '-timeout' in cmd and '-reconnect' not in cmd, cmd)
        lst._teardown()
        lst, c = net_cam(tmp, 'rtsp://h/x', rtsp_transport='bogus')
        cmd = c._cmd(False)
        check('bad rtsp transport falls back to tcp', cmd[cmd.index('-rtsp_transport') + 1] == 'tcp', cmd)
        lst._teardown()
        for bad in ('file:///etc/passwd', 'concat:a|b', '/dev/video0', 'ftp://h/x', 'javascript:1'):
            lst, c = net_cam(tmp, bad, device='/nonexistent/video0')
            check(f'rejects {bad!r}', c.url == '' and not c.available() and c.status()['source'] == 'usb', c.status())
            lst._teardown()
        lst, c = net_cam(tmp, '', device='/nonexistent/video0')
        check('no url, no camera -> not available (unchanged)', not c.available())
        lst._teardown()
        check('clean_url strips whitespace', clean_url('  HTTP://x/y ') == 'HTTP://x/y')
        check('redact hides user:password', redact('Error opening http://admin:s3cret@10.0.0.5:8080/video: Refused') ==
              'Error opening http://***@10.0.0.5:8080/video: Refused')
        check('redact leaves plain urls and text alone', redact('http://10.0.0.5/video x@y') == 'http://10.0.0.5/video x@y')


def test_cam_net_pipeline():
    print('cam: network camera end to end (fake phone serving multipart MJPEG)')
    phone = FakePhone()
    try:
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, 'net.mkv')
            lst, c = net_cam(tmp, f'http://127.0.0.1:{phone.port}/video', out)
            c.hold(60)
            time.sleep(0.8)
            check('encoder running from the phone feed', c.running and not c.audio and c.error == '', c.status())
            time.sleep(4.0)
            check('phone saw exactly one client', phone.hits == 1, phone.hits)
            c._stop('test')
            lst._teardown()
            check('output has H.264 video', ffprobe_start(out, 'v:0') is not None)
            check('4:3 phone picture fitted into 1280x720 (good tier default)', video_size(out) == (1280, 720), video_size(out))
            nfr = subprocess.run(['ffprobe', '-v', 'error', '-select_streams', 'v:0', '-count_packets', '-show_entries',
                                  'stream=nb_read_packets', '-of', 'csv=p=0', out], capture_output=True, text=True).stdout.strip()
            check('about 4 s of 30 fps video was produced', nfr.isdigit() and 60 <= int(nfr) <= 160, nfr)
    finally:
        phone.stop()
    print('cam: network camera that is not there -> gives up with a reason, password not leaked')
    with tempfile.TemporaryDirectory() as tmp:
        out = os.path.join(tmp, 'dead.mkv')
        lst, c = net_cam(tmp, 'http://admin:s3cretpw@127.0.0.1:9/video', out)
        c.hold(60)
        t0 = time.time()
        while time.time() - t0 < 40 and (c.running or not c.error):
            time.sleep(0.25)
        check('gives up instead of looping forever', not c.running, c.status())
        check('error says something', bool(c.error), c.status())
        check('password not in error / status', 's3cretpw' not in c.error and 's3cretpw' not in str(c.status()), c.error)
        check('restarts were bounded', 1 <= c.restarts <= 6, c.restarts)
        lst._teardown()


def test_cam_api():
    print('cam: /mixer API, snapshot, saved settings')
    import json
    fake = FakeWing()
    tmp, mixer, mx, c = setup({'mixer_ip': '127.0.0.1', 'mixer_type': 'wing', 'spotify': {'enabled': False},
                               'cam': {'device': '/nonexistent/video0'}})
    mx2 = None
    try:
        wait_for(lambda: mx.wing.loaded, 12)
        st = c.get('/mixer/api/state').get_json()
        cs = st.get('cam') or {}
        check('snapshot carries cam status', cs.get('enabled') is True and 'qualities' in cs, cs)
        check('no camera plugged in -> not available', cs.get('available') is False and cs.get('running') is False, cs)
        check('audio would be available on a WING', cs.get('audio') is True, cs)
        r = c.post('/mixer/api/cam/whep', data=b'v=0\r\n', content_type='application/sdp')
        check('whep with no camera -> 404 (nothing started)', r.status_code == 404 and not mx.cam.running, r.status_code)
        check('v3.5 defaults: Good tier, 128k sound, 5 tiers, 4 sound rates',
              cs.get('quality') == 'good' and cs.get('audio_bitrate') == '128k' and len(cs.get('qualities', [])) == 5
              and cs.get('audio_rates') == ['64k', '96k', '128k', '160k'], cs)
        r = c.post('/mixer/api/cam/set', json={'feed': 'bus2', 'delay_ms': 250, 'quality': 'low'}).get_json()
        check('set feed / delay / quality', r.get('ok') and r['cam']['feed'] == 'bus2' and r['cam']['delay_ms'] == 250 and r['cam']['quality'] == 'low', r)
        r = c.post('/mixer/api/cam/set', json={'audio_bitrate': '160k'}).get_json()
        check('set the video sound bitrate', r.get('ok') and r['cam']['audio_bitrate'] == '160k', r)
        check('unknown sound bitrate rejected', c.post('/mixer/api/cam/set', json={'audio_bitrate': '999k'}).status_code == 400)
        ls = st.get('listen') or {}
        check('listen status: Opus bitrate 128k + choices', ls.get('opus_bitrate') == '128k' and ls.get('opus_rates') == ['64k', '96k', '128k', '160k'], ls)
        seen = []
        orig = mx.cam.blend_changed
        mx.cam.blend_changed = lambda: (seen.append(mx.cam._blend()), orig())
        mx.cam.feed = 'main1'
        c.post('/mixer/api/listen/set', json={'sub_on': True, 'sub_db': 6})
        b = mx.cam._blend()
        check('v4.2: Listen + Subs reaches the video (Main 2 pair, +6 dB)', seen and b and abs(b[2] - 10 ** 0.3) < 1e-3
              and b[:2] == mx._blend_for('main1', mx.feeds())[:2], (seen, b))
        c.post('/mixer/api/listen/set', json={'sub_on': False})
        check('v4.2: + Subs off -> video sound plain again', mx.cam._blend() is None and seen[-1] is None, seen)
        mx.cam.blend_changed = orig
        mx.cam.feed = 'bus2'                          # back to what the checks below expect
        r = c.post('/mixer/api/listen/set', json={'opus_bitrate': '64k'})
        check('set the Listen low-latency bitrate', r.status_code == 200 and r.get_json()['listen']['opus_bitrate'] == '64k'
              and mx.listener.opus_bitrate == '64k', r.get_json())
        check('unknown Listen bitrate rejected', c.post('/mixer/api/listen/set', json={'opus_bitrate': '7k'}).status_code == 400
              and mx.listener.opus_bitrate == '64k')
        check('delay clamps to 0..3000', c.post('/mixer/api/cam/set', json={'delay_ms': 99999}).get_json()['cam']['delay_ms'] == 3000
              and c.post('/mixer/api/cam/set', json={'delay_ms': -40}).get_json()['cam']['delay_ms'] == 0)
        check('unknown feed rejected', c.post('/mixer/api/cam/set', json={'feed': 'nope'}).status_code == 400)
        check('unknown quality rejected', c.post('/mixer/api/cam/set', json={'quality': '4k'}).status_code == 400)
        check('bad delay rejected', c.post('/mixer/api/cam/set', json={'delay_ms': 'x'}).status_code == 400)
        c.post('/mixer/api/cam/set', json={'delay_ms': 320})
        with open(os.path.join(tmp, 'mixer_state.json')) as f:
            sf = json.load(f)
        check('settings saved in mixer_state.json', sf.get('cam') == {'feed': 'bus2', 'delay_ms': 320, 'quality': 'low', 'audio_bitrate': '160k',
                                                                     'source': '', 'rot': {}, 'cams': []}, sf.get('cam'))
        # v3.8: Wi-Fi cameras added on the page, chosen, rotated, removed -- the URL never comes back
        r = c.post('/mixer/api/cam/add', json={'name': 'Phone', 'url': 'ftp://10.9.9.9/x'})
        check('add: only http(s) / rtsp addresses', r.status_code == 400)
        r = c.post('/mixer/api/cam/add', json={'name': 'Phone', 'url': 'http://user:pw@10.9.9.9:8080/video', 'select': True}).get_json()
        cid = r.get('id', '')
        st2 = r.get('cam') or {}
        check('add + select: listed, chosen, available', r.get('ok') and any(x['id'] == cid and x['name'] == 'Phone' and x['kind'] == 'net' and x['mic'] and x['auto']
              for x in st2.get('sources', [])) and st2.get('choice') == cid and st2.get('available') is True, st2)
        check('the address / password never reach the page', '10.9.9.9' not in json.dumps(st2) and 'pw' not in json.dumps(st2.get('sources')), st2.get('sources'))
        r = c.post('/mixer/api/cam/set', json={'rotate': '90'}).get_json()
        check('rotate the chosen camera', r.get('ok') and r['cam']['rotate'] == '90' and r['cam']['rotate_mode'] == '90', r.get('cam', {}).get('rotate'))
        check('auto-rotate accepted for an IP Webcam', c.post('/mixer/api/cam/set', json={'rotate': 'auto'}).status_code == 200)
        check('bad rotation rejected', c.post('/mixer/api/cam/set', json={'rotate': '45'}).status_code == 400)
        check('unknown camera rejected', c.post('/mixer/api/cam/set', json={'source': 'n000000'}).status_code == 400)
        with open(os.path.join(tmp, 'mixer_state.json')) as f:
            sc = json.load(f).get('cam') or {}
        check('cameras + choice + rotation saved on the Pi', sc.get('source') == cid and sc.get('rot') == {cid: 'auto'}
              and sc.get('cams') == [{'id': cid, 'name': 'Phone', 'url': 'http://user:pw@10.9.9.9:8080/video'}], sc)
        r = c.post('/mixer/api/cam/set', json={'feed': 'mic'}).get_json()
        check('sound from the camera mic can be chosen', r.get('ok') and r['cam']['feed'] == 'mic' and r['cam']['audio_kind'] == 'mic', r.get('cam', {}).get('audio_kind'))
        c.post('/mixer/api/cam/set', json={'feed': 'bus2'})
        r = c.post('/mixer/api/cam/remove', json={'id': cid}).get_json()
        check('remove: gone, choice back to automatic', r.get('ok') and not any(x['id'] == cid for x in r['cam']['sources']) and mx.cam.choice == '', r.get('cam', {}).get('choice'))
        check('remove unknown -> 400', c.post('/mixer/api/cam/remove', json={'id': 'nope'}).status_code == 400)
        c.post('/mixer/api/cam/set', json={'delay_ms': 320})
        check('Listen bitrate saved in mixer_state.json', sf.get('listen') == {'opus_bitrate': '64k', 'sub_on': False, 'sub_db': 6.0}, sf.get('listen'))
        check('listen feed untouched by cam feed change', mx.feed_id == 'main1', mx.feed_id)
        r = c.delete('/mixer/api/cam/session/not-a-session')
        check('session delete validates the id', r.status_code == 400)
        r = c.get('/mixer')
        check('page still served', r.status_code == 200 and b'listen-card' in r.data)
    finally:
        mx.wing.stop()
    # a fresh controller picks the saved values up again
    tmp2, mixer2, mx2, c2 = setup({'mixer_ip': '127.0.0.1', 'mixer_type': 'wing', 'spotify': {'enabled': False},
                                   'cam': {'device': '/nonexistent/video0'}},
                                  state={'cam': {'feed': 'bus2', 'delay_ms': 320, 'quality': 'low', 'audio_bitrate': '160k'},
                                         'listen': {'opus_bitrate': '64k'}})
    try:
        cs = mx2.cam.status()
        check('restart: feed / delay / quality / sound restored', (cs['feed'], cs['delay_ms'], cs['quality'], cs['audio_bitrate']) == ('bus2', 320, 'low', '160k'), cs)
        check('restart: Listen bitrate restored', mx2.listener.opus_bitrate == '64k', mx2.listener.opus_bitrate)
    finally:
        mx2.wing.stop()
    # a v3.4 saved tier name falls back to the default tier
    tmp4, mixer4, mx4, c4 = setup({'mixer_ip': '127.0.0.1', 'mixer_type': 'wing', 'spotify': {'enabled': False},
                                   'cam': {'device': '/nonexistent/video0'}},
                                  state={'cam': {'feed': 'main1', 'delay_ms': 0, 'quality': '720p30'}})
    try:
        cs = mx4.cam.status()
        check('old 720p30 setting -> Good tier, 128k sound', cs['quality'] == 'good' and cs['audio_bitrate'] == '128k', cs)
    finally:
        mx4.wing.stop()
    # MediaMTX off in the config -> the cam is off and the listener is not told about a cam file
    tmp3, mixer3, mx3, c3 = setup({'mixer_ip': '127.0.0.1', 'mixer_type': 'wing', 'spotify': {'enabled': False},
                                   'rtc': {'enabled': False}})
    try:
        check('rtc disabled -> cam disabled', mx3.cam.status()['enabled'] is False and mx3.listener.cam_ctl == '')
        check('rtc disabled -> whep 404', c3.post('/mixer/api/cam/whep', data=b'v=0\r\n').status_code == 404)
    finally:
        mx3.wing.stop()
        fake.stop()


# A steady 1 kHz tone on USB 5/6, delivered with scheduling hiccups like a busy Pi (bursty fifo writes).
FAKE_TONE = r"""
import sys, time, math, random
secs = float(sys.argv[1]); CH = 48; N = 480
w = sys.stdout.buffer
t0 = time.monotonic(); n = 0
random.seed(1)
while n * 0.01 < secs:
    b = bytearray(CH * 3 * N)
    for f in range(N):
        bb = int(3000000 * math.sin(2 * math.pi * 1000 * (n * N + f) / 48000)).to_bytes(3, 'little', signed=True)
        o = f * CH * 3
        b[o + 12:o + 15] = bb; b[o + 15:o + 18] = bb
    w.write(b); w.flush(); n += 1
    d = t0 + n * 0.01 - time.monotonic()
    if random.random() < 0.03:
        d += random.uniform(0.02, 0.08)
    if d > 0:
        time.sleep(d)
"""


def test_cam_pitch_steady():
    print('cam: sound keeps its pitch when the fifo delivers in bursts (v3.7: no stretching -> no warble)')
    with tempfile.TemporaryDirectory() as tmp:
        out = os.path.join(tmp, 'tone.mkv')
        ctl = os.path.join(tmp, 'cam.ctl')
        lst = Listener(capture_cmd=[sys.executable, '-c', FAKE_TONE, '14'], bitrate='64k', cam_ctl=ctl)
        c = Cam({'enabled': True, 'delay_ms': 0, 'feed': 'main1', 'idle_s': 60}, lst, lambda: [{'id': 'main1', 'usb': [5, 6]}],
                probe=lambda: (False, False, 0), ctl_path=ctl, video_input=LAVFI, output=['-y', '-f', 'matroska', out])
        c.hold(60)
        time.sleep(10)
        c._stop('test')
        lst._teardown()
        raw = subprocess.run(['ffmpeg', '-v', 'error', '-i', out, '-map', '0:a:0', '-f', 's16le', '-ac', '1', '-ar', '48000', '-'],
                             capture_output=True).stdout
        smp = [int.from_bytes(raw[i:i + 2], 'little', signed=True) for i in range(0, len(raw) - 1, 2)][48000:]
        W = 9600                                       # 200 ms windows: 400 zero crossings at 1 kHz, 2.5 Hz resolution
        freqs = []
        for k in range(0, len(smp) - W, W):
            seg = smp[k:k + W]
            z = sum(1 for i in range(1, W) if (seg[i - 1] < 0) != (seg[i] < 0))
            freqs.append(z / 2 / 0.2)
        bad = [f for f in freqs if abs(f - 1000) > 6]
        check('tone encoded for several seconds', len(freqs) >= 25, len(freqs))
        check('pitch steady at 1 kHz in every 200 ms window (no +-2 % warble)', not bad, (min(freqs or [0]), max(freqs or [0]), len(bad)))


def test_cam_helpers():
    print('cam (v3.8): camera names, built-in mic lookup, IP Webcam urls, rotation from gravity')
    from mixer.cam import usb_name, usb_mic, ipw_base, host_port, phys_rotation
    check('USB name without vendor / serial', usb_name('/dev/v4l/by-id/usb-Nexight_Inc_NexiGo_N930E_FHD_Webcam_AN202312190001-video-index0') == 'NexiGo N930E FHD Webcam',
          usb_name('/dev/v4l/by-id/usb-Nexight_Inc_NexiGo_N930E_FHD_Webcam_AN202312190001-video-index0'))
    check('USB name: plain one kept', usb_name('/dev/v4l/by-id/usb-046d_HD_Pro_Webcam_C920_ABCD1234-video-index0') == '046d HD Pro Webcam C920')
    with tempfile.TemporaryDirectory() as t:                  # a fake sysfs: webcam 3-1 (video + mic), WING 1-2
        dev = os.path.join(t, 'devices')
        for d in ('3-1/3-1:1.0', '3-1/3-1:1.2', '1-2/1-2:1.0'):
            os.makedirs(os.path.join(dev, d))
        os.makedirs(os.path.join(t, 'sys/class/video4linux/video0'))
        os.symlink(os.path.join(dev, '3-1/3-1:1.0'), os.path.join(t, 'sys/class/video4linux/video0/device'))
        for n, (path, cid) in enumerate((('1-2/1-2:1.0', 'WING'), ('3-1/3-1:1.2', 'Webcam'))):
            cd = os.path.join(t, f'sys/class/sound/card{n}')
            os.makedirs(cd)
            os.symlink(os.path.join(dev, path), os.path.join(cd, 'device'))
            open(os.path.join(cd, 'id'), 'w').write(cid + '\n')
        os.makedirs(os.path.join(t, 'dev/v4l/by-id'))
        open(os.path.join(t, 'dev/video0'), 'w').close()
        link = os.path.join(t, 'dev/v4l/by-id/usb-x-video-index0')
        os.symlink(os.path.join(t, 'dev/video0'), link)
        check('webcam mic = the sound card on the same USB device (not the WING)', usb_mic(link, os.path.join(t, 'sys')) == 'hw:CARD=Webcam',
              usb_mic(link, os.path.join(t, 'sys')))
        check('no sysfs -> no mic', usb_mic(link, os.path.join(t, 'nothing')) == '')
    check('IP Webcam base from /video url (keeps the login)', ipw_base('http://u:p@192.168.1.195:8080/video') == 'http://u:p@192.168.1.195:8080'
          and ipw_base('rtsp://h/x') == '' and ipw_base('http://h/cam.mjpg') == '')
    check('host / port with defaults', host_port('http://h/x') == ('h', 80) and host_port('rtsp://u:p@h/x') == ('h', 554) and host_port('http://h:8080/v') == ('h', 8080))
    check('gravity -> rotation', (phys_rotation(9.8, 0.3), phys_rotation(0.2, 9.7), phys_rotation(-9.6, 1), phys_rotation(0.5, -9.8))
          == ('0', '90', '180', '270'))
    check('flat or diagonal -> keep (None)', phys_rotation(0.3, 0.4) is None and phys_rotation(6.5, 6.0) is None)


def test_cam_sources():
    print('cam (v3.8): camera list -- USB first, chosen Wi-Fi camera while online, fall back, follow, rotate, mic')
    phone = FakePhone(ipw=True)
    online = {'v': None}

    def fake_probe(url, timeout=1.5):
        return (online['v'] if online['v'] is not None else True), ({'ipw': True, 'orientation': 'landscape'} if online['v'] else {})
    try:
        with tempfile.TemporaryDirectory() as tmp:
            ctl = os.path.join(tmp, 'cam.ctl')
            out = os.path.join(tmp, 's.mkv')
            lst = Listener(capture_cmd=['false'], bitrate='64k', cam_ctl=ctl)
            purl = f'http://127.0.0.1:{phone.port}/video'
            saved = []
            c = Cam({'enabled': True, 'idle_s': 30, 'video_url': 'rtsp://10.1.1.1/x', 'video_name': 'Cfg cam',
                     'cams': [{'id': 'nabc123', 'name': 'Phone', 'url': purl}, {'id': 'bad', 'url': purl}, {'id': 'n000001', 'url': 'file:///x'}]},
                    lst, lambda: [], probe=lambda: (False, False, 0), ctl_path=ctl, video_input=LAVFI,
                    output=['-y', '-f', 'matroska', out], net_probe=fake_probe, save=saved.append)
            ids = [x['id'] for x in c.sources()]
            check('sources: built-in camera first, config camera, saved phone (bad entries dropped)', ids == ['test', 'net0', 'nabc123'], ids)
            check('default = the built-in (USB-like) camera even with a Wi-Fi one configured', c.active_source()['id'] == 'test' and c.url == '')
            check('IP Webcam url -> its mic + auto-rotate; rtsp camera -> sound rides in the stream',
                  c.sources()[2]['mic'] == f'http://127.0.0.1:{phone.port}/audio.wav' and c.sources()[2]['ipw'] and c.sources()[1]['mic'] == 'same')
            c.hold(60)
            time.sleep(1.0)
            e0 = c.epoch
            check('running on the built-in camera', c.running and c._active['id'] == 'test' and e0 >= 1, c.status())
            check('choose the phone', c.set_source('nabc123'))
            time.sleep(1.5)
            check('encoder moved to the phone (new epoch)', c.running and c._active['id'] == 'nabc123' and c.epoch > e0 and phone.hits >= 1, (c._active, c.epoch, phone.hits))
            online['v'] = False
            c.probe_once()
            time.sleep(1.0)
            st = c.status()
            check('phone offline -> back on the built-in camera, says it is a fallback', c._active['id'] == 'test' and st['fallback'] and st['choice'] == 'nabc123'
                  and next(x for x in st['sources'] if x['id'] == 'nabc123')['online'] is False, (c._active['id'], st['fallback']))
            online['v'] = True
            c.probe_once()
            time.sleep(1.0)
            check('phone back online -> goes back to it by itself', c._active['id'] == 'nabc123' and not c.status()['fallback'], c._active['id'])
            online['v'] = False
            c._started_at = time.time() - 10                  # streaming from it for a while: its own stream proves it
            c.probe_once()
            check('while the phone is streaming fine, a failed status check does not switch away', c._active['id'] == 'nabc123' and c._online['nabc123'] is True)
            c._started_at = time.time()
            c.probe_once()
            check('one missed check on busy Wi-Fi is not "offline"', c._online['nabc123'] is True)
            online['v'] = True
            c.probe_once()
            check('rotate 90 -> encoder restarts turned, portrait 720x1280', c.set_rotate('90') and c._eff_rot(c._active) == '90')
            cmd = c._cmd(False, c._active)
            vf = cmd[cmd.index('-vf') + 1]
            check('rotation filter + portrait fit', 'transpose=1' in vf and 'scale=720:1280' in vf and 'pad=720:1280' in vf, vf)
            check('auto only for IP Webcam', not c.set_rotate('auto', 'net0') and c.set_rotate('auto', 'nabc123'))
            # auto-rotate from the accelerometer: portrait held 1.5 s -> 90
            restarts = []
            c._restart = lambda why: restarts.append(why)
            c._accel = lambda base: None
            c._auto_step()
            check('no sensor data -> tells you to turn it on', 'sensor' in c.status()['auto_note'], c.status()['auto_note'])
            c._accel = lambda base: (0.3, 9.7, 0.5)
            c._auto_step()
            check('first portrait reading only arms it', not restarts and c._auto_cand and c._auto_cand[0] == '90')
            c._auto_cand = ('90', time.time() - 2)
            c._auto_step()
            check('held portrait -> picture turned 90 and the encoder restarted', restarts == ['phone rotated'] and c._eff_rot(c._active) == '90', (restarts, c._auto))
            c._info['nabc123'] = {'ipw': True, 'orientation': 'portrait'}
            c._auto_cand = ('0', time.time() - 2)
            c._accel = lambda base: (0.3, 9.7, 0.5)
            restarts.clear(); c._auto_step()
            check('app already streaming portrait -> held portrait needs no turn (0)', c._auto['nabc123'] == '0' and restarts == ['phone rotated'], c._auto)
            del c._restart
            # camera mic
            c.feed = 'mic'
            check('mic chosen + camera has one -> mic sound', c._audio_plan(c._active) == 'mic')
            c.delay_ms = 150
            c._fifo = '/dev/shm/x.pcm'
            cmd = c._cmd('mic', c._active)
            check('mic (v3.8.2): encoder reads a steady fifo (not the phone directly), no filter delay, stereo',
                  f'http://127.0.0.1:{phone.port}/audio.wav' not in cmd and '/dev/shm/x.pcm' in cmd and cmd[cmd.index('-map', cmd.index('-map') + 1) + 1] == '1:a:0'
                  and 'adelay' not in cmd[cmd.index('-af') + 1] and cmd[cmd.index('-ac', cmd.index('-c:a')) + 1] == '2', cmd)
            rc = c._mic_reader_cmd(c._active['mic'])
            check('mic decoder: phone audio.wav -> raw 48 kHz stereo on stdout', f'http://127.0.0.1:{phone.port}/audio.wav' in rc and rc[-1] == 'pipe:1'
                  and rc[rc.index('-f', rc.index('-i')) + 1] == 's24le', rc)
            rc = c._mic_reader_cmd('hw:CARD=Webcam')
            check('USB webcam mic decoder: ALSA card, mono 48 kHz (alsa demuxer options)', 'alsa' in rc and 'hw:CARD=Webcam' in rc
                  and rc[rc.index('-channels') + 1] == '1' and rc[rc.index('-sample_rate') + 1] == '48000', rc)
            c._fifo = ''
            rt = {'id': 'net0', 'kind': 'net', 'url': 'rtsp://10.1.1.1/x', 'mic': 'same', 'name': 'x'}
            cmd = c._cmd('mic', rt)
            check('rtsp camera mic: optional audio from the same input, delay as a filter', '0:a:0?' in cmd and cmd.count('-i') == 1
                  and cmd[cmd.index('-af') + 1].startswith('adelay=150:all=1,'), cmd)
            check('mic chosen but the built-in camera has none -> no console here -> picture only', c._audio_plan(c.sources()[0]) == '')
            c._stop('test')
            c.delay_ms = 0
            check('settings saved with cameras, choice and rotation', saved and saved[-1]['source'] == 'nabc123' and saved[-1]['rot'].get('nabc123') == 'auto'
                  and [x['id'] for x in saved[-1]['cams']] == ['nabc123'], saved[-1] if saved else None)
            # add / remove
            check('add rejects other schemes', c.add_camera('x', 'file:///etc/passwd') is None)
            nid = c.add_camera('  Back of room camera that has a long name  ', 'http://10.0.0.7:4747/video')
            src = next(x for x in c.sources() if x['id'] == nid)
            check('add: id + trimmed name (30 max); DroidCam port -> no mic / no auto', bool(re.match(r'^n[0-9a-f]{6}$', nid))
                  and src['name'].startswith('Back of room') and len(src['name']) <= 30 and not src['mic'] and not src['ipw'], src)
            check('remove', c.remove_camera(nid) and not any(x['id'] == nid for x in c.sources()))
            check('status never carries addresses', '127.0.0.1' not in json.dumps(c.status()) and '10.1.1.1' not in json.dumps(c.status()))
            lst._teardown()
    finally:
        phone.stop()


def test_cam_mic_end_to_end():
    print('cam (v3.8): sound from the phone mic (IP Webcam /audio.wav) ends up in the stream')
    phone = FakePhone(ipw=True)
    try:
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, 'mic.mkv')
            lst, c = net_cam(tmp, f'http://127.0.0.1:{phone.port}/video', out, feed='mic', idle_s=30)
            c.hold(60)
            time.sleep(5.5)
            check('running with the camera mic', c.running and c.audio_kind == 'mic' and phone.audio_hits == 1, (c.status(), phone.audio_hits))
            c._stop('test')
            lst._teardown()
            raw = subprocess.run(['ffmpeg', '-v', 'error', '-i', out, '-map', '0:a:0', '-f', 's16le', '-ac', '1', '-ar', '48000', '-'],
                                 capture_output=True).stdout
            smp = [int.from_bytes(raw[i:i + 2], 'little', signed=True) for i in range(0, len(raw) - 1, 2)]
            fr = []
            for k in range(max(0, len(smp) - 48000), len(smp) - 4800 + 1, 4800):   # last second, 100 ms windows
                seg = smp[k:k + 4800]
                fr.append(sum(1 for i in range(1, 4800) if (seg[i - 1] < 0) != (seg[i] < 0)) / 2 / 0.1)
            good = sum(1 for f in fr if abs(f - 440) <= 10)
            check('the phone\'s 440 Hz tone is in the encoded sound (steady in the last second)', len(smp) > 2 * 48000 and good >= 7, (fr, len(smp)))
    finally:
        phone.stop()


def test_mic_relay():
    print('cam (v3.8.2): MicRelay turns a bursty mic into a steady 10 ms stream, live delay, gaps -> silence')
    from mixer.cam import MicRelay
    # a "phone" that delivers 1 kHz tone in 100 ms bursts (like IP Webcam's audio.wav), 4 s
    BURSTY = r"""
import sys, time, math
w = sys.stdout.buffer
t0 = time.monotonic()
for k in range(40):
    b = bytearray()
    for f in range(4800):
        n = k * 4800 + f
        v = int(4000000 * math.sin(2 * math.pi * 1000 * n / 48000)).to_bytes(3, 'little', signed=True)
        b += v + v
    w.write(b); w.flush()
    d = t0 + (k + 1) * 0.1 - time.monotonic()
    if d > 0:
        time.sleep(d)
"""
    with tempfile.TemporaryDirectory() as tmp:
        fifo = os.path.join(tmp, 'm.pcm')
        os.mkfifo(fifo)
        holder = os.open(fifo, os.O_RDWR | os.O_NONBLOCK)
        rd = os.open(fifo, os.O_RDONLY | os.O_NONBLOCK)
        delay = {'ms': 0}
        r = MicRelay([sys.executable, '-c', BURSTY], fifo, lambda: delay['ms'])
        time.sleep(0.3)
        r.go()
        arrivals, data = [], bytearray()
        t_end = time.time() + 3.0
        while time.time() < t_end:
            select.select([rd], [], [], 0.05)
            try:
                b = os.read(rd, 65536)
            except BlockingIOError:
                continue
            if b:
                arrivals.append((time.time(), len(b))); data += b
        r.stop(); os.close(rd); os.close(holder)
        # steadiness: bytes delivered per 100 ms window stay near 28800 (10 x 10 ms chunks)
        t0 = arrivals[0][0] if arrivals else 0
        win = {}
        for t, n in arrivals:
            win[int((t - t0) / 0.1)] = win.get(int((t - t0) / 0.1), 0) + n
        vals = [win.get(i, 0) for i in range(2, 28)]
        check('relay delivers ~10 ms every 10 ms (no 100 ms bursts)', vals and min(vals) >= 0.6 * 28800 and max(vals) <= 1.4 * 28800, vals)
        check('3 s of audio came through', abs(len(data) - 3 * 288000) < 0.15 * 3 * 288000, len(data))
        check('mic decoder ran (got data), not dead', r.got > 0 and not r.dead)


def test_wait_fifo_open():
    print('cam (v3.8): console sound is switched on only once ffmpeg really has the fifo open')
    with tempfile.TemporaryDirectory() as tmp:
        lst = Listener(capture_cmd=['false'], bitrate='64k', cam_ctl=os.path.join(tmp, 'c'))
        c = Cam({'enabled': True}, lst, lambda: [], probe=lambda: (False, False, 0), ctl_path=os.path.join(tmp, 'c'), video_input=LAVFI)
        c._fifo = os.path.join(tmp, 'f.pcm')
        os.mkfifo(c._fifo)
        holder = os.open(c._fifo, os.O_RDWR | os.O_NONBLOCK)
        p = subprocess.Popen([sys.executable, '-c', f'import time, os; time.sleep(1.2); f = open({c._fifo!r}, "rb"); time.sleep(2)'])
        t0 = time.time()
        ok = c._wait_fifo_open(p, c._gen, timeout=5)
        dt = time.time() - t0
        check('waited for the reader to open the fifo (~1.2 s), not a fixed 0.4 s', ok and 1.1 <= dt <= 2.0, (ok, dt))
        p.kill(); os.close(holder)


def test_cam_tiers():
    print('cam: picture tiers (v3.5) -- frame rate, size, preset, bitrate really end up in the stream')
    from mixer.cam import QUALITIES
    with tempfile.TemporaryDirectory() as tmp:
        for name, want_h, want_fps in (('min', 360, 5), ('medium', 720, 15)):
            out = os.path.join(tmp, f'{name}.mkv')
            lst, c, ctl = make_cam(tmp, 0, out, lambda: [], quality=name, idle_s=30)
            c._video_input = ['-re', '-use_wallclock_as_timestamps', '1', '-f', 'lavfi', '-i', 'testsrc2=size=1280x720:rate=30']
            cmd = c._cmd(False)
            q = QUALITIES[name]
            check(f'{name}: x264 preset {q["preset"]}, bitrate {q["bitrate"]}',
                  cmd[cmd.index('-preset') + 1] == q['preset'] and cmd[cmd.index('-b:v') + 1] == q['bitrate'], cmd)
            c.hold(1.0)
            time.sleep(3.5)
            c._stop('test')
            lst._teardown()
            r = subprocess.run(['ffprobe', '-v', 'error', '-select_streams', 'v:0', '-count_frames', '-show_entries',
                                'stream=width,height,nb_read_frames:format=duration', '-of', 'default=nw=1', out],
                               capture_output=True, text=True).stdout
            kv = dict(l.split('=', 1) for l in r.split() if '=' in l)
            try:
                h, n, dur = int(kv['height']), int(kv['nb_read_frames']), float(kv['duration'])
            except (KeyError, ValueError):
                h, n, dur = 0, 0, 0.0
            check(f'{name}: {want_h}p output', h == want_h and int(kv.get('width', 0)) == round(want_h * 16 / 9 / 2) * 2, kv)
            fps = n / dur if dur else 0
            check(f'{name}: ~{want_fps} fps encoded', abs(fps - want_fps) <= max(1.5, want_fps * 0.2), (n, dur, fps))
        # the real camera's capture args: always 1280x720 MJPEG at a rate the N930E offers
        c = Cam({'enabled': True, 'quality': 'low', 'device': '/dev/null'}, Listener(capture_cmd=['true']), lambda: [],
                probe=lambda: (False, False, 0), ctl_path=os.path.join(tmp, 'x.ctl'))
        cmd = c._cmd(False)
        check('low tier: camera opened at 1280x720 @ 20 fps, scaled to 854x480',
              cmd[cmd.index('-video_size') + 1] == '1280x720' and cmd[cmd.index('-framerate') + 1] == '20'
              and 'scale=854:480' in cmd[cmd.index('-vf') + 1], cmd)
        c.quality = 'good'; cmd = c._cmd(True)
        check('good tier: no scaling, 30 fps, sound at the chosen Opus bitrate',
              'scale' not in cmd[cmd.index('-vf') + 1] and cmd[cmd.index('-framerate') + 1] == '30'
              and cmd[cmd.index('-b:a') + 1] == '128k', cmd)
    lst = Listener(capture_cmd=['true'], rtc_url='rtsp://127.0.0.1:1/x', opus_bitrate='128k')
    calls = []
    lst._restart_encoder = lambda why: calls.append(why)
    lst.running = True
    ok = lst.set_opus_bitrate('96k'); lst.set_opus_bitrate('96k')
    check('Listen bitrate change respawns a running encoder once', ok and lst.opus_bitrate == '96k' and len(calls) == 1, calls)
    check('Listen bitrate: unknown value refused, nothing restarted', not lst.set_opus_bitrate('100k') and len(calls) == 1)
    lst.running = False


def main():
    test_delay_line()
    test_camout_never_blocks()
    test_picker_end_to_end()
    test_picker_stalled_cam_reader()
    test_cam_sub_blend()
    test_cam_pipeline()
    test_cam_video_only_and_idle()
    test_cam_capture_unavailable()
    test_cam_failure()
    test_cam_net_source_setup()
    test_cam_net_pipeline()
    test_cam_helpers()
    test_cam_sources()
    test_cam_mic_end_to_end()
    test_mic_relay()
    test_wait_fifo_open()
    test_cam_pitch_steady()
    test_cam_tiers()
    test_cam_api()
    print()
    print('ALL PASS' if not FAILS else f'{len(FAILS)} FAILED: {FAILS}')
    return 0 if not FAILS else 1


if __name__ == '__main__':
    sys.exit(main())
