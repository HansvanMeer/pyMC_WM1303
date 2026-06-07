# Release Notes — v2.5.4

**Release date:** 2026-06-07  
**Type:** Release — hardware watchdog, HAL GPIO adaptivity, v2.5.3 hotfix rollup, systemd venv visibility fix  
**Upgrade path:** `curl -sSL https://raw.githubusercontent.com/HansvanMeer/pyMC_WM1303/main/bootstrap.sh | sudo bash`

---

## Summary

Adds a two-layer hardware watchdog for unattended uptime, makes the HAL GPIO
reset paths configurable so non-SenseCAP boards work without HAL patching, rolls
up the v2.5.3 hotfix (SX1302 RX restore, LBT control sync, companion-app
visibility, cache-stats diagnostic) into a formal release, and fixes the
`python3-systemd` venv visibility so the watchdog `sd_notify` keep-alive uses
the native binding instead of the pure-Python fallback.

---

## Theme 1 — Hardware Watchdog (NEW feature)

Two-layer watchdog so the device self-recovers from a hung repeater process or a
fully frozen kernel.

| Layer | Mechanism | Timeout |
|---|---|---|
| Service-level | systemd `Type=notify` + `WatchdogSec` + sd_notify keep-alive from the repeater | 60 s |
| OS-level | BCM2835 hardware watchdog via `RuntimeWatchdogSec` in `/etc/systemd/system.conf` | 60 s (hardware-fixed) |

**Why:** A frozen repeater previously required a manual power cycle. The watchdog
restarts the service (service layer) or reboots the board (OS layer) automatically,
improving unattended uptime. The 60 s figure is the BCM2835 hardware maximum.

**Note on the 60 s value:** Initial design used `RuntimeWatchdogSec=15s`, but the
BCM2835 hardware timer has a fixed ~16 s max that systemd rounds; 60 s was chosen
as the documented, reliable value matching `WatchdogSec`.

---

## Theme 2 — HAL GPIO Path Adaptivity (NEW feature)

Makes the SX1261/SX1302 reset GPIO paths configurable instead of hard-coded for
the SenseCAP M1, so other boards with different BCM pin mappings work without
patching the HAL.

**Why:** Hard-coded GPIO paths broke recovery on boards with non-SenseCAP pin
mappings. The packet forwarder pushes resolved paths from `global_conf.json`'s
`gpio_paths` block into the HAL before `lgw_start()`. Empty fields fall back to
the legacy default, so existing installs need no config change.

---

## Theme 3 — v2.5.3 Hotfix (rolled up into v2.5.4)

Critical regression fixes originally documented in
`release_notes/RELEASE_NOTES_v2.5.3.md`. v2.5.3 was prepared but never
published as a standalone release; its full detail document is kept for history
and its changes ship inside v2.5.4.

**Why:** Restores SX1302 RX and UI LBT control that earlier regressions broke;
adds companion-app visibility and a cache-stats diagnostic.

---

## Theme 4 — python3-systemd venv visibility fix (watchdog robustness)

Makes the **native** `systemd` Python binding usable inside the repeater venv so
the service-level watchdog keep-alive (`sd_notify`) uses the native path instead
of relying on the pure-Python fallback.

**Root cause:** `python3-systemd` was already installed via apt and listed in the
script dependencies, **but** the venv (`/opt/pymc_repeater/venv`) is created with
`include-system-site-packages = false`. The apt package lives in
`/usr/lib/python3/dist-packages/` and is therefore invisible inside the venv, so
`import systemd.daemon` failed there. The `_sd_notify()` code has a pure-Python
AF_UNIX fallback to `$NOTIFY_SOCKET`, so the watchdog kept working — but the
native binding was never used.

**Fix:** A new symlink step (mirroring the existing rrdtool symlink) links the
apt `systemd` package directory into the venv `site-packages` and verifies
`import systemd.daemon`. The install and upgrade flows (including the
venv-rebuild path) all run this step, idempotently.

**Why it works:** venv and system Python are both 3.13 aarch64, so the
`cpython-313-aarch64` `.so` extensions are ABI-compatible — a directory symlink
suffices, no rebuild/pip-compile needed. The source dir is resolved dynamically
via the system Python (no hard-coded paths).

**Safety:** The pure-Python fallback in `wm1303_backend.py` remains in place as a
safety net, so this is a robustness/cleanliness improvement, not a behavioural
dependency.

**Verified on pi03 (`pyMC-WM1303-01`):**
- Before: `import systemd.daemon` → `ModuleNotFoundError` in venv.
- After symlink: `import systemd.daemon` → **OK** (native binding active).
- Service restarted → `active (running)`, `Type=notify`, `WatchdogUSec=60s`.
- Stress-test (SIGSTOP → auto-restart after ~60 s, `NRestarts 0→1`) confirmed the watchdog itself works regardless of binding path.
