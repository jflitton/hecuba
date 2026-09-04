#!/usr/bin/env python3
"""defect_map.py - decode the Priam 3450's reserved area and attribute damage.

The drive is 525 cylinders but the iRMX volume is only 513. Intel's own iSBC 215
driver configuration for Xenix 286 (c215g.c, the #if PRIAM32 block) documents
exactly what the other 12 hold:

    Partitions[1-4] address cylinders 0-512, excluding track 0.
    513-522 are alternate track cylinders.  Cylinder 523 contains
    the bad-track data.  Cylinder 524 is for diagnostics.

Cylinder 523 holds a bad-track table written by the format utility: magic
0xABCD, u16 count, then count x (cylinder u16, head u16). Tracks listed there
are factory/format-time media defects, not storage-era rot, and normally read
back fully unreadable in a capture.

This matters for the damage numbers: iRMX's allocator avoids flagged tracks, so
they carry no file data. Sectors lost there are not lost data. This script
splits the unrecovered-sector list into "factory" (expected, empty) and
"storage" (real loss), and attributes the real losses to owning files.

Usage:
  python3 defect_map.py                          # volume.img + volume.missing.json
  python3 defect_map.py --img volume.dist.img
  python3 defect_map.py --json defects.json      # also write the table as JSON
"""
import argparse
import json
import struct
import sys
from pathlib import Path

import irmx_fs

SEC = 512
CYL, HEAD, SPT = 525, 5, 23
CPT = SPT * SEC                  # bytes per track
CPC = HEAD * CPT                 # bytes per cylinder

# Reserved-area map for the Priam 3450 on an iSBC 215, per c215g.c #if PRIAM32.
VOLUME_CYLS = 513                # 0..512
ALT_FIRST, ALT_LAST = 513, 522   # 10 alternate-track cylinders (Nalt=10)
BADTRK_CYL = 523                 # bad-track data
DIAG_CYL = 524                   # diagnostics
FILL = 0xCC                      # format fill byte in never-written sectors
BADTRK_MAGIC = 0xABCD


def lba_to_chs(lba):
    c, r = divmod(lba, HEAD * SPT)
    h, s = divmod(r, SPT)
    return c, h, s


def track_of(lba):
    c, r = divmod(lba, HEAD * SPT)
    return c, r // SPT


def read_badtrack_table(img):
    """-> (list of (cyl, head), head the table was found on).

    The table is replicated every 4 sectors across the track; take the first
    copy that carries the magic, so a hole in one copy is not fatal.
    """
    for h in range(HEAD):
        base = BADTRK_CYL * CPC + h * CPT
        for s in range(SPT):
            blk = img[base + s * SEC: base + (s + 1) * SEC]
            if len(blk) < 4:
                continue
            magic, n = struct.unpack_from('<HH', blk, 0)
            if magic != BADTRK_MAGIC:
                continue
            if not 0 < n <= (SEC - 4) // 4:
                continue
            w = struct.unpack_from('<%dH' % (2 * n), blk, 4)
            ents = [(w[2 * i], w[2 * i + 1]) for i in range(n)]
            if all(0 <= c < CYL and 0 <= hd < HEAD for c, hd in ents):
                return ents, h
    return [], None


def classify_sector(blk):
    if not any(blk):
        return 'zero'
    if blk == bytes([FILL]) * len(blk):
        return 'fill'
    v = struct.unpack('<%dH' % (len(blk) // 2), blk)
    if all((v[i + 1] - v[i]) & 0xFFFF == 1 for i in range(len(v) - 1)):
        return 'ramp'
    return 'data'


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--img', default='volume.img')
    ap.add_argument('--missing', default='volume.missing.json')
    ap.add_argument('--json', help='write the decoded defect table here as JSON')
    a = ap.parse_args()

    for p in (a.img, a.missing):
        if not Path(p).exists():
            print(f'{p}: not found. Point --img/--missing at the volume image '
                  f'and its missing-sector list.',
                  file=sys.stderr)
            return 2
    img = Path(a.img).read_bytes()
    if len(img) != CYL * CPC:
        print(f'warning: {a.img} is {len(img):,} B, expected {CYL * CPC:,} '
              f'({CYL} cyl)', file=sys.stderr)

    bad, on_head = read_badtrack_table(img)
    if not bad:
        print(f'no bad-track table found on cylinder {BADTRK_CYL}', file=sys.stderr)
        return 1
    badset = set(bad)
    print(f'=== bad-track table (cyl {BADTRK_CYL} head {on_head}, '
          f'magic {BADTRK_MAGIC:#06x}) ===')
    print(f'{len(bad)} defective tracks flagged at format time:')
    for i in range(0, len(bad), 7):
        print('  ' + '  '.join(f'({c:3d},{h})' for c, h in bad[i:i + 7]))

    # --- reserved-area verification -------------------------------------
    print('\n=== reserved area (c215g.c #if PRIAM32) ===')
    regions = [(f'{ALT_FIRST}-{ALT_LAST}', range(ALT_FIRST, ALT_LAST + 1),
                'alternate tracks'),
               (str(BADTRK_CYL), range(BADTRK_CYL, BADTRK_CYL + 1),
                'bad-track data'),
               (str(DIAG_CYL), range(DIAG_CYL, DIAG_CYL + 1), 'diagnostics')]
    for label, cyls, what in regions:
        tally = {}
        for c in cyls:
            for i in range(0, CPC, SEC):
                k = classify_sector(img[c * CPC + i: c * CPC + i + SEC])
                tally[k] = tally.get(k, 0) + 1
        detail = '  '.join(f'{k}={v}' for k, v in sorted(tally.items()))
        print(f'  cyl {label:<9} {what:<17} {detail}')

    # --- damage attribution ---------------------------------------------
    missing = sorted(json.loads(Path(a.missing).read_text()))
    factory = [l for l in missing if track_of(l) in badset]
    storage = [l for l in missing if track_of(l) not in badset]
    print(f'\n=== unrecovered sectors: {len(missing)} ===')
    print(f'  factory defects (flagged, never allocated): {len(factory):>5}'
          f'  ({len(factory) / len(missing):.0%})')
    print(f'  real storage-era loss:                      {len(storage):>5}'
          f'  ({len(storage) / len(missing):.0%})')

    covered = {track_of(l) for l in factory}
    print(f'\n  bad-list tracks fully unreadable: {len(covered)}/{len(bad)}')
    if covered != badset:
        print(f'  NOTE: {sorted(badset - covered)} were (partly) readable')

    lost_tracks = sorted({track_of(l) for l in storage})
    print(f'  storage-era loss spans {len(lost_tracks)} tracks:')
    for i in range(0, len(lost_tracks), 6):
        print('    ' + '  '.join(f'({c:3d},{h})' for c, h in lost_tracks[i:i + 6]))

    # --- per-file: is any of it factory-defect? -------------------------
    vol = irmx_fs.Volume(a.img, a.missing)
    files = irmx_fs.walk(vol, open('/dev/null', 'w'))
    rows = []
    for path, f in files:
        if f['type'] == 6 or not path:
            continue
        lost = [l for blk, nb in vol.extents(f)
                for l in range(blk, blk + nb) if l in vol.missing]
        if lost:
            fac = sum(1 for l in lost if track_of(l) in badset)
            rows.append((path, len(lost), fac, len(lost) - fac))
    print(f'\n=== damaged files: {len(rows)} ===')
    print(f"  {'file':<36}{'lost':>6}{'factory':>9}{'storage':>9}")
    for path, tot, fac, sto in sorted(rows, key=lambda r: -r[1]):
        print(f'  {path:<36}{tot:>6}{fac:>9}{sto:>9}')
    tf = sum(r[2] for r in rows)
    print(f'\n  file data lost to factory defects: {tf} sectors')
    print(f'  file data lost to storage-era rot:  {sum(r[3] for r in rows)} sectors')
    if tf == 0:
        print(f'  => iRMX allocated no file data onto the {len(bad)} '
              f'flagged tracks.')

    if a.json:
        Path(a.json).write_text(json.dumps({
            'source': 'cyl 523 head %s, magic 0x%04X' % (on_head, BADTRK_MAGIC),
            'geometry': {'cylinders': CYL, 'heads': HEAD, 'sectors': SPT,
                         'sector_bytes': SEC},
            'reserved': {'volume_cylinders': VOLUME_CYLS,
                         'alternate_tracks': [ALT_FIRST, ALT_LAST],
                         'bad_track_data': BADTRK_CYL,
                         'diagnostics': DIAG_CYL},
            'bad_tracks': [{'cylinder': c, 'head': h} for c, h in bad],
            'unrecovered': {'total': len(missing), 'factory': len(factory),
                            'storage': len(storage)},
        }, indent=2) + '\n')
        print(f'\ndefect table -> {a.json}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
