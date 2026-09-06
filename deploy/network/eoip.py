#!/usr/bin/env python3
"""Bounded lab-only EoIP/TAP transport. Original implementation; no production claim.
Wire framing reference: https://github.com/amphineko/eoip (project protocol description).
"""
import fcntl
import os
import select
import signal
import socket
import struct
import sys
import time

from agent import command, config, STATE, atomic
import json

MAGIC = b'\x20\x01\x64\x00'


def encode(frame, tunnel_id):
    if len(frame) > 65535:
        raise ValueError('oversize frame')
    return MAGIC + struct.pack('!H', len(frame)) + struct.pack('<H', tunnel_id) + frame


def decode(packet, tunnel_id):
    if len(packet) < 8 or packet[:4] != MAGIC:
        return None
    length = struct.unpack('!H', packet[4:6])[0]
    incoming_id = struct.unpack('<H', packet[6:8])[0]
    if incoming_id != tunnel_id or length != len(packet) - 8:
        return None
    return packet[8:]


def frame_metadata(frame, direction):
    result = {'direction': direction}
    if len(frame) < 20:
        return result
    ethertype = int.from_bytes(frame[12:14], 'big')
    result['ether_type'] = hex(ethertype)
    if ethertype in (0x8863, 0x8864):
        result['pppoe_code'] = frame[15]
        if ethertype == 0x8864 and len(frame) >= 26:
            protocol = int.from_bytes(frame[20:22], 'big')
            result['ppp_protocol'] = hex(protocol)
            if protocol in (0xc021, 0x8021):
                result['control_code'] = frame[22]
                result['identifier'] = frame[23]
                end = min(22 + int.from_bytes(frame[24:26], 'big'), len(frame))
                options = []
                offset = 26
                while offset + 2 <= end:
                    kind, length = frame[offset:offset + 2]
                    if length < 2 or offset + length > end:
                        break
                    options.append({'kind': kind, 'value_hex': frame[offset+2:offset+length].hex()})
                    offset += length
                result['options'] = options
    return result


def main(router_id):
    cfg = config(router_id)
    tap = os.open('/dev/net/tun', os.O_RDWR)
    fcntl.ioctl(tap, 0x400454ca, struct.pack('16sH', cfg['tap'].encode(), 0x0002 | 0x1000))
    command('ip', 'link', 'set', cfg['tap'], 'alias', f'fireisp:{router_id}:lab')
    command('ip', 'link', 'set', cfg['tap'], 'mtu', '1500', 'up')
    raw = socket.socket(socket.AF_INET, socket.SOCK_RAW, 47)
    raw.setsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE, cfg['wg'].encode() + b'\0')
    raw.bind((cfg['server'], 0))
    def stop(*args):
        raise SystemExit
    signal.signal(signal.SIGTERM, stop)
    counts = {'tx': 0, 'rx': 0, 'frames': []}
    last_keepalive_reply = 0
    atomic(STATE / f'router-{router_id}.eoip-status', json.dumps(counts))
    try:
        while True:
            ready, _, _ = select.select([tap, raw], [], [], 5)
            if tap in ready:
                frame = os.read(tap, 65535)
                raw.sendto(encode(frame, router_id), (cfg['router'], 0))
                counts['tx'] += 1
                counts['frames'] = (counts['frames'] + [frame_metadata(frame, 'tx')])[-30:]
                atomic(STATE / f'router-{router_id}.eoip-status', json.dumps(counts))
            if raw in ready:
                packet, address = raw.recvfrom(65535)
                if address[0] != cfg['router'] or len(packet) < 20:
                    continue
                ihl = (packet[0] & 0x0F) * 4
                frame = decode(packet[ihl:], router_id)
                if frame == b'' and time.monotonic() - last_keepalive_reply > 5:
                    raw.sendto(encode(b'', router_id), (cfg['router'], 0))
                    last_keepalive_reply = time.monotonic()
                if frame:
                    os.write(tap, frame)
                    counts['rx'] += 1
                    counts['frames'] = (counts['frames'] + [frame_metadata(frame, 'rx')])[-30:]
                    atomic(STATE / f'router-{router_id}.eoip-status', json.dumps(counts))
    finally:
        raw.close()
        os.close(tap)


if __name__ == '__main__':
    main(int(sys.argv[1]))
