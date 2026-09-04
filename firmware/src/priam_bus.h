// priam_bus.h - PRIAM register-bus master + high-level commands (FUN-1..7)
#ifndef HECUBA_PRIAM_BUS_H
#define HECUBA_PRIAM_BUS_H

#include <stdint.h>
#include <stdbool.h>
#include "priam_proto.h"

// One-time GPIO setup: control outputs parked deasserted, -RESET held high,
// DBUS released to input, status pins as inputs. Call once at startup.
void priam_bus_init(void);

// ---- Raw register access (CAP-5 bus AC timing enforced) ---------------------
// Writes obey: address stable >=60 ns before -WR, -WR pulse >=100 ns,
// data setup >=60 ns, >=200 ns recovery. Reads: -RD pulse >=100 ns, sample
// after data-valid (<=60 ns after -RD).
void    priam_reg_write(priam_reg_t reg, uint8_t value);
uint8_t priam_reg_read(priam_reg_t reg);

// ---- Convenience accessors --------------------------------------------------
static inline uint8_t priam_status(void) { return priam_reg_read(PRIAM_REG_CMD_OR_STATUS); }
void priam_command(priam_cmd_t cmd);   // write a code to the Command Register

// ---- Head select (FUN-3, §6.5). head 0..4; >4 falls back to 0. --------------
void priam_select_head(uint8_t head);

// ---- -READ GATE (FUN-5). Caller honors post-gate settling (CAP-4). ----------
void priam_read_gate(bool asserted);

// ---- High-level operations (block, polling Status) --------------------------
// Each returns true on success; on COMMAND REJECT / DRIVE FAULT / timeout it
// returns false and leaves the latest status in *out_status (if non-NULL).
// SAF-5: callers must NOT blindly retry on fault.
bool priam_sequence_up(uint32_t timeout_ms, uint8_t *out_status);   // ~30 s typical
bool priam_sequence_down(uint32_t timeout_ms, uint8_t *out_status);
bool priam_seek(uint16_t cylinder, uint32_t timeout_ms, uint8_t *out_status); // FUN-6 range-checks
bool priam_wait_not_busy(uint32_t timeout_ms, uint8_t *out_status);

// Decode a status byte into a human string (for the host CLI / logging).
void priam_format_status(uint8_t status, char *buf, int buflen);

#endif // HECUBA_PRIAM_BUS_H
