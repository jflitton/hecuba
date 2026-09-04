// priam_bus.c - PRIAM register-bus master implementation.
//
// Polarity recap (see board_pins.h): control lines go through the non-inverting
// U2 '4245, so an ACTIVE-LOW drive line is asserted by driving the core GPIO
// LOW. DBUS goes through U1 with DIR on GP8 (0=write/B->A, 1=read/A->B).

#include "priam_bus.h"
#include "board_pins.h"

#include <stdio.h>
#include "pico/stdlib.h"
#include "hardware/gpio.h"
#include "hardware/clocks.h"

// ---- ns-accurate short delays for bus AC timing (CAP-5) ---------------------
// busy_wait_at_least_cycles guarantees a *minimum*, which is exactly the
// semantics the PRIAM setup/hold/pulse specs want. Margins are generous: the
// 8035-side bus is slow and these figures are floors, not ceilings.
static uint32_t s_cycles_per_us;

static inline void delay_ns(uint32_t ns) {
    // cycles = ns * f_sys / 1e9, rounded up. Derived from cycles_per_us.
    uint32_t cycles = (uint32_t)(((uint64_t)ns * s_cycles_per_us + 999u) / 1000u);
    if (cycles == 0) cycles = 1;
    busy_wait_at_least_cycles(cycles);
}

// Named timing floors from the manual §6.1 (Tables 3-8/3-9), with headroom.
#define T_ADDR_SETUP_NS   80    // >=60: address stable before -WR
#define T_DATA_SETUP_NS   80    // >=60: data stable before -WR rising edge
#define T_WR_PULSE_NS    150    // >=100: -WR low width
#define T_RD_PULSE_NS    150    // >=100: -RD low width
#define T_RD_VALID_NS     80    // >=60: data valid after -RD asserted
#define T_RECOVERY_NS    250    // >=200: between successive register strobes

// ---- low-level helpers ------------------------------------------------------
static inline void set_address(priam_reg_t reg) {
    // AD0/AD1 are plain logic levels (non-inverting path).
    gpio_put(PIN_AD0, (reg & 0x1u) != 0);
    gpio_put(PIN_AD1, (reg & 0x2u) != 0);
}

static inline void dbus_drive(uint8_t value) {
    gpio_put(PIN_DBUS_DIR, 0);            // U1: B->A (core drives drive)
    gpio_set_dir_out_masked(DBUS_MASK);   // core pins are outputs
    gpio_put_masked(DBUS_MASK, (uint32_t)value << PIN_DBUS_BASE);
}

static inline void dbus_release_to_input(void) {
    gpio_set_dir_in_masked(DBUS_MASK);    // float core pins first...
    gpio_put(PIN_DBUS_DIR, 1);            // ...then U1: A->B (drive drives core)
}

void priam_bus_init(void) {
    s_cycles_per_us = clock_get_hz(clk_sys) / 1000000u;

    // Order matters throughout: gpio_init() zeroes a pin's output latch, so
    // switching a pin to output before writing its idle level would drive it
    // LOW, asserting every active-low strobe at the drive, and pulsing -RESET
    // (drive-fault condition #7 on a sequenced-up drive). Latch the parked
    // level FIRST, then flip direction, so the first driven level is idle.

    // DBUS: core pins stay inputs (released); U1 DIR latched to 1 (A->B, drive
    // drives core) before it drives, so U1 never sources the drive-side bus.
    for (uint pin = PIN_DBUS0; pin <= PIN_DBUS7; pin++) {
        gpio_init(pin);
    }
    gpio_init(PIN_DBUS_DIR);
    gpio_put(PIN_DBUS_DIR, 1);
    gpio_set_dir(PIN_DBUS_DIR, GPIO_OUT);

    // Control/address outputs: active-low lines park HIGH (deasserted, -RESET
    // included); AD0/AD1 are logic levels and park low.
    for (uint pin = PIN_AD0; pin <= PIN_RESET_N; pin++) {
        gpio_init(pin);
        gpio_put(pin, (CONTROL_IDLE_HIGH_MASK >> pin) & 1u);
        gpio_set_dir(pin, GPIO_OUT);
    }

    // Status inputs.
    for (uint pin = PIN_INDEX; pin <= PIN_SECTOR_MARK; pin++) {
        gpio_init(pin);
        gpio_set_dir(pin, GPIO_IN);
    }
    // READ/REF CLK + READ DATA: init as plain inputs so `pins` can observe
    // them before the capture engine exists (uninitialized RP2350 pads read 0,
    // seen live as CLK~0 with the drive spinning). pio_gpio_init() hands
    // them to PIO at first capture; SIO-side gpio_get still reads the pads.
    gpio_init(PIN_READ_REF_CLK);
    gpio_init(PIN_READ_DATA);
}

void priam_reg_write(priam_reg_t reg, uint8_t value) {
    set_address(reg);
    dbus_drive(value);
    delay_ns(T_ADDR_SETUP_NS > T_DATA_SETUP_NS ? T_ADDR_SETUP_NS : T_DATA_SETUP_NS);

    gpio_put(PIN_WR_N, 0);          // assert -WR
    delay_ns(T_WR_PULSE_NS);
    gpio_put(PIN_WR_N, 1);          // deassert -WR (data latched on rising edge)

    delay_ns(T_RECOVERY_NS);
    // Leave DBUS driving the (now-latched) value briefly; release to input so we
    // never contend with the drive on the next read.
    dbus_release_to_input();
}

uint8_t priam_reg_read(priam_reg_t reg) {
    set_address(reg);
    dbus_release_to_input();        // U1 A->B; drive will source DBUS under -RD
    delay_ns(T_ADDR_SETUP_NS);

    gpio_put(PIN_RD_N, 0);          // assert -RD
    delay_ns(T_RD_VALID_NS);
    uint8_t value = (uint8_t)((gpio_get_all() & DBUS_MASK) >> PIN_DBUS_BASE);
    // Hold -RD for the full minimum pulse before releasing.
    delay_ns(T_RD_PULSE_NS - T_RD_VALID_NS);
    gpio_put(PIN_RD_N, 1);          // deassert -RD

    delay_ns(T_RECOVERY_NS);
    return value;
}

void priam_command(priam_cmd_t cmd) {
    priam_reg_write(PRIAM_REG_CMD_OR_STATUS, (uint8_t)cmd);
}

void priam_select_head(uint8_t head) {
    // §6.5: HS1=bit0, HS2=bit1, HS4=bit2; a SET bit means assert (line LOW).
    // head 0 => all lines high (none asserted); head 4 => HS4 low only.
    if (head >= PRIAM_NUM_HEADS) head = 0;
    gpio_put(PIN_HS1_N, !((head >> 0) & 1u));
    gpio_put(PIN_HS2_N, !((head >> 1) & 1u));
    gpio_put(PIN_HS4_N, !((head >> 2) & 1u));
}

void priam_read_gate(bool asserted) {
    gpio_put(PIN_READ_GATE_N, asserted ? 0 : 1);   // active-low
}

// ---- polled high-level operations -------------------------------------------
bool priam_wait_not_busy(uint32_t timeout_ms, uint8_t *out_status) {
    absolute_time_t deadline = make_timeout_time_ms(timeout_ms);
    uint8_t st;
    do {
        st = priam_status();
        if (out_status) *out_status = st;
        if (!(st & PRIAM_ST_BUSY)) return true;
        sleep_ms(1);
    } while (!time_reached(deadline));
    return false;
}

static bool wait_status(uint8_t mask_set, uint8_t mask_clear, uint32_t timeout_ms,
                        uint8_t *out_status) {
    absolute_time_t deadline = make_timeout_time_ms(timeout_ms);
    uint8_t st;
    do {
        st = priam_status();
        if (out_status) *out_status = st;
        if (st & PRIAM_ST_DRIVE_FAULT)   return false;  // SAF-5: surface, don't retry
        // SEEK COMPLETE / SEEK FAULT / COMMAND REJECT (bits 1/2/7) are
        // documented invalid while BUSY (§6.3, Table 3-19); judge them only
        // once BUSY clears. Seen live: spin-up flutters REJECT while BUSY,
        // which used to abort `up` spuriously (status 0xD0).
        if (!(st & PRIAM_ST_BUSY)) {
            if (st & PRIAM_ST_COMMAND_REJECT) return false;
            if (st & PRIAM_ST_SEEK_FAULT)     return false;
            if (((st & mask_set) == mask_set) && ((st & mask_clear) == 0)) return true;
        }
        sleep_ms(2);
    } while (!time_reached(deadline));
    return false;
}

bool priam_sequence_up(uint32_t timeout_ms, uint8_t *out_status) {
    priam_command(PRIAM_CMD_SEQUENCE_UP);
    // BUSY clears, then READY + CYLINDER ZERO + SEEK COMPLETE set (§6.4 step 3).
    return wait_status(PRIAM_ST_READY | PRIAM_ST_SEEK_COMPLETE,
                       PRIAM_ST_BUSY, timeout_ms, out_status);
}

bool priam_sequence_down(uint32_t timeout_ms, uint8_t *out_status) {
    priam_command(PRIAM_CMD_SEQUENCE_DOWN);
    return wait_status(0, PRIAM_ST_BUSY, timeout_ms, out_status);
}

bool priam_seek(uint16_t cylinder, uint32_t timeout_ms, uint8_t *out_status) {
    // FUN-6: range-check before issuing; >524 would COMMAND REJECT on native iface.
    if (cylinder > PRIAM_MAX_CYLINDER) {
        if (out_status) *out_status = PRIAM_ST_COMMAND_REJECT;
        return false;
    }
    priam_reg_write(PRIAM_REG_TADDR_UPPER, priam_cyl_upper(cylinder));
    priam_reg_write(PRIAM_REG_TADDR_LOWER, priam_cyl_lower(cylinder));
    priam_command(PRIAM_CMD_SEEK);
    // READY drops during the seek; wait for BUSY clear + SEEK COMPLETE (§6.4 step 4).
    return wait_status(PRIAM_ST_SEEK_COMPLETE, PRIAM_ST_BUSY, timeout_ms, out_status);
}

void priam_format_status(uint8_t st, char *buf, int buflen) {
    snprintf(buf, buflen, "0x%02X [%s%s%s%s%s%s%s%s]", st,
             (st & PRIAM_ST_READY)          ? "READY "   : "",
             (st & PRIAM_ST_SEEK_COMPLETE)  ? "SEEKCMP " : "",
             (st & PRIAM_ST_SEEK_FAULT)     ? "SEEKFLT " : "",
             (st & PRIAM_ST_CYL_ZERO)       ? "CYL0 "    : "",
             (st & PRIAM_ST_BUSY)           ? "BUSY "    : "",
             (st & PRIAM_ST_DRIVE_FAULT)    ? "FAULT "   : "",
             (st & PRIAM_ST_WRITE_PROTECT)  ? "WRPROT "  : "",
             (st & PRIAM_ST_COMMAND_REJECT) ? "REJECT "  : "");
}
