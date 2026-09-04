#!/usr/bin/env python3
"""rescue.py - position-based (header-free) sector recovery for hecuba.

Sectors the sweep classified 'failed'/'no-header' almost always have a
magnetically dead HEADER but an intact, ECC-valid DATA field. The decoder
gated every data-field search on a valid header, so it never looked.
Captures are INDEX-triggered, so physical slots sit at stable bit positions
across reads: build the slot->sector map from the union of header hits across
all reads (filling gaps with an interleave fit sec = a + b*slot mod 23), then
scan each slot's neighborhood for a data preamble+sync and let the data ECC
(exact match, 2^-32 false rate) arbitrate.

Safety rails:
  - a raw capture is used only if >=1 header matches the expected cylinder and
    none match another cylinder (guards against mis-seeked wiggle captures)
  - the interleave fit must be consistent with EVERY observed header
  - if two reads yield different ECC-valid data for one sector: skip + warn

!! Do NOT run while sweep.py or vote_repair.py runs (all write manifest.json).

Usage: python3 rescue.py [--image image] [--dry-run] [--jobs 8] [--cyls A:B]
"""
import argparse
import json
import multiprocessing as mp
import sys
from collections import Counter, defaultdict
from pathlib import Path

from decode_scout import (words_to_bits, bits_to_byte, find_headers, crc32p,
                          correct_burst, K_DATA, SYNC, SECTOR_SIZE,
                          SECTORS_PER_TRACK)

FIELD_BYTES = 1 + SECTOR_SIZE + 4
FIELD_BITS = FIELD_BYTES * 8
HDR_TO_SYNC = 298          # header start -> data sync start (measured 225+72+1)
SLOT_TOL = 600             # max |field pos - nominal slot pos| in bits
DEAD = ('failed', 'no-header', 'erased-ff')

def sync_scan(bits):
    """All data-sync candidates in a capture: (pos, data512, corrected)."""
    out, zeros, i = [], 0, 0
    hi = len(bits) - FIELD_BITS
    while i < hi:
        if bits[i] == 0:
            zeros += 1; i += 1; continue
        if zeros >= 19:
            for off in (3, 4, 7):
                if i < off: continue
                b0 = bits_to_byte(bits, i - off)
                if bin(b0 ^ SYNC).count('1') > 2: continue
                s = i - off
                by = bytes(bits_to_byte(bits, s + 8 * k)
                           for k in range(FIELD_BYTES))
                stored = int.from_bytes(by[1 + SECTOR_SIZE:], 'big')
                body = by[:1 + SECTOR_SIZE]
                if by[0] == SYNC and (crc32p(body) ^ stored) == K_DATA:
                    out.append((s, by[1:1 + SECTOR_SIZE], False))
                    break
                fixed = correct_burst(body, stored, K_DATA)
                if fixed is not None and fixed[0] == SYNC:
                    out.append((s, fixed[1:], True))
                    break
        zeros = 0; i += 1
    return out

def fit_interleave(pairs):
    """pairs: {slot: sec} observed. Fit sec = (a + b*slot) % 23; return
    callable or None. 23 is prime so any two distinct slots determine (a,b)."""
    items = sorted(pairs.items())
    if len(items) < 2: return None
    (s0, v0), (s1, v1) = items[0], items[1]
    db, ds = (v1 - v0) % 23, (s1 - s0) % 23
    try:
        b = (db * pow(ds, -1, 23)) % 23
    except ValueError:
        return None
    a = (v0 - b * s0) % 23
    if all((a + b * sl) % 23 == se for sl, se in items):
        return lambda sl: (a + b * sl) % 23
    return None

def rescue_track(task):
    cyl, head, sectors = task
    dead = {int(s) for s, st in sectors.items() if st in DEAD}
    raws = sorted(Path(_IMG, 'raw').glob(f'cyl{cyl:03d}_h{head}.*.bin'))
    if not dead or not raws:
        return cyl, head, {}, 'no-work'
    slot_votes = defaultdict(Counter)
    slot_pos = defaultdict(list)
    usable, pitches = [], []
    for rp in raws:
        bits = words_to_bits(rp.read_bytes())
        good = bad = 0
        hs = []
        for pos, c, h, sec in find_headers(bits):
            if c == cyl and sec < SECTORS_PER_TRACK:
                good += 1; hs.append((pos, sec))
            elif c != cyl:
                bad += 1
        if good == 0 or bad > 0:
            continue                        # unverifiable or mis-seeked raw
        usable.append(bits)
        ps = sorted(p for p, _ in hs)
        pitches += [b - a for a, b in zip(ps, ps[1:]) if 4000 < b - a < 5400]
        for pos, sec in hs:
            slot_pos[sec].append(pos)
    if not usable or not pitches:
        return cyl, head, {}, 'no-usable-raw'
    pitch = sorted(pitches)[len(pitches) // 2]
    # slot indices from positions (INDEX-aligned across reads)
    slot_map = defaultdict(Counter)         # slot -> sector votes
    nominal = {}                            # slot -> median header pos
    tmp = defaultdict(list)
    for sec, plist in slot_pos.items():
        for pos in plist:
            sl = round(pos / pitch)
            slot_map[sl][sec] += 1
            tmp[sl].append(pos)
    slot_sec = {sl: c.most_common(1)[0][0] for sl, c in slot_map.items()}
    for sl, v in tmp.items():
        nominal[sl] = sorted(v)[len(v) // 2]
    fit = fit_interleave({sl % 23: se for sl, se in slot_sec.items()})
    max_slot = int((max(len(b) for b in usable) - FIELD_BITS) // pitch)
    base = sorted(nominal[sl] - sl * pitch for sl in nominal)
    base = base[len(base) // 2]
    def sec_of(slot):
        for s in (slot, slot - 23, slot + 23):
            if s in slot_sec: return slot_sec[s]
        return fit(slot % 23) if fit else None
    def pos_of(slot):
        for s, d in ((slot, 0), (slot - 23, 23 * pitch), (slot + 23, -23 * pitch)):
            if s in nominal: return nominal[s] + d
        return base + slot * pitch
    rescued, conflict = {}, set()
    for bits in usable:
        for s, data, corr in sync_scan(bits):
            slot = round((s - HDR_TO_SYNC - base) / pitch)
            if abs(s - HDR_TO_SYNC - pos_of(slot)) > SLOT_TOL: continue
            sec = sec_of(slot)
            if sec is None or sec not in dead: continue
            if sec in rescued and rescued[sec][0] != data:
                conflict.add(sec)
            if sec not in rescued or (rescued[sec][1] and not corr):
                rescued[sec] = (data, corr)
    for sec in conflict:
        rescued.pop(sec, None)
    note = f'{len(conflict)} conflicts' if conflict else ''
    return cyl, head, rescued, note

_IMG = 'image'
def _init(img):
    global _IMG
    _IMG = img

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--image', default='image')
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--jobs', type=int, default=8)
    ap.add_argument('--cyls', help='A:B restrict cylinder range')
    args = ap.parse_args()
    image = Path(args.image)
    mpath = image / 'manifest.json'
    manifest = json.loads(mpath.read_text())
    lo, hi = 0, 10 ** 9
    if args.cyls:
        a, _, b = args.cyls.partition(':')
        lo = int(a); hi = int(b) if b else lo
    tasks = []
    for key, entry in sorted(manifest['tracks'].items(),
                             key=lambda kv: tuple(map(int, kv[0].split(':')))):
        cyl, head = map(int, key.split(':'))
        if not (lo <= cyl <= hi): continue
        if any(st in DEAD for st in entry['sectors'].values()):
            tasks.append((cyl, head, entry['sectors']))
    print(f'{len(tasks)} tracks with dead sectors', flush=True)

    tot = won = tracks_won = completed = 0
    with mp.Pool(args.jobs, initializer=_init,
                 initargs=(str(image),)) as pool:
        for n, (cyl, head, rescued, note) in enumerate(
                pool.imap_unordered(rescue_track, tasks, chunksize=4), 1):
            entry = manifest['tracks'][f'{cyl}:{head}']
            ndead = sum(1 for st in entry['sectors'].values() if st in DEAD)
            tot += ndead
            if rescued:
                tracks_won += 1
                won += len(rescued)
                binp = image / 'tracks' / f'cyl{cyl:03d}_h{head}.bin'
                blob = bytearray(binp.read_bytes()) if binp.exists() else \
                    bytearray(SECTOR_SIZE * SECTORS_PER_TRACK)
                for sec, (data, corr) in rescued.items():
                    blob[sec * SECTOR_SIZE:(sec + 1) * SECTOR_SIZE] = data
                    entry['sectors'][str(sec)] = 'position'
                entry['valid'] = sum(1 for st in entry['sectors'].values()
                                     if st in ('valid', 'corrected', 'voted',
                                               'position'))
                if entry['valid'] == SECTORS_PER_TRACK:
                    entry['status'] = 'complete'
                    completed += 1
                if not args.dry_run:
                    binp.write_bytes(bytes(blob))
                    if tracks_won % 50 == 0:
                        t = mpath.with_suffix('.tmp')
                        t.write_text(json.dumps(manifest, indent=1,
                                                sort_keys=True))
                        t.replace(mpath)
            print(f'[{n}/{len(tasks)}] cyl {cyl:3d} h{head}: '
                  f'+{len(rescued)}/{ndead} {note} -> {entry["valid"]}/23',
                  flush=True)
    if not args.dry_run:
        t = mpath.with_suffix('.tmp')
        t.write_text(json.dumps(manifest, indent=1, sort_keys=True))
        t.replace(mpath)
    print(f'\nrescue: {won}/{tot} dead sectors recovered, '
          f'{tracks_won} tracks improved, {completed} newly complete'
          + (' (dry run, nothing written)' if args.dry_run else ''), flush=True)
    return 0

if __name__ == '__main__':
    sys.exit(main())
