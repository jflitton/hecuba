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

#endif // HECUBA_CAPTURE_H
