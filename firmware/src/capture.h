// capture.h - PIO+DMA whole-track capture engine (runs on core1, CORE-4).
#ifndef HECUBA_CAPTURE_H
#define HECUBA_CAPTURE_H

#include <stdint.h>
#include <stdbool.h>

// One unformatted track = 13,440 bytes (CAP-1). Packed 1 captured bit/word-bit:
#define BITS_PER_TRACK   (13440u * 8u)                 // 107,520 bits / revolution
#define WORDS_PER_REV    ((BITS_PER_TRACK + 31u) / 32u) // 3,360 32-bit words
// Capture ~12.5% past one revolution so we always span a full INDEX-to-INDEX
// window despite software INDEX-alignment jitter (CAP-3).
#define CAPTURE_WORDS    (WORDS_PER_REV + WORDS_PER_REV / 8u)  // ~3,780 words (~15 KB)

// Inter-core FIFO protocol (capture engine lives on core1).
#define CAP_MSG_READY    0xC0DE0001u   // core1 -> core0 once initialized
#define CAP_REQ_GO       0x60000001u   // core0 -> core1: capture one INDEX-gated track
#define CAP_RESP_FAIL    0xFFFFFFFFu   // core1 -> core0 on failure (e.g. no INDEX)

// ---- core1 side -------------------------------------------------------------
// Entry point for multicore_launch_core1(). Initializes PIO/DMA, signals READY,
// then services capture requests forever.
void capture_core_main(void);

// ---- core0 side -------------------------------------------------------------
// Launch the capture engine (core1) if it isn't already running. Returns true
// if this call started it. Idempotent; called automatically by the first
// capture_run_blocking(), so the console need not start it explicitly.
bool capture_ensure_started(void);

// Trigger one INDEX-gated whole-track capture and block for the result. The
// caller (host CLI) must have already selected the head, asserted -READ GATE,
// and waited the CAP-4 settling window. Returns words captured: the full
// buffer on success, a PARTIAL count if the READ/REF CLOCK stalled mid-track
// (DMA timeout), or 0 if no INDEX arrived / the clock never ran at all.
uint32_t capture_run_blocking(void);

// Access the most recent capture buffer (valid after capture_run_blocking).
const uint32_t *capture_buffer(void);
uint32_t        capture_buffer_capacity_words(void);

// ---- Per-field -READ GATE re-sync ------------------------------------------
// The original controller asserts -READ GATE in the zero gap after each SECTOR
// MARK and never holds it across a write splice. While a capture is armed,
// core1 can pulse the gate off/on at each SECTOR MARK (GATING_MARK) and again
// at a fixed offset after the mark for the ID-to-data gap (GATING_BOTH). The
// PIO keeps sampling throughout. Settings are read by core1 at capture time.
typedef enum { GATING_OFF = 0, GATING_MARK = 1, GATING_BOTH = 2 } gating_mode_t;

// drop_us: gate-off pulse at each SECTOR MARK. data_off_us / data_drop_us: start
// (after the mark) and width of the second pulse that spans the data-field
// write splice (GATING_BOTH). Defaults 3 / 37 / 11 us, see capture.c.
void          capture_set_gating(gating_mode_t mode, uint32_t drop_us,
                                 uint32_t data_off_us, uint32_t data_drop_us);
gating_mode_t capture_gating_mode(void);
uint32_t      capture_gating_drop_us(void);
uint32_t      capture_gating_data_us(void);
uint32_t      capture_gating_data_drop_us(void);
const char   *capture_gating_name(gating_mode_t mode);
// Number of gate off/on pulses issued during the most recent capture window
// (INDEX wait plus track).
uint32_t      capture_last_gate_drops(void);
// Microseconds from the INDEX edge to the first SECTOR MARK of the most recent
// capture (PRIAM t_IS, 44.6 +/- 1.4 us), or -1 if gating was off. Confirms the
// mark timing the second pulse is placed against.
int32_t       capture_last_first_mark_us(void);

#endif // HECUBA_CAPTURE_H
