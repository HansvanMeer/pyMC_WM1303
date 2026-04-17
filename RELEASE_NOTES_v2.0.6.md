# Release Notes — v2.0.6

**Release date:** 2026-04-18

---

## Overview

This release introduces a **non-blocking TX queue**, a **custom LBT framework** that replaces the broken HAL AGC-based Listen-Before-Talk mechanism, and several **performance optimizations** across the HAL, bridge engine, and packet forwarder. A new **debug bundle collector** provides one-click diagnostic export from the UI.

---

## 🚀 Non-Blocking TX Queue

The TX scheduler has been redesigned so that **LBT-blocked channels no longer stall the entire TX pipeline**. Previously, when a channel failed the LBT check, the scheduler would `await asyncio.sleep()` through all retry attempts — blocking all other channels from transmitting.

### How it works now

1. Channel dequeues a packet and performs an LBT check.
2. If LBT **passes** → transmit immediately.
3. If LBT **blocks** → store the packet in a per-channel `_blocked` dict with a `retry_after` timestamp, and **skip to the next channel**.
4. On subsequent round-robin passes, blocked channels are retried once their delay has elapsed.
5. After all retries exhausted (4 attempts: 0s, 0.5s, 1.0s, 1.5s), a **force-send** is scheduled after a 2.0s delay.

### Impact

| Before (v2.0.5) | After (v2.0.6) |
|---|---|
| LBT block on Channel A → Channels B, C, D, E all wait 5+ seconds | LBT block on Channel A → Channels B, C, D, E continue transmitting immediately |
| Total LBT retry window: up to 8.5s blocking | Total retry window: 3.5s non-blocking, then force-send |
| Single blocked channel could stall entire TX pipeline | Only the blocked channel is delayed |

### Additional TX queue improvements

- **Stale-future detection**: Packets whose caller already timed out (`future.done()`) are now dropped instead of transmitted, preventing wasted airtime.
- **RF-chain guard optimized**: Reduced from `max(150ms, airtime+120ms)` to `max(50ms, airtime+50ms)`, applied uniformly across all channels. This reclaims ~100ms per TX cycle.
- **LBT/CAD stats refactored** into a shared `_record_lbt_cad_stats()` method, eliminating duplicated stat-tracking code.

---

## 🔧 Custom LBT Framework (⚠️ Work In Progress)

> **⚠️ WARNING: LBT is currently non-functional and under active development.**
> The HAL's built-in LBT mechanism is broken (see below), and the new custom LBT framework is not yet operational.
> **Until LBT is fully working, disable LBT on every active channel in the UI** (Channels tab → per-channel settings → LBT: Off).
> Leaving LBT enabled on any channel may cause TX failures or blocked transmissions.

The HAL's built-in LBT mechanism (AGC-based) was found to be **non-functional**: the AGC firmware never sets the TX-status bit, causing `lgw_lbt_tx_status()` to timeout on every call. This resulted in **zero transmitted packets** when HAL LBT was enabled.

### New architecture

A two-layer LBT system replaces the broken HAL mechanism:

| Layer | Location | Method | Role |
|-------|----------|--------|------|
| **Software LBT** | Python (`GlobalTXScheduler`) | Cached spectral scan data | Pre-filter: blocks TX before packet reaches pkt_fwd |
| **Hardware LBT** | C (`lora_pkt_fwd.c` JIT thread) | Real-time SX1261 RSSI | Diagnostic: logs channel occupancy before each TX |

### New HAL files

| File | Description |
|------|-------------|
| `loragw_lbt.c` | Complete LBT implementation including `lgw_lbt_start()`, `lgw_lbt_tx_status()`, and the new `lgw_lbt_rssi_check()` |
| `loragw_lbt.h` | Public header with `lgw_lbt_rssi_check()` prototype |
| `loragw_aux.c` | HAL auxiliary functions (`wait_us`, `wait_ms`, `lgw_time_on_air()`) required by the LBT module |

### Custom LBT in pkt_fwd (JIT thread)

The packet forwarder's JIT thread now performs a **real-time SX1261 RSSI measurement** before each TX:

1. **TX guard check**: If a previous TX is still within its airtime window (+50ms margin), skip the RSSI check to avoid reading our own TX signal.
2. **SX1261 RSSI read**: Tunes SX1261 to the TX frequency, waits 5ms for RSSI to settle, reads instantaneous RSSI.
3. **Log-only mode**: The hardware RSSI check currently **does not block TX** — it only logs the result. This is because the co-located SX1302 TX signal leaks into the SX1261 at approximately -63 dBm, causing false positives. The software LBT layer handles actual TX gating decisions.

### Airtime estimation

The pkt_fwd now estimates TX airtime per SF/BW combination to set accurate TX guard windows, preventing the SX1261 from measuring our own transmissions as interference.

### Configuration

The `bridge_conf.json` now includes a `custom_lbt` section under `SX130x_conf.sx1261`:

```json
"custom_lbt": {
    "enable": false,
    "rssi_target": -80
}
```

When enabled, the pkt_fwd performs the real-time RSSI check. Currently **disabled by default** — the custom LBT framework is still in the diagnostic data collection phase and **not yet ready for production use**.

---

## 🏗️ Bridge Engine Cleanup

### Removed echo detection layers

Two secondary echo detection mechanisms have been **removed**:

- **Forwarded-packet echo detection** (`_recently_forwarded` hash tracking)
- **Payload-based echo detection** (`_recently_tx_payloads` hash tracking)

These were added as safety nets but proved to cause **false positives** — legitimate packets with identical payloads (e.g., repeated status broadcasts) were incorrectly dropped. The primary dedup mechanism (SHA-256 hash + TTL window) is sufficient.

### Performance optimizations

| Change | Impact |
|--------|--------|
| Default `tx_delay_ms` reduced from 50ms to **0ms** | Packets forwarded immediately, no artificial delay |
| Dedup `_seen` dict cleanup changed from per-call to **every 5 seconds** | Reduces CPU overhead on high-traffic bridges |
| Removed dead echo-detection code paths | Cleaner code, fewer dict allocations per RX packet |

---

## 🔬 SX1261 & HAL Improvements

### BW_62K5HZ support throughout

62.5 kHz bandwidth is now a first-class citizen across the HAL:

| Component | Change |
|-----------|--------|
| `loragw_hal.c` | LBT channel validation now accepts `BW_62K5HZ` |
| `loragw_sx1261.c` | `sx1261_set_rx_params()` maps BW_62K5HZ to GFSK RX_BW_117300 Hz |
| `lora_pkt_fwd.c` | JSON parser handles `bandwidth: 62500` → `BW_62K5HZ` for LBT channels |
| `loragw_lbt.c` | `is_matching_bw()` allows BW_62K5HZ TX to match BW_125KHZ LBT scan (wider scan covers narrower TX) |

### Configurable LNA RX gain (boosted mode)

Channel E LoRa RX gain is now configurable:

- **Boosted** (default): Maximum sensitivity, register 0x029F = 0x01
- **Power-saving**: Reduced sensitivity, register 0x029F = 0x00

The `boosted` parameter flows from `wm1303_ui.json` → `bridge_conf.json` → `sx1261_lora_rx_configure()` → `sx1261_lora_rx_start()`.

### Channel E frequency matching relaxed

The Channel E RX frequency matching tolerance in `wm1303_backend.py` was changed from 10 kHz (with bandwidth guard) to **100 kHz** (without bandwidth guard). The strict BW guard was causing legitimate SX1261 packets to be rejected when the HAL reported slightly different bandwidth values.

---

## 📦 Debug Bundle Collector

A new diagnostic export feature accessible from the **Logs** tab in the UI:

- **One-click generation** of a comprehensive `.tar.gz` bundle
- **Automatic redaction** of sensitive data (identity keys, JWT secrets, passwords)
- **Contents**: system info, journal logs (30min full + 24h errors), config files, runtime stats, file integrity checksums
- **15-minute expiry** with countdown timer in the UI
- **API endpoints**: `GET /api/wm1303/debug/status`, `POST /api/wm1303/debug/generate`, `GET /api/wm1303/debug/download`

---

## ⚙️ Configuration Changes

### Default value updates

| Key | Old Value | New Value | Reason |
|-----|-----------|-----------|--------|
| `bridge.dedup_ttl` | `dedup_ttl_seconds: 300` | `dedup_ttl: 15` | Renamed key; 15s is sufficient for dedup, 300s caused legitimate packets to be dropped |
| `repeater.cache_ttl` | 60 | **30** | Shorter window allows legitimate re-sends while still suppressing immediate duplicates |
| `delays.tx_delay_factor` | 1.0 | **0.5** | Reduces artificial TX delay, improving responsiveness |
| `repeater.tx_delay_factor` | 1.0 | **0.5** | Same as above, for the repeater engine |

### Upgrade migration

The `upgrade.sh` now includes an **automatic config migration step** that:
- Renames `dedup_ttl_seconds` → `dedup_ttl`
- Updates old values to new defaults
- Is idempotent (second run reports "up-to-date")

---

## 🌡️ Concentrator Temperature Reporting

The packet forwarder now writes the SX1302 concentrator temperature to `/tmp/concentrator_temp` on each stats cycle. The API prefers this value over the Raspberry Pi CPU temperature for the dashboard display, providing more accurate hardware monitoring.

---

## 📖 README Improvements

Added a comprehensive section on **Channel Count & LoRa Settings Impact**:

- Visual TX timeline diagrams showing how channels add up
- Airtime comparison table (50-byte packet across different SF/BW/CR settings)
- RX availability matrix by channel count and LoRa speed
- Practical recommendations for channel configuration

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

The upgrade script will:
1. Copy new HAL files (`loragw_lbt.c`, `loragw_lbt.h`, `loragw_aux.c`) and recompile
2. Deploy updated Python overlay files (including `debug_collector.py`)
3. Migrate config keys and values automatically
4. Restart the service

---

## Files Changed

```
VERSION                                                      |   2 +-
README.md                                                    |  70 +-
config/config.yaml.template                                  |   8 +-
install.sh                                                   |   5 +-
upgrade.sh                                                   |  79 ++-
overlay/hal/libloragw/inc/loragw_hal.h                       |   3 +-
overlay/hal/libloragw/inc/loragw_lbt.h                       |  79 +++  (NEW)
overlay/hal/libloragw/inc/loragw_sx1261.h                    |  13 +-
overlay/hal/libloragw/src/loragw_aux.c                       | 230 ++++++  (NEW)
overlay/hal/libloragw/src/loragw_hal.c                       |  16 +-
overlay/hal/libloragw/src/loragw_lbt.c                       | 354 +++++++++ (NEW)
overlay/hal/libloragw/src/loragw_sx1261.c                    |  74 +-
overlay/hal/packet_forwarder/src/capture_thread.c            |   2 +-
overlay/hal/packet_forwarder/src/lora_pkt_fwd.c              | 147 +++-
overlay/pymc_core/src/pymc_core/hardware/tx_queue.py         | 454 +++++++-----
overlay/pymc_core/src/pymc_core/hardware/wm1303_backend.py   |  99 ++-
overlay/pymc_repeater/repeater/bridge_engine.py              |  86 +--
overlay/pymc_repeater/repeater/engine.py                     |  54 +-
overlay/pymc_repeater/repeater/web/debug_collector.py        | 767 ++++++++++++++++++ (NEW)
overlay/pymc_repeater/repeater/web/html/wm1303.html          | 116 +++-
overlay/pymc_repeater/repeater/web/wm1303_api.py             |  82 ++-
```

**21 files changed, 2,409 insertions, 331 deletions**
