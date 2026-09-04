// board_pins.h - Hecuba controller GPIO map (RP2350 / Pico 2 W, ref A1)
//
// SINGLE SOURCE OF TRUTH for pin assignments, transcribed from the KiCad
// schematic in ../hardware. If the schematic changes, update this file to
// match it.
//
// ============================ POLARITY / SIGNAL CHAIN =======================
// Every line between the core and the 5 V drive is voltage-translated:
//
//   * DBUS0..7      U1  SN74LVC4245A  bidirectional, non-inverting.
//                       DIR = GP8 (PIN_DBUS_DIR): 0 => B->A (core drives = WRITE),
//                       1 => A->B (drive drives = READ). OE tied low (always on).
//   * Control outs  U2  SN74LVC4245A  DIR strapped low => always B->A,
//                       non-inverting. So a core GPIO level passes straight to the
//                       drive line. These drive lines are ACTIVE-LOW, therefore:
//                       assert  => drive GPIO LOW,  deassert => drive GPIO HIGH.
//                       (AD0/AD1 are plain logic levels, not active-low strobes.)
//   * -RESET        U5  SN74LVC1T45   DIR low => B->A, non-inverting. Active-low:
//                       GPIO LOW = reset asserted. R29 10k pulls GP17 high so the
//                       drive sees -RESET deasserted through boot (GP17 resets to
//                       input). Firmware drives GP17 output HIGH early to hold it.
//   * Status ins    U3  SN74LVC14A    INVERTING Schmitt. The drive's active-low
//                       -INDEX/-READY/-SECTOR MARK arrive at the core ACTIVE-HIGH:
//                       gpio_get(pin) == 1  means the signal is ASSERTED.
//   * READ DATA /   U4  AM26LV32E      RS-422 receiver, non-inverting -> clean
//     READ/REF CLK                     3V3 logic at the core.
//
// There is no -DRIVE SELECT GPIO: it is hard-tied asserted on the board for the
// single-drive setup (SIG-5), so firmware never drives a chip select.
// ============================================================================

#ifndef HECUBA_BOARD_PINS_H
#define HECUBA_BOARD_PINS_H

// ----- DBUS 0..7 : contiguous on GP0..GP7 (one byte, mask-addressable) ------
#define PIN_DBUS_BASE      0u
#define PIN_DBUS0          0u
#define PIN_DBUS7          7u
#define DBUS_MASK          (0xFFu << PIN_DBUS_BASE)   // 0x000000FF

// ----- Control / address outputs (via U2, non-inverting, active-low) --------
#define PIN_DBUS_DIR       8u    // U1 direction: 0=write(B->A), 1=read(A->B)
#define PIN_AD0            9u    // register select A0 (logic level)
#define PIN_AD1           10u    // register select A1 (logic level)
#define PIN_RD_N          11u    // -RD  strobe (assert = LOW)
#define PIN_WR_N          12u    // -WR  strobe (assert = LOW)
#define PIN_HS1_N         13u    // -HEAD SELECT 1 (assert = LOW)
#define PIN_HS2_N         14u    // -HEAD SELECT 2 (assert = LOW)
#define PIN_HS4_N         15u    // -HEAD SELECT 4 (assert = LOW)
#define PIN_READ_GATE_N   16u    // -READ GATE (assert = LOW)
#define PIN_RESET_N       17u    // -RESET via U5 (assert = LOW); R29 holds high

// ----- Status inputs (via U3, INVERTING -> active-HIGH at the core) ---------
#define PIN_INDEX         18u    // -INDEX       : core HIGH = index pulse
#define PIN_READY         19u    // -READY       : core HIGH = drive ready
#define PIN_SECTOR_MARK   20u    // -SECTOR MARK : core HIGH = sector mark

// ----- High-speed serial capture inputs (via U4 RS-422 rx) ------------------
#define PIN_READ_REF_CLK  21u    // recovered bit clock  (PIO `wait gpio`)
#define PIN_READ_DATA     22u    // recovered NRZ data   (PIO `in pins`)

// Aggregate masks for batch GPIO init.
#define CONTROL_OUT_MASK ( (1u<<PIN_DBUS_DIR) | (1u<<PIN_AD0) | (1u<<PIN_AD1) | \
                           (1u<<PIN_RD_N) | (1u<<PIN_WR_N) | \
                           (1u<<PIN_HS1_N) | (1u<<PIN_HS2_N) | (1u<<PIN_HS4_N) | \
                           (1u<<PIN_READ_GATE_N) | (1u<<PIN_RESET_N) )

#define STATUS_IN_MASK   ( (1u<<PIN_INDEX) | (1u<<PIN_READY) | (1u<<PIN_SECTOR_MARK) )

// Active-low control lines whose idle (deasserted) state is HIGH. -RESET is
// included so init parks it deasserted; DBUS_DIR/AD0/AD1 are excluded (they are
// logic levels, not active-low strobes; they default to a benign 0).
#define CONTROL_IDLE_HIGH_MASK ( (1u<<PIN_RD_N) | (1u<<PIN_WR_N) | \
                                 (1u<<PIN_HS1_N) | (1u<<PIN_HS2_N) | (1u<<PIN_HS4_N) | \
                                 (1u<<PIN_READ_GATE_N) | (1u<<PIN_RESET_N) )

#endif // HECUBA_BOARD_PINS_H
