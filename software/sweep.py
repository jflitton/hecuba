#!/usr/bin/env python3
"""sweep.py - whole-drive imaging for hecuba.

Drives the firmware's USB-CDC console (pyserial), captures each track,
decodes inline (decode_scout), and retries dynamically:

  stop on 23/23 valid sectors, or after DRY_LIMIT consecutive reads that add
  no new valid sector (drive errors are deterministic: one dry read already
  means converged, two is margin), or at the hard cap.

Outputs under --out (default ./image):
  manifest.json                 per-track: status, per-sector states, reads
  tracks/cylCCC_hN.bin          23 x 512 B; never-valid sectors zero-filled
                                (manifest is authoritative for validity)
  raw/cylCCC_hN.rK.bin          every distinct raw capture (15,120 B each),
                                deduplicated by hash, a bit-faithful archive
                                so better decoders can re-run later

Resumable: reruns skip tracks already complete (and, unless --revisit,
tracks that previously dry-stopped).

Safety: read-only command set (seek + read gate only). The script refuses to
start unless the drive reports READY, and stops on any FAIL/fault status
rather than retrying blindly. Spin-up/down stay manual bench steps
unless --up / --down are given.

Usage:
  python3 sweep.py --probe                     # connect + status, no motion
  python3 sweep.py                             # image everything
  python3 sweep.py --cyls 0:100 --heads 1,2,3,4
  python3 sweep.py --revisit                   # retry previously-failed tracks
"""
import argparse
import hashlib
import json
import re
import sys
import time
from base64 import b64decode
from glob import glob
from pathlib import Path

try:
    import serial
except ImportError:
    sys.exit("pyserial required:  python3 -m pip install pyserial")

from decode_scout import (decode_track, classify_missing,
                          SECTORS_PER_TRACK, SECTOR_SIZE)

CAPTURE_WORDS = 3780
RAW_BYTES = CAPTURE_WORDS * 4
NUM_CYLS = 525
NUM_HEADS = 5

B64_BEGIN = b'-----BEGIN CAPTURE'
B64_END = b'-----END CAPTURE-----'


class Console:
    """Line-oriented driver for the hecuba USB-CDC console."""

    def __init__(self, port, quiet=False):
        try:
            self.ser = serial.Serial(port, 115200, timeout=0.5)
        except serial.SerialException as e:
            if 'busy' in str(e).lower():
                sys.exit(f'{port} is busy; close the screen session first '
                         f'(Ctrl-A k), then rerun')
            raise
        self.quiet = quiet
        time.sleep(0.3)                      # banner-on-connect settles
        self.ser.reset_input_buffer()

    def cmd(self, line, terminator=b'> ', timeout=10.0):
        """Send a command, return everything up to the next prompt."""
        self.ser.reset_input_buffer()
        self.ser.write(line.encode() + b'\r')
        self.ser.flush()
        buf = bytearray()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            chunk = self.ser.read(4096)
            if chunk:
                buf += chunk
                if buf.rstrip().endswith(terminator.rstrip()):
                    return bytes(buf)
            elif buf.rstrip().endswith(terminator.rstrip()):
                return bytes(buf)
        raise TimeoutError(f'no prompt after {line!r}: {bytes(buf[-120:])!r}')

    def status_byte(self):
        out = self.cmd('status').decode('ascii', 'replace')
        m = re.search(r'status 0x([0-9A-Fa-f]{2})', out)
        if not m:
            raise RuntimeError(f'unparseable status: {out!r}')
        return int(m.group(1), 16)

    def capture(self, cyl, head, timeout=15.0):
        """capture + dumpb64 -> raw track bytes. Raises on FAIL/fault."""
        out = self.cmd(f'capture {cyl} {head}', timeout=timeout)
        text = out.decode('ascii', 'replace')
        if 'OK:' not in text:
            raise RuntimeError(f'capture {cyl} {head}: {text.strip()!r}')
        dump = self.cmd('dumpb64', timeout=30.0)
        i, j = dump.find(B64_BEGIN), dump.find(B64_END)
        if i < 0 or j < 0:
            raise RuntimeError('dumpb64: markers not found')
        body = dump[dump.index(b'\n', i) + 1:j]
        raw = b64decode(b''.join(body.split()))
        if len(raw) != RAW_BYTES:
            raise RuntimeError(f'dumpb64: {len(raw)} bytes, expected {RAW_BYTES}')
        return raw


def find_port():
    ports = sorted(glob('/dev/cu.usbmodem*'))
    if not ports:
        sys.exit('no /dev/cu.usbmodem* found; is the Pico connected?')
    return ports[0]


def load_manifest(path):
    if path.exists():
        return json.loads(path.read_text())
    return {'drive': 'Priam DISKOS 3450, iSBC 215 512B format, 23 sec/track',
            'tracks': {}}


def save_manifest(path, manifest):
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(manifest, indent=1, sort_keys=True))
    tmp.replace(path)


def image_track(con, cyl, head, args, rawdir, prior=None):
    """Dynamic-retry image of one track. Returns manifest entry.
    `prior` seeds already-recovered sectors so revisits union across runs."""
    merged = dict(prior or {})     # sector -> (data, class-label)
    raw_hashes = set()
    distinct_raws = []
    reads = dry = 0
    while reads < args.max_reads:
        if args.wiggle:
            # arrive via a fresh long seek, alternating approach direction,
            # so each read gets an independent servo settle position
            far = 524 if (reads % 2) else 0
            if abs(far - cyl) < 30:
                far = 262
            out = con.cmd(f'seek {far}', timeout=10.0).decode('ascii', 'replace')
            if 'OK' not in out:
                raise RuntimeError(f'wiggle seek {far}: {out.strip()!r}')
        raw = con.capture(cyl, head)
        reads += 1
        h = hashlib.sha1(raw).hexdigest()
        if h not in raw_hashes:
            raw_hashes.add(h)
            distinct_raws.append(raw)
            p = rawdir / f'cyl{cyl:03d}_h{head}.{h[:10]}.bin'
            if not p.exists():        # content-addressed: dedupes across runs
                p.write_bytes(raw)
        got = decode_track(raw, expect_cyl=cyl, expect_head=head)
        new = 0
        for s, (c, hd, data, fx) in got.items():
            if s not in merged:
                new += 1
                merged[s] = (data, 'corrected' if fx else 'valid')
            elif merged[s][1] == 'corrected' and not fx:
                merged[s] = (data, 'valid')   # clean read beats corrected
        if len(merged) == SECTORS_PER_TRACK:
            break
        dry = dry + 1 if new == 0 else 0
        if dry >= args.dry_limit:
            break

    sectors = {}
    for s in range(SECTORS_PER_TRACK):
        if s in merged:
            sectors[str(s)] = merged[s][1]
        else:
            # classify against every distinct read until a header is found
            verdict = 'no-header'
            for raw in distinct_raws:
                verdict = classify_missing(raw, s, expect_cyl=cyl)
                if verdict != 'no-header':
                    break
            sectors[str(s)] = verdict
    complete = len(merged) == SECTORS_PER_TRACK
    return {
        'status': 'complete' if complete else 'partial',
        'valid': len(merged), 'reads': reads,
        'sectors': sectors,
        'data': {s: d for s, (d, _) in merged.items()},
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--port', help='serial port (default: first cu.usbmodem*)')
    ap.add_argument('--out', default='image', help='output directory')
    ap.add_argument('--cyls', default=f'0:{NUM_CYLS}',
                    help='cylinder range A:B (half-open) or single cyl')
    ap.add_argument('--heads', default='0,1,2,3,4')
    ap.add_argument('--max-reads', type=int, default=8)
    ap.add_argument('--dry-limit', type=int, default=2)
    ap.add_argument('--revisit', action='store_true',
                    help='re-image tracks recorded as partial (unions with '
                         'previously recovered sectors)')
    ap.add_argument('--min-valid', type=int, default=0,
                    help='with --revisit: only re-image partial tracks that '
                         'already have at least this many valid sectors '
                         '(skip hopeless tracks, focus drive time on near-misses)')
    ap.add_argument('--wiggle', action='store_true',
                    help='long seek away and back before every read, '
                         'alternating approach direction so each read settles '
                         'independently (raise --max-reads/--dry-limit with this)')
    ap.add_argument('--probe', action='store_true',
                    help='connect, print status, exit (no drive motion)')
    ap.add_argument('--park', action='store_true',
                    help='Sequence Down immediately and exit (no imaging)')
    ap.add_argument('--up', action='store_true', help='issue Sequence Up first')
    ap.add_argument('--down', action='store_true', help='Sequence Down when finished')
    args = ap.parse_args()

    con = Console(args.port or find_port())
    st = con.status_byte()
    print(f'drive status: 0x{st:02X}')
    if args.probe:
        return 0
    if args.park:
        out = con.cmd('down', timeout=50.0).decode('ascii', 'replace')
        print(out.strip().splitlines()[-2] if out.strip() else out)
        return 0

    outdir = Path(args.out)
    trackdir, rawdir = outdir / 'tracks', outdir / 'raw'
    trackdir.mkdir(parents=True, exist_ok=True)
    rawdir.mkdir(parents=True, exist_ok=True)
    mpath = outdir / 'manifest.json'
    manifest = load_manifest(mpath)

    if ':' in args.cyls:
        a, b = args.cyls.split(':')
        cyls = range(int(a), int(b))
    else:
        cyls = [int(args.cyls)]
    heads = [int(h) for h in args.heads.split(',')]

    # select tracks BEFORE any drive motion, so a bad flag combination
    # can't cost a pointless spin-up/power cycle
    todo = []
    for c in cyls:
        for h in heads:
            prev = manifest['tracks'].get(f'{c}:{h}')
            if prev and (prev['status'] == 'complete'
                         or (prev['status'] == 'partial' and not args.revisit)
                         or (prev['status'] == 'partial' and args.revisit
                             and prev['valid'] < args.min_valid)):
                continue
            todo.append((c, h))
    print(f'{len(todo)} track(s) selected')
    if not todo:
        print('nothing to image; check --revisit / --min-valid / --cyls '
              '(--min-valid is a FLOOR: partials below it are skipped; '
              '0 revisits every partial)')
        return 0

    if args.up and not (st & 0x01):
        print('sequencing up (~30 s)...')
        out = con.cmd('up', timeout=40.0).decode('ascii', 'replace')
        if 'OK' not in out:
            sys.exit(f'sequence up failed: {out.strip()}')
        st = con.status_byte()
    if not (st & 0x01):
        sys.exit('drive not READY; spin it up first (or pass --up)')

    # discard one capture: first read after spin-up has proven unreliable
    print('warm-up capture (discarded)...')
    con.capture(todo[0][0], todo[0][1])

    t0 = time.monotonic()
    for n, (cyl, head) in enumerate(todo):
        key = f'{cyl}:{head}'
        prev = manifest['tracks'].get(key)
        # seed a revisit with everything already recovered for this track:
        # ALL recovered classes, incl. offline repairs (voted/position/deep);
        # anything not seeded here would be zeroed in the rewritten blob
        prior = {}
        if prev:
            binp = trackdir / f'cyl{cyl:03d}_h{head}.bin'
            if binp.exists():
                blob = binp.read_bytes()
                for s in range(SECTORS_PER_TRACK):
                    scls = prev['sectors'].get(str(s))
                    if scls in ('valid', 'corrected', 'voted', 'position',
                                'deep'):
                        prior[s] = (blob[s * SECTOR_SIZE:(s + 1) * SECTOR_SIZE],
                                    scls)
        try:
            entry = image_track(con, cyl, head, args, rawdir, prior=prior)
        except (RuntimeError, TimeoutError) as e:
            print(f'\n!! cyl {cyl} head {head}: {e}')
            print('stopping: check drive status before resuming')
            save_manifest(mpath, manifest)
            return 1
        data = entry.pop('data')
        img = b''.join(data.get(s, b'\x00' * SECTOR_SIZE)
                       for s in range(SECTORS_PER_TRACK))
        (trackdir / f'cyl{cyl:03d}_h{head}.bin').write_bytes(img)
        manifest['tracks'][key] = entry
        save_manifest(mpath, manifest)
        rate = (n + 1) / (time.monotonic() - t0)
        eta = (len(todo) - n - 1) / rate / 60 if rate > 0 else 0
        print(f'cyl {cyl:3d} head {head}: {entry["valid"]:2d}/23 '
              f'({entry["reads"]} reads, {entry["status"]})  '
              f'[{n + 1}/{len(todo)}, ~{eta:.0f} min left]')

    if args.down:
        print(con.cmd('down', timeout=50.0).decode('ascii', 'replace').strip())

    tracks = manifest['tracks']
    comp = sum(1 for t in tracks.values() if t['status'] == 'complete')
    print(f'\n{comp}/{len(tracks)} tracks complete '
          f'({sum(t["valid"] for t in tracks.values())} sectors recovered)')
    return 0


if __name__ == '__main__':
    sys.exit(main())
