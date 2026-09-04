// main.c - Hecuba acquisition controller entry point.
//
// Core split (CORE-4):
//   * core1: PIO+DMA whole-track capture engine (capture.c), launched lazily
//             on the first `capture` so the console never waits on it
//   * core0: register-bus protocol + USB-CDC host console (this file)
//
// SAFETY (SAF-2): there is no firmware path that asserts -WRITE GATE or drives
// the WRITE DATA/CLOCK lines. -WRITE GATE is held deasserted in hardware and
// those nets are not even wired to the core.

#include <stdio.h>
#include "pico/stdlib.h"
#include "pico/stdio_usb.h"
#include "pico/cyw43_arch.h"

#include "priam_bus.h"
#include "host_cli.h"

// Onboard-LED heartbeat: 1 Hz = firmware alive. The LED sits on the CYW43
// radio (no RP2350 GPIO), hence cyw43_arch. It freezes during a capture
// (core0 blocks on core1); that stall is itself a useful bench signal.
#define HEARTBEAT_HALF_PERIOD_MS 500u

int main(void) {
    // Park the drive-facing lines before anything else. From power-on until
    // this call the RP2350 pad pull-downs hold U2's inputs low, i.e. the
    // active-low strobes sit asserted at the drive, so keep that window minimal
    // (and see README: power the Pico before the drive rails).
    priam_bus_init();

    stdio_init_all();          // USB-CDC console (CMake: stdio_usb on, uart off)

    bool led_ok = (cyw43_arch_init() == 0);   // LED lives on the CYW43

    // The capture engine (core1) is started lazily on the first `capture`
    // command (see capture_run_blocking), so the console + register bus come up
    // unconditionally; reading Status never depends on PIO/DMA bring-up.
    host_cli_init();

    bool connected = false;
    bool led_on = false;
    absolute_time_t next_beat = get_absolute_time();

    for (;;) {
        // Print the banner on each (re)connect rather than at boot: bytes
        // written before a terminal attaches are dropped by the USB stack.
        bool now_connected = stdio_usb_connected();
        if (now_connected && !connected) {
            sleep_ms(25);      // let the host finish opening the port
            printf("\n=== Hecuba PRIAM acquisition controller (RP2350 / Pico 2 W) ===\n");
            printf("read-only: no write path in firmware. type 'help'.\n");
            if (!led_ok) printf("(cyw43_arch_init failed: no LED heartbeat)\n");
            printf("> ");
        }
        connected = now_connected;

        if (led_ok && time_reached(next_beat)) {
            led_on = !led_on;
            cyw43_arch_gpio_put(CYW43_WL_GPIO_LED_PIN, led_on);
            next_beat = make_timeout_time_ms(HEARTBEAT_HALF_PERIOD_MS);
        }

        host_cli_task();
        tight_loop_contents();
    }
}
