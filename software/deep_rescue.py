#!/usr/bin/env python3
"""deep_rescue.py - last-ditch offline recovery for slot-mapped tracks where
position-based anchor rescue (rescue.py) found no valid data field.

Per residue sector, in escalating order (data ECC arbitrates every step):
  1. sliding scan:  test EVERY bit offset in the slot window as a field
     start (no preamble/sync anchor required; catches clobbered preambles).
     O(1) per offset: R(w[s:s+4104]) = pw[s+4104] ^ pw[s]*x^4104 mod P.
  2. aligned vote:  align the slot window across all reads (coarse INDEX
     position + fine xcorr on a 256-bit probe), majority-vote per bit, then
     sliding-scan the consensus; per-read + consensus burst correction.
  3. slip search:   single bit-slip repair (slip_search method) on the
     consensus and on each read.

Geometry (measured on the iSBC 215 512 B format, global): sector=(8*slot)%23,
pitch 4656 bits, slot-0 header ~437 bits after INDEX. Identity comes from
position, never headers.

!! Do NOT run while sweep.py / vote_repair.py / rescue.py runs.

Usage: python3 deep_rescue.py [--image image] [--dry-run] [--jobs 8]
"""
import argparse
import json
import multiprocessing as mp
import sys
from collections import Counter, defaultdict
from pathlib import Path

from decode_scout import (words_to_bits, bits_to_byte, find_headers, crc32p,
                          correct_burst, POLY, K_DATA, SYNC, SECTOR_SIZE,
                          SECTORS_PER_TRACK)

FIELD_BYTES = 1 + SECTOR_SIZE + 4
FIELD_BITS = FIELD_BYTES * 8            # 4136
BODY_BITS = (1 + SECTOR_SIZE) * 8       # 4104
PITCH = 4656
HDR_TO_SYNC = 298                       # header start -> data sync start
WIN_BEFORE, WIN_AFTER = 400, 900        # slot window around nominal sync
P_FULL = (1 << 32) | POLY
MASK = 0xFFFFFFFF
DEAD = ('failed', 'no-header', 'erased-ff')

def mulmod(a, b):
    r = 0
    while b:
        if b & 1: r ^= a
        a <<= 1
        if a >> 32: a ^= P_FULL
        b >>= 1
    return r

XP = [1]
for _ in range(BODY_BITS + 8):
    v = XP[-1] << 1
    if v >> 32: v ^= P_FULL
    XP.append(v)
X4104 = XP[BODY_BITS]

def prefix_crcs(w):
    pw = [0] * (len(w) + 1)
    c = 0
    for i, bit in enumerate(w):
        top = (c >> 31) & 1
        c = (c << 1) & MASK
        if top ^ bit: c ^= POLY
        pw[i + 1] = c
    return pw

def windows32(w):
    if len(w) < 32: return []
    sw = [0] * (len(w) - 31)
    v = 0
    for i in range(32): v = (v << 1) | w[i]
    sw[0] = v
    for i in range(1, len(w) - 31):
        v = ((v << 1) & MASK) | w[i + 31]
        sw[i] = v
    return sw

def field_at(w, s):
    by = bytes(bits_to_byte(w, s + 8 * k) for k in range(FIELD_BYTES))
    stored = int.from_bytes(by[1 + SECTOR_SIZE:], 'big')
    body = by[:1 + SECTOR_SIZE]
    if by[0] == SYNC and (crc32p(body) ^ stored) == K_DATA:
        return by[1:1 + SECTOR_SIZE], 'exact'
    fixed = correct_burst(body, stored, K_DATA)
    if fixed is not None and fixed[0] == SYNC:
        return fixed[1:], 'burst'
    return None, None

def sliding_scan(w):
    """Exact-ECC test of every field start offset in w. Returns offsets."""
    pw = prefix_crcs(w)
    sw = windows32(w)
    hits = []
    for s in range(0, len(w) - FIELD_BITS):
        lhs = mulmod(pw[s], X4104) ^ pw[s + BODY_BITS]
        if lhs ^ sw[s + BODY_BITS] == K_DATA:
            hits.append(s)
    return hits

def slip_search(w):
    """Single-slip repair anywhere in the field starting at w[0]."""
    pw = prefix_crcs(w)
    sw = windows32(w)
    for X in range(8, BODY_BITS):
        sX = XP[BODY_BITS - X]
        pX = pw[X]
        for d in (1, 2, 3, 4):
            if X + d >= len(pw) or BODY_BITS + d >= len(sw): continue
            lhs = mulmod(pX ^ pw[X + d], sX) ^ pw[BODY_BITS + d]
            if lhs ^ sw[BODY_BITS + d] == K_DATA:
                fb = w[:X] + w[X + d: X + d + (FIELD_BITS - X)]
                data, kind = field_at(fb, 0)
                if data is not None: return data
        for d in (1, 2, 3):
            if BODY_BITS - X - d < 0: continue
            for g in range(1 << d):
                gb = [(g >> (d - 1 - k)) & 1 for k in range(d)]
                rg = 0
                for bit in gb:
                    top = (rg >> 31) & 1
                    rg = (rg << 1) & MASK
                    if top ^ bit: rg ^= POLY
                lhs = mulmod(pX, sX) ^ \
                    mulmod(rg ^ pX, XP[BODY_BITS - X - d]) ^ pw[BODY_BITS - d]
                if lhs ^ sw[BODY_BITS - d] == K_DATA:
                    fb = w[:X] + gb + w[X: X + (FIELD_BITS - X - d)]
                    data, kind = field_at(fb, 0)
                    if data is not None: return data
    return None

def xcorr_align(ref, w, span=200):
    """Best shift of w against ref using a 256-bit probe, or None."""
    probe = ref[WIN_BEFORE:WIN_BEFORE + 256]
    best, bshift = 0, None
    for sh in range(-span, span + 1):
        a = WIN_BEFORE + sh
        if a < 0 or a + 256 > len(w): continue
        score = sum(1 for j in range(256) if w[a + j] == probe[j])
        if score > best: best, bshift = score, sh
    return bshift if best >= 224 else None          # >=87.5% agreement

def rescue_sector(wins):
    """wins: list of slot windows (bit lists), one per read. -> (data, how)."""
    # 1. sliding scan each read (exact), then near-sync offsets with burst
    for w in wins:
        for s in sliding_scan(w):
            data, kind = field_at(w, s)
            if data is not None: return data, f'slide-{kind}'
    for w in wins:
        for s in range(0, len(w) - FIELD_BITS):
            if bin(bits_to_byte(w, s) ^ SYNC).count('1') <= 1:
                data, kind = field_at(w, s)
                if data is not None: return data, f'nearsync-{kind}'
    if len(wins) >= 3:
        ref = wins[0]
        aligned = [ref]
        for w in wins[1:]:
            sh = xcorr_align(ref, w)
            if sh is None: continue
            aligned.append(w[max(0, sh):] if sh >= 0 else [0] * (-sh) + w)
        if len(aligned) >= 3:
            n = min(len(w) for w in aligned)
            cons = [1 if sum(w[j] for w in aligned) * 2 > len(aligned) else 0
                    for j in range(n)]
            for s in sliding_scan(cons):
                data, kind = field_at(cons, s)
                if data is not None: return data, f'vote-{kind}'
            # 3. slip search on consensus at plausible field starts
            zeros, i = 0, 0
            while i < min(len(cons) - FIELD_BITS - 8, WIN_BEFORE + 700):
                if cons[i] == 0:
                    zeros += 1; i += 1; continue
                if zeros >= 19:
                    for off in (3, 4, 7):
                        if i < off: continue
                        s = i - off
                        if s + FIELD_BITS + 8 > len(cons): continue
                        data = slip_search(cons[s:s + FIELD_BITS + 8])
                        if data is not None: return data, 'vote-slip'
                zeros = 0; i += 1
    return None, None

def track_task(task):
    cyl, head, sectors = task
    dead = {int(s) for s, st in sectors.items() if st in DEAD}
    raws = sorted(Path(_IMG, 'raw').glob(f'cyl{cyl:03d}_h{head}.*.bin'))
    if not dead or not raws: return cyl, head, {}
    # per-read slot base from that read's own headers (fallback: global 437)
    reads = []
    for rp in raws:
        bits = words_to_bits(rp.read_bytes())
        allh = find_headers(bits)
        hs = [(pos, sec) for pos, c, h, sec in allh
              if c == cyl and sec < SECTORS_PER_TRACK]
        # mis-seek guard: any header of another cylinder disqualifies the raw
        if any(c != cyl for _, c, _, _ in allh):
            continue
        if hs:
            base = sorted(p - round(p / PITCH) * PITCH for p, _ in hs)
            base = base[len(base) // 2]
        else:
            base = 437
        reads.append((bits, base))
    if not reads: return cyl, head, {}
    out = {}
    for sec in sorted(dead):
        # slots carrying this sector: slot = sec * inv(8) mod 23 (+23 wrap)
        slot0 = (sec * pow(8, -1, 23)) % 23
        wins = []
        for bits, base in reads:
            for slot in (slot0, slot0 + 23):
                nominal = base + slot * PITCH + HDR_TO_SYNC
                lo = nominal - WIN_BEFORE
                hi = nominal + WIN_AFTER + FIELD_BITS
                if lo < 0 or hi > len(bits): continue
                wins.append(bits[lo:hi])
        if not wins: continue
        data, how = rescue_sector(wins)
        if data is not None:
            out[sec] = (data, how)
    return cyl, head, out

_IMG = 'image'
def _init(img):
    global _IMG
    _IMG = img

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--image', default='image')
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--jobs', type=int, default=8)
    args = ap.parse_args()
    image = Path(args.image)
    mpath = image / 'manifest.json'
    manifest = json.loads(mpath.read_text())
    tasks = []
    for key, entry in sorted(manifest['tracks'].items(),
                             key=lambda kv: tuple(map(int, kv[0].split(':')))):
        cyl, head = map(int, key.split(':'))
        if cyl == 0: continue                     # sequential geometry, done
        n_dead = sum(1 for st in entry['sectors'].values() if st in DEAD)
        n_good = sum(1 for st in entry['sectors'].values() if st not in DEAD)
        if n_dead and n_good:                     # mapped-partial tracks only
            tasks.append((cyl, head, entry['sectors']))
    print(f'{len(tasks)} mapped-partial tracks', flush=True)
    won = Counter(); tot_sectors = 0; completed = 0
    with mp.Pool(args.jobs, initializer=_init, initargs=(str(image),)) as pool:
        for n, (cyl, head, rescued) in enumerate(
                pool.imap_unordered(track_task, tasks, chunksize=2), 1):
            entry = manifest['tracks'][f'{cyl}:{head}']
            if rescued:
                tot_sectors += len(rescued)
                binp = image / 'tracks' / f'cyl{cyl:03d}_h{head}.bin'
                blob = bytearray(binp.read_bytes()) if binp.exists() else \
                    bytearray(SECTOR_SIZE * SECTORS_PER_TRACK)
                for sec, (data, how) in rescued.items():
                    won[how] += 1
                    blob[sec * SECTOR_SIZE:(sec + 1) * SECTOR_SIZE] = data
                    entry['sectors'][str(sec)] = 'deep'
                entry['valid'] = sum(1 for st in entry['sectors'].values()
                                     if st in ('valid', 'corrected', 'voted',
                                               'position', 'deep'))
                if entry['valid'] == SECTORS_PER_TRACK:
                    entry['status'] = 'complete'; completed += 1
                if not args.dry_run:
                    binp.write_bytes(bytes(blob))
            print(f'[{n}/{len(tasks)}] cyl {cyl:3d} h{head}: +{len(rescued)} '
                  f'-> {entry["valid"]}/23', flush=True)
            if not args.dry_run and n % 25 == 0:
                t = mpath.with_suffix('.tmp')
                t.write_text(json.dumps(manifest, indent=1, sort_keys=True))
                t.replace(mpath)
    if not args.dry_run:
        t = mpath.with_suffix('.tmp')
        t.write_text(json.dumps(manifest, indent=1, sort_keys=True))
        t.replace(mpath)
    print(f'\ndeep-rescue: {tot_sectors} sectors ({dict(won)}), '
          f'{completed} tracks newly complete'
          + (' (dry run)' if args.dry_run else ''), flush=True)
    return 0

if __name__ == '__main__':
    sys.exit(main())
