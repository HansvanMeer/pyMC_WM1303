# Software Components

> Backend architecture, pyMC modifications, and key subsystems

## Overview

The software stack consists of two main Python packages with custom modifications (overlays) applied during installation:

- **pyMC_core** (dev branch) — MeshCore core library with WM1303-specific hardware drivers
- **pyMC_Repeater** (dev branch) — MeshCore repeater application with bridge engine and web UI

Both run inside a Python virtual environment at `/opt/pymc_repeater/venv/` and are managed as a single systemd service (`pymc-repeater`).

## pyMC_core Modifications

The following files are added or modified in `pymc_core/hardware/` via overlay:

### WM1303Backend (`wm1303_backend.py`)

The central backend component (~112 KB, largest file in the system). It manages all hardware interaction and serves as the bridge between the packet forwarder and the Python application.

**Key responsibilities:**

| Function | Description |
|----------|-------------|
| Packet forwarder management | Start/stop/restart `lora_pkt_fwd` process |
| UDP protocol handling | Parse PUSH_DATA (RX), send PULL_RESP (TX), handle TX_ACK |
| Channel dispatch | Map received packets to channels via frequency matching |
| Channel sourcing (`get_radios()`) | Read channel definitions primarily from `wm1303_ui.json` (SSOT); fall back to `config.yaml` only when no active SSOT channels exist |
| Configuration sync | Generate `global_conf.json` and `bridge_conf.json` from SSOT |
| NoiseFloorMonitor | Periodic noise floor measurement via SX1261 spectral scan |
| RX Watchdog | 3-mode detection of RX failures with auto-recovery |
| Statistics | Track per-channel RX/TX counts, RSSI, SNR, timing |
| Database logging | Store channel stats history in SQLite for metrics/charts |
| Self-echo detection | Discard own TX packets heard back on RX |

**RF0-TX Architecture:**

```python
"""RF0-TX Architecture (proper hardware TX path):
  - SX1250_0 (RF0): RX + TX via SKY66420 FEM (PA + LNA + RF Switch)
  - SX1250_1 (RF1): RX only (no PA, SAW filter path)
  - IF chains 0-2 on RF0 for RX
  - TX via PULL_RESP to lora_pkt_fwd -> HAL routes to RF0 (tx_enable=true)
  - Direct TX: no stop/start cycle needed (RF0 has proper FEM for TX/RX switching)
  - Self-echo detection discards own TX heard back on RX
  - Per-channel TX queues serialize transmissions
"""
```

### SX1302HAL (`sx1302_hal.py`)

Minimal Python ctypes wrapper for the libloragw shared library. Provides type definitions and function signatures for direct HAL access when needed. In normal operation, the packet forwarder handles HAL interaction; this module is used for diagnostics and configuration validation.

### TXQueue (`tx_queue.py`)

Per-channel TX queue system with sophisticated flow control. See [TX Queue & Scheduling](tx_queue.md) for complete details.

**Key classes:**

| Class | Purpose |
|-------|----------|
| `ChannelTXQueue` | Per-channel queue with LBT/CAD checks, TTL, overflow management |
| `TXQueueManager` | Manages all channel queues, provides unified statistics |
| `GlobalTXScheduler` | Round-robin TX scheduling across channels with inter-packet gap |

**Queue parameters:**

| Parameter | Value | Description |
|-----------|-------|-------------|
| Max queue size | 15 | Per channel (reduced from 50 to prevent overflow) |
| TTL | 5 seconds | Packets older than 5s are dropped |
| Overflow policy | Drop oldest | Oldest packet removed when queue is full |
| Processing order | FIFO | First In, First Out |
| Inter-packet gap | 50 ms | Minimum time between consecutive TX events |

### SX1261Driver (`sx1261_driver.py`)

Driver for the SX1261 companion chip (~33 KB). Handles:

- SPI communication via spidev0.1
- Spectral scan configuration and execution
- RSSI point measurements for LBT
- CAD (Channel Activity Detection) mode
- Frequency and bandwidth configuration
- Results parsing and per-channel mapping

The SX1261 operates independently of the SX1302, allowing spectral analysis while the main concentrator continues RX/TX operations.

### SignalUtils (`signal_utils.py`)

Utility module for signal processing:

- RSSI averaging and filtering
- SNR calculation helpers
- dBm conversion functions
- Rolling buffer statistics

### VirtualLoRaRadio (`virtual_radio.py`)

Abstraction layer that presents each configured channel as an independent "virtual radio" to the pyMC_core framework.

**Concept:** The SX1302 concentrator is a single physical radio, but MeshCore expects each channel to behave like a separate radio interface. VirtualLoRaRadio bridges this gap:

```
Physical Reality:          Virtual Abstraction:
┌──────────────┐           ┌──────────────┐
│   SX1302     │           │ VirtualRadio │  ← Channel A (SF8, 869.461 MHz)
│  + SX1250_0  │    →      │ VirtualRadio │  ← Channel B (SF7, 869.588 MHz)
│  + SX1250_1  │           │ VirtualRadio │  ← Channel D (SF7, 869.300 MHz)
└──────────────┘           └──────────────┘
```

Each VirtualLoRaRadio instance:
- Has its own RX callback for received packets
- Routes TX through the backend's UDP interface
- Maintains per-channel statistics
- Maps to a specific IF chain in the SX1302

## pyMC_Repeater Modifications

The following files are added or modified in the `repeater/` directory:

### BridgeEngine (`bridge_engine.py`)

Cross-channel packet bridge with rule-based forwarding and deduplication.

**Packet processing flow:**

1. **RX event** received from a VirtualLoRaRadio
2. **Dedup check** — hash of packet payload compared against recently seen packets
3. **Bridge rules evaluation** — which rules match this source channel?
4. **Packet-type filtering** — per-rule filtering based on MeshCore header:
   - Route types: TFLOOD, FLOOD, DIRECT, TDIRECT
   - Payload types: REQ, RESP, TXT, ACK, ADVERT, GRP_TXT, etc.
5. **Repeater handler** — MeshCore-specific processing:
   - Hop count incremented by 1
   - Path bytes updated
   - Packet size adjusted
6. **TX batch window** (2 seconds) — all target channels queued simultaneously
7. **Fire sends** — concurrent TX to all target channel TX queues

**Additional processing:**

- **ADVERT neighbor tracking** — When a packet of type ADVERT (0x04) is received, the bridge engine extracts the sender information and logs it to the `adverts` table in SQLite. This provides a neighbor discovery mechanism, tracking which MeshCore nodes are visible on each channel.
- **Packet logging** — All bridged packets (source channel, target channel, RSSI, SNR, payload type, timestamp) are stored in the `packets` table in SQLite. This data powers the recent packets API (`/api/recent_packets`) and the dashboard packet display.


**MeshCore header parsing:**

```
Byte 0: [VER(2 bits) | TYPE(4 bits) | ROUTE(2 bits)]
  bits 0-1  = route type (TFLOOD=0, FLOOD=1, DIRECT=2, TDIRECT=3)
  bits 2-5  = payload type (REQ=0, RESP=1, TXT=2, ACK=3, ADVERT=4, ...)
  bits 6-7  = protocol version
```

### ConfigManager (`config_manager.py`)

Manages configuration loading, validation, and synchronization:

- Loads `config.yaml` for service settings
- Loads `wm1303_ui.json` for channel/bridge SSOT
- 5-second cache TTL for hot-reload without restart
- Validates configuration consistency
- Generates derived configs (`global_conf.json`, `bridge_conf.json`)

### Engine (`engine.py`)

Core repeater engine modifications:

- Integration with WM1303Backend as hardware driver
- VirtualLoRaRadio instantiation per channel
- Bridge Engine initialization and lifecycle management
- Graceful shutdown handling

### PacketRouter (`packet_router.py`)

Packet routing between the repeater engine and the bridge:

- Routes RX packets to the bridge engine
- Handles MeshCore protocol specifics (route types, path tracking)
- Manages the node identity for the repeater

### IdentityManager (`identity_manager.py`)

Manages the repeater's MeshCore node identity:

- Auto-generates identity key on first run
- Stores in `config.yaml` (persisted across restarts)
- Provides the repeater's node name and address
- Handles JWT token generation for API authentication

### Additional Components

| File | Purpose |
|------|----------|
| `main.py` | Application entry point — argument parsing, service initialization |
| `config.py` | Configuration data classes and defaults |
| `data_acquisition/sqlite_handler.py` | SQLite database handler for metrics storage |
| `data_acquisition/storage_collector.py` | Background data collection for statistics |
| `web/http_server.py` | HTTP and WebSocket server (port 8000) |
| `web/wm1303_api.py` | REST API endpoints for WM1303 Manager |
| `web/spectrum_collector.py` | Spectral scan data collection and processing |
| `web/cad_calibration_engine.py` | CAD threshold calibration logic |
| `web/html/wm1303.html` | WM1303 Manager UI (single-page application) |

## WM1303 Backend — Role and Integration

The WM1303 Backend serves as the integration layer between three components:

```
┌─────────────────┐     ┌────────────────┐     ┌──────────────────┐
│  lora_pkt_fwd   │◄───►│ WM1303 Backend │◄───►│  pyMC_Repeater   │
│  (C binary)     │ UDP │ (Python)       │     │  (Python)        │
│                 │     │                │     │                  │
│  HAL → Radio    │     │  UDP handler   │     │  Bridge Engine   │
│  Spectral scan  │     │  Channel mgmt  │     │  Packet Router   │
│  TX/RX          │     │  Noise floor   │     │  Config Manager  │
│                 │     │  RX Watchdog   │     │  Web UI/API      │
└─────────────────┘     └────────────────┘     └──────────────────┘
```

The Backend:
1. **Starts** the packet forwarder as a subprocess
2. **Listens** on UDP port 1730 for PUSH_DATA (RX packets)
3. **Parses** the JSON payload and dispatches to the correct VirtualLoRaRadio
4. **Sends** PULL_RESP (TX packets) when the bridge engine decides to forward
5. **Monitors** the radio health via the RX Watchdog
6. **Manages** noise floor measurements via the NoiseFloorMonitor

## TX/RX Echo Prevention

When the bridge forwards a packet from Channel A to Channel B, the transmitted packet on Channel B could be received back by the SX1302 ("self-echo"). This would create an infinite forwarding loop.

**Prevention mechanism:**

1. Before TX, compute SHA-256 hash of the packet payload
2. Store hash in a "self-echo" set with timestamp
3. When RX receives a packet, compute its hash
4. If hash matches a recent self-echo entry → discard (it is our own TX)
5. Self-echo entries expire after the dedup TTL window

This is separate from the bridge dedup system, which prevents external duplicate packets from being forwarded multiple times.

## Bridge Rules (SSOT)

Bridge rules define which channels forward packets to which other channels. They are stored in `wm1303_ui.json` as the Single Source of Truth.

### Rule Structure

```json
{
    "bridge": {
        "rules": [
            {
                "id": "r1774374816452",
                "source": "channel_a",
                "handler": "repeater",
                "target": "channel_a",
                "packet_types": ["all"]
            },
            {
                "id": "r1774374817571",
                "source": "channel_a",
                "handler": "repeater",
                "target": "channel_d",
                "packet_types": ["FLOOD", "TFLOOD", "ADVERT"]
            }
        ]
    }
}
```

### Rule Fields

| Field | Description |
|-------|-------------|
| `id` | Unique rule identifier (auto-generated) |
| `source` | Source channel for matching RX packets |
| `handler` | Processing mode — `repeater` (modify hop/path) or `direct` (pass-through) |
| `target` | Destination channel for TX |
| `packet_types` | Which MeshCore packet types to forward (`all` or specific list) |

### Packet Type Selection

Each rule can filter by MeshCore packet types:

| Type | Code | Description |
|------|------|-------------|
| REQ | 0 | Request |
| RESP | 1 | Response |
| TXT | 2 | Text message |
| ACK | 3 | Acknowledgment |
| ADVERT | 4 | Node advertisement |
| GRP_TXT | 5 | Group text |
| GRP_DATA | 6 | Group data |
| ANON | 7 | Anonymous |
| PATH | 8 | Path discovery |
| TRACE | 9 | Route trace |
| MULTI | 10 | Multi-part |
| CTRL | 11 | Control |
| RAW | 15 | Raw data |

## Deduplication System

The dedup system prevents the same packet from being forwarded multiple times:

1. **Hash computation:** SHA-256 of the full packet payload
2. **Dedup window:** Configurable TTL (default 300 seconds in config.yaml)
3. **Hash storage:** In-memory dictionary with timestamps
4. **Check:** Before forwarding, check if hash exists in the dedup store
5. **Expiry:** Old entries are periodically cleaned up
6. **Statistics:** Dedup events are logged to SQLite for the dedup chart

The dedup system was refined multiple times during development:
- Initially too broad (rejected legitimate similar packets)
- Then too narrow (missed actual duplicates)
- Final tuning balances sensitivity with false positive prevention

## Watchdog System (3-Mode Detection)

The RX Watchdog continuously monitors radio health:

| Detection Mode | Trigger Condition | Action |
|---------------|-------------------|--------|
| **PUSH_DATA statistics** | 2 consecutive intervals with `rxnb=0` while TX is active (~60s) | Restart packet forwarder |
| **RSSI spike detection** | 5+ strong signals received but no successful RX decode (~60s) | Restart packet forwarder |
| **RX timeout** | No RX packet received for 180 seconds | Restart packet forwarder |

When triggered, the watchdog:
1. Logs the detection event
2. Stops the packet forwarder process
3. Executes the GPIO reset script
4. Restarts the packet forwarder
5. Clears internal counters

## Database Logging

The system uses SQLite (`/var/lib/pymc_repeater/repeater.db`) for persistent data storage:

| Table | Purpose |
|-------|----------|
| `packets` | Bridged packet log — source/target channel, RSSI, SNR, payload type. Powers the recent packets API and dashboard |
| `adverts` | ADVERT packet tracking — neighbor node advertisements with sender info and timestamps |
| `noise_floor` | Noise floor measurements from the SX1261 spectral scan (timestamp + dBm) |
| `crc_errors` | CRC error event log with timestamps and counts |
| `dedup_events` | Deduplication events for the dedup chart |
| `transport_keys` | Transport encryption key storage |
| `api_tokens` | API authentication token storage |

Data is used for:
- Dashboard charts and graphs in the WM1303 Manager UI
- Recent bridged packets display via `/api/recent_packets`
- Noise floor history charts via `/api/wm1303/noise_floor_history`
- Historical trend analysis
- Signal quality monitoring over time
- LBT/CAD decision visualization

## JWT Token Authentication

The pyMC_Repeater generates a JWT (JSON Web Token) secret on first run, stored in `config.yaml`:

```yaml
repeater:
  security:
    jwt_secret: <auto-generated>
    jwt_expiry_minutes: 10080  # 7 days
    admin_password: <set during install or auto-generated>
```

The JWT token is used for:
- Authenticating REST API requests
- WM1303 Manager UI login
- Protecting configuration changes

During installation, the JWT secret from the pyMC_Repeater setup is linked into the WM1303 configuration to enable seamless authentication between components.

---

*See also: [TX Queue & Scheduling](tx_queue.md) | [WM1303 API](api.md) | [WM1303 Manager UI](ui.md)*
