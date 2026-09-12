# Hecuba firmware

Firmware for the Raspberry Pi Pico 2 W (RP2350) on the Hecuba board (ref `A1` in
`../hardware`). It drives a Priam DISKOS 8-inch drive over its native 50-pin
interface and captures whole-track NRZ bit streams for offload over USB serial.

Read-only. No code path asserts -WRITE GATE or drives WRITE DATA / WRITE CLOCK,
and the board has no copper on those pins.

## Build and flash

VS Code: install the official Raspberry Pi Pico extension and open `firmware/`
as the project root (the extension only handles single-folder workspaces). It
supplies the SDK, CMake, Ninja, the ARM toolchain, picotool, and OpenOCD.

Command line:

```bash
export PICO_SDK_PATH=/path/to/pico-sdk          # 2.1 or newer
cmake -B build -G Ninja \
  -DPICO_TOOLCHAIN_PATH=/path/to/arm-gnu-toolchain \
  -DCMAKE_PREFIX_PATH="$(brew --prefix)"        # locates picotool
cmake --build build                             # build/hecuba.uf2
```

Flash: hold BOOTSEL while plugging in USB, then drag `build/hecuba.uf2` onto the
`RP2350` drive or run `picotool load -fx build/hecuba.uf2`.

- Pico SDK 2.1 is the minimum. Older SDKs do not know the RP2350.
- Use Arm's own `arm-none-eabi` toolchain. Homebrew's formula lacks newlib and
  fails at link with `cannot read spec file 'nosys.specs'`.

## Console

All control and diagnostics go over USB serial at 115200. There is no debug
UART (GP0 and GP1 are data bus lines). The banner reprints on every connect.
Type `help`.

```bash
screen /dev/tty.usbmodem* 115200
```

| Command | Action |
|---|---|
| `status` | Read and decode the Status register |
| `up [secs]` | Sequence Up: spin up, restore to cylinder 0, wait for READY. About 30 s; timeout default 35 |
| `down` | Sequence Down: park heads, brake spindle |
| `seek <cyl>` | Seek to cylinder 0 to 524 |
| `head <0-4>` | Select a data head |
| `gate on\|off` | Assert or deassert -READ GATE |
| `id` | Read Drive ID. A 3450 returns `0x04` |
| `capture <cyl> <head>` | Seek, select head, open gate, settle, capture one INDEX-aligned track |
| `capraw [head]` | Capture with no seek and no status checks (bench and loopback) |
| `dump [n]` | Hexdump the first n words of the last capture (default 16) |
| `dumpb64 [n]` | Base64 the capture buffer to the host |
| `pins` | GPIO levels plus rising-edge counts over 100 ms |
| `reg r <ad>` | Raw register read (ad = 0, 1, 2) |
| `reg w <ad> <hex>` | Raw register write |
| `stair` | Seek staircase 0, 1, 0, 2, ... 524 with address readback at every step. About 1,050 seeks, a few minutes, any key aborts |
| `gating [off\|mark\|both] [drop_us] [data_off_us] [data_drop_us]` | Per-field -READ GATE pulses during capture. See below |
| `reset` | Pulse -RESET for 1 ms. Not part of any normal sequence |

Typical session:

```
> id
drive id = 0x04 (expect 0x04 for 3450)
> up
sequence up: OK  status 0x0B [READY SEEKCMP CYL0 ]
> pins
levels : INDEX=0 READY=1 SECTOR=0 CLK=1 DATA=0
edges/100ms: INDEX=6 SECTOR=138 (expect 6 / 138 spinning) CLK~... DATA~... (aliased; nonzero = alive)
> capture 0 0
(capture engine started on core1)
capture cyl 0 head 0: OK: 3780 words (15120 bytes)
> dumpb64
-----BEGIN CAPTURE b64 (3780 words, LE, first bit = LSB)-----
...
> down
sequence down: OK  status 0x00 []
```

`../software/sweep.py` drives this console to image a whole drive.

## Bench procedure

1. Power the Pico over USB first and let it boot. Do not replug USB while the
   drive is powered. Until `priam_bus_init()` runs, the Pico's pins are inputs
   with pull-downs, which asserts every active-low control line at once. Only
   -RESET is protected in hardware (R29).
2. Bring up the drive rails.
3. `up`, then `id`, then `pins`. A spinning drive shows about 6 INDEX and 138
   SECTOR MARK edges per 100 ms with READY=1. Clock and data counts are
   aliased; only nonzero matters.
4. Run `stair` before a long imaging run. It exercises the servo over its full
   travel and confirms the cylinder bit packing. It stops on the first failure.
5. Capture. `down` when finished.

Capture failures:

| Situation | Timeout | Message |
|---|---|---|
| No INDEX pulse | 60 ms | `FAIL: no INDEX edge, or clock never ran` |
| Clock stops mid-track | 250 ms | `PARTIAL: n/3780 words` |

Both point at `pins`. Zero clock edges with the drive spinning and the gate
open means the read channel is not reaching the board.

## Capture format

Raw sampled bits, no decoding. One capture is 3,780 32-bit words (15,120
bytes), about 12.5% more than one revolution (3,360 words, 13,440 bytes,
16.7 ms at 3,600 RPM). INDEX alignment is done in software, so the margin
guarantees a full INDEX-to-INDEX window with overlap rather than a gap.

- The first bit sampled is bit 0 of word 0, the 32nd is bit 31 (the PIO shifts
  right).
- `dumpb64` emits each word little-endian. The first bit off the platter is the
  LSB of the first byte received.

`../software/decode_scout.py` decodes this stream (iSBC 215 format, 512 B
sectors).

## Per-field gate re-sync

-READ GATE is pulsed off and on per field during a capture, following the
original controllers:

- PRIAM OEM/Service Manual 308000-0, Figure 3-40 and Table 3-15: READ GATE is
  asserted in the zero gap after SECTOR MARK and dropped before the next write
  splice. READ DATA is invalid for 9 µs after each assertion (PLO sync).
- iSBC 215 Hardware Reference Manual 121593-002, paragraphs 4-29 and 4-30:
  RD GATE is reset by END TIME at the end of the ID field's ECC and "again
  raised to search for the sync byte of the data field".

With the gate held across a write splice the separator's output for the
following field is unreliable and the field fails ECC.

Timing for the iSBC 215 sector format (1.24 µs per byte), from the SECTOR MARK
leading edge: ID sync byte about 22 µs, ID field end about 33 µs, data-field
write splice about 44 µs (the 215 writes ID fields and data fields in separate
format passes, so every sector has one), data sync byte about 68 µs.

```
gating                   show the setting (default: both 3 37 11)
gating both 3 37 11      gate off 3 µs at each SECTOR MARK, and off from 37 to
                         48 µs after it (spans the splice, ~18 µs of zeros left
                         for the resync)
gating mark [drop_us]    mark pulse only
gating off               gate held for the whole revolution
```

Capture lines report `[gating both, N drops, mark0 +NNus]`: N is the number of
gate pulses over the INDEX wait plus the track; `mark0` is the INDEX to first
SECTOR MARK time (spec 44.6 ± 1.4 µs).

## Noteworthy facts

- **Capture is PIO plus DMA, not CPU.** `src/capture.pio` is three
  instructions: wait for the drive's clock high, wait for it low, shift in one
  READ DATA bit. No local clock or sample rate exists; the drive's recovered
  clock paces everything. DMA drains the FIFO into RAM. A CPU breakpoint does
  not stop a capture.
- **Sample on the falling clock edge.** READ DATA changes shortly after the
  rising edge of READ/REF CLOCK, so a sample there lands on the transition.
  The falling edge is mid-cell.
- **Gate per field.** A capture with the gate held for the whole revolution
  loses the field after each write splice. See "Per-field gate re-sync".
- **Two cores by role.** core0 runs the register bus and USB console. core1
  runs only the capture engine and starts on the first capture, so a PIO or
  DMA problem cannot take the console down. The 1 Hz LED heartbeat stops during
  a capture because core0 blocks on core1.
- **pio1, not pio0.** The Pico 2 W LED hangs off the CYW43 radio, whose driver
  claims a pio0 state machine at startup. The wireless stack is linked only for
  the LED. There is no networking.
- **Status bits are invalid while BUSY is set.** SEEK COMPLETE, SEEK FAULT, and
  COMMAND REJECT flutter during spin-up. Wait for BUSY to clear before judging
  them. An early version aborted a good `up` with status `0xD0` for this reason.
- **DRIVE FAULT is never retried.** The firmware reports it and stops.
- **`down` can report FAIL with status `0x50` after a successful park.** The
  spindle brake outlasts the status timeout, leaving BUSY set.
- **`reset` on a sequenced-up drive latches DRIVE FAULT.** Clear it with
  `reg w 0 05` (Fault Reset).
- **Init order.** `priam_bus_init()` is the first call in `main()`. It writes
  each pin's idle level before switching it to output, because the SDK zeroes
  the output latch on init. The reverse order drives every active-low line low
  for a few microseconds.
- **Polarity.** Control outputs pass through a non-inverting buffer, so assert
  means drive the GPIO low. The three status inputs pass through an inverting
  Schmitt trigger and read active-high at the Pico. `include/board_pins.h`
  documents the chain for every line.
- **Geometry is the 3450's.** `include/priam_proto.h` fixes 525 cylinders and
  5 heads, and `seek`, `stair`, and `capture` reject cylinders above 524. A
  DISKOS 7050 (1049 cylinders, same interface) should work with those constants
  changed. Untested.

## Register interface

Three registers selected by AD0/AD1. Read and write address different
registers.

| Address | Write | Read |
|---|---|---|
| 0 | Command | Status |
| 1 | Target Address upper | Current Address upper |
| 2 | Target Address lower | Current Address lower |

Commands: `0x01` Sequence Up, `0x02` Sequence Down, `0x03` Restore, `0x04`
Seek, `0x05` Fault Reset, `0x10` Read Drive ID. A seek writes the cylinder into
registers 1 and 2, writes `0x04` to register 0, then polls register 0 until
BUSY clears.

Status bits: READY, SEEK COMPLETE, SEEK FAULT, CYLINDER ZERO, BUSY, DRIVE
FAULT, WRITE PROTECT, COMMAND REJECT. The console decodes them (`0x0B` prints
`[READY SEEKCMP CYL0 ]`).

Bus timing. There is no handshake beyond the strobes; every value is padded
above the manual's minimum using the SDK's minimum-cycle busy-wait.

| Parameter | Manual minimum | Used |
|---|---|---|
| Address setup before strobe | 60 ns | 80 ns |
| Data setup before -WR release | 60 ns | 80 ns |
| -WR low | 100 ns | 150 ns |
| -RD low | 100 ns | 150 ns |
| Data valid after -RD | 60 ns | 80 ns |
| Gap between strobes | 200 ns | 250 ns |

## Pin map

`include/board_pins.h` is authoritative.

| GPIO | Signal | Dir | Via | Notes |
|---|---|---|---|---|
| GP0-7 | DBUS 0-7 | both | U1 '4245 | Direction from GP8: 0 = Pico drives, 1 = drive drives |
| GP8 | DBUS direction | out | | Controls U1 |
| GP9, GP10 | AD0, AD1 | out | U2 '4245 | Register select, plain logic levels |
| GP11, GP12 | -RD, -WR | out | U2 | Strobes, assert = low |
| GP13-15 | -HEAD SELECT 1, 2, 4 | out | U2 | Assert = low. HS1 is bit 0, HS4 is bit 2 |
| GP16 | -READ GATE | out | U2 | Assert = low |
| GP17 | -RESET | out | U5 1T45 | Assert = low. R29 holds it deasserted through boot |
| GP18 | -INDEX | in | U3 '14, inverting | Reads active-high at the Pico |
| GP19 | -READY | in | U3, inverting | Reads active-high |
| GP20 | -SECTOR MARK | in | U3, inverting | Reads active-high |
| GP21 | READ/REF CLOCK | in | U4 RS-422 | The PIO `wait gpio` pin |
| GP22 | READ DATA | in | U4 RS-422 | The PIO `in pins` pin |

No drive-select GPIO. DRIVE SELECT 1 is tied asserted on the board.

## Debugging

No probe is needed. The drive was imaged using BOOTSEL flashing and the serial
console.

`.vscode/launch.json` is set up for a CMSIS-DAP probe (the Raspberry Pi Debug
Probe, or a Pico running `debugprobe_on_pico`: GP2 to SWCLK, GP3 to SWDIO,
ground to ground on the 3-pin debug header) with RTT logging. Enable RTT with
`pico_enable_stdio_rtt(hecuba 1)` in `CMakeLists.txt`; it keeps `printf` off
USB and away from the capture core. PIO and DMA keep running under a
breakpoint, so inspect captures with `dump` afterwards.

`c_cpp_properties.json` points IntelliSense at `build/compile_commands.json`,
which exists after the first configure. `settings.json` disables CMake Tools so
it does not fight the Pico extension.

## Files

| Path | Contents |
|---|---|
| `include/board_pins.h` | GPIO map with polarity and translator chain per line |
| `include/priam_proto.h` | Register addresses, command codes, Status bits, geometry, cylinder packing |
| `src/main.c` | Startup: park drive-facing lines, bring up USB and LED, run the console loop |
| `src/priam_bus.c`, `.h` | Register bus: strobes and timing through Sequence Up, Seek, head select |
| `src/capture.pio` | The three-instruction sampler |
| `src/capture.c`, `.h` | Capture engine on core1: PIO and DMA setup, INDEX alignment |
| `src/host_cli.c`, `.h` | The console |
| `CMakeLists.txt` | Selects the Pico 2 W, assembles the PIO program, links PIO, DMA, multicore, USB stdio |
| `pico_sdk_import.cmake` | Stock SDK locator, unmodified |
| `.vscode/` | Editor and debugger configuration |
| `build/` | Build output including `hecuba.uf2`. Generated, not committed |
