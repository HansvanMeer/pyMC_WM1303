# System Architecture

> pyMC WM1303 — LoRa Multi-Channel Bridge/Repeater for SenseCAP M1

## Overview

The pyMC WM1303 system transforms a **SenseCAP M1** (Raspberry Pi 4 + WM1303 LoRa concentrator HAT) into a **MeshCore multi-channel bridge and repeater**. It enables up to 4 simultaneous LoRa channels with different frequency, bandwidth, and spreading factor configurations, bridging them together so MeshCore nodes on any channel can communicate through the repeater.

The system replaces the standard LoRaWAN packet forwarder stack with a custom integration that combines the Semtech SX1302 HAL, a modified packet forwarder, and the pyMC (Python MeshCore) software stack.

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
│  │  │ Processor│  │ (TX+RX)  │  │ (RX only)│  │ (Spectral/LBT)│    │    │
│  │  └────┬─────┘  └────┬─────┘  └────┬─────┘  └──────┬────────┘    │    │
│  │       │              │              │               │           │    │
│  │       └──────────────┴──────────────┘               │           │    │
│  │                      │ SPI bus                      │ SPI bus   │    │
│  └──────────────────────┼──────────────────────────────┼───────────┘    │
│                         │                              │                │
│              /dev/spidev0.0                  /dev/spidev0.1             │
│                    (2 MHz)                       (2 MHz)                │
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
│  │  - SX1261 spectral scan             │  │  - Spectral scan thread  │  │
│  │  - Calibration routines             │  │  - JSON config loading   │  │
│  └─────────────────────────────────────┘  └───────────┬──────────────┘  │
│                                                       │                 │
└───────────────────────────────────────────────────────┼─────────────────┘
                                                        │ UDP :1730
                                                        ▼
┌─────────────────────────────────────────────────────────────────────────┐
│                          BACKEND LAYER                                  │
│                                                                         │
│  ┌─────────────────────────────────────────────────────────────────┐    │
│  │                    WM1303 Backend                               │    │
│  │                                                                 │    │
│  │  ┌──────────────┐  ┌─────────────────┐  ┌───────────────────┐   │    │
│  │  │ UDP Handler  │  │ NoiseFloor      │  │   RX Watchdog     │   │    │
│  │  │ _handle_udp()│  │ Monitor (30s)   │  │ (3 detect modes)  │   │    │
│  │  │ RX dispatch  │  │ TX Hold (4s)    │  │ PUSH_DATA stats   │   │    │
│  │  └──────┬───────┘  │ Spectral harvest│  │ RSSI spike detect │   │    │
│  │         │          │ Rolling buffer  │  │ RX timeout (180s) │   │    │
│  │         │          └────────┬────────┘  └───────────────────┘   │    │
│  │         │                   │                                   │    │
│  │         ▼                   ▼                                   │    │
│  │  ┌──────────────────────────────────────────┐                   │    │
│  │  │         VirtualLoRaRadio (per channel)   │                   │    │
│  │  │  ch-1 (SF7) │ ch-2 (SF8) │ ch-3 │ ch-4   │                   │    │
│  │  └──────────────────────┬───────────────────┘                   │    │
│  └─────────────────────────┼───────────────────────────────────────┘    │
│                            │                                            │
│  ┌─────────────────────────▼───────────────────────────────────────┐    │
│  │                     Bridge Engine                               │    │
│  │                                                                 │    │
│  │  1. RX packet received on channel_x                             │    │
│  │  2. Dedup check (hash + time window)                            │    │
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
│  │              pyMC Console (Vue.js app)                          │    │
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
│                                      pyMC Console UI             │
└──────────────────────────────────────────────────────────────────┘
```

## Programming Languages

| Layer | Language | Components |
|-------|----------|------------|
| HAL / Concentrator | **C** | libloragw (SX1302 HAL), lora_pkt_fwd (packet forwarder) |
| Backend / Logic | **Python 3** | WM1303 Backend, Bridge Engine, TX Queue, pymc_core, pymc_repeater |
| Web UI | **HTML / JavaScript / CSS** | WM1303 Manager (wm1303.html), pyMC Console (Vue.js) |
| Configuration | **JSON / YAML** | global_conf.json, wm1303_ui.json, config.yaml |
| System Scripts | **Shell (sh)** | install.sh, upgrade.sh, reset_lgw.sh, power_cycle_lgw.sh |
| Build System | **Make** | HAL and packet forwarder compilation |

## Data Flow

### RX Path (Radio → User Interface)

```
RF Signal → Antenna → SX1250 Radio → SX1302 Baseband → IF Chain Demodulation
    → HAL lgw_receive() → Packet Forwarder → PUSH_DATA (UDP :1730)
    → WM1303 Backend _handle_udp() → Parse rxpk JSON
    → Frequency-to-channel mapping → VirtualLoRaRadio dispatch
    → Bridge Engine → Dedup check → Bridge rules evaluation
    → Statistics update → SQLite logging
    → WebSocket push → WM1303 Manager UI update
```

### TX Path (Bridge Decision → Radio Transmission)

```
Bridge Engine decision → Repeater handler (hop +1, path update)
    → TX batch window (2s) → Per-channel TX Queue
    → TTL check (5s max) → Queue overflow check (15 max)
    → TX Hold check (noise floor scan window)
    → LBT check (per-channel, if enabled)
    → CAD check (per-channel, if enabled)
    → PULL_RESP (UDP :1730) → Packet Forwarder
    → HAL lgw_send() → SX1250 Radio → RF Transmission
    → TX_ACK feedback → Statistics update
```

### Spectral Scan Path

```
NoiseFloorMonitor (every 30s) → TX Hold (4s pause)
    → SX1261 spectral scan via spidev0.1 (863-870 MHz)
    → Results to /tmp/pymc_spectral_results.json
    → Per-channel frequency matching (freq ± BW/2)
    → RSSI values → Rolling buffer (20 samples) per TX queue
    → TX Hold release → TX queues resume
    → WebSocket → Spectrum chart in UI
```

## Directory Structure (Installed System)

```
/opt/pymc_repeater/                    # Main installation directory
├── repos/
│   ├── pyMC_core/                     # MeshCore core library (dev branch)
│   │   └── src/pymc_core/
│   │       └── hardware/
│   │           ├── wm1303_backend.py  # WM1303 concentrator backend
│   │           ├── tx_queue.py        # Per-channel TX queue system
│   │           ├── sx1261_driver.py   # SX1261 companion chip driver
│   │           ├── virtual_radio.py   # VirtualLoRaRadio abstraction
│   │           ├── signal_utils.py    # Signal processing utilities
│   │           ├── sx1302_hal.py      # HAL ctypes wrapper
│   │           └── __init__.py        # Hardware module init
│   └── pyMC_Repeater/                 # MeshCore repeater app (dev branch)
│       └── repeater/
│           ├── main.py                # Application entry point
│           ├── engine.py              # Core repeater engine
│           ├── bridge_engine.py       # Cross-channel bridge logic
│           ├── packet_router.py       # Packet routing system
│           ├── config_manager.py      # Configuration management
│           ├── config.py              # Config data classes
│           ├── identity_manager.py    # Node identity handling
│           ├── data_acquisition/
│           │   ├── sqlite_handler.py  # SQLite database handler
│           │   └── storage_collector.py # Data collection
│           └── web/
│               ├── http_server.py     # HTTP/WebSocket server
│               ├── wm1303_api.py      # REST API endpoints
│               ├── spectrum_collector.py # Spectral scan collector
│               ├── cad_calibration_engine.py # CAD calibration
│               └── html/
│                   └── wm1303.html    # WM1303 Manager UI
└── venv/                              # Python virtual environment

/home/pi/
├── sx1302_hal/                        # Semtech SX1302 HAL (v2.10)
│   ├── libloragw/                     # HAL library source
│   │   ├── src/                       # C source files
│   │   ├── inc/                       # Header files
│   │   └── libloragw.a                # Compiled static library
│   └── packet_forwarder/              # Packet forwarder source
│       └── lora_pkt_fwd               # Compiled binary
└── wm1303_pf/                         # Packet forwarder runtime directory
    ├── lora_pkt_fwd                   # Packet forwarder binary (copy)
    ├── global_conf.json               # HAL configuration
    ├── bridge_conf.json               # Runtime bridge config (auto-generated)
    ├── reset_lgw.sh                   # GPIO reset script
    └── power_cycle_lgw.sh             # Full power cycle script

/etc/pymc_repeater/                    # Configuration directory
├── config.yaml                        # Main service configuration
└── wm1303_ui.json                     # UI/channel SSOT configuration

/etc/systemd/system/
└── pymc-repeater.service              # Systemd service unit

/var/lib/pymc_repeater/
└── repeater.db                        # SQLite database (metrics, stats)
```

## Key Architectural Decisions

1. **Packet Forwarder as TX/RX proxy**: Rather than driving the SX1302 directly from Python, the system uses the C-based packet forwarder as an intermediary. This leverages the well-tested HAL implementation while allowing Python-based bridge logic via UDP.

2. **Single Source of Truth (SSOT)**: `wm1303_ui.json` serves as the central configuration file for channel definitions, bridge rules, GPIO pins, and advanced HAL settings. Changes in the UI are written to this file and automatically picked up by the backend (5-second cache TTL).

3. **Per-channel TX queues**: Each channel has its own TX queue with independent LBT/CAD settings, overflow management, and statistics. This prevents one busy channel from blocking others.

4. **Software LBT over HAL LBT**: The HAL's built-in LBT was replaced with a software implementation using SX1261 spectral scan data. This provides per-channel control and avoids conflicts with the multi-channel setup.

5. **Overlay modification approach**: Rather than modifying the forked repositories directly, changes are maintained as overlay files that are applied during installation. This keeps the forks clean and makes updates easier.

6. **Dual SPI architecture**: The SX1302 (main concentrator) and SX1261 (companion chip) use separate SPI devices (spidev0.0 and spidev0.1), allowing simultaneous operation without bus contention.

---

*See also: [Hardware & HAL](hardware.md) | [Software Components](software.md) | [Configuration](configuration.md)*
