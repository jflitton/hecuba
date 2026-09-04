# Hecuba software

Host-side tools for the Hecuba board: image a Priam drive over the firmware's USB
console, recover more sectors offline from the saved raw captures, and walk the
iRMX-86 filesystem on the assembled image. Python 3. Only `sweep.py` needs an extra
package (pyserial).

Scripts in pipeline order:

## sweep.py

Whole-drive imaging. Drives the firmware console, captures each track, decodes it
inline, and re-reads until every sector validates or reads stop adding sectors.
Writes `image/manifest.json`, one `image/tracks/cylCCC_hN.bin` per track, and every
distinct raw capture under `image/raw/`. Resumable; reruns skip completed tracks.

    python3 sweep.py --probe                     # connect + status, no motion
    python3 sweep.py                             # image everything
    python3 sweep.py --cyls 0:100 --heads 1,2,3,4
    python3 sweep.py --revisit                   # retry previously-failed tracks

## vote_repair.py

Offline. For every sector still marked `failed`, extracts that sector's data field
from every saved raw capture, drops outlier reads, majority-votes per bit, and
ECC-checks the result. Writes winners back into `tracks/` and the manifest as `voted`.

    python3 vote_repair.py [--image image] [--min-reads 3] [--dry-run]

## rescue.py

Offline. Recovers sectors whose header is dead but whose data field is intact.
Builds a slot-to-sector map from header positions across all reads, then scans each
slot for an ECC-valid data field. Marks results `position`.

    python3 rescue.py [--image image] [--dry-run] [--jobs 8] [--cyls A:B]

## deep_rescue.py

Offline, last resort. Sliding ECC scan over the slot window, aligned per-bit vote
across reads, and single bit-slip repair. Marks results `deep`.

    python3 deep_rescue.py [--image image] [--dry-run] [--jobs 8]

## irmx_fs.py

Walks an iRMX-86 named volume: label, fnode table, directory tree. Reports per-file
intactness against a missing-sector list and writes the full listing to a file.

    python3 irmx_fs.py [--img volume.img] [--missing volume.missing.json] [--tree-out filetree.txt]

## defect_map.py

Decodes the Priam 3450 reserved area (cylinders 513 to 524) and the format-time
bad-track table on cylinder 523, then splits unrecovered sectors into factory
defects and real storage-era loss, attributed to owning files.

    python3 defect_map.py [--img volume.img] [--missing volume.missing.json] [--json defects.json]

## decode_scout.py

The iSBC 215 sector decoder every other script imports: header and data field
search at any bit alignment, CRC-32 ECC check, 4-bit burst correction. Also runs
standalone on a screen log or raw track files.

    python3 decode_scout.py <screenlog|track.bin>... [--out track.bin] [--peek]

## Notes

- `sweep.py`, `vote_repair.py`, `rescue.py`, and `deep_rescue.py` all write
  `image/manifest.json` and `image/tracks/`. Never run two of them at once.
- Sector states in the manifest: `valid`, `corrected` (burst-corrected on read),
  `voted`, `position`, `deep` (the three offline passes), `failed`, `no-header`,
  `erased-ff`.
- Images, raw captures, and anything recovered from a particular drive are not
  part of this repository.
