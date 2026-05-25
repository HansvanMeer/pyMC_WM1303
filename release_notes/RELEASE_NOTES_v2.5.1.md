# Release Notes — v2.5.1

**Release date:** 2026-05-25  
**Type:** Patch release — portability, configurability & documentation  
**Upgrade path:** `curl -sSL https://raw.githubusercontent.com/HansvanMeer/pyMC_WM1303/main/bootstrap.sh | sudo bash`

---

## Summary

v2.5.1 removes all hardcoded `/home/pi` username and path assumptions, making WM1303 installable on any Linux user account and any Raspberry Pi-based distribution (Armbian, DietPi, Ubuntu, etc.). SPI device paths are now configurable through the Adv. Config UI, and the install/upgrade scripts gracefully handle boards where the concentrator module is not immediately detected. A new SPI Troubleshooting Guide and in-UI help text for both GPIO and SPI configuration make it easier to set up non-SenseCAP boards.

Triggered by [GitHub Issue #9](https://github.com/HansvanMeer/pyMC_WM1303/issues/9) — a Pisces P100 user on Armbian who could not install because the scripts assumed user `pi`.

---

## Changes

### A. Dynamic User Detection — No More Hardcoded `pi` User

**Shell scripts** (`install.sh`, `upgrade.sh`, `bootstrap.sh`):
- New `detect_user()` function with 5-step priority:
  1. `--user=<name>` CLI argument
  2. `SUDO_USER` environment variable
  3. Common default users scan (`pi`, `orangepi`, `radxa`, `rock`, `dietpi`)
  4. First user with UID ≥ 1000 and a valid home directory
  5. Fail with clear instructions if no suitable user found
- Home directory resolved via `getent passwd` (primary) with `eval echo ~` fallback
- Triple validation: not empty, not literal `~user`, directory must exist
- All paths (`PKTFWD_DIR`, `HAL_DIR`, `BACKUP_DIR`) derived from detected home
- Users can override with `--user=<name>`:
  ```bash
  sudo bash install.sh --user=youri
  sudo bash upgrade.sh --user=youri
  ```

**Python code** (`wm1303_api.py`, `wm1303_backend.py`, `debug_collector.py`):
- New `_detect_pktfwd_dir()` / `_detect_user_home()` helpers with 4-step priority:
  1. `config.yaml` → `wm1303.pktfwd_dir`
  2. systemd service `User=` → home directory
  3. Scan all users with UID ≥ 1000 for existing `wm1303_pf/` directory
  4. Fallback to `/home/pi/wm1303_pf`
- Module-level `_PKTFWD_DIR` / `_HAL_DIR` variables replace ~25 hardcoded path references

**Config templates:**
- `config.yaml.template`: `pktfwd_dir: __PKTFWD_DIR__` placeholder
- `pymc-repeater.service`: `User=__PI_USER__`, `Group=__PI_USER__`, `ReadWritePaths=... __PI_HOME__ ...` placeholders
- Placeholders substituted by `sed` during install/upgrade

**Documentation:**
- All `/home/pi` references in `docs/*.md`, `release_notes/*.md`, and `TODO.md` replaced with `~/` notation

**Total: ~95 hardcoded references removed across 16 files.**

---

### B. SPI Device Configuration — Configurable via Adv. Config UI

SPI device paths (`/dev/spidev0.0` for SX1302, `/dev/spidev0.1` for SX1261) are no longer hardcoded:

- New `spi_devices` section in `wm1303_ui.json` (source of truth)
- New "SPI Device Configuration" panel in Adv. Config tab with editable fields
- Collapsible help text explaining how to find the correct SPI device paths
- When SPI paths are changed via the UI, `global_conf.json` / `bridge_conf.json` are automatically updated
- `wm1303_api.py` reads SPI paths from `wm1303_ui.json` instead of using hardcoded fallbacks

This allows users with non-standard SPI bus configurations (e.g., different concentrator HATs) to adjust the paths without manual config file editing.

---

### C. Concentrator Detection Check — Graceful Install/Upgrade Completion

The install and upgrade scripts now include a post-startup concentrator detection check:

- After service start, the scripts check journal logs for successful startup indicators (`lora_pkt_fwd started`, `pktfwd ready`, `backend started`)
- If the concentrator is detected: success message displayed
- If the concentrator is NOT detected: informational warning block with:
  - Possible causes (GPIO pin mismatch, SPI device path, power supply)
  - Next steps (Adv. Config → SPI / GPIO settings)
  - Link to the SPI Troubleshooting Guide
- **The installation/upgrade completes successfully regardless** — no abort on hardware detection failure
- This ensures users can finish installation first and configure hardware settings afterward via the web UI

---

### D. SPI Troubleshooting Guide

New file: `docs/spi-troubleshooting.md` (244 lines)

- Common SPI errors and their causes (including the `Failed to set SX1250_0 in STANDBY_RC mode` error)
- How to find SPI device numbers (`/dev/spidev*` discovery)
- GPIO pin configuration: BCM vs sysfs numbering, base offset explanation
- 4 methods to determine the correct GPIO base offset
- SPI bus speed explanation (2 MHz default)
- Board compatibility table
- Quick diagnostic one-liner script

---

### E. GPIO Help Text in Adv. Config UI

New collapsible info block in the GPIO Pin Configuration section of the Adv. Config tab:

- How to find GPIO base offset (`cat /sys/class/gpio/gpiochip*/base`)
- BCM pin numbers are hardware-dependent, not OS-dependent
- Default pin values for common boards (SenseCAP M1, Pisces P100)
- Sysfs number calculation explanation
- Direct link to the SPI Troubleshooting Guide on GitHub

---

## Upgrade Notes

- **Existing installations** (user `pi`): No action needed. The detection logic finds `pi` automatically and all paths remain unchanged.
- **Non-pi users**: The upgrade script will detect the correct user from the existing service file and update all paths accordingly.
- **New installations on non-pi systems**: Use `--user=<name>` if automatic detection doesn't find the right user.
- **SPI device paths**: Existing installations retain the default `/dev/spidev0.0` and `/dev/spidev0.1`. Adjust via Adv. Config if needed.

> ⚠️ After upgrading, perform a hard refresh in your browser: **Ctrl+Shift+R** on `http://<pi-ip>:8000/wm1303.html`

---

## Files Changed

| Category | Files |
|----------|-------|
| Shell scripts | `install.sh`, `upgrade.sh`, `bootstrap.sh` |
| Python code | `wm1303_api.py`, `wm1303_backend.py`, `debug_collector.py` |
| Config templates | `config.yaml.template`, `pymc-repeater.service` |
| Config defaults | `config/wm1303_ui.json` (added `spi_devices` section) |
| UI | `wm1303.html` (SPI Device Configuration panel, GPIO help block) |
| Documentation | `docs/spi-troubleshooting.md` (new), `docs/architecture.md`, `docs/configuration.md`, `docs/installation.md`, `release_notes/v2.0.1.md`, `release_notes/v2.0.5.md`, `release_notes/v2.4.0.md`, `TODO.md` |
