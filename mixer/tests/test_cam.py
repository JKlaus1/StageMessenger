"""
Cam feed (v3.4-v3.7): picker cam output + delay line (off-hardware).

The picker slices a second stereo pair out of the same capture for the video encoder, delays it, and
writes it to a fifo through a bounded queue. The listen-back output must stay bit-exact and never wait
on the cam side. A fake 48-channel capture encodes (channel, frame) into every sample so each output
frame can be traced back to where it came from.

    cd ~/stage-messenger && python3 -m mixer.tests.test_cam
"""
import os
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
    def __init__(self, size='800x600', rate=15, user=None):
        import http.server, socketserver
        outer = self
        self.size, self.rate, self.hits, self.procs = size, rate, 0, []

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
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
        self.srv = S(('127.0.0.1', 0), H)
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
        check('settings saved in mixer_state.json', sf.get('cam') == {'feed': 'bus2', 'delay_ms': 320, 'quality': 'low', 'audio_bitrate': '160k'}, sf.get('cam'))
        check('Listen bitrate saved in mixer_state.json', sf.get('listen') == {'opus_bitrate': '64k'}, sf.get('listen'))
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
    test_cam_pipeline()
    test_cam_video_only_and_idle()
    test_cam_capture_unavailable()
    test_cam_failure()
    test_cam_net_source_setup()
    test_cam_net_pipeline()
    test_cam_pitch_steady()
    test_cam_tiers()
    test_cam_api()
    print()
    print('ALL PASS' if not FAILS else f'{len(FAILS)} FAILED: {FAILS}')
    return 0 if not FAILS else 1


if __name__ == '__main__':
    sys.exit(main())
