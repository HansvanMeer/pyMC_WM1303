# Software Components

> Detailed description of all software components in the WM1303 system

## Overview

The WM1303 software stack combines modified upstream projects with overlay files from this repository. The implementation spans three layers:

1. **C layer** — Modified SX1302 HAL and packet forwarder
2. **Python layer** — WM1303 backend, bridge engine, TX queues, and API
3. **Web layer** — WM1303 Manager UI and pyMC Repeater UI integration

## Upstream Sources & Overlay Strategy

The software is built from upstream repositories with overlay files applied during installation:

| Repository | Branch | Role |
|-----------|--------|------|
| `HansvanMeer/sx1302_hal` | HAL v2.10 | Concentrator HAL + packet forwarder |
| `HansvanMeer/pyMC_core` | `dev` | MeshCore core library |
| `HansvanMeer/pyMC_Repeater` | `dev` | Repeater application |
| `HansvanMeer/pyMC_WM1303` | `main` | Overlays, scripts, config, docs |

Overlay files replace or extend specific source files in the upstream repos without modifying the forks directly. See [`repositories.md`](./repositories.md).

## C Layer — HAL and Packet Forwarder

### libloragw (SX1302 HAL)

Modified HAL library (v2.10 base). Key overlay changes:

| File | Changes |
|------|---------|
| `loragw_hal.c` / `.h` | Updated initialization, channel management, Channel E support |
| `loragw_sx1261.c` / `.h` | Extended SX1261 driver for full RX/TX (not just scan/LBT) |
| `loragw_sx1302.c` / `.h` | Updated concentrator interface |
| `loragw_spi.c` / `.h` | **SPI optimizations** — 16 MHz clock, 16 KB burst chunks |
| `loragw_aux.c` | Added `BW_62K5HZ` bandwidth support for Channel E |
| `sx1261_defs.h` | Updated register definitions |
| `capture_thread.c` / `.h` | CAPTURE_RAM streaming thread (disabled to avoid SPI contention) |

### lora_pkt_fwd (Packet Forwarder)

Modified packet forwarder. Key changes:

- Channel E packet handling (SX1261 RX/TX integration)
- Spectral scan thread with configurable pacing (`pace_s=1`, `nb_scan=100`)
- UDP server on port 1730 (PUSH_DATA for RX, PULL_RESP for TX, TX_ACK for feedback)
- JSON configuration loading from `bridge_conf.json`

## Python Layer — Backend Components

### WM1303 Backend (`wm1303_backend.py`)

The main coordinator (~2970 lines). Responsibilities:

| Function | Description |
|----------|-------------|
| UDP handler | Receives PUSH_DATA (RX) from pkt_fwd, sends PULL_RESP (TX) |
| Self-echo detection | Stores TX hashes, discards own TX heard on RX (30s TTL) |
| Multi-demod dedup | Catches hardware-level duplicates from multiple IF chains (2s TTL) |
| Channel dispatch | Maps RX packets to correct VirtualLoRaRadio by frequency/SF |
| Channel E dispatch | Frequency + BW guard (10 kHz tolerance, 10% BW tolerance since v2.0.5) |
| TX emission | Builds PULL_RESP packets, socket auto-recovery on failure |
| NoiseFloorMonitor | 30s interval, harvests spectral scan results, does NOT pause TX |
| RX Watchdog | 3 detection modes — RSSI spike, PUSH_DATA stats, RX timeout (180s) |
| AGC management | Debounced AGC reset recovery (SX1302-specific) |
| pkt_fwd management | Start/stop/restart, stdout reader, watchdog |
| Config generation | `_generate_bridge_conf()` reads SSOT and generates HAL config |
| Channel stats | Per-channel RX/TX counters, SQLite snapshots |

### VirtualLoRaRadio (`virtual_radio.py`)

Per-channel radio abstraction (~198 lines):

- Implements the standard `LoRaRadio` interface used by pymc_core/repeater
- Each instance represents one logical channel (A, B, C, or D)
- Async RX queue with thread-safe enqueue from UDP handler thread
- Per-channel noise floor estimation from RSSI history
- TX delegates to `WM1303Backend.send()`
- One instance per active concentrator channel

### TX Queue (`tx_queue.py`)

Per-channel TX queue system (~668 lines):

- FIFO ordering with fair round-robin scheduling across channels
- TTL check (5s max per packet)
- Queue overflow management (15 packets max)
- LBT check (per-channel, when enabled)
- CAD check (per-channel, requires LBT to be enabled)
- TX batch window (2s) — the only remaining intentional TX hold
- Rotating start index for fair multi-channel scheduling
- Noise floor values fed from NoiseFloorMonitor for LBT decisions

See [`tx_queue.md`](./tx_queue.md) for detailed documentation.

### SX1261 Driver (`sx1261_driver.py`)

SX1261 companion radio driver (~956 lines):

- Direct SPI communication via `/dev/spidev0.1`
- Spectral scan engine for noise floor measurement
- LBT RSSI measurement support
- CAD (Channel Activity Detection) support
- Full RX/TX capability for Channel E (since v2.0.0)
- Support for sub-125 kHz bandwidths (62.5 kHz)

### SX1302 HAL Wrapper (`sx1302_hal.py`)

Thin Python wrapper (~37 lines) around the C HAL library.

### Bridge Engine (`bridge_engine.py`)

Cross-channel packet routing engine (~865 lines):

| Feature | Description |
|---------|-------------|
| Rule-based routing | Source → target channel mapping with packet-type filtering |
| Deduplication | SHA256 full-packet hash with 15s TTL |
| Packet type filtering | Per-rule: all, advert, text, position, etc. |
| Repeater handler | Hop count +1, path bytes update via pymc_repeater |
| TX batch window | 2-second window for concurrent queuing |
| Channel aliases | Dynamic alias resolution (channel_a, ch-1, n1, etc.) |
| Dedup event logging | Ring buffer (500 entries) + SQLite background writer |
| Statistics | Forwarded, dropped (duplicate/filtered), echo detected |

### Channel E Bridge (`channel_e_bridge.py`)

Dedicated bridge component for Channel E packet routing:

- Integrates SX1261 RX/TX with the main bridge engine
- Handles Channel E packet injection into the bridge
- Manages Channel E-specific packet forwarding

### Modifications to Upstream pymc_core

| File | Changes |
|------|---------|
| `__init__.py` | Conditional imports for WM1303Backend + VirtualLoRaRadio |
| (hardware/) | Added: wm1303_backend.py, virtual_radio.py, tx_queue.py, sx1261_driver.py, sx1302_hal.py |

### Modifications to Upstream pymc_repeater

| File | Lines Added | Changes |
|------|------------|--------|
| `main.py` | +279 | Bridge handler, bridge init, SSOT rules loading |
| `config.py` | +19 | `radio_type: wm1303` handling in radio factory |
| `sqlite_handler.py` | +144 | `dedup_events` table, query/aggregation methods |
| `http_server.py` | +32 | Mount WM1303 API + serve wm1303.html |
| `api_endpoints.py` | +13 | WM1303 as hardware selection option |
| (new files) | — | bridge_engine.py, channel_e_bridge.py, wm1303_api.py, spectrum_collector.py, wm1303.html |

## Web Layer

### WM1303 API (`wm1303_api.py`)

Dedicated REST API (~2974 lines) under `/api/wm1303/*`:

- Channel status and configuration
- Bridge rules management (SSOT)
- Spectrum/noise floor data
- Dedup event history
- Channel statistics and metrics
- System health and version info
- JWT authentication

See [`api.md`](./api.md) for endpoint documentation.

### Spectrum Collector (`spectrum_collector.py`)

Collects and serves spectral scan data (~275 lines) for the Spectrum tab charts.

### CAD Calibration Engine (`cad_calibration_engine.py`)

CAD parameter calibration support for optimal activity detection.

### WM1303 Manager UI (`wm1303.html`)

Single-page web application with:

- Status tab — channel statistics, signal quality, system info
- Channels tab — per-channel configuration
- Bridge tab — bridge rules management
- Spectrum tab — spectral scan, CAD, LBT, noise floor charts
- Advanced Config tab — GPIO, RF chains, IF chains, advanced parameters

See [`ui.md`](./ui.md).

## Runtime Services

### systemd Service

The WM1303 repeater runs as a systemd service (`pymc-repeater.service`):

- Automatic start on boot
- Restart on failure
- Runtime file permissions via ExecStartPre
- Logs via journalctl

### Process Hierarchy

```
systemd → pymc-repeater.service
    → Python (pymc_repeater main.py)
        → WM1303Backend
            → lora_pkt_fwd (child process)
            → UDP handler thread
            → NoiseFloorMonitor thread
            → RX Watchdog
            → Dedup SQLite writer thread
        → Bridge Engine
            → RX loops (one per channel)
            → TX Queue schedulers (one per channel)
        → Channel E Bridge
        → CherryPy HTTP server
            → WM1303 API
            → pyMC Repeater API
            → WebSocket server
```

## Database

SQLite database for persistent storage:

| Table | Purpose |
|-------|---------|
| `noise_floor_history` | Per-channel noise floor snapshots |
| `cad_events` | CAD detection events |
| `dedup_events` | Bridge dedup/echo event history |
| `channel_stats` | Periodic channel counter snapshots |
| Standard pymc_repeater tables | Nodes, messages, etc. |

## Related Documents

- [`architecture.md`](./architecture.md) — System architecture
- [`api.md`](./api.md) — REST API reference
- [`ui.md`](./ui.md) — WM1303 Manager UI
- [`tx_queue.md`](./tx_queue.md) — TX queue system
- [`configuration.md`](./configuration.md) — Configuration files
