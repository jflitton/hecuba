// priam_proto.h - PRIAM register/command protocol constants (DISKOS 3450)
//
// Source of truth: PRIAM OEM/Service Manual, doc 308000-0, §6 (Tables
// 3-16..3-20). The standard PRIAM interface is a tiny demultiplexed bus:
// DBUS0-7 + AD0/AD1 + -RD/-WR, gated by -DRIVE SELECT, with no handshake beyond
// strobe timing. We are the sole bus master.

#ifndef HECUBA_PRIAM_PROTO_H
#define HECUBA_PRIAM_PROTO_H

#include <stdint.h>

// ---- Register addresses (AD1,AD0). Manual §6.1, Table 3-16 -----------------
// Selected by AD1/AD0; whether it's the write- or read-side register is chosen
// by which strobe (-WR vs -RD) is asserted.
typedef enum {
    PRIAM_REG_CMD_OR_STATUS   = 0u,  // AD1=0 AD0=0 : Command (WR) / Status (RD)
    PRIAM_REG_TADDR_UPPER     = 1u,  // AD1=0 AD0=1 : Target Addr Upper (WR) / Current Upper (RD)
    PRIAM_REG_TADDR_LOWER     = 2u,  // AD1=1 AD0=0 : Target Addr Lower (WR) / Current Lower (RD)
    // AD1=1 AD0=1 is unused.
} priam_reg_t;

// ---- Command codes written to the Command Register. §6.2, Table 3-17 --------
typedef enum {
    PRIAM_CMD_SEQUENCE_UP   = 0x01,  // spin up, restore to cyl 0, set READY (~30 s)
    PRIAM_CMD_SEQUENCE_DOWN = 0x02,  // park heads, brake spindle
    PRIAM_CMD_RESTORE       = 0x03,  // recalibrate carriage to cyl 0
    PRIAM_CMD_SEEK          = 0x04,  // seek to cylinder in Target Address regs
    PRIAM_CMD_FAULT_RESET   = 0x05,  // clear SEEK FAULT / DRIVE FAULT
    PRIAM_CMD_READ_DRIVE_ID = 0x10,  // load drive ID into Current Address lower
    PRIAM_CMD_READ_BYTES_PER_SECTOR = 0x11, // load bytes/sector into Current Address
} priam_cmd_t;

// ---- Status Register bits. §6.3, Table 3-19 ---------------------------------
// Note bits 1 (SEEK COMPLETE) and 2 (SEEK fault) are invalid while BUSY is set.
#define PRIAM_ST_READY         (1u << 0)  // up to speed, servo locked
#define PRIAM_ST_SEEK_COMPLETE (1u << 1)  // seek finished (invalid while BUSY)
#define PRIAM_ST_SEEK_FAULT    (1u << 2)  // fault during a seek (invalid while BUSY)
#define PRIAM_ST_CYL_ZERO      (1u << 3)  // carriage at cylinder 0
#define PRIAM_ST_BUSY          (1u << 4)  // executing a command
#define PRIAM_ST_DRIVE_FAULT   (1u << 5)  // fault during write / unsafe condition
#define PRIAM_ST_WRITE_PROTECT (1u << 6)  // selected head write-protected
#define PRIAM_ST_COMMAND_REJECT (1u << 7) // wrote register while !READY, or invalid cmd

// ---- Geometry / addressing. §0, §6 ------------------------------------------
#define PRIAM_NUM_CYLINDERS    525u    // valid cylinders 0..524
#define PRIAM_MAX_CYLINDER     524u    // target > 524 -> COMMAND REJECT (bit 7)
#define PRIAM_NUM_HEADS        5u      // data heads 0..4
#define PRIAM_DRIVE_ID_3450    0x04u   // expected Read Drive ID result (verify at bench)

// Cylinder is up to 11 bits split across Target Upper/Lower (C10..C0; C10=MSB).
// Table 3-20: the exact bit packing across the two bytes is to be confirmed at
// the bench (the manual's split for the 3450). For now: lower 8 bits in LOWER,
// remaining high bits in UPPER. Verified by reading Current Address back.
static inline uint8_t priam_cyl_upper(uint16_t cyl) { return (uint8_t)((cyl >> 8) & 0x07u); }
static inline uint8_t priam_cyl_lower(uint16_t cyl) { return (uint8_t)(cyl & 0xFFu); }

#endif // HECUBA_PRIAM_PROTO_H
