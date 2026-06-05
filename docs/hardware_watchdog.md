# Hardware Watchdog

The WM1303 gateway uses a **two-layer watchdog** so that the device recovers
automatically from both application hangs and full operating-system freezes,
without requiring a manual power cycle.

| Layer | Scope | Mechanism | Recovers from |
|-------|-------|-----------|---------------|
| **A — OS watchdog** | Whole system | BCM2835 hardware timer (`/dev/watchdog0`) fed by systemd via `RuntimeWatchdogSec` | A complete OS freeze (kernel lock-up, total hang) → the board reboots automatically |
| **B — Service watchdog** | `pymc-repeater` service | `systemd` `Type=notify` + `WatchdogSec` fed by the backend with `sd_notify WATCHDOG=1` | A hung application/backend that is still "running" but no longer working → systemd restarts the service |

These two layers are complementary: layer B catches a stuck application quickly
(restart only), while layer A is the last line of defence if the entire OS stops
responding (full reboot).

---

## Layer A — OS hardware watchdog

The Raspberry Pi exposes the Broadcom **BCM2835 hardware watchdog** as
`/dev/watchdog0`. When `RuntimeWatchdogSec` is set, `systemd` (PID 1) keeps
petting this hardware timer at roughly half the configured interval. If the OS
freezes and `systemd` can no longer pet the timer, the hardware forces a reboot.

**Configuration** (applied by `install.sh` / `upgrade.sh`):

- The `bcm2835_wdt` module is registered in `/etc/modules-load.d/bcm2835_wdt.conf`.
  On most current Pi OS kernels the watchdog is built into the kernel, so
  `/dev/watchdog0` is already present without an explicit module.
- `RuntimeWatchdogSec=60s` is set in `/etc/systemd/system.conf`.

### Why 60 s and not less?

The BCM2835 hardware watchdog enforces a **fixed maximum timeout of 60 seconds**
and does **not** support shorter custom values. This can be confirmed with
`wdctl`, which reports `Timeout: 60 seconds` and the `SETTIMEOUT` capability as
`0` (the driver does not allow setting the timeout in seconds). Any lower
`RuntimeWatchdogSec` value (for example `15s`) is silently clamped to 60 s by the
kernel. We therefore configure `60s` so the configuration matches the real
hardware behaviour.

**Effective behaviour:** on a full OS freeze the board reboots automatically
within about 60 seconds.

### Verifying layer A

```bash
# Effective watchdog interval as seen by systemd
systemctl show -p RuntimeWatchdogUSec

# Hardware timer status (timeout, time left, keep-alive)
wdctl
```

A healthy system shows the BCM2835 device, `Timeout: 60 seconds`, a `Timeleft`
value that keeps being refreshed, and `KEEPALIVEPING` active.

---

## Layer B — Service watchdog

The `pymc-repeater.service` unit runs as `Type=notify` with `WatchdogSec=60`.
The backend's existing RX-watchdog loop sends a periodic keep-alive
(`sd_notify WATCHDOG=1`, roughly every 5 seconds) and a one-time `READY=1` once
the service has finished starting. If `systemd` does not receive a keep-alive
within `WatchdogSec`, it considers the service hung and restarts it.

Relevant unit settings:

```ini
Type=notify
NotifyAccess=main
WatchdogSec=60
TimeoutStartSec=120   # allow slow concentrator/HAL startup before READY=1
Restart=always        # restart on failure and on watchdog timeout
```

### sd_notify implementation (no extra build dependency)

The keep-alive helper prefers the `python3-systemd` module when available, but
falls back to a small pure-Python implementation that writes directly to the
`$NOTIFY_SOCKET` UNIX datagram socket provided by systemd. This means the
feature works even when `python3-systemd` is not importable inside the virtual
environment, and it is a safe no-op when the process is not started by systemd
(for example during manual/dev runs).

### Verifying layer B

```bash
# Service must be active, of Type=notify, with a watchdog interval set
systemctl show pymc-repeater.service -p Type -p WatchdogUSec -p NRestarts -p SubState

# Confirm READY=1 was sent at startup
journalctl -u pymc-repeater.service -b | grep "sd_notify READY=1 sent"
```

A healthy service shows `Type=notify`, `WatchdogUSec=1min`, `SubState=running`,
and a `sd_notify READY=1 sent (systemd watchdog active)` log line shortly after
start. `NRestarts` should stay `0` during normal operation.

---

## Startup ordering note

Because the unit is `Type=notify`, systemd only marks the service as *started*
once the backend sends `READY=1`. The backend sends this from its RX-watchdog
thread, which starts at the end of backend initialisation (after the
concentrator/HAL is up). `TimeoutStartSec=120` gives the hardware enough time to
initialise so a slow start does not cause a false-positive restart.

## Summary of recovery behaviour

| Failure | Detected by | Action | Approx. time |
|---------|-------------|--------|--------------|
| Backend/application hang | Layer B (no `WATCHDOG=1`) | Service restart | up to ~60 s |
| Full OS freeze | Layer A (systemd stops petting `/dev/watchdog`) | Hardware reboot of the Pi | up to ~60 s |
