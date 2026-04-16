# Release Notes — v2.0.0 — Sub-125 kHz Bandwidth Support (62.5 kHz)

**Release date:** 2026-04-16  
**Type:** Major release

---

## Summary

Version 2.0.0 is a **major release** that introduces full support for a 5th receive/transmit channel ("Channel E") using the SX1261 radio on the WM1303 concentrator module. The most significant addition is that **bandwidths smaller than 125 kHz (such as 62.5 kHz) can now be used** — exclusively on Channel E, since the SX1302 IF chains are limited to 125 kHz minimum. This release also includes a comprehensive channel renaming across the entire UI and backend, numerous bug fixes, and significant UI improvements.

---

## New Features

### Channel E — SX1261 LoRa RX/TX Support

The WM1303 module contains an SX1261 radio that was previously used only for spectral scanning and LBT/CAD operations. This release promotes it to a full 5th channel with complete RX and TX capabilities.

- **Full RX/TX support** through the bridge engine, identical to Channels A–D
- **Configurable via UI** — all parameters editable in the WM1303 Manager interface:
  - Frequency (MHz)
  - Bandwidth (kHz)
  - Spreading Factor (SF)
  - Coding Rate (CR)
  - Preamble length
  - TX power (dBm)
  - LBT (Listen Before Talk) enable/threshold
  - CAD (Channel Activity Detection) enable
  - RX Boost mode
- **Single source of truth** — all Channel E settings read dynamically from `wm1303_ui.json`
- **Integrated into all subsystems:**
  - Metrics collection and dashboard charts
  - Noise floor monitoring
  - Status tab with full channel statistics
  - Spectrum tab
  - Packet Activity chart (displayed in orange)
- **LBT/CAD** works identically to Channels A–D via the `GlobalTXScheduler`
- **New module:** `channel_e_bridge.py` — dedicated bridge component for Channel E packet routing

---

## Changes

### Channel Rename

All channel identifiers have been renamed throughout the entire codebase for clarity and consistency:

| Old Name | New Name |
|----------|----------|
| IF0      | Channel A |
| IF1      | Channel B |
| IF2      | Channel C |
| IF3      | Channel D |
| SX1261   | Channel E |

This rename applies to:
- Web UI (Status tab, Configuration tab, Spectrum tab, Packet Activity chart)
- Backend API responses
- Database `channel_id` fields
- Log messages
- Configuration file references

---

## Bug Fixes

### TX Duty Cycle Calculation
- **Fixed:** TX Duty Cycle was calculated as an average across channels instead of a sum. Since Channels A–D share an RF chain (SX1250), the correct calculation is the **sum** of all individual channel duty cycles.

### SPI Contention (TX STATUS 0xC0)
- **Fixed:** The capture thread was causing SPI bus contention with TX operations, resulting in `TX STATUS 0xC0` errors. The capture thread has been disabled to resolve this issue.

### Hardcoded Channel E Parameters
- **Fixed:** Channel E parameters (frequency, SF, BW, CR, preamble, TX power) were previously hardcoded in the backend. They are now read dynamically from `wm1303_ui.json`, making them fully configurable.

### Coding Rate Conversion Crash
- **Fixed:** A type mismatch between string and integer representations of `coding_rate` caused crashes during packet processing. Proper type conversion is now applied.

### UI/API Field Name Mismatches
- **Fixed:** Several field names were inconsistent between the UI frontend and backend API:
  - `enable` vs `enabled`
  - `lbt_rssi_target` vs `lbt_threshold`
  
  These are now consistent throughout.

### Active Channels Count
- **Fixed:** The Active Channels count in the Status tab did not include Channel E. The total now correctly reports 5 active channels when all are enabled.

### Spectrum Tab Chain Count
- **Fixed:** The Spectrum tab incorrectly displayed "10 chains" instead of the actual number of active chains.

### Missing Capture Thread Files
- **Fixed:** HAL compilation failed on fresh installations because `capture_thread.c` and `capture_thread.h` were missing from the overlay. These files (for the CAPTURE_RAM streaming feature) have been added to the overlay and are now correctly copied during installation and upgrades.

### Missing Sudoers Configuration
- **Fixed:** After installation and reboot, the systemd service failed to execute `sudo` commands (GPIO reset, pkt_fwd) because the `pi` user lacked passwordless sudo privileges. The install script now creates `/etc/sudoers.d/010_pi-nopasswd` and adds the `pi` user to hardware groups (spi, i2c, gpio, dialout).

---

## UI Improvements

- **Aligned configuration grids** — IF Channel (A–D) and SX1261 Channel (E) configuration sections now use a consistent grid layout
- **Channel E in Status tab** — full metrics display including RSSI, SNR, packet count, noise floor, and duty cycle
- **Channel E in Packet Activity chart** — shown in orange to distinguish from Channels A–D
- **RX Boost toggle** — renamed from "Boosted" to "RX Boost" and repositioned before the Active toggle for better workflow
- **CAD toggle behavior** — CAD toggle is now correctly disabled when LBT is turned off (consistent with Channels A–D behavior)
- **Frequency display** — frequencies shown in MHz throughout the UI
- **Bandwidth display** — bandwidths shown in kHz throughout the UI

---

## HAL / Firmware Changes

Updated overlay files for the SX1302 HAL (v2.10):

- `loragw_hal.c` / `loragw_hal.h` — Updated HAL initialization and channel management
- `loragw_sx1261.c` / `loragw_sx1261.h` — Extended SX1261 driver for full RX/TX channel support
- `loragw_sx1302.c` / `loragw_sx1302.h` — Updated SX1302 concentrator interface
- `sx1261_defs.h` — Updated register definitions and constants
- `lora_pkt_fwd.c` — Packet forwarder updated for Channel E packet handling

---

## Breaking Changes

- **Channel ID format change:** Database records now use `channel_a` through `channel_e` instead of `IF0`–`IF3` / `SX1261`. The upgrade script automatically cleans old-format records from the database.
- **Configuration key changes:** Some keys in `wm1303_ui.json` have been renamed or added for Channel E support. The upgrade script will merge new keys into existing configurations without overwriting user settings.

---

## Files Changed

### New Files
- `overlay/pymc_repeater/repeater/channel_e_bridge.py` — Channel E bridge module
- `overlay/hal/packet_forwarder/src/capture_thread.c` — CAPTURE_RAM streaming thread
- `overlay/hal/packet_forwarder/inc/capture_thread.h` — Capture thread header

### Modified Overlay Files
- `overlay/hal/libloragw/src/loragw_hal.c`
- `overlay/hal/libloragw/src/loragw_sx1261.c`
- `overlay/hal/libloragw/src/loragw_sx1302.c`
- `overlay/hal/libloragw/inc/loragw_hal.h`
- `overlay/hal/libloragw/inc/loragw_sx1261.h`
- `overlay/hal/libloragw/inc/sx1261_defs.h`
- `overlay/hal/packet_forwarder/src/lora_pkt_fwd.c`
- `overlay/pymc_core/src/pymc_core/hardware/tx_queue.py`
- `overlay/pymc_repeater/repeater/bridge_engine.py`
- `overlay/pymc_repeater/repeater/main.py`

### Modified Config Files
- `config/wm1303_ui.json` — Added Channel E configuration parameters
- `config/reset_lgw.sh` — Updated GPIO reset script
- `config/power_cycle_lgw.sh` — Updated power cycle script

### Modified Scripts
- `install.sh` — Added Channel E overlay support, fixed HAL overlay (added `loragw_hal.h`), added capture_thread overlay copies, added sudoers NOPASSWD configuration and hardware group membership (spi, i2c, gpio, dialout) for the pi user
- `upgrade.sh` — Added Channel E overlay support, added `loragw_hal.h` to checksum list, added capture_thread overlay copies and file comparison entries

---

## Upgrade Instructions

### From v1.x to v2.0.0

1. **SSH into your Raspberry Pi** and navigate to the pyMC_WM1303 repository:
   ```bash
   cd ~/pyMC_WM1303
   ```

2. **Pull the latest code:**
   ```bash
   git pull origin main
   ```

3. **Run the upgrade script:**
   ```bash
   sudo bash upgrade.sh --force-rebuild
   ```
   The `--force-rebuild` flag ensures the HAL is rebuilt with the new Channel E support.

4. **Verify the service is running:**
   ```bash
   sudo systemctl status pymc-repeater
   ```

5. **Access the web interface** at `http://<pi-ip>:8000/wm1303.html` and verify Channel E appears in the Status and Configuration tabs.

### Fresh Installation

```bash
git clone https://github.com/HansvanMeer/pyMC_WM1303.git
cd pyMC_WM1303
sudo bash install.sh
```

---

## Known Issues

- **Frequency decimal separator:** In the RF-Chain and IF-Chain configuration, decimal values currently use a comma instead of a period. This will be fixed in a future release.
- **Channel friendly names:** Channel aliases should not be used as reference IDs. This is documented but not yet enforced in the UI.
- **Metrics retention:** Automatic cleanup of old metrics data (> 8 days) is not yet implemented.
- **GPIO pin configuration:** PiHat + WM1303 pin assignments are not yet configurable via the Advanced Configuration tab.

---

## Dependencies

- Raspberry Pi OS Lite (Bookworm or newer)
- Python 3.11+
- SX1302 HAL v2.10 (from fork: `HansvanMeer/sx1302_hal`)
- pyMC_core dev branch (from fork: `HansvanMeer/pyMC_core`)
- pyMC_Repeater dev branch (from fork: `HansvanMeer/pyMC_Repeater`)
