#!/usr/bin/env python3
"""irmx_fs.py - iRMX-86 named-volume filesystem walker for hecuba.

Input: a volume image (logical byte order) + a JSON list of the LBAs never
recovered. Parses the named-volume label (byte 384), the fnode table, and the
directory tree; emits a full listing with per-file intactness and a reverse
map missing-sector -> owning file.

Usage: python3 irmx_fs.py [--img volume.img] [--missing volume.missing.json]
                          [--tree-out filetree.txt]

iRMX-86 on-disk structures (Disk Verification Utility reference):
  label@384: name[10] flags B driver B gran W size DW maxfnode W
             fnode$start DW fnode$size W root$fnode W
  fnode:     flags W type B gran B owner W cr/acc/mod times DW*3
             total$size DW total$blocks DW ptr[8]{nblocks W, blk 3B}
             this$size DW res W*2 id$count W acc[3]{B,W} parent W
  flags bit0 = allocated, bit1 = long file (ptrs -> indirect blocks)
  types: 0 fnode-file 1 volmap 2 fnodemap 3 account 4 badblocks
         6 directory 8 data 9 vlabel
  dir entry: fnode W + name[14], fnode 0 = empty slot
  times: seconds since 1978-01-01 GMT
"""
import argparse
import json
import struct
import sys
from datetime import datetime, timedelta
from pathlib import Path

SEC = 512
TYPES = {0: 'fnode-file', 1: 'volmap', 2: 'fnodemap', 3: 'account',
         4: 'badblocks', 6: 'dir', 8: 'data', 9: 'vlabel'}

def t2s(t):
    if t == 0: return '-'
    return (datetime(1978, 1, 1) + timedelta(seconds=t)).strftime('%Y-%m-%d %H:%M')

class Volume:
    def __init__(self, img_path, missing_path):
        self.img = Path(img_path).read_bytes()
        self.missing = set(json.loads(Path(missing_path).read_text()))
        lab = self.img[384:512]
        self.name = lab[0:10].rstrip(b'\x00 ').decode('ascii', 'replace')
        (self.gran, self.vsize) = struct.unpack_from('<HI', lab, 12)
        (self.maxfn, self.fnstart, self.fnsize,
         self.rootfn) = struct.unpack_from('<HIHH', lab, 18)

    def fnode_raw(self, n):
        off = self.fnstart + n * self.fnsize
        return self.img[off:off + self.fnsize]

    def fnode(self, n):
        b = self.fnode_raw(n)
        f = {'num': n}
        (f['flags'], f['type'], f['gran'], f['owner'], f['crtime'],
         f['acctime'], f['modtime'], f['total_size'],
         f['total_blocks']) = struct.unpack_from('<HBBHIIIII', b, 0)
        ptrs = []
        for k in range(8):
            nb, = struct.unpack_from('<H', b, 26 + 5 * k)
            blk = int.from_bytes(b[26 + 5 * k + 2:26 + 5 * k + 5], 'little')
            if nb: ptrs.append((nb, blk))
        f['ptrs'] = ptrs
        f['this_size'], = struct.unpack_from('<I', b, 66)
        f['alloc'] = bool(f['flags'] & 1)
        f['long'] = bool(f['flags'] & 2)
        return f

    def extents(self, f):
        """-> list of (first_lba, nblocks) data extents of the file."""
        out = []
        if not f['long']:
            for nb, blk in f['ptrs']:
                out.append((blk, nb))
            return out
        for nb, blk in f['ptrs']:                # blk = indirect block
            ind = self.img[blk * SEC:(blk + 1) * SEC]
            got, pos = 0, 0
            while got < nb and pos + 3 <= SEC:
                cnt = ind[pos]
                sub = int.from_bytes(ind[pos + 1:pos + 4], 'little')
                pos += 4
                if cnt == 0: break
                out.append((sub, cnt))
                got += cnt
        return out

    def read_file(self, f):
        data = bytearray()
        for blk, nb in self.extents(f):
            data += self.img[blk * SEC:(blk + nb) * SEC]
        return bytes(data[:f['total_size']])

    def damage(self, f):
        """-> (missing_sectors, total_sectors) over the file's extents."""
        miss = tot = 0
        used = -(-f['total_size'] // SEC)
        for blk, nb in self.extents(f):
            for l in range(blk, blk + nb):
                if tot >= used: break
                tot += 1
                if l in self.missing: miss += 1
        return miss, max(tot, 1)

    def readdir(self, f):
        raw = self.read_file(f)
        out = []
        for off in range(0, len(raw) - 15, 16):
            fn, = struct.unpack_from('<H', raw, off)
            if fn == 0: continue
            nm = raw[off + 2:off + 16].rstrip(b'\x00').decode('ascii', 'replace')
            out.append((fn, nm))
        return out

def walk(vol, out=sys.stdout):
    seen = {}
    files = []                     # (path, fnode-dict)
    def rec(fn, path, depth):
        if fn in seen:
            print(f'{"  "*depth}{path} -> LOOP fnode {fn}', file=out)
            return
        f = vol.fnode(fn)
        seen[fn] = path
        files.append((path, f))
        if f['type'] == 6:
            try:
                entries = vol.readdir(f)
            except Exception as e:
                print(f'{"  "*depth}{path}/ : unreadable dir ({e})', file=out)
                return
            for sub, nm in sorted(entries, key=lambda e: e[1]):
                rec(sub, f'{path}/{nm}', depth + 1)
    rec(vol.rootfn, '', 0)
    return files

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--img', default='volume.img',
                    help='volume image, logical byte order')
    ap.add_argument('--missing', default='volume.missing.json',
                    help='JSON list of unrecovered LBAs')
    ap.add_argument('--tree-out', default='filetree.txt',
                    help='where to write the full listing')
    args = ap.parse_args()
    vol = Volume(args.img, args.missing)
    print(f'volume {vol.name!r}: {vol.vsize:,} bytes, {vol.maxfn} fnodes '
          f'@ {vol.fnstart:,} (size {vol.fnsize}), root fnode {vol.rootfn}')
    # fnode-table self-damage check
    fn_secs = range(vol.fnstart // SEC,
                    (vol.fnstart + vol.maxfn * vol.fnsize + SEC - 1) // SEC)
    fn_miss = [l for l in fn_secs if l in vol.missing]
    print(f'fnode table: sectors {fn_secs.start}..{fn_secs.stop - 1}, '
          f'{len(fn_miss)} missing{" <-- METADATA DAMAGE" if fn_miss else ""}')

    files = walk(vol)
    intact = part = 0
    dmg_report = []
    total_bytes = 0
    for path, f in files:
        if f['type'] == 6 or not path: continue
        total_bytes += f['total_size']
        miss, tot = vol.damage(f)
        if miss == 0: intact += 1
        else:
            part += 1
            dmg_report.append((path, f, miss, tot))
    print(f'\nfiles walked: {len(files)} nodes, {intact} intact files, '
          f'{part} damaged, {total_bytes:,} file bytes total')
    print('\n=== DAMAGED FILES ===')
    for path, f, miss, tot in sorted(dmg_report, key=lambda r: -r[2]):
        print(f'  {path}  [{TYPES.get(f["type"],"?")}] '
              f'{f["total_size"]:,}B  missing {miss}/{tot} sectors '
              f'({100*miss/tot:.0f}%)  mod {t2s(f["modtime"])}')

    # reverse map: every missing in-volume sector -> owner
    owner = {}
    for path, f in files:
        for blk, nb in vol.extents(f):
            for l in range(blk, blk + nb):
                if l in vol.missing:
                    owner.setdefault(l, path or '/')
    in_vol = [l for l in sorted(vol.missing) if l < vol.vsize // SEC]
    unowned = [l for l in in_vol if l not in owner]
    print(f'\nmissing sectors inside volume: {len(in_vol)} '
          f'(outside volume: {len(vol.missing) - len(in_vol)})')
    print(f'  owned by files: {len(owner)}   free/unreferenced: {len(unowned)}')
    # save tree + full listing
    with open(args.tree_out, 'w') as out:
        for path, f in files:
            miss, tot = vol.damage(f) if f['type'] != 6 else (0, 1)
            mark = f'  !!MISSING {miss}/{tot}' if miss else ''
            print(f'{path or "/":60s} {TYPES.get(f["type"],"?"):9s} '
                  f'{f["total_size"]:>10,}  {t2s(f["modtime"])}{mark}',
                  file=out)
    print(f"\nfull listing -> {args.tree_out}")
    return 0

if __name__ == '__main__':
    sys.exit(main())
