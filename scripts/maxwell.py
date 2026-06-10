#!/usr/bin/env python3
"""Read battery and status info from an Audeze Maxwell wireless dongle.

Speaks the Maxwell HID protocol (reverse-engineered by the HeadsetControl
project): 62-byte requests on report ID 0x06, responses fetched as input
report 0x07 via the HIDIOCGINPUT ioctl. The dongle exposes battery, chatmix,
sidetone and mic status; it does not report latency or charging state.

Usage: maxwell.py [--json]
"""

import array
import fcntl
import json
import os
import struct
import sys
import time

VENDOR_ID = 0x3329          # Audeze
PRODUCT_IDS = (0x4B19, 0x4B18)  # Maxwell dongle, Maxwell Xbox dongle
MSG_SIZE = 62
DELAY_S = 0.075             # Audeze HQ uses ~52ms; <70ms is flaky in practice

# Linux ioctl encoding: dir(2) | size(14) | type(8) | nr(8)
def _ioc(direction, typ, nr, size):
    return (direction << 30) | (size << 16) | (ord(typ) << 8) | nr

HIDIOCGRAWINFO = _ioc(2, 'H', 0x03, 8)                # read struct hidraw_devinfo
HIDIOCGINPUT = _ioc(3, 'H', 0x0A, MSG_SIZE)           # read/write input report

# Status requests; response[12] (in order) carries battery flag, mic mute,
# EQ preset, chatmix, mic noise filter, sidetone level.
STATUS_REQUESTS = [
    [0x06, 0x08, 0x80, 0x05, 0x5A, 0x04, 0x00, 0x01, 0x09, 0x22],
    [0x06, 0x08, 0x80, 0x05, 0x5A, 0x04, 0x00, 0x01, 0x09],
    [0x06, 0x08, 0x80, 0x05, 0x5A, 0x04, 0x00, 0x83, 0x2C, 0x0B],
    [0x06, 0x08, 0x80, 0x05, 0x5A, 0x04, 0x00, 0x01, 0x09, 0x24],
    [0x06, 0x08, 0x80, 0x05, 0x5A, 0x04, 0x00, 0x01, 0x09, 0x2C],
    [0x06, 0x08, 0x80, 0x05, 0x5A, 0x04, 0x00, 0x83, 0x2C, 0x07],
]

# Init sequence mimicking the official software; the dongle needs the whole
# thing before every status read, else it reports the headset as absent.
INIT_REQUESTS = [
    [0x06, 0x08, 0x80, 0x05, 0x5A, 0x04, 0x00, 0x01, 0x09, 0x20],
    [0x06, 0x08, 0x80, 0x05, 0x5A, 0x04, 0x00, 0x01, 0x09, 0x25],
    [0x06, 0x07, 0x80, 0x05, 0x5A, 0x03, 0x00, 0x07, 0x1C],
    [0x06, 0x08, 0x80, 0x05, 0x5A, 0x04, 0x00, 0x01, 0x09, 0x28],
    [0x06, 0x08, 0x80, 0x05, 0x5A, 0x04, 0x00, 0x83, 0x2C, 0x01],
    [0x06, 0x08, 0x80, 0x05, 0x5A, 0x04, 0x00, 0x83, 0x2C, 0x07],
    [0x06, 0x07, 0x00, 0x05, 0x5A, 0x03, 0x00, 0x07, 0x1C],
    [0x06, 0x08, 0x80, 0x05, 0x5A, 0x04, 0x00, 0x01, 0x09, 0x2D],
    [0x06, 0x08, 0x80, 0x05, 0x5A, 0x04, 0x00, 0x01, 0x09, 0x2C],
    [0x06, 0x08, 0x80, 0x05, 0x5A, 0x04, 0x00, 0x01, 0x09],
    [0x06, 0x08, 0x80, 0x05, 0x5A, 0x04, 0x00, 0x83, 0x2C, 0x0B],
    [0x06, 0x08, 0x80, 0x05, 0x5A, 0x04, 0x00, 0x01, 0x09, 0x24],
    [0x06, 0x08, 0x80, 0x05, 0x5A, 0x04, 0x00, 0x01, 0x09, 0x2F],
    # NOTE: PR #517 sends a 15th packet here (00 09 25 00 7A) that WRITES
    # parameter 0x25; replaying it on this firmware altered audio balance,
    # so it is deliberately omitted. Reads only below.
    [0x06, 0x07, 0x80, 0x05, 0x5A, 0x03, 0x00, 0xD6, 0x0C],
]

EQ_PRESETS = {1: 'Audeze', 2: 'Treble Boost', 3: 'Bass Boost', 4: 'Immersive',
              5: 'Competition', 6: 'Footsteps', 7: 'EQ1', 8: 'EQ2', 9: 'EQ3', 10: 'EQ4'}
NOISE_FILTER = {0: 'off', 1: 'low', 2: 'high'}


def find_dongle():
    for name in sorted(os.listdir('/sys/class/hidraw')):
        path = f'/dev/{name}'
        try:
            fd = os.open(path, os.O_RDWR)
        except PermissionError:
            info = open(f'/sys/class/hidraw/{name}/device/uevent').read()
            if f'{VENDOR_ID:08X}' in info:
                sys.exit(
                    f"{path} is the Maxwell dongle but you lack permission.\n"
                    f"Either run with sudo, or install a udev rule:\n"
                    f"  echo 'KERNEL==\"hidraw*\", ATTRS{{idVendor}}==\"3329\", "
                    f"TAG+=\"uaccess\"' | sudo tee /etc/udev/rules.d/70-audeze-maxwell.rules\n"
                    f"  sudo udevadm control --reload && sudo udevadm trigger"
                )
            continue
        except OSError:
            continue
        buf = array.array('B', [0] * 8)
        fcntl.ioctl(fd, HIDIOCGRAWINFO, buf, True)
        _bustype, vendor, product = struct.unpack('<Ihh', buf.tobytes())
        if vendor & 0xFFFF == VENDOR_ID and product & 0xFFFF in PRODUCT_IDS:
            return fd, path
        os.close(fd)
    sys.exit('No Audeze Maxwell dongle found. Is it plugged in?')


def query(fd, request):
    time.sleep(DELAY_S)
    packet = bytes(request) + b'\x00' * (MSG_SIZE - len(request))
    os.write(fd, packet)
    buf = bytearray(MSG_SIZE)
    buf[0] = 0x07
    fcntl.ioctl(fd, HIDIOCGINPUT, buf, True)
    return buf


def read_status(fd):
    # The dongle only reports headset state after the full init handshake.
    for req in INIT_REQUESTS:
        query(fd, req)
    responses = [query(fd, req) for req in STATUS_REQUESTS]

    status = {'battery_percent': None, 'mic_muted': None, 'eq_preset': None,
              'chatmix': None, 'noise_filter': None, 'sidetone_level': None}

    bat = responses[0]
    for i in range(MSG_SIZE - 4):
        if bat[i:i + 4] == b'\xD6\x0C\x00\x00':
            status['battery_percent'] = bat[i + 4]
            break
    if status['battery_percent'] is not None:
        status['mic_muted'] = responses[1][12] != 0xFF
        status['eq_preset'] = responses[2][12]
        status['chatmix'] = responses[3][12]            # 0-20, 10 = centered
        status['noise_filter'] = responses[4][12]       # 0 off, 1 low, 2 high
        status['sidetone_level'] = responses[5][12]     # 0-31, 0 = off

    return status


def bar(percent, width=20):
    filled = round(percent / 100 * width)
    return '[' + '#' * filled + '-' * (width - filled) + ']'


def main():
    as_json = '--json' in sys.argv
    fd, path = find_dongle()

    status = read_status(fd)
    if status['battery_percent'] is None:
        status = read_status(fd)  # one retry; first pass after replug can miss
    os.close(fd)

    if as_json:
        print(json.dumps(status))
        return

    print(f'Audeze Maxwell  ({path})')
    print('-' * 46)
    if status['battery_percent'] is None:
        print('Headset is off or out of range (dongle found,')
        print('but it reports no connected headset).')
        return

    pct = status['battery_percent']
    cm = status['chatmix']
    game = min(cm, 10) * 10
    chat = min(20 - cm, 10) * 10
    st = status['sidetone_level']
    rows = [
        ('Battery', f'{pct:3d}%  {bar(pct)}'),
        ('Microphone', 'muted' if status['mic_muted'] else 'active'),
        ('Noise filter', NOISE_FILTER.get(status['noise_filter'], '?')),
        ('EQ preset', EQ_PRESETS.get(status['eq_preset'], '?')),
        ('ChatMix', f'{cm}/20  (game {game}% / chat {chat}%)'),
        ('Sidetone', f'on, level {st}/31' if st else 'off'),
    ]
    for label, value in rows:
        print(f'{label:<12} {value}')


if __name__ == '__main__':
    main()
