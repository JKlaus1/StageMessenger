"""
Console discovery (v3.3): find whichever console is on the mixer network -- no fixed IP.

One UDP socket on the mixer interface (eth0, the rack / mixer-network segment) asks every way a
console answers, and takes whatever replies:
  * WING      'WING?'  -> udp 2222   reply 'WING,<ip>,<name>,<model>,<serial>,<fw>'
  * X32 / M32 '/xinfo' -> udp 10023  reply /xinfo ,ssss <ip> <name> <model> <fw>
Order: unicast to the hint addresses (the last console seen, mixer_scan extras), the interface's
directed broadcast, then -- only if nothing answered and the subnet is /22 or smaller -- a unicast
sweep of the subnet (some switches / console firmware ignore broadcast). The address used is the
packet's source, never the IP the console reports about itself.

Only the configured interface is searched (default eth0): a venue / hotspot WiFi network could hold
someone else's console, and the Pi must never grab that.
"""
import ipaddress
import json
import select
import socket
import subprocess
import time

from .wing import osc_msg

WING_DISC_PORT = 2222
X32_PORT = 10023
WING_MODELS = {'ngc-full': 'WING', 'wing-rack': 'WING Rack', 'wing-compact': 'WING Compact'}   # discovery model ids
SWEEP_MAX_PREFIX = 22          # sweep only subnets this size or smaller (<= 1022 hosts)


def iface_net(iface):
    """-> (local ip, IPv4Interface) of the interface's IPv4 address, or None (no such interface /
    no address, e.g. eth0 unplugged)."""
    if not iface:
        return None
    try:                                      # SIOCGIFADDR / SIOCGIFNETMASK: no subprocess every pass
        import fcntl
        import struct
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            req = struct.pack('256s', iface[:15].encode())
            local = socket.inet_ntoa(fcntl.ioctl(s.fileno(), 0x8915, req)[20:24])
            mask = socket.inet_ntoa(fcntl.ioctl(s.fileno(), 0x891b, req)[20:24])
        finally:
            s.close()
        return local, ipaddress.IPv4Interface(f'{local}/{mask}')
    except OSError:
        pass                                  # no address (errno 99) / no such device (19)
    except Exception:
        pass
    try:                                      # fallback: iproute2
        out = subprocess.run(['ip', '-4', '-j', 'addr', 'show', 'dev', iface],
                             capture_output=True, text=True, timeout=2).stdout
        for link in json.loads(out or '[]'):
            for a in link.get('addr_info', []):
                if a.get('family') == 'inet' and a.get('local'):
                    return a['local'], ipaddress.IPv4Interface(f"{a['local']}/{a.get('prefixlen', 32)}")
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    return None


def _x32_reply(d):
    from .x32 import _parse
    try:
        addr, args = _parse(d)
    except Exception:
        return None
    if addr == '/xinfo' and len(args) >= 4:
        return {'kind': 'x32', 'name': str(args[1]), 'model': str(args[2]) or 'X32', 'fw': str(args[3])}
    return None


def _wing_reply(d):
    try:
        t = d.rstrip(b'\0').decode('ascii', 'replace')
    except Exception:
        return None
    if not t.startswith('WING,'):
        return None
    f = t.split(',')
    model = f[3] if len(f) > 3 else ''
    return {'kind': 'wing', 'name': f[2] if len(f) > 2 else '', 'model': WING_MODELS.get(model.lower(), model or 'WING'),
            'fw': f[5] if len(f) > 5 else ''}


def parse_reply(data, port):
    if port == WING_DISC_PORT:
        return _wing_reply(data)
    if port == X32_PORT:
        return _x32_reply(data)
    return _wing_reply(data) or _x32_reply(data)


class Finder:
    """discover() is safe to call repeatedly (the console watcher does, every few seconds)."""

    def __init__(self, iface='eth0', extra=(), timeout=0.8):
        self.iface = iface
        self.extra = [str(x) for x in (extra or []) if x]
        self.timeout = timeout
        self.last_where = ''           # what was searched last time (for the log)

    def discover(self, hints=(), sweep=True, want=None):
        """-> [{'kind','ip','name','model','fw'}] (reply order, one per ip). want = 'wing' | 'x32' | None."""
        net = iface_net(self.iface)
        local = net[0] if net else ''
        targets = []
        for t in list(hints or []) + self.extra:
            if t and t not in targets and t != local:
                targets.append(t)
        if net and net[1].network.prefixlen < 31:
            targets.append(str(net[1].network.broadcast_address))
        self.last_where = (f'{self.iface} {net[1].network}' if net else
                           (f'{self.iface} (no IPv4 address)' if self.iface else 'configured addresses'))
        keep = (lambda fs: [f for f in fs if f['kind'] == want]) if want else (lambda fs: fs)
        found = keep(self._ask(local, targets))       # v4.2: filter first, so "only the other type answered" still sweeps
        if not found and sweep and net and net[1].network.prefixlen >= SWEEP_MAX_PREFIX:
            hosts = [str(h) for h in net[1].network.hosts() if str(h) != local and str(h) not in targets]
            found = keep(self._ask(local, hosts))
        return found

    @staticmethod
    def _sock(local):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        try:
            s.bind((local, 0))
        except OSError:
            s.bind(('', 0))
        s.setblocking(False)
        return s

    def _ask(self, local, targets):
        """Ask every target; collect replies for `timeout` s. Packets to absent hosts sit in the ARP
        queue (~3 s) charged to the sending socket, so a sweep uses one socket per 32 hosts and never
        blocks on send (a full socket just skips -- the next pass asks again)."""
        if not targets:
            return []
        wing_q, x32_q = b'WING?', osc_msg('/xinfo')
        socks = []
        try:
            for i in range(0, len(targets), 32):
                s = self._sock(local)
                socks.append(s)
                for t in targets[i:i + 32]:
                    for payload, port in ((wing_q, WING_DISC_PORT), (x32_q, X32_PORT)):
                        try:
                            s.sendto(payload, (t, port))
                        except OSError:
                            pass
            found, seen = [], set()
            end = time.time() + self.timeout
            while True:
                left = end - time.time()
                if left <= 0:
                    break
                ready, _, _ = select.select(socks, [], [], left)
                for s in ready:
                    while True:
                        try:
                            d, (ip, port) = s.recvfrom(4096)
                        except (BlockingIOError, InterruptedError):
                            break
                        except OSError:              # ICMP unreachable etc. from a non-console host
                            break
                        r = parse_reply(d, port)
                        if r and ip not in seen:
                            seen.add(ip)
                            r['ip'] = ip
                            found.append(r)
                            if len(targets) > 8:     # sweep hit: a moment for any other reply, then go
                                end = min(end, time.time() + 0.15)
            return found
        finally:
            for s in socks:
                s.close()
