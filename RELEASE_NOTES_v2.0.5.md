# Release Notes — v2.0.5

**Release date:** 2026-04-17

---

## 🚨 Critical Bug Fix: Concentrator IF Channel Flooding

This release fixes a critical bug that caused Channel E (SX1261) to stop working correctly after any UI configuration save.

### Root Cause

`_generate_bridge_conf()` had a bug where unused IF demodulator slots (`chan_multiSF_1` through `_7`) were created with `enable: True` instead of `enable: False`. With only one channel configured, this caused **7 extra IF channels** to listen on the radio center frequency (869.525 MHz), receiving every nearby packet up to 7 times.

Combined with a loose 100 kHz frequency tolerance for Channel E matching, these duplicate concentrator packets (BW125) were incorrectly labeled as Channel E traffic — flooding the bridge engine with thousands of garbage packets per minute.

### Timeline

1. **Fresh install** → template `global_conf.json` is copied → only 2 IF channels enabled → works correctly
2. **UI config save** → triggers `_generate_bridge_conf()` → unused IF slots enabled with `enable: True` → concentrator floods
3. **Channel E breaks** → real SX1261 packets (2–3/min) drowned by concentrator packets (thousands/min)

---

## Changes

### Bug Fixes

| Fix | File | Description |
|-----|------|-------------|
| **Unused IF channels disabled** | `wm1303_backend.py` | Changed fallback loop from `enable: True` to `enable: False` for undefined `chan_multiSF` slots. This is the core fix that prevents concentrator flooding. |
| **Frequency tolerance tightened** | `wm1303_backend.py` | Channel E RX matching reduced from 100 kHz to 10 kHz. Defense-in-depth: even if IF channels are accidentally enabled, concentrator packets won't match. |
| **Bandwidth guard added** | `wm1303_backend.py` | Channel E now checks RX bandwidth (10% tolerance) in addition to frequency. BW125 concentrator packets are rejected; only BW62.5 SX1261 packets accepted. |
| **BW dropdown limited** | `wm1303.html` | Channel E bandwidth dropdown now only offers 62.5 kHz. Removed 125/250/500 kHz options that are unsupported by the SX1261 RX path. |
| **JS BW defaults corrected** | `wm1303.html` | Default bandwidth changed from 125000 to 62500 Hz in 4 locations (live display, RF diagram, form loader, initial state). |

### Configuration

| Change | File | Description |
|--------|------|-------------|
| **`name` field added** | `config/wm1303_ui.json` | Added `"name": "EU-Narrow"` to `channel_e` section. Ensures new installations show the correct display label. |
| **Deep merge for sub-keys** | `upgrade.sh` | JSON merge logic now handles nested dictionaries, adding missing sub-keys (e.g., `channel_e.name`) without overwriting existing values. |

---

## Upgrade Instructions

### One-liner upgrade
```bash
curl -sSL https://raw.githubusercontent.com/HansvanMeer/pyMC_WM1303/main/upgrade_bootstrap.sh | sudo bash
```

### Manual upgrade
```bash
cd /tmp
git clone https://github.com/HansvanMeer/pyMC_WM1303.git pymc_wm1303_upgrade
cd pymc_wm1303_upgrade
sudo bash upgrade.sh
```

The fix takes effect automatically after the upgrade restarts the service — `_generate_bridge_conf()` runs on every service start and will regenerate `bridge_conf.json` with the correct disabled IF channels.

---

## Verification

After upgrading, verify the fix is active:

```bash
# Check that unused IF channels are disabled
sudo cat /home/pi/wm1303_pf/bridge_conf.json | python3 -c "
import sys, json
c = json.load(sys.stdin)
for i in range(8):
    k = f'chan_multiSF_{i}'
    if k in c.get('SX130x_conf', {}):
        ch = c['SX130x_conf'][k]
        print(f'{k}: enable={ch.get(\"enable\")}, if={ch.get(\"if\", 0)}')
"
```

Expected: only `chan_multiSF_0` should show `enable=True`. All others should be `enable=False`.

---

## Files Changed

```
VERSION                                              |  2 +-
config/wm1303_ui.json                                |  3 ++-
overlay/pymc_core/src/pymc_core/hardware/wm1303_backend.py | 20 ++++++++++++---
overlay/pymc_repeater/repeater/web/html/wm1303.html  | 13 +++++----
upgrade.sh                                           |  6 +++++
TODO.md                                              | updated
```
