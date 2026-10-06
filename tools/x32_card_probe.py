#!/usr/bin/env python3
"""
READ-ONLY X32 / M32 probe for the v3.9 listen block (card out 25-32 <- User Out 1-8).
Sends only queries -- changes nothing on the console.

  usage: python3 tools/x32_card_probe.py <console_ip>   > ~/x32_probe_card.txt

Dumps: /xinfo, the card-output routing blocks, the 48 User Out sources, card prefs/status,
both as raw ints (direct queries) and as the console's own text (/node), so the codes for
"User Out 1-8" (card block) and "Local Out 14/15/16 / Mon L/R" (user-out taps) can be read off.
Tip: set the routing on the console by hand first, then run this -- the readback gives the codes.
"""
import socket
import struct
import sys
import time

PORT = 10023


def pad(b):
    return b + b'\0' * (4 - len(b) % 4)


def msg(addr, *args):
    tags, data = ',', b''
    for a in args:
        if isinstance(a, str):
            tags += 's'; data += pad(a.encode())
        elif isinstance(a, int):
            tags += 'i'; data += struct.pack('>i', a)
    return pad(addr.encode()) + pad(tags.encode()) + data


def parse(d):
    def rstr(i):
        j = d.index(b'\0', i)
        return d[i:j].decode(errors='replace'), (j // 4 + 1) * 4
    addr, i = rstr(0)
    if i >= len(d):
        return addr, []
    tags, i = rstr(i)
    out = []
    for t in tags[1:]:
        if t == 'i':
            out.append(struct.unpack('>i', d[i:i + 4])[0]); i += 4
        elif t == 'f':
            out.append(round(struct.unpack('>f', d[i:i + 4])[0], 4)); i += 4
        elif t == 's':
            s, i = rstr(i); out.append(s)
        elif t == 'b':
            n = struct.unpack('>i', d[i:i + 4])[0]; out.append(f'<blob {n}>'); i += 4 + (n + 3) // 4 * 4
    return addr, out


def main():
    if len(sys.argv) < 2:
        sys.exit('usage: x32_card_probe.py <console_ip>')
    ip = sys.argv[1]
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(0.4)

    def ask(addr, *args):
        s.sendto(msg(addr, *args), (ip, PORT))
        end = time.time() + 0.6
        while time.time() < end:
            try:
                d, _ = s.recvfrom(65536)
            except socket.timeout:
                break
            a, v = parse(d)
            if a == addr or (addr == '/node' and a == 'node') or addr == '/xinfo':
                return v
        return None

    print('# x32_card_probe', time.strftime('%Y-%m-%d %H:%M:%S'), ip)
    print('/xinfo', ask('/xinfo'))
    print('\n## /node text (console names for each value)')
    for n in ('config/routing/CARD', 'config/routing/OUT', 'config/userrout/out', '-prefs/card',
              '-stat/xcardtype', '-stat/urec'):
        print(f'node {n}:', ask('/node', n))
    print('\n## card output blocks (raw ints)')
    for blk in ('1-8', '9-16', '17-24', '25-32'):
        print(f'/config/routing/CARD/{blk}', ask(f'/config/routing/CARD/{blk}'))
    print('\n## user out sources (raw ints)')
    for u in range(1, 49):
        print(f'/config/userrout/out/{u:02d}', ask(f'/config/userrout/out/{u:02d}'))
    print('\n## local outs 13-16 + monitor (for reference)')
    for o in (13, 14, 15, 16):
        print(f'/outputs/main/{o:02d}/src', ask(f'/outputs/main/{o:02d}/src'))
    print('/config/solo/source', ask('/config/solo/source'))


if __name__ == '__main__':
    main()
