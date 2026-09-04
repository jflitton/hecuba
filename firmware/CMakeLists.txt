# Hecuba acquisition-controller firmware - Raspberry Pi Pico 2 W (RP2350)
#
# Command line (SDK 2.1+ required for RP2350):
#   export PICO_SDK_PATH=/path/to/pico-sdk
#   cmake -B build -G Ninja && cmake --build build
# Output: build/hecuba.uf2  (drag-drop while holding BOOTSEL, or `picotool load`).
#
# In VS Code, the official "Raspberry Pi Pico" extension (which bundles the SDK,
# CMake, Ninja, ARM GCC, picotool and OpenOCD) can build this too, but it only
# handles single-folder workspaces: open `firmware/` as the project root, not
# the repo root. See README.md.

cmake_minimum_required(VERSION 3.13)

# Board selection MUST precede the SDK import. pico2_w => RP2350 + CYW43.
set(PICO_BOARD pico2_w CACHE STRING "Target board")

# Pull in the Pico SDK (from PICO_SDK_PATH or the extension's managed copy).
include(pico_sdk_import.cmake)

project(hecuba C CXX ASM)
set(CMAKE_C_STANDARD 11)
set(CMAKE_CXX_STANDARD 17)

pico_sdk_init()

add_executable(hecuba
    src/main.c
    src/priam_bus.c
    src/capture.c
    src/host_cli.c
)

target_include_directories(hecuba PRIVATE
    ${CMAKE_CURRENT_LIST_DIR}/include
    ${CMAKE_CURRENT_LIST_DIR}/src
)

# Assemble the capture PIO program -> generated header capture.pio.h
pico_generate_pio_header(hecuba ${CMAKE_CURRENT_LIST_DIR}/src/capture.pio)

target_link_libraries(hecuba
    pico_stdlib          # GPIO, timers, clocks, busy-wait
    pico_multicore       # core1 runs the capture engine (CORE-4)
    hardware_pio         # clocked READ DATA sampler (CORE-2)
    hardware_dma         # whole-track DMA into SRAM (CORE-3)
    hardware_clocks      # sys clock for ns-accurate bus timing (CAP-5)
    pico_cyw43_arch_none # onboard LED only (it's on the CYW43); no networking
)

# Diagnostics over USB-CDC; UART stdio off (no spare GPIO for a debug UART;
# GP0/GP1 are DBUS0/DBUS1). See README "Debugging".
pico_enable_stdio_usb(hecuba 1)
pico_enable_stdio_uart(hecuba 0)

# .uf2 / .elf / .bin / map + disassembly
pico_add_extra_outputs(hecuba)
