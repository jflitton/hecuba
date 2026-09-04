# Hecuba hardware

KiCad project for the Hecuba interface board. It sits between a Priam DISKOS
8-inch Winchester drive's native 50-pin interface and a Raspberry Pi Pico 2 W
(ref `A1`), which runs the firmware in `../firmware`. Rev 0.9A is fabricated,
assembled, and has imaged a full Priam 3450. Read-only by design.

## Why a custom board

The Priam 3450 (about 35 MB, 3,600 RPM, 5 heads, 525 cylinders) fits none of
the usual readers.

- No STEP line. Heads are positioned by a closed-loop voice-coil servo under an
  on-board 8035. A seek is a register write plus a Seek command, and the drive
  must be powered, spun up, and taking commands before it returns a bit.
- Not a flux interface. The drive does its own data separation and outputs
  recovered NRZ data plus clock on RS-422 pairs at 6.4 Mbit/s.
- The sector format belongs to the controller that wrote the disk (an Intel
  iSBC 215 here), not to the drive. Decoding happens offline in `../software`.
- Not SASI, despite the 50-pin ribbon and 8-bit active-low bus. PRIAM has no
  REQ/ACK handshake and carries read data on separate differential pairs.

## Building one

| | |
|---|---|
| Board | 2 layers, 125 x 104 mm, 1.6 mm. Signals on top; bottom is a ground pour with +5 V and +3.3 V routed through it as 1 mm paths |
| Tracks and vias | 0.2 mm signal, 1.0 mm power. Vias 0.6 mm / 0.3 mm hole (1.0 / 0.6 on power) |
| Smallest features | 0.2 mm track, 0.3 mm hole, 0.5 mm copper to edge |
| Packages | SOIC and SOT-23-6 ICs, 0805 passives. Hand-solderable in about an hour |
| Mounting | Four 3 mm plated holes, grounded |
| Parts list | `bom/bom.csv`, with a column for parts deliberately left unpopulated |
| Placement guide | `bom/ibom.html`, interactive; click a part to see where it goes |

All ICs are current-production TI parts as of 2026. Gerbers are plotted from
KiCad when needed and are not committed.

Routing rules for a respin:

- Only READ DATA and READ/REF CLOCK need care. Route each as a tight parallel
  pair over unbroken ground, and keep the two pairs apart. No impedance control
  and no length matching (the skew budget at 6.4 Mbit/s is about 170 mm).
- Everything else is slow. Keep the 8 data-bus lines grouped.
- Damping resistors at the source, termination at the receiver, and each
  100 nF capacitor hard against its power pin with its own ground via.

## Parts

| Ref | Part | Function | Datasheet |
|---|---|---|---|
| A1 | Pico 2 W (RP2350) | Controller. Powers the board over USB | [raspberrypi.com](https://datasheets.raspberrypi.com/picow/pico-2-w-datasheet.pdf) |
| U1 | SN74LVC4245A | 8-bit data bus, bidirectional. Direction set by the Pico (GP8) | [ti.com](https://www.ti.com/lit/ds/symlink/sn74lvc4245a.pdf) |
| U2 | SN74LVC4245A | Outbound control lines: address, strobes, head select, read gate. Direction strapped | [ti.com](https://www.ti.com/lit/ds/symlink/sn74lvc4245a.pdf) |
| U3 | SN74LVC14A | Schmitt-trigger inverters on INDEX, READY, SECTOR MARK | [ti.com](https://www.ti.com/lit/ds/symlink/sn74lvc14a.pdf) |
| U4 | AM26LV32E | RS-422 receivers for READ DATA and READ/REF CLOCK. Receive only | [ti.com](https://www.ti.com/lit/ds/symlink/am26lv32e.pdf) |
| U5 | SN74LVC1T45 | -RESET driver. Direction strapped | [ti.com](https://www.ti.com/lit/ds/symlink/sn74lvc1t45.pdf) |
| J1 | 2x25 IDC header | 50-pin ribbon to the drive | |

## Termination

Measured on one drive. Measure yours before populating. The manual's generic
values assume the controller terminates everything; this drive terminates most
of the bus internally. Values were measured at the connector with the drive
unpowered and the ribbon off.

| Part | Value | Fitted | Reason |
|---|---|---|---|
| R1-R16, R28 | 0 ohm | yes | Series damping on outbound lines. The drive terminates this bus at about 179 ohm; even 33 ohm in series lifts a logic low to about 0.72 V against a 0.8 V threshold. Fit 10 ohm or less only if a scope shows overshoot |
| R17 / R20 | 220 / 330 ohm | yes | INDEX pull-up and pull-down. INDEX measured open at the drive. Settles near 3.0 V released |
| R18-R19 / R21-R22 | 220 / 330 ohm | no | READY and SECTOR MARK are pulled up inside the drive (130 / 175 ohm measured). Fit only if J1 pin 28 is left unpowered |
| R26 | 1 k | yes | Bias across the CLOCK pair, which measured 95 ohm. 100 ohm here would load the pair to 49 ohm |
| R27 | 100 ohm | yes | Termination across the DATA pair, which measured 252 ohm |
| R29 | 10 k | yes | Pull-up on GP17 (-RESET). Holds reset deasserted through boot |
| C1-C8 | 100 nF | yes | Decoupling, one per power pin |
| C9 / C10 | 10 uF | yes | Bulk on each rail at the Pico's power pins |
| JP1 | 2-pin header | shunt open | +5 V to J1 pin 28 (terminator power). Not needed on the drive tested |

## 50-pin connector (J1)

| Signal | Pin | Direction | Connects to |
|---|---|---|---|
| DBUS 0-7 | 2-9 | both | U1 via R1-R8 |
| -READ GATE | 11 | to drive | U2 via R9 |
| -RESET | 13 | to drive | U5 via R28 |
| -WRITE GATE | 15 | to drive | not connected |
| -RD / -WR | 17 / 18 | to drive | U2 via R10 / R11 |
| +AD 1 / +AD 0 | 19 / 20 | to drive | U2 via R12 / R13 |
| -DRIVE SELECT 1 | 22 | to drive | ground (always selected) |
| -DRIVE SELECT 2-4 | 23-25 | to drive | not connected |
| +5 V to terminator | 28 | power | JP1 (open) |
| -HEAD SELECT 4 / 2 / 1 | 29 / 30 / 31 | to drive | U2 via R14 / R15 / R16 |
| -INDEX | 33 | from drive | U3, with R17 / R20 |
| -READY | 35 | from drive | U3 |
| -SECTOR MARK | 37 | from drive | U3 |
| WRITE DATA pair | 39 / 40 | to drive | not connected |
| WRITE CLOCK pair | 42 / 43 | to drive | not connected |
| READ/REF CLOCK pair | 45 / 46 | from drive | U4, R26 across |
| READ DATA pair | 48 / 49 | from drive | U4, R27 across |

Ground pins 1, 10, 12, 14, 16, 21, 26, 27, 32, 34, 36, 38, 41, 44, 47, 50
connect to the ground plane.

## Power

- Drive rails (+24 V, +5 V, -5 V, -12 V, about 100 W total) come from a bench
  supply into the drive's own connector. The board neither carries nor senses
  them. Current-limit and watch +24 V at power-up; it runs the spindle and the
  voice coil.
- Board rails come from the Pico's USB. VBUS (pin 40) feeds the drive-facing
  side of the translators and the status pull-ups. The Pico's 3.3 V regulator
  (pin 36) feeds the Pico-facing side.
- Board draw is about 250 mA idle and 400 to 600 mA peak (the radio spikes on
  transmit). Use a 1.5 A supply or powered hub.

Power the Pico first, then the drive, and do not replug USB while the drive is
powered. Until firmware init runs, the Pico's pins are inputs with pull-downs,
which asserts every active-low control line through U2. Only -RESET is
protected in hardware (R29).

## Noteworthy facts

- **Write lines have no copper.** WRITE GATE, WRITE DATA, and WRITE CLOCK
  appear on the connector symbol only. No trace, no resistor. Writing would
  require adding wires to the board. The drive's own Write Enable switch is
  also off, and its internal terminator holds WRITE GATE deasserted.
- **The drive is 5 V TTL; the RP2350 is 3.3 V and not 5 V tolerant.** Every
  signal crosses a translator. Net names carry the level: `DBUS0` on the drive
  side, `DBUS0_3V3` on the Pico side.
- **Status lines invert.** U3 inverts, so INDEX, READY, and SECTOR MARK read
  active-high at the Pico while every other PRIAM signal is active-low. The
  schematic writes active-low nets as `~{NAME}`; the drive manual uses a
  leading minus. The full per-line chain is in
  `../firmware/include/board_pins.h`.
- **U3 is a Schmitt trigger on purpose.** The drive's status outputs are
  open-collector 75462s with slow RC rising edges. Hysteresis turns each into
  one clean transition. The drive's own end uses 74LS14s for the same reason.
- **U4 is the E variant on purpose.** AM26LV32E adds hysteresis and an
  open-input fail-safe, so the read pairs hold a defined level before READ GATE
  asserts and before the drive's clock recovery locks.
- **R29 is load-bearing.** U5 has no output enable and follows GP17 from
  power-on, and the RP2350 boots GP17 as an input with a pull-down. Without
  R29 every power-up would reset the drive.
- **DRIVE SELECT 1 is tied to ground.** One drive, permanently selected, no
  chip-select GPIO. The data bus still only drives back during a read strobe.
- **Unused U3 and U4 inputs are tied off.** Floating Schmitt or differential
  inputs oscillate.
- **R26 and R27 differ by 10x** for two pairs on the same connector. Only
  measurement finds that.

## Files

| Path | Contents |
|---|---|
| `hecuba.kicad_pro` | Project settings, net classes, design rules |
| `hecuba.kicad_sch` | Schematic, one sheet |
| `hecuba.kicad_pcb` | Board layout |
| `hecuba.kicad_prl` | KiCad UI state |
| `hecuba.kicad_sym` | Five project symbols. `PRIAM_50` is the drive connector with real PRIAM signal names |
| `footprints.pretty/` | Footprints for the four ICs, registered by `fp-lib-table` so the project opens standalone |
| `fp-lib-table` | Points KiCad at the folder above |
| `lib/<PART>/` | Vendor symbol and footprint downloads per chip, untouched. Datasheets are linked from the parts table, not stored here |
| `bom/bom.csv` | Parts list with populate / do-not-populate column |
| `bom/ibom.html` | Interactive BOM |

`.gitignore` covers KiCad backups and netlist output.
