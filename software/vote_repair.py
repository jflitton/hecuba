#!/usr/bin/env python3
"""vote_repair.py - offline cross-read majority-vote recovery for hecuba.

For every sector the sweep left as 'failed', extract that sector's data field
from EVERY saved raw capture of the track, discard outlier reads (smeared or
misaligned extractions), majority-vote per bit across the survivors, then
ECC-check the consensus (with 4-bit burst correction as a final step).
Successful sectors are written back into tracks/*.bin and the manifest
(status 'voted').

Works entirely from image/raw/, no drive time.

!! Do NOT run while sweep.py is running: both write manifest.json and
tracks/*.bin.

Usage:  python3 vote_repair.py [--image image] [--min-reads 3] [--dry-run]
"""
import argparse
import json
import sys
from collections import Counter
from pathlib import Path

from decode_scout import (words_to_bits, read_bytes, find_headers, crc32p,
                          correct_burst, K_DATA, SYNC, SECTOR_SIZE,
                          SECTORS_PER_TRACK)

OUTLIER_BITS = 300      # reads farther than this from the medoid are dropped
FIELD_BYTES = 1 + SECTOR_SIZE + 4          # sync + data + ecc


def extract_instances(raw_bits_list, cyl, sector):
    """Every candidate extraction of this sector's data field across all
    reads, using the same tolerant anchor walk as the decoder's find_data (all
    zeros-run anchors, near-sync at bit offsets 3/4/7), WITHOUT requiring
    validation. The outlier filter downstream keeps the aligned cluster."""
    from decode_scout import bits_to_byte
    out = []      # (alignment_key, field): key = bit offset of field vs header
    for bits in raw_bits_list:
        for pos, c, h, sec in find_headers(bits):
            if c != cyl or sec != sector:
                continue
            lo = pos + 72
            hi = min(lo + 700, len(bits) - FIELD_BYTES * 8)
            zeros = 0
            i = lo
            while i < hi:
                if bits[i] == 0:
                    zeros += 1
                    i += 1
                    continue
                if zeros >= 19:
                    for off in (3, 4, 7):
                        if i < off:
                            continue
                        b0 = bits_to_byte(bits, i - off)
                        if bin(b0 ^ 0x19).count('1') > 2:
                            continue
                        fld = read_bytes(bits, i - off, FIELD_BYTES)
                        if fld:
                            out.append(((i - off) - pos, fld))
                zeros = 0
                i += 1
    return out


def hamming(a, b):
    return sum(bin(x ^ y).count('1') for x, y in zip(a, b))


def filter_outliers(insts):
    """Keep the cluster around the medoid (min total distance to others)."""
    if len(insts) <= 2:
        return insts
    sums = []
    for i, fi in enumerate(insts):
        sums.append((sum(hamming(fi, fj) for j, fj in enumerate(insts)
                         if i != j), i))
    _, med = min(sums)
    ref = insts[med]
    return [f for f in insts if hamming(f, ref) <= OUTLIER_BITS]


def vote(insts):
    n = len(insts)
    out = bytearray(FIELD_BYTES)
    for k in range(FIELD_BYTES):
        acc = [0] * 8
        for f in insts:
            b = f[k]
            for bit in range(8):
                if b & (0x80 >> bit):
                    acc[bit] += 1
        v = 0
        for bit in range(8):
            if acc[bit] * 2 > n:
                v |= 0x80 >> bit
        out[k] = v
    return bytes(out)


def validate(field):
    """ECC-check a candidate field; return 512-byte data or None."""
    stored = int.from_bytes(field[1 + SECTOR_SIZE:], 'big')
    body = field[:1 + SECTOR_SIZE]
    if field[0] == SYNC and (crc32p(body) ^ stored) == K_DATA:
        return field[1:1 + SECTOR_SIZE]
    fixed = correct_burst(body, stored, K_DATA)
    if fixed is not None and fixed[0] == SYNC:
        return fixed[1:1 + SECTOR_SIZE]
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--image', default='image')
    ap.add_argument('--min-reads', type=int, default=3)
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--heads', default='0,1,2,3,4')
    args = ap.parse_args()

    image = Path(args.image)
    mpath = image / 'manifest.json'
    manifest = json.loads(mpath.read_text())
    heads = {int(h) for h in args.heads.split(',')}

    tried = won = 0
    tracks_repaired = 0
    for key, entry in sorted(manifest['tracks'].items(),
                             key=lambda kv: tuple(map(int, kv[0].split(':')))):
        if entry['status'] != 'partial':
            continue
        cyl, head = map(int, key.split(':'))
        if head not in heads:
            continue
        failed = [int(s) for s, st in entry['sectors'].items() if st == 'failed']
        if not failed:
            continue
        raws = sorted((image / 'raw').glob(f'cyl{cyl:03d}_h{head}.*.bin'))
        if len(raws) < args.min_reads:
            continue
        bits_list = [words_to_bits(p.read_bytes()) for p in raws]

        binp = image / 'tracks' / f'cyl{cyl:03d}_h{head}.bin'
        blob = bytearray(binp.read_bytes()) if binp.exists() else \
            bytearray(SECTOR_SIZE * SECTORS_PER_TRACK)
        got_any = False
        for sec in failed:
            insts = extract_instances(bits_list, cyl, sec)
            if len(insts) < args.min_reads:
                continue
            tried += 1
            # group by field-vs-header bit alignment; vote each group
            # independently and let the ECC pick the true alignment
            groups = {}
            for key, fld in insts:
                groups.setdefault(key, []).append(fld)
            data = None
            for key in sorted(groups, key=lambda k: -len(groups[k])):
                kept = filter_outliers(groups[key])
                if len(kept) < args.min_reads:
                    continue
                data = validate(vote(kept))
                if data is not None:
                    break
            if data is None:
                continue
            won += 1
            got_any = True
            blob[sec * SECTOR_SIZE:(sec + 1) * SECTOR_SIZE] = data
            entry['sectors'][str(sec)] = 'voted'
        if got_any:
            tracks_repaired += 1
            entry['valid'] = sum(1 for st in entry['sectors'].values()
                                 if st in ('valid', 'corrected', 'voted'))
            if entry['valid'] == SECTORS_PER_TRACK:
                entry['status'] = 'complete'
            if not args.dry_run:
                binp.write_bytes(bytes(blob))
                if tracks_repaired % 25 == 0:      # checkpoint the manifest
                    tmp = mpath.with_suffix('.tmp')
                    tmp.write_text(json.dumps(manifest, indent=1, sort_keys=True))
                    tmp.replace(mpath)
            print(f'cyl {cyl:3d} h{head}: +{sum(1 for st in entry["sectors"].values() if st == "voted")} '
                  f'voted -> {entry["valid"]}/23 ({entry["status"]})', flush=True)

    if not args.dry_run:
        tmp = mpath.with_suffix('.tmp')
        tmp.write_text(json.dumps(manifest, indent=1, sort_keys=True))
        tmp.replace(mpath)
    print(f'\nvote-repair: {won}/{tried} failed sectors recovered '
          f'across {tracks_repaired} tracks'
          + (' (dry run, nothing written)' if args.dry_run else ''))
    return 0


if __name__ == '__main__':
    sys.exit(main())
