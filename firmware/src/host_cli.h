// host_cli.h - USB-CDC diagnostic console (HST-3): register-level access plus
// high-level bring-up commands. Runs on core0.
#ifndef HECUBA_HOST_CLI_H
#define HECUBA_HOST_CLI_H

void host_cli_init(void);
void host_cli_task(void);   // call repeatedly from the core0 main loop (non-blocking input)

#endif // HECUBA_HOST_CLI_H
