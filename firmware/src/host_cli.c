// host_cli.c - line-oriented diagnostic console over USB-CDC.
//
// Commands (type `help`):
//   help                      this list
//   status                    read + decode the Status register
//   reg r <ad>                read register ad (0,1,2)            [HST-3 raw access]
//   reg w <ad> <val>          write hex val to register ad        [HST-3 raw access]
//   up [secs]                 Sequence Up (default 35 s)
//   down                      Sequence Down (park)
//   seek <cyl>                seek to cylinder (0..524)
//   head <0-4>                select data head
//   gate on|off               assert / deassert -READ GATE
//   id                        Read Drive ID
//   capture <cyl> <head>      seek+select+gate+settle, then capture one track
//   capraw [head]             capture with NO seek (bench/loopback; optional head)
//   dump [n]                  hexdump first n words of the last capture (default 16)
//   dumpb64 [n]               base64 dump of the capture buffer (host offload)
//   pins                      status/clock/data GPIO levels + 100 ms edge counts
//   stair                     FUN-7 seek staircase 0..524 with readback verify
//   reset                     pulse -RESET 1 ms (drive-fault #7 if sequenced up!)
//
// This is a bring-up tool, not the final host protocol; HST-1 bulk track
// offload is a later addition.

#include "host_cli.h"
#include "priam_bus.h"
#include "capture.h"
#include "board_pins.h"

#include <stdio.h>
#include <string.h>
#include <stdlib.h>
#include "pico/stdlib.h"

// CAP-4 settling: READ DATA valid >=9 us after gate, clock ~6 us, head-change
// -to-readable ~24 us. Wait comfortably past the worst case before capturing.
#define CAP_SETTLE_US   60u

#define LINE_MAX 96
static char s_line[LINE_MAX];
static int  s_len;

static void print_help(void) {
    printf(
      "commands:\n"
      "  status                 read+decode Status\n"
      "  reg r <ad>             read register (ad 0,1,2)\n"
      "  reg w <ad> <hexval>    write register\n"
      "  up [secs]              Sequence Up (default 35)\n"
      "  down                   Sequence Down (park)\n"
      "  seek <cyl>             seek 0..524\n"
      "  head <0-4>             select head\n"
      "  gate on|off            -READ GATE\n"
      "  id                     Read Drive ID\n"
      "  capture <cyl> <head>   capture one INDEX-gated track\n"
      "  capraw [head]          capture without seeking (bench)\n"
      "  dump [n]               hexdump last capture (n words)\n"
      "  dumpb64 [n]            base64 dump of last capture (default: all)\n"
      "  pins                   status/clock/data levels + edge counts\n"
      "  stair                  seek staircase 0..524 w/ readback verify\n"
      "  reset                  pulse -RESET (faults a sequenced-up drive!)\n");
}

static void cmd_status(void) {
    char buf[80];
    priam_format_status(priam_status(), buf, sizeof buf);
    printf("status %s\n", buf);
}

static void report(const char *what, bool ok, uint8_t st) {
    char buf[80];
    priam_format_status(st, buf, sizeof buf);
    printf("%s: %s  status %s\n", what, ok ? "OK" : "FAIL", buf);
}

static void report_capture(uint32_t words) {
    uint32_t cap = capture_buffer_capacity_words();
    if (words == cap) {
        printf("OK: %lu words (%lu bytes)\n",
               (unsigned long)words, (unsigned long)words * 4u);
    } else if (words > 0) {
        printf("PARTIAL: %lu/%lu words; READ/REF CLOCK stalled mid-track? (try `pins`)\n",
               (unsigned long)words, (unsigned long)cap);
    } else {
        printf("FAIL: no INDEX edge, or clock never ran (try `pins`)\n");
    }
}

static void cmd_capture(uint16_t cyl, uint8_t head) {
    if (capture_ensure_started()) printf("(capture engine started on core1)\n");
    uint8_t st = 0;
    if (!priam_seek(cyl, 5000, &st))      { report("seek", false, st); return; }
    priam_select_head(head);
    priam_read_gate(true);
    sleep_us(CAP_SETTLE_US);              // CAP-4 settling window
    uint32_t words = capture_run_blocking();
    priam_read_gate(false);
    printf("capture cyl %u head %u: ", cyl, head);
    report_capture(words);
}

// Capture with no seek and no status checks: the raw gate+PIO+DMA path, for
// bench loopback and clock experiments. Head select only if given.
static void cmd_capraw(int head) {
    if (capture_ensure_started()) printf("(capture engine started on core1)\n");
    if (head >= 0) priam_select_head((uint8_t)head);
    priam_read_gate(true);
    sleep_us(CAP_SETTLE_US);
    uint32_t words = capture_run_blocking();
    priam_read_gate(false);
    printf("capraw: ");
    report_capture(words);
}

// Live GPIO view: instantaneous levels plus rising edges counted over 100 ms.
// INDEX/READY/SECTOR are active-high at the core (U3 inverts). Spinning drive:
// expect INDEX ~6 edges, SECTOR ~138 (23/rev). CLK/DATA poll far below the
// 6.4 MHz line rate, so their counts are aliased; nonzero just means alive.
static void cmd_pins(void) {
    uint32_t prev = gpio_get_all();
    uint32_t idx = 0, sec = 0, clk = 0, dat = 0;
    absolute_time_t end = make_timeout_time_ms(100);
    while (!time_reached(end)) {
        uint32_t now  = gpio_get_all();
        uint32_t rise = now & ~prev;
        if (rise & (1u << PIN_INDEX))        idx++;
        if (rise & (1u << PIN_READ_REF_CLK)) clk++;
        if (rise & (1u << PIN_READ_DATA))    dat++;
        if (rise & (1u << PIN_SECTOR_MARK))  sec++;
        prev = now;
    }
    uint32_t lv = gpio_get_all();
    printf("levels : INDEX=%u READY=%u SECTOR=%u CLK=%u DATA=%u\n",
           (unsigned)((lv >> PIN_INDEX) & 1u),        (unsigned)((lv >> PIN_READY) & 1u),
           (unsigned)((lv >> PIN_SECTOR_MARK) & 1u),  (unsigned)((lv >> PIN_READ_REF_CLK) & 1u),
           (unsigned)((lv >> PIN_READ_DATA) & 1u));
    printf("edges/100ms: INDEX=%lu SECTOR=%lu (expect 6 / 138 spinning) "
           "CLK~%lu DATA~%lu (aliased; nonzero = alive)\n",
           (unsigned long)idx, (unsigned long)sec,
           (unsigned long)clk, (unsigned long)dat);
}

// FUN-7 staircase: 0 -> 1 -> 0 -> 2 -> ... -> 524 -> 0,
// verifying SEEK COMPLETE and the Current Address readback at every step.
// Any key aborts between steps.
static bool seek_verified(uint16_t cyl) {
    uint8_t st = 0;
    if (!priam_seek(cyl, 5000, &st)) {
        char buf[80];
        priam_format_status(st, buf, sizeof buf);
        printf("\nseek %u FAILED  status %s\n", cyl, buf);
        return false;
    }
    uint8_t up = priam_reg_read(PRIAM_REG_TADDR_UPPER);
    uint8_t lo = priam_reg_read(PRIAM_REG_TADDR_LOWER);
    if (up != priam_cyl_upper(cyl) || lo != priam_cyl_lower(cyl)) {
        printf("\nseek %u: Current Address mismatch: read %02x/%02x, expect %02x/%02x\n",
               cyl, up, lo, priam_cyl_upper(cyl), priam_cyl_lower(cyl));
        return false;
    }
    return true;
}

static void cmd_stair(void) {
    printf("staircase 0->524 (~1050 seeks, a couple of minutes; any key aborts)\n");
    for (uint16_t c = 1; c <= PRIAM_MAX_CYLINDER; c++) {
        if (getchar_timeout_us(0) != PICO_ERROR_TIMEOUT) {
            printf("aborted before cyl %u\n", c);
            return;
        }
        if (!seek_verified(0) || !seek_verified(c)) {
            printf("staircase STOPPED at cyl %u (SAF-5: no blind retry)\n", c);
            return;
        }
        if ((c % 25u) == 0u) printf("  cyl %u OK\n", c);
    }
    if (seek_verified(0))
        printf("staircase complete: cylinders 0..524 all verified\n");
}

// Pulse -RESET. Escape hatch for a wedged register bus (see U5 on the
// schematic); NOT routine. On a sequenced-up drive this itself latches
// DRIVE FAULT (fault condition #7); clear with Fault Reset (reg w 0 05).
static void cmd_reset(void) {
    gpio_put(PIN_RESET_N, 0);
    sleep_ms(1);
    gpio_put(PIN_RESET_N, 1);
    printf("-RESET pulsed (1 ms). If sequenced up, expect DRIVE FAULT: "
           "check `status`, clear with `reg w 0 05`.\n");
}

// Base64 dump for host-side analysis (edge-repeatability diffs, decode
// experiments). Bytes are little-endian per 32-bit word; bit 0 of word 0 is
// the first-sampled bit (capture.pio shifts right, autopush at 32).
static void cmd_dumpb64(uint32_t nwords) {
    static const char T[] =
        "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    uint32_t cap = capture_buffer_capacity_words();
    if (nwords > cap) nwords = cap;
    const uint8_t *p = (const uint8_t *)capture_buffer();
    uint32_t nbytes = nwords * 4u;
    printf("-----BEGIN CAPTURE b64 (%lu words, LE, first bit = LSB)-----\n",
           (unsigned long)nwords);
    char line[80];
    int li = 0;
    for (uint32_t i = 0; i < nbytes; i += 3u) {
        uint32_t v = (uint32_t)p[i] << 16;
        if (i + 1 < nbytes) v |= (uint32_t)p[i + 1] << 8;
        if (i + 2 < nbytes) v |= (uint32_t)p[i + 2];
        line[li++] = T[(v >> 18) & 63u];
        line[li++] = T[(v >> 12) & 63u];
        line[li++] = (i + 1 < nbytes) ? T[(v >> 6) & 63u] : '=';
        line[li++] = (i + 2 < nbytes) ? T[v & 63u] : '=';
        if (li >= 76) { line[li] = '\0'; printf("%s\n", line); li = 0; }
    }
    if (li) { line[li] = '\0'; printf("%s\n", line); }
    printf("-----END CAPTURE-----\n");
}

static void cmd_dump(uint32_t n) {
    const uint32_t *buf = capture_buffer();
    uint32_t cap = capture_buffer_capacity_words();
    if (n > cap) n = cap;
    for (uint32_t i = 0; i < n; i++) {
        printf("%08lx ", (unsigned long)buf[i]);
        if ((i & 7u) == 7u) printf("\n");
    }
    if (n & 7u) printf("\n");
}

static void dispatch(char *line) {
    char *cmd = strtok(line, " \t");
    if (!cmd) return;

    if (!strcmp(cmd, "help")) {
        print_help();
    } else if (!strcmp(cmd, "status")) {
        cmd_status();
    } else if (!strcmp(cmd, "reg")) {
        char *rw = strtok(NULL, " \t");
        char *ad = strtok(NULL, " \t");
        if (!rw || !ad) { printf("usage: reg r|w <ad> [hexval]\n"); return; }
        int reg = atoi(ad);
        if (reg < 0 || reg > 2) { printf("ad must be 0,1,2\n"); return; }
        if (rw[0] == 'r') {
            printf("reg[%d] = 0x%02x\n", reg, priam_reg_read((priam_reg_t)reg));
        } else if (rw[0] == 'w') {
            char *val = strtok(NULL, " \t");
            if (!val) { printf("usage: reg w <ad> <hexval>\n"); return; }
            uint8_t v = (uint8_t)strtoul(val, NULL, 16);
            priam_reg_write((priam_reg_t)reg, v);
            printf("reg[%d] <= 0x%02x\n", reg, v);
        } else {
            printf("usage: reg r|w <ad> [hexval]\n");
        }
    } else if (!strcmp(cmd, "up")) {
        char *secs = strtok(NULL, " \t");
        uint32_t to = secs ? (uint32_t)atoi(secs) * 1000u : 35000u;
        uint8_t st = 0; bool ok = priam_sequence_up(to, &st);
        report("sequence up", ok, st);
    } else if (!strcmp(cmd, "down")) {
        // Spindle brake can exceed 10 s; seen live as a spurious FAIL (0x50,
        // still BUSY braking) with the park actually completing fine.
        uint8_t st = 0; bool ok = priam_sequence_down(45000, &st);
        report("sequence down", ok, st);
    } else if (!strcmp(cmd, "seek")) {
        char *c = strtok(NULL, " \t");
        if (!c) { printf("usage: seek <cyl>\n"); return; }
        uint8_t st = 0; bool ok = priam_seek((uint16_t)atoi(c), 5000, &st);
        report("seek", ok, st);
    } else if (!strcmp(cmd, "head")) {
        char *h = strtok(NULL, " \t");
        if (!h) { printf("usage: head <0-4>\n"); return; }
        uint8_t head = (uint8_t)atoi(h);
        priam_select_head(head);
        printf("head <= %u\n", head);
    } else if (!strcmp(cmd, "gate")) {
        char *s = strtok(NULL, " \t");
        bool on = s && !strcmp(s, "on");
        priam_read_gate(on);
        printf("-READ GATE %s\n", on ? "asserted" : "deasserted");
    } else if (!strcmp(cmd, "id")) {
        priam_command(PRIAM_CMD_READ_DRIVE_ID);
        priam_wait_not_busy(1000, NULL);
        printf("drive id = 0x%02x (expect 0x%02x for 3450)\n",
               priam_reg_read(PRIAM_REG_TADDR_LOWER), PRIAM_DRIVE_ID_3450);
    } else if (!strcmp(cmd, "capture")) {
        char *c = strtok(NULL, " \t");
        char *h = strtok(NULL, " \t");
        if (!c || !h) { printf("usage: capture <cyl> <head>\n"); return; }
        cmd_capture((uint16_t)atoi(c), (uint8_t)atoi(h));
    } else if (!strcmp(cmd, "capraw")) {
        char *h = strtok(NULL, " \t");
        cmd_capraw(h ? atoi(h) : -1);
    } else if (!strcmp(cmd, "dump")) {
        char *n = strtok(NULL, " \t");
        cmd_dump(n ? (uint32_t)strtoul(n, NULL, 10) : 16u);
    } else if (!strcmp(cmd, "dumpb64")) {
        char *n = strtok(NULL, " \t");
        cmd_dumpb64(n ? (uint32_t)strtoul(n, NULL, 10)
                      : capture_buffer_capacity_words());
    } else if (!strcmp(cmd, "pins")) {
        cmd_pins();
    } else if (!strcmp(cmd, "stair")) {
        cmd_stair();
    } else if (!strcmp(cmd, "reset")) {
        cmd_reset();
    } else {
        printf("unknown command '%s' (try 'help')\n", cmd);
    }
}

void host_cli_init(void) {
    s_len = 0;
}

void host_cli_task(void) {
    int ch = getchar_timeout_us(0);     // non-blocking
    if (ch == PICO_ERROR_TIMEOUT) return;

    if (ch == '\r' || ch == '\n') {
        putchar('\n');
        s_line[s_len] = '\0';
        if (s_len > 0) dispatch(s_line);
        s_len = 0;
        printf("> ");
    } else if (ch == 0x7f || ch == '\b') {   // backspace
        if (s_len > 0) { s_len--; printf("\b \b"); }
    } else if (s_len < LINE_MAX - 1 && ch >= 0x20) {
        s_line[s_len++] = (char)ch;
        putchar(ch);                          // local echo
    }
}
