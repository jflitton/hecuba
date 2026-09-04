#!/usr/bin/env python3
"""decode_scout.py - iSBC 215 sector decoder for hecuba whole-track NRZ captures.

Reads `dumpb64` capture blocks out of a screen log (or raw .bin track files),
finds iSBC 215 sector structure at any bit alignment, ECC-checks every field,
and merges results across captures: a sector is recovered once its data ECC
validates in ANY read.

FORMAT (verified against real Priam 3450 captures; differs from Gesswein's
trk_ISBC215_512B in two ways: no A1 bytes on the Priam-native NRZ interface,
and data sync is 0x19, not 0xD9, per the iSBC 215 manual's "sync byte is
always 19H except Shugart/Quantum" note):

  header: 00-preamble | 19 | flags+size 20 | cyl-lo | sector | head | ECC32
  data:   00-preamble | 19 | 512 data bytes | ECC32          (write splice
          between the fields => independent bit alignment)

  ECC: CRC-32, poly 0x00A00805 (x^32+...; 11-bit burst correcting), MSB-first,
  non-reflected, computed over sync byte + field bytes, init 0. The controller's
  init/complement conventions fold into per-field constants:
      crc(field_bytes, init=0) XOR stored_ecc == K
  K_HDR  = 0xD9FFFEFD   (validated 20/20 distinct sectors)
  K_DATA = 0xE33E6F19   (validated on ECC-consistent data sectors)

Geometry: 23 sectors/track (hard-sectored, interleave 1), 512 B/sector,
cylinder in header low-nibble<<8 | cyl-lo, track = 13,440 bytes, ~107,523 bits.
"""
import argparse
import base64
import re
import sys
from collections import Counter

B64_BLOCK = re.compile(
    r'-----BEGIN CAPTURE b64 \((\d+) words.*?-----\r?\n(.*?)-----END CAPTURE-----',
    re.S)

POLY = 0x00A00805
K_HDR = 0xD9FFFEFD
K_DATA = 0xE33E6F19
SYNC = 0x19
SECTOR_SIZE = 512
SECTORS_PER_TRACK = 23
DATA_SEARCH_BITS = 700       # window after header end to hunt the data sync
PREAMBLE_BITS = 16           # zero bits required before a data sync candidate


def crc32p(data: bytes, init: int = 0) -> int:
    crc = init
    for b in data:
        crc ^= b << 24
        for _ in range(8):
            crc = ((crc << 1) ^ POLY) if (crc & 0x80000000) else (crc << 1)
            crc &= 0xFFFFFFFF
    return crc


# ---- burst-error correction (error trapping) --------------------------------
# The code corrects short bursts: syndrome s = crc(received field+ecc) ^ const.
# An error burst b(x)*x^j has syndrome b(x)*x^j mod p, so stepping the syndrome
# by x^-1 walks j down; when the register fits in MAX_BURST bits, the burst is
# trapped at offset j from the codeword end. Span limited to 4 bits, the same
# limit Gesswein uses for this polynomial to avoid miscorrection.
MAX_BURST = 4


def correct_burst(field: bytes, stored: int, k_const: int):
    """Return corrected field bytes, or None. field excludes the stored ECC."""
    syn = crc32p(field) ^ stored ^ k_const
    if syn == 0:
        return field
    # full codeword = field bits + 32 ecc bits
    n = len(field) * 8 + 32
    p_full = (1 << 32) | POLY
    t = syn
    for j in range(n):
        if t and t < (1 << MAX_BURST):
            # burst pattern t ends j bits from the codeword end
            if j < 32:
                return None       # burst inside the ECC bytes: field is fine
            e_bit_from_end = j    # LSB of t sits j bits from end
            fixed = bytearray(field)
            for bi in range(MAX_BURST):
                if t & (1 << bi):
                    pos_from_end = e_bit_from_end + bi
                    bit_index = n - 32 - 1 - (pos_from_end - 32)
                    if not (0 <= bit_index < len(field) * 8):
                        return None
                    fixed[bit_index >> 3] ^= 0x80 >> (bit_index & 7)
            fixed = bytes(fixed)
            if (crc32p(fixed) ^ stored ^ k_const) == 0:
                return fixed
            return None
        # t *= x^-1 mod p  (p monic degree 32, constant term 1)
        if t & 1:
            t ^= p_full
        t >>= 1
    return None


def load_captures(path):
    data = open(path, 'rb').read()
    if path.endswith('.bin'):
        return [data]
    text = data.decode('utf-8', 'replace')
    return [base64.b64decode(''.join(b.split()))
            for _, b in B64_BLOCK.findall(text)]


def words_to_bits(raw: bytes) -> list:
    """LE 32-bit words -> bit list; bit 0 of word 0 = first bit after INDEX."""
    bits = []
    for i in range(0, len(raw), 4):
        w = int.from_bytes(raw[i:i + 4], 'little')
        bits.extend((w >> k) & 1 for k in range(32))
    return bits


def bits_to_byte(bits, pos):
    v = 0
    for k in range(8):
        v = (v << 1) | bits[pos + k]
    return v


def read_bytes(bits, pos, n):
    if pos < 0 or pos + 8 * n > len(bits):
        return None
    return bytes(bits_to_byte(bits, pos + 8 * k) for k in range(n))


def find_marks(bits, run=40):
    """positions of 1-bits ending zero runs (legacy helper)."""
    marks, r = [], 0
    for i, b in enumerate(bits):
        if b == 0:
            r += 1
        else:
            if r >= run:
                marks.append(i)
            r = 0
    return marks


def bit_views(bits):
    """8 byte-aligned views of the bitstream, one per bit offset."""
    out = []
    for off in range(8):
        ba = bytearray()
        v = cnt = 0
        for i in range(off, len(bits)):
            v = (v << 1) | bits[i]
            cnt += 1
            if cnt == 8:
                ba.append(v)
                v = cnt = 0
        out.append(bytes(ba))
    return out


def find_headers(bits):
    """All ECC-valid headers: (bitpos, cyl, head, sector).
    Flags byte is 0x20 | cyl[10:8], so match 0x20-0x27 (cyl up to 524 needs
    0x20/0x21/0x22)."""
    out = []
    for off, view in enumerate(bit_views(bits)):
        start = 0
        while True:
            idx = view.find(bytes([SYNC]), start)
            if idx < 0:
                break
            start = idx + 1
            w = view[idx:idx + 9]
            if len(w) < 9 or (w[1] & 0xF8) != 0x20:
                continue
            if (crc32p(w[:5]) ^ int.from_bytes(w[5:9], 'big')) != K_HDR:
                continue
            cyl = ((w[1] & 0x0F) << 8) | w[2]
            out.append((off + idx * 8, cyl, w[4], w[3]))
    return sorted(out)


def find_data(bits, hdr_end):
    """Try every zeros+sync candidate after a header; return ECC-valid data."""
    n = len(bits)
    lo = hdr_end
    hi = min(hdr_end + DATA_SEARCH_BITS, n - 8 * (SECTOR_SIZE + 5))
    zeros = 0
    i = lo
    while i < hi:
        if bits[i] == 0:
            zeros += 1
            i += 1
            continue
        # The sync byte 0x19 (00011001) starts with three 0-bits that visually
        # belong to the preamble; the anchoring 1-bit is normally sync bit 3.
        # A flipped sync bit shifts which bit anchors, so try the plausible
        # alignments and let the ECC (and burst corrector) arbitrate. The
        # sync byte is inside the ECC-covered field, so a corrupted sync is
        # itself a correctable burst.
        if zeros >= PREAMBLE_BITS + 3:
            for off in (3, 4, 7):       # anchor = sync bit 3 (clean), 4, or 7
                b0 = bits_to_byte(bits, i - off) if i >= off else -1
                if b0 < 0 or bin(b0 ^ SYNC).count('1') > 2:
                    continue
                body = read_bytes(bits, i - off + 8, SECTOR_SIZE + 4)
                if body is None:
                    continue
                stored = int.from_bytes(body[SECTOR_SIZE:], 'big')
                field = bytes([b0]) + body[:SECTOR_SIZE]
                if b0 == SYNC and (crc32p(field) ^ stored) == K_DATA:
                    return body[:SECTOR_SIZE], False
                fixed = correct_burst(field, stored, K_DATA)
                if fixed is not None and fixed[0] == SYNC:
                    return fixed[1:], True          # drop sync byte
        zeros = 0
        i += 1
    return None


def decode_track(raw, expect_cyl=None, expect_head=None):
    """One capture -> {sector: (cyl, head, bytes, corrected)} of valid sectors.
    A clean (uncorrected) read replaces a burst-corrected one. Pass
    expect_cyl/expect_head to reject sectors from a mis-seeked track."""
    bits = words_to_bits(raw)
    got = {}
    for pos, cyl, head, sec in find_headers(bits):
        if sec >= SECTORS_PER_TRACK:
            continue
        if expect_cyl is not None and cyl != expect_cyl:
            continue
        if expect_head is not None and head != expect_head:
            continue
        if sec in got and not got[sec][3]:
            continue                       # already have a clean read
        r = find_data(bits, pos + 72)
        if r is not None:
            data, corrected = r
            if sec not in got or not corrected:
                got[sec] = (cyl, head, data, corrected)
    return got


def classify_missing(raw, sector, expect_cyl=None):
    """Best-effort diagnosis of a sector that never validated:
    'erased-ff' (data field ~= FF-fill with no ECC), 'no-header', or 'failed'.
    """
    bits = words_to_bits(raw)
    for pos, cyl, head, sec in find_headers(bits):
        if sec != sector or (expect_cyl is not None and cyl != expect_cyl):
            continue
        lo = pos + 72
        zeros = 0
        i = lo
        while i < min(lo + 700, len(bits)):
            if bits[i] == 0:
                zeros += 1
                i += 1
                continue
            if zeros >= 22:
                break
            zeros = 0
            i += 1
        field = read_bytes(bits, i - 3, 517)
        if field is None:
            return 'failed'
        ideal = bytes([SYNC]) + b'\xff' * 516
        dist = sum(bin(a ^ b).count('1') for a, b in zip(field, ideal))
        return 'erased-ff' if dist < 0.04 * len(field) * 8 else 'failed'
    return 'no-header'


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('logs', nargs='+', help='screenlog or .bin track files')
    ap.add_argument('--out', help='write merged 23-sector track image (.bin)')
    ap.add_argument('--peek', action='store_true',
                    help='show first 32 bytes of each recovered sector')
    args = ap.parse_args()

    caps = []
    for p in args.logs:
        caps.extend(load_captures(p))
    print(f'{len(caps)} capture(s) loaded')

    merged = {}
    for ci, raw in enumerate(caps):
        got = decode_track(raw)
        new = 0
        for s, v in got.items():
            if s not in merged or (merged[s][3] and not v[3]):
                if s not in merged:
                    new += 1
                merged[s] = v
        nfix = sum(1 for v in got.values() if v[3])
        print(f'  capture {ci}: {len(got):2d} valid sectors '
              f'({nfix} burst-corrected, {new} new) '
              f'-> total {len(merged)}/{SECTORS_PER_TRACK}')

    print(f'\nrecovered {len(merged)}/{SECTORS_PER_TRACK} sectors: '
          f'{sorted(merged)}')
    missing = [s for s in range(SECTORS_PER_TRACK) if s not in merged]
    if missing:
        print(f'missing: {missing}')

    if merged:
        cyls = {v[0] for v in merged.values()}
        heads = {v[1] for v in merged.values()}
        print(f'cyl(s): {sorted(cyls)}  head(s): {sorted(heads)}')
        fills = Counter()
        for s, (_, _, d, fx) in sorted(merged.items()):
            u = set(d)
            desc = f'uniform 0x{d[0]:02x}' if len(u) == 1 else \
                   f'{len(u)} distinct byte values'
            fills[desc] += 1
            if args.peek:
                tag = ' (burst-corrected)' if fx else ''
                print(f'  sec {s:2d}: {desc:24s} {d[:32].hex(" ")}{tag}')
        for desc, n in fills.most_common():
            print(f'  {n:2d} sectors: {desc}')

    if args.out and len(merged) == SECTORS_PER_TRACK:
        with open(args.out, 'wb') as f:
            for s in range(SECTORS_PER_TRACK):
                f.write(merged[s][2])
        print(f'wrote {args.out}')
    elif args.out:
        print(f'--out skipped: track incomplete')
    return 0


if __name__ == '__main__':
    sys.exit(main())
