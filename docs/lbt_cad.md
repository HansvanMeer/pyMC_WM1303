# Listen Before Talk & Channel Activity Detection

> Software-based LBT and CAD implementation for polite spectrum access

## Overview

The WM1303 system implements **software-level Listen Before Talk (LBT)** and **Channel Activity Detection (CAD)** to ensure polite spectrum usage and reduce collisions. Both mechanisms operate per-channel and are independently toggleable through the [web UI](ui.md) or directly in `wm1303_ui.json`.

These checks are performed during TX queue processing — after a packet passes the dedup and TTL checks, but before it is handed to the packet forwarder for radio transmission. See [TX Queue & Scheduling](tx_queue.md) for the full TX flow.

## Why Software LBT (Not HAL-Level)

The SX1302 HAL includes a built-in LBT mechanism, but it is designed for single-channel LoRaWAN gateways. When configured for multi-channel operation, the HAL LBT produces:

```
ERROR: Cannot start LBT - wrong channel
```

This occurs because the HAL LBT expects a fixed frequency-to-channel mapping that does not match our dynamic multi-channel MeshCore setup. The solution is a **software LBT implementation** that:

- Operates per-channel with individual enable/disable toggles
- Uses the SX1261 companion chip for spectral scanning (separate SPI bus)
- Does not interfere with SX1302 RX operation
- Adapts thresholds based on rolling noise floor measurements

The HAL-level LBT is therefore **disabled** in `global_conf.json` (`"lbt": {"enable": false}`).

## Noise Floor Monitoring

The `NoiseFloorMonitor` is the foundation for both LBT and CAD. It runs as a background task that periodically measures the RF environment.

### How It Works

```
┌─────────────────────────────────────────────────────────────────┐
│                   NoiseFloorMonitor Cycle (every 30s)           │
│                                                                 │
│  1. Set TX Hold (4 seconds)                                     │
│     └─ All TX queues pause                                      │
│                                                                 │
│  2. SX1261 spectral scan gets a clean measurement window        │
│     └─ No TX interference on the SPI bus                        │
│                                                                 │
│  3. Read scan results from /tmp/pymc_spectral_results.json      │
│     └─ SX1261 scans 863-870 MHz (36 channels, 100 scans/ch)     │
│                                                                 │
│  4. Per-channel frequency matching                              │
│     └─ Match scan freq to channel freq ± BW/2                   │
│                                                                 │
│  5. Feed RSSI values into rolling buffer (20 samples per ch)    │
│     └─ Buffer provides avg, min, max noise floor                │
│                                                                 │
│  6. Release TX Hold                                             │
│     └─ TX queues resume normal operation                        │
└─────────────────────────────────────────────────────────────────┘
```

### Parameters

| Parameter | Default | Source | Description |
|-----------|---------|--------|-------------|
| `noise_floor_interval_seconds` | 30 | `wm1303_ui.json` → `adv_config` | Interval between measurement cycles |
| `noise_floor_tx_hold_seconds` | 4 | `wm1303_ui.json` → `adv_config` | TX pause duration during measurement |
| `noise_floor_buffer_size` | 20 | `wm1303_ui.json` → `adv_config` | Rolling buffer size (samples per channel) |

### SX1261 Spectral Scan

The SX1261 companion chip runs on a separate SPI bus (`/dev/spidev0.1`) and performs continuous spectral scanning independently of the SX1302 concentrator. This means:

- **No RX interruption** — The SX1302 continues receiving on `/dev/spidev0.0` while the SX1261 scans on `/dev/spidev0.1`
- **Band coverage** — Scans 863–870 MHz in 36 channels (~200 kHz steps)
- **Scan pace** — 1 second per sweep, 100 scans per channel per sweep
- **Result file** — HAL writes results to `/tmp/pymc_spectral_results.json`

The spectral scan is configured in `global_conf.json`:

```json
"sx1261_conf": {
  "spi_path": "/dev/spidev0.1",
  "spectral_scan": {
    "enable": true,
    "freq_start": 863000000,
    "nb_chan": 36,
    "nb_scan": 100,
    "pace_s": 1
  }
}
```

See [Radio Configuration](radio.md) for details on the SX1261 hardware and [Configuration Reference](configuration.md) for the full `global_conf.json` specification.

### TX Hold Mechanism

The TX hold is a temporary pause on all TX queue processing. There are two types:

| Type | Duration | Purpose |
|------|----------|--------|
| Noise floor scan | 4 seconds | Clean measurement window for SX1261 spectral scan |
| Batch window | 2 seconds | Collect bridge forwarding targets before sequential TX |

During a noise floor TX hold:
1. All TX queues stop dequeuing packets (packets remain queued)
2. The SX1261 spectral scan thread gets an interference-free window
3. Fresh RSSI values are harvested and fed into per-channel rolling buffers
4. After the hold expires, queues resume and use the updated noise floor data

The 4-second default provides enough time for at least 4 complete SX1261 spectral sweeps, ensuring reliable measurements.

## LBT — Listen Before Talk

### Decision Logic

When LBT is enabled for a channel, each packet in the TX queue must pass an RSSI check before transmission:

```
noise_floor = rolling_buffer.average()      # e.g. -93.4 dBm
threshold   = noise_floor + 10 dB           # adaptive: -83.4 dBm
measured    = latest_spectral_scan_rssi     # current measurement

if measured > threshold:
    → BLOCKED  (another signal detected)
    → lbt_blocked counter +1
    → retry after backoff
else:
    → PASS     (channel is clear)
    → lbt_passed counter +1
    → proceed to CAD check (if enabled) or TX
```

The threshold is **adaptive** — it floats 10 dB above the rolling average noise floor. This means:

- In a quiet environment (noise floor -115 dBm), threshold is -105 dBm
- In a noisy environment (noise floor -90 dBm), threshold is -80 dBm
- The system adapts automatically to the local RF environment

### LBT Per-Channel Configuration

LBT is configured per channel in the IF Chain Configuration section of the [WM1303 Manager UI](ui.md). Settings are stored in `wm1303_ui.json`:

```json
{
  "channels": [
    {
      "name": "ch-1",
      "frequency": 869525000,
      "lbt_enabled": true,
      "lbt_rssi_target": -115,
      "cad_enabled": false
    }
  ]
}
```

| Field | Type | Description |
|-------|------|-------------|
| `lbt_enabled` | boolean | Enable/disable LBT for this channel |
| `lbt_rssi_target` | integer (dBm) | Initial RSSI target (before adaptive threshold takes over) |

Changes in the UI are saved to `wm1303_ui.json` and picked up by the TX queue within **5 seconds** (configuration cache TTL). No service restart is required.

## CAD — Channel Activity Detection

CAD provides a second layer of spectrum sensing that detects whether a **LoRa signal is actively being transmitted** on the channel. While LBT checks energy levels, CAD checks for LoRa-specific patterns.

### How It Works

CAD uses spectral histogram analysis from the SX1261 scan data to identify LoRa signal patterns. This is a software implementation — not the hardware CAD mode of the SX1261.

The CAD check runs **after** the LBT check (if both are enabled):

```
Packet in TX queue
    │
    ▼
┌──────────┐     blocked    ┌─────────────┐
│ LBT Check├───────────────►│Retry/Backoff│
│ (energy) │                └─────────────┘
└────┬─────┘
     │ passed
     ▼
┌──────────┐     detected   ┌─────────────┐
│ CAD Check├───────────────►│ Wait + Retry│
│ (pattern)│                └─────────────┘
└────┬─────┘
     │ clear
     ▼
  Radio TX
```

### CAD Statistics

| Counter | Description |
|---------|-------------|
| `cad_clear` | Channel was free — no LoRa activity detected |
| `cad_detected` | LoRa activity detected — TX delayed |
| `cad_timeout` | CAD check timed out (inconclusive) |

### CAD Calibration Engine

The system includes a CAD calibration engine (`cad_calibration_engine.py`) that helps tune detection sensitivity. Calibration data is collected over time and used to improve the distinction between noise and actual LoRa signals.

## Noise Floor Display

### Threshold Color Coding

The [WM1303 Manager UI](ui.md) displays per-channel noise floor values with color coding:

| RSSI Range | Color | Meaning |
|-----------|-------|--------|
| < -110 dBm | Green | Clean channel, excellent for TX |
| -110 to -90 dBm | Yellow | Moderate noise, LBT may occasionally block |
| ≥ -90 dBm | Red | High noise level, TX likely blocked frequently |

### Noise Floor Statistics

Per-channel noise floor data is available via the [API](api.md) at `/api/wm1303/channels/live`:

| Field | Description |
|-------|-------------|
| `noise_floor` | Current noise floor (from LBT RSSI buffer or fallback) |
| `noise_floor_lbt_avg` | Rolling average (20 samples) |
| `noise_floor_lbt_min` | Minimum observed in buffer |
| `noise_floor_lbt_max` | Maximum observed in buffer |
| `noise_floor_lbt_samples` | Number of samples currently in buffer |

If noise floor consistently shows -120.0 dBm for all channels, the spectral scan data is not being collected. See [Troubleshooting](#troubleshooting) below.

## SPI Bus Impact

The LBT/CAD implementation has minimal impact on system performance because the SX1261 operates on a **separate SPI bus**:

| Device | SPI Path | Clock | Function |
|--------|----------|-------|----------|
| SX1302 | `/dev/spidev0.0` | 2 MHz | Main concentrator (RX + TX) |
| SX1261 | `/dev/spidev0.1` | 2 MHz | Spectral scan, LBT, CAD |

The LBT RSSI measurement via SX1261 (~1-5 ms) does **not** interrupt SX1302 RX operation. It only adds ~5 ms of extra latency before each TX.

See [Hardware Overview](hardware.md) for the full SPI bus architecture.

## Troubleshooting

### Noise Floor Always Shows -120.0 dBm

**Symptoms:** Channel Status displays -120.0 for all channels.

**Cause:** Spectral scan data is not being collected. Possible reasons:
- SX1261 not initialized (check `/dev/spidev0.1` exists)
- Spectral scan disabled in `global_conf.json`
- TX contention preventing scan harvesting

**Solutions:**
1. Verify `spectral_scan.enable: true` in `global_conf.json`
2. Check journal logs: `journalctl -u pymc-repeater -f | grep spectral`
3. Look for `skip spectral scan` messages
4. Wait for NoiseFloorMonitor cycle (30 seconds) — the TX hold should allow scanning

### LBT Blocks All Transmissions

**Symptoms:** `lbt_blocked` counter increases rapidly, no packets are sent.

**Cause:** Noise floor readings are unrealistically high, or a nearby transmitter is raising the ambient level.

**Solutions:**
1. Check noise floor values in the UI or via `/api/wm1303/channels/live`
2. If values seem wrong, restart the service: `sudo systemctl restart pymc-repeater`
3. Temporarily disable LBT per channel to test if TX works without it
4. Check for local interference sources near the antenna

### CAD Always Detects Activity

**Symptoms:** `cad_detected` counter is very high, legitimate TX is delayed.

**Cause:** CAD sensitivity may be too high for the local environment.

**Solutions:**
1. Check if there are actual LoRa transmitters nearby (LoRaWAN gateways, other nodes)
2. Consider disabling CAD and relying on LBT only
3. Check the CAD calibration data in the logs

## Related Documentation

- [TX Queue & Scheduling](tx_queue.md) — Full TX processing pipeline where LBT/CAD checks occur
- [Radio Configuration](radio.md) — RF chains, SX1261 spectral scan hardware
- [Configuration Reference](configuration.md) — Full config file specification
- [API Reference](api.md) — REST endpoints for noise floor and LBT/CAD statistics
- [Web UI](ui.md) — Channel configuration interface for LBT/CAD toggles
