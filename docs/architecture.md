# System Architecture

> pyMC WM1303 — LoRa Multi-Channel Bridge/Repeater for SenseCAP M1

## Overview

The pyMC WM1303 system transforms a **SenseCAP M1** (Raspberry Pi 4 + WM1303 LoRa concentrator HAT) into a **MeshCore multi-channel bridge and repeater**. It supports up to **5 simultaneous LoRa channels** with independent frequency, bandwidth, and spreading factor configurations, bridging them together so MeshCore nodes on any channel can communicate through the repeater.

The system replaces the standard LoRaWAN packet forwarder stack with a custom integration that combines the Semtech SX1302 HAL, a modified packet forwarder, and the pyMC (Python MeshCore) software stack.

### 5-Channel Architecture

Since v2.0.0 the system operates as a **5-channel platform**:

| Channel | Radio Path | Backend | Notes |
|---------|-----------|---------|-------|
| Channel A | SX1302 IF chain → SX1250 | VirtualLoRaRadio | Concentrator-backed |
| Channel B | SX1302 IF chain → SX1250 | VirtualLoRaRadio | Concentrator-backed |
| Channel C | SX1302 IF chain → SX1250 | VirtualLoRaRadio | Concentrator-backed |
| Channel D | SX1302 IF chain → SX1250 | VirtualLoRaRadio | Concentrator-backed |
| Channel E | SX1261 companion radio | Dedicated SX1261 path | Sub-125 kHz BW support (e.g. 62.5 kHz) |

- **Channels A–D** use the SX1302 concentrator's multi-channel demodulators via the IF chain system.
- **Channel E** uses the SX1261 companion chip, which was originally only used for spectral scanning and LBT/CAD. Since v2.0.0 it operates as a full RX/TX radio channel. See [`channel_e_sx1261.md`](./channel_e_sx1261.md) for details.

> **Design guideline:** fewer active channels = more stable operation. 4 channels maximum is recommended for optimal performance.

## Architecture Diagram

```
┌─────────────────────────────────────────────────────────────────────────┐
│                           HARDWARE LAYER                                │
│                                                                         │
│  ┌─────────────────────────────────────────────────────────────────┐    │
│  │                    WM1303 Pi HAT Module                         │    │
│  │                                                                 │    │
│  │  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌───────────────┐    │    │
│  │  │ SX1302/03│  │ SX1250_0 │  │ SX1250_1 │  │    SX1261     │    │    │
│  │  │ Baseband │  │ RF0 Radio│  │ RF1 Radio│  │ Companion Chip│    │    │
│  │  │ Processor│  │ (TX+RX)  │  │ (RX only)│  │(RX/TX/Scan/LBT│    │    │
│  │  └────┬─────┘  └────┬─────┘  └────┬─────┘  └──────┬────────┘    │    │
│  │       │              │              │               │           │    │
│  │       └──────────────┴──────────────┘               │           │    │
│  │                      │ SPI bus                      │ SPI bus   │    │
│  └──────────────────────┼──────────────────────────────┼───────────┘    │
│                         │                              │                │
│              /dev/spidev0.0 (16 MHz)        /dev/spidev0.1             │
│                                                                         │
│  ┌─────────────────────────────────────────────────────────────────┐    │
│  │                   Raspberry Pi 4 (SenseCAP M1)                  │    │
│  │  GPIO: BCM17(reset) BCM18(power) BCM5(SX1261) BCM13(AD5338R)    │    │
│  │  GPIO base offset: 512 (sysfs = BCM + 512)                      │    │
│  └─────────────────────────────────────────────────────────────────┘    │
└─────────────────────────────────────────────────────────────────────────┘
                         │
                         │ SPI
                         ▼
┌─────────────────────────────────────────────────────────────────────────┐
│                         HAL & FORWARDER LAYER                           │
│                                                                         │
│  ┌─────────────────────────────────────┐  ┌──────────────────────────┐  │
│  │       libloragw.a (SX1302 HAL)      │  │   lora_pkt_fwd           │  │
│  │                                     │  │   (Packet Forwarder)     │  │
│  │  - Board/RF/IF chain configuration  │  │                          │  │
│  │  - SX1250 radio control             │  │  - UDP server (:1730)    │  │
│  │  - TX/RX packet handling            │  │  - PUSH_DATA (RX→UDP)    │  │
│  │  - AGC management (debounced)       │  │  - PULL_RESP (UDP→TX)    │  │
│  │  - FEM/LNA register control         │  │  - TX_ACK feedback       │  │
│  │  - SX1261 spectral scan + RX/TX     │  │  - Spectral scan thread  │  │
│  │  - Calibration routines             │  │  - Channel E packet I/O  │  │
│  │  - SPI optimized (16 MHz, 16K burst)│  │  - JSON config loading   │  │
│  └─────────────────────────────────────┘  └───────────┬──────────────┘  │
│                                                       │                 │
└───────────────────────────────────────────────────────┼─────────────────┘
                                                        │ UDP :1730
                                                        ▼
┌─────────────────────────────────────────────────────────────────────────┐
│                          BACKEND LAYER                                  │
│                                                                         │
│  ┌───────────────────────────────────────────────────────────────┐      │
│  │                    WM1303 Backend                             │      │
│  │                                                               │      │
│  │  ┌──────────────┐  ┌─────────────────┐  ┌───────────────────┐ │      │
│  │  │ UDP Handler  │  │ NoiseFloor      │  │   RX Watchdog     │ │      │
│  │  │ _handle_udp()│  │ Monitor (30s)   │  │ (3 detect modes)  │ │      │
│  │  │ Retry TX-free│  │ PUSH_DATA stats │  │ RSSI spike detect │ │      │
│  │  │ Self-echo det│  │ Spectral harvest│  │ RX timeout (180s) │ │      │
│  │  └──────┬───────┘  │ Rolling buffer  │  └───────────────────┘ │      │
│  │         │          └────────┬────────┘                        │      │
│  │         │                   │                                 │      │
│  │         ▼                   ▼                                 │      │
│  │  ┌───────────────────────────────────────────────────────┐    │      │
│  │  │         VirtualLoRaRadio (per channel)                │    │      │
│  │  │  Channel A │ Channel B │ Channel C │ Channel D        │    │      │
│  │  └──────────────────────┬────────────────────────────────┘    │      │
│  │                         │                                     │      │
│  │  ┌──────────────────────┴────────────────────────────────┐    │      │
│  │  │              Channel E Bridge                         │    │      │
│  │  │  SX1261 RX/TX path (62.5 kHz BW support)             │    │      │
│  │  └──────────────────────┬────────────────────────────────┘    │      │
│  └─────────────────────────┼─────────────────────────────────────┘      │
│                            │                                            │
│  ┌─────────────────────────▼───────────────────────────────────────┐    │
│  │                     Bridge Engine                               │    │
│  │                                                                 │    │
│  │  1. RX packet received on channel_x                             │    │
│  │  2. Dedup check (3-layer: self-echo + multi-demod + hash)       │    │
│  │  3. Bridge rules evaluation (source → target mapping)           │    │
│  │  4. Repeater handler (hop count +1, path bytes update)          │    │
│  │  5. Packet-type filtering (per rule)                            │    │
│  │  6. TX batch window (2s) for concurrent queuing                 │    │
│  │  7. Fire sends to all target channel TX queues                  │    │
│  └───────────┬─────────────┬──────────────┬────────────────────────┘    │
│              │             │              │                             │
│       ┌──────▼──────┐ ┌───▼────────┐ ┌───▼────────┐                     │
│       │ TX Queue    │ │ TX Queue   │ │ TX Queue   │  (per channel)      │
│       │ LBT check   │ │ CAD check  │ │ TTL check  │                     │
│       │ Overflow mgt│ │ FIFO order │ │ Hold check │                     │
│       └──────┬──────┘ └───┬────────┘ └───┬────────┘                     │
│              └────────────┴──────────────┘                              │
│                           │ PULL_RESP (UDP)                             │
│                           ▼                                             │
│                    Packet Forwarder → Radio TX                          │
└─────────────────────────────────────────────────────────────────────────┘
                                                        │
┌─────────────────────────────────────────────────────────────────────────┐
│                           WEB / API LAYER                               │
│                                                                         │
│  ┌──────────────────┐  ┌──────────────────┐  ┌─────────────────────┐    │
│  │  HTTP Server     │  │  REST API        │  │  WebSocket          │    │
│  │  (port 8000)     │  │  /api/wm1303/*   │  │  Real-time updates  │    │
│  │  Static files    │  │  JWT auth        │  │  Stats push         │    │
│  └──────────────────┘  └──────────────────┘  └─────────────────────┘    │
│                                                                         │
│  ┌─────────────────────────────────────────────────────────────────┐    │
│  │              WM1303 Manager UI (wm1303.html)                    │    │
│  │                                                                 │    │
│  │  Tabs: Status | Channels | Bridge | Spectrum | Adv. Config      │    │
│  │  Charts: Signal Quality, LBT, CAD, Dedup, Spectrum              │    │
│  │  Real-time: WebSocket-driven auto-refresh                       │    │
│  └─────────────────────────────────────────────────────────────────┘    │
│                                                                         │
│  ┌─────────────────────────────────────────────────────────────────┐    │
│  │              pyMC Repeater UI (Vue.js app)                      │    │
│  │  Existing MeshCore functionality: nodes, mesh, messaging        │    │
│  └─────────────────────────────────────────────────────────────────┘    │
└─────────────────────────────────────────────────────────────────────────┘
```

## Component Relationships

```
┌──────────────────────────────────────────────────────────────────┐
│                      Component Dependency Graph                  │
│                                                                  │
│   libloragw.a ◄── lora_pkt_fwd ◄── WM1303 Backend                │
│       │                │                    │                    │
│       │           UDP :1730            ┌────┴────────┐           │
│       │                                │             │           │
│   SX1302/SX1261                   pymc_core    pymc_repeater     │
│   (hardware)                      (library)    (application)     │
│                                       │             │            │
│                                       │        Bridge Engine     │
│                                       │        Channel E Bridge  │
│                                       │        Config Manager    │
│                                       │        Packet Router     │
│                                       │             │            │
│                                  VirtualLoRaRadio   │            │
│                                  TXQueue            │            │
│                                  SX1261Driver       │            │
│                                       │             │            │
│                                       └──────┬──────┘            │
│                                              │                   │
│                                         WM1303 API               │
│                                         HTTP Server              │
│                                         WebSocket                │
│                                              │                   │
│                                      WM1303 Manager UI           │
│                                      pyMC Repeater UI            │
└──────────────────────────────────────────────────────────────────┘
```

## Programming Languages

| Layer | Language | Components |
|-------|----------|------------|
| HAL / Concentrator | **C** | libloragw (SX1302 HAL), lora_pkt_fwd (packet forwarder) |
| Backend / Logic | **Python 3** | WM1303 Backend, Bridge Engine, Channel E Bridge, TX Queue, pymc_core, pymc_repeater |
| Web UI | **HTML / JavaScript / CSS** | WM1303 Manager (wm1303.html), pyMC Repeater UI (Vue.js) |
| Configuration | **JSON / YAML** | global_conf.json, bridge_conf.json, wm1303_ui.json, config.yaml |
| System Scripts | **Shell (sh)** | install.sh, upgrade.sh, bootstrap.sh, reset_lgw.sh, power_cycle_lgw.sh |
| Build System | **Make** | HAL and packet forwarder compilation |

## Configuration Model — Single Source of Truth (SSOT)

The WM1303-specific runtime configuration uses a **single source of truth** model:

| File | Role |
|------|------|
| `wm1303_ui.json` | **Authoritative** — all WM1303 channel, bridge, and UI settings |
| `config.yaml` | Repeater-level config — channel data synchronized from wm1303_ui.json on save |
| `bridge_conf.json` | **Generated** — HAL/forwarder config, regenerated from wm1303_ui.json on every service start |
| `global_conf.json` | **Copy of bridge_conf.json** — kept for HAL compatibility |

### How it works

1. User changes settings via the WM1303 Manager UI
2. API writes to `wm1303_ui.json` (the SSOT)
3. `_sync_config_yaml_channels()` synchronizes active channels into `config.yaml`
4. On service start, `_generate_bridge_conf()` reads `wm1303_ui.json` and generates `bridge_conf.json`
5. `bridge_conf.json` is copied to `global_conf.json` for HAL/forwarder consumption

## Data Flow

### RX Path (Radio → User Interface)

```
RF Signal → Antenna → SX1250 Radio → SX1302 Baseband → IF Chain Demodulation
    → HAL lgw_receive() → Packet Forwarder → PUSH_DATA (UDP :1730)
    → WM1303 Backend _handle_udp() → Parse rxpk JSON
    → Self-echo detection (TX hash check, 30s TTL)
    → Multi-demod dedup (same packet via multiple IF chains, 2s TTL)
    → Frequency-to-channel mapping → VirtualLoRaRadio dispatch
    → Bridge Engine → Full-packet hash dedup (15s TTL)
    → Bridge rules evaluation
    → Statistics update → SQLite logging
    → WebSocket push → WM1303 Manager UI update
```

### Channel E RX Path

```
RF Signal → Antenna → SX1261 Radio → HAL sx1261_receive()
    → Packet Forwarder → PUSH_DATA (UDP :1730) with SX1261 marker
    → WM1303 Backend → Frequency + BW guard (10 kHz freq tolerance, 10% BW tolerance)
    → Channel E Bridge → Bridge Engine injection
    → Same bridge rules evaluation as Channels A–D
```

### TX Path (Bridge Decision → Radio Transmission)

```
Bridge Engine decision → Repeater handler (hop +1, path update)
    → TX batch window (2s) → Per-channel TX Queue (async)
    → Fair round-robin scheduling (rotating start index)
    → TTL check (5s max) → Queue overflow check (15 max)
    → LBT check (per-channel, if enabled)
    → CAD check (per-channel, requires LBT enabled)
    → PULL_RESP (UDP :1730) → Socket auto-recovery on failure
    → Packet Forwarder → HAL lgw_send() → SX1250 Radio → RF Transmission
    → TX hash stored for self-echo detection
    → TX_ACK feedback → Statistics update
```

### Spectral Scan Path

```
HAL spectral scan thread (pace_s=1, nb_scan=100)
    → SX1261 spectral scan via /dev/spidev0.1
    → Scan during TX-free windows only
    → Results to /tmp/pymc_spectral_results.json

NoiseFloorMonitor (every 30s, does NOT pause TX queues)
    → Read /tmp/pymc_spectral_results.json
    → Per-channel frequency matching (freq ± BW/2)
    → RSSI values → Rolling buffer (20 samples) per TX queue
    → Noise floor values persisted to database
    → WebSocket → Spectrum chart in UI
```

## Deduplication Architecture

The system uses a **3-layer deduplication strategy** at two different levels:

### Layer 1: WM1303 Backend (hardware/UDP level)

| Mechanism | Dict | TTL | Purpose |
|-----------|------|-----|----------|
| **TX Self-Echo Detection** | `_tx_echo_hashes` | 30s | Discards own TX heard back via antenna. Hash stored on every TX, checked on every RX. |
| **Multi-Demod RX Dedup** | `_rx_dedup_cache` | 2s | SX1302 can receive the same packet via multiple IF chains/demodulators simultaneously. This catches hardware-level duplicates. |

### Layer 2: Bridge Engine (bridge/routing level)

| Mechanism | Dict | TTL | Purpose |
|-----------|------|-----|----------|
| **Full-Packet Hash Dedup** | `_seen` | 15s | Primary bridge-level dedup. SHA256 of complete packet. Catches cross-channel duplicates (same packet received on multiple channels). |

### Dedup event persistence

All dedup events are logged to a ring buffer (500 entries) and persisted to SQLite via a background writer thread for visualization in the UI dedup chart. Cleanup runs hourly, removing events older than 7 days.

## Per-Channel Metrics Mapping

Runtime metrics are tracked using **direct channel identifiers** (e.g., `channel_a`, `channel_e`), not frequencies. This is critical when channels share the same frequency but use different spreading factors — frequency-based mapping would incorrectly merge their statistics.

This applies to:
- Noise floor values per channel
- CAD/LBT event history
- Signal quality charts
- API responses
- Database records

## Directory Structure (Installed System)

```
/opt/pymc_repeater/                    # Main installation directory
├── repos/
│   ├── pyMC_core/                     # MeshCore core library (dev branch)
│   │   └── src/pymc_core/
│   │       └── hardware/
│   │           ├── wm1303_backend.py  # WM1303 concentrator backend
│   │           ├── virtual_radio.py   # VirtualLoRaRadio per-channel abstraction
│   │           ├── tx_queue.py        # Per-channel TX queue
│   │           ├── sx1261_driver.py   # SX1261 companion radio driver
│   │           └── sx1302_hal.py      # HAL wrapper
│   └── pyMC_Repeater/                 # Repeater application (dev branch)
│       └── repeater/
│           ├── main.py                # Main daemon (with bridge init)
│           ├── bridge_engine.py       # Cross-channel packet routing
│           ├── channel_e_bridge.py    # Channel E integration
│           ├── engine.py              # Repeater engine
│           ├── config.py              # Config (radio_type: wm1303)
│           └── web/
│               ├── wm1303_api.py      # WM1303 REST API
│               ├── spectrum_collector.py
│               ├── cad_calibration_engine.py
│               └── html/
│                   └── wm1303.html    # WM1303 Manager UI
│
/etc/pymc_repeater/                    # Runtime configuration
├── config.yaml                        # Repeater config
├── wm1303_ui.json                     # WM1303 SSOT config
└── version                            # Deployed version
│
/home/pi/wm1303_pf/                    # Packet forwarder runtime
├── lora_pkt_fwd                       # Compiled binary
├── bridge_conf.json                   # Generated HAL config (authoritative)
├── global_conf.json                   # Copy of bridge_conf.json
└── spectral_scan/                     # Scan result storage
│
/tmp/                                  # Transient runtime files
├── pymc_spectral_results.json         # Latest spectral scan output
└── various runtime state files
```

## Design Principles

1. **RX availability is the #1 priority** — RX must be available as much of the time as possible
2. **TX duration must be as short as possible** — minimize time spent transmitting
3. **TX must be sent ASAP** after a message enters the TX queue — no unnecessary delays
4. Monitoring tasks (spectral scan, noise floor) must **not block RX or pause TX queues**
5. The old noise-floor TX hold has been removed; only the **2-second batch window** remains

## Related Documents

- [`channel_e_sx1261.md`](./channel_e_sx1261.md) — Channel E / SX1261 companion radio
- [`radio.md`](./radio.md) — Radio architecture and RF behavior
- [`software.md`](./software.md) — Software components
- [`hardware.md`](./hardware.md) — Hardware details
- [`lbt_cad.md`](./lbt_cad.md) — LBT and CAD behavior
- [`tx_queue.md`](./tx_queue.md) — TX queue system
- [`configuration.md`](./configuration.md) — Configuration files
- [`api.md`](./api.md) — REST API reference
- [`ui.md`](./ui.md) — WM1303 Manager UI
- [`installation.md`](./installation.md) — Installation guide
- [`repositories.md`](./repositories.md) — Repository structure
