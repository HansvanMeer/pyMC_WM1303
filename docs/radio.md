# Radio Architecture

> Detailed description of the WM1303 radio topology, channel model, and RF behavior

## Radio Topology

The WM1303 Pi HAT contains the following radio components:

| Chip | Role | SPI Device | Capabilities |
|------|------|------------|---------------|
| **SX1302** | Baseband processor | `/dev/spidev0.0` | 8 IF demodulators, AGC, timestamp engine |
| **SX1250_0** (RF0) | Front-end radio 0 | via SX1302 | TX + RX, center freq configurable |
| **SX1250_1** (RF1) | Front-end radio 1 | via SX1302 | RX only, center freq configurable |
| **SX1261** | Companion radio | `/dev/spidev0.1` | Full RX/TX, spectral scan, LBT, CAD |

## 5-Channel Model

Since v2.0.0, the system operates as a **5-channel platform**:

| Channel | Radio Chain | IF Chain | Backend | Max BW |
|---------|------------|----------|---------|--------|
| **Channel A** | SX1250 (RF0 or RF1) | chan_multiSF_0 | VirtualLoRaRadio | 125 kHz |
| **Channel B** | SX1250 (RF0 or RF1) | chan_multiSF_1+ | VirtualLoRaRadio | 125 kHz |
| **Channel C** | SX1250 (RF0 or RF1) | chan_multiSF_2+ | VirtualLoRaRadio | 125 kHz |
| **Channel D** | SX1250 (RF0 or RF1) | chan_multiSF_3+ | VirtualLoRaRadio | 125 kHz |
| **Channel E** | SX1261 | Dedicated | Channel E Bridge | 62.5 kHz |

> **Design guideline:** Maximum 4 channels recommended. Fewer active channels = more stable operation.

### Channel A–D: Concentrator-Backed

Channels A–D use the SX1302 concentrator's multi-channel demodulator system:

- Each channel maps to one or more IF chain demodulator slots
- All channels share the SX1250 RF front-ends (RF0 for TX+RX, RF1 for RX)
- IF chain offsets are calculated from the RF chain center frequency
- Maximum bandwidth: 125 kHz (SX1302 IF chain limitation)
- Each channel can have an independent frequency, spreading factor, and bandwidth

### Channel E: SX1261-Backed

Channel E uses the SX1261 companion chip directly:

- Fully independent RF path from the concentrator
- Supports **sub-125 kHz bandwidths** (e.g., 62.5 kHz) — unique capability
- Also used for spectral scanning, LBT measurements, and CAD when not actively receiving/transmitting
- See [`channel_e_sx1261.md`](./channel_e_sx1261.md) for full details

## RF Chain Configuration

The SX1302 has two RF chains (radio front-ends):

| RF Chain | Radio | Capabilities | Typical Use |
|----------|-------|-------------|-------------|
| RF0 | SX1250_0 | TX + RX | Primary TX chain + RX |
| RF1 | SX1250_1 | RX only | Additional RX coverage |

Each RF chain has a center frequency. The IF chain demodulators are configured as **offsets** from their assigned RF chain center frequency. The maximum IF offset is ±250 kHz from center.

### Bridge Configuration Generation

The `_generate_bridge_conf()` function reads channel definitions from `wm1303_ui.json` and calculates:

1. RF chain center frequency (auto-calculated from active channels or manually set)
2. IF chain offsets for each channel relative to the RF center
3. IF chain enable/disable state — **unused slots are disabled** (critical fix in v2.0.5)

> **Important (v2.0.5):** Unused IF demodulator slots (`chan_multiSF_1` through `_7`) must have `enable: false`. A bug in earlier versions set these to `enable: true`, causing phantom packets that flooded the bridge engine.

## VirtualLoRaRadio

`VirtualLoRaRadio` is the per-channel radio abstraction used by the bridge/repeater stack:

- Implements the standard `LoRaRadio` interface (send, receive, sleep, rssi, snr)
- Each instance represents one logical channel
- Contains an async RX queue with thread-safe enqueue
- Provides per-channel noise floor estimation from RSSI history
- TX operations delegate to `WM1303Backend.send()` which handles the UDP PULL_RESP path
- One VirtualLoRaRadio instance per active channel (A–D)

## MeshCore Sync Word

MeshCore uses a specific LoRa sync word that differs from LoRaWAN. This is configured in the HAL/forwarder layer and ensures:

- MeshCore packets are received correctly
- LoRaWAN traffic is filtered out at the radio level
- No cross-protocol interference

## LoRaWAN Compatibility

The WM1303 system is **not compatible with LoRaWAN** in its current configuration:

- Different sync word
- Different packet format
- No LoRaWAN MAC layer
- The concentrator hardware is LoRaWAN-capable but repurposed for MeshCore

## Noise Floor Monitoring

Noise floor values are maintained **per channel** using **direct channel IDs** (not frequencies). This is critical when multiple channels share the same frequency but use different spreading factors.

### Two-Level Timing

| Layer | Interval | What |
|-------|----------|------|
| HAL spectral scan thread | ~1 second (`pace_s=1`) | SX1261 scans during TX-free windows |
| Python NoiseFloorMonitor | 30 seconds | Harvests results, calculates per-channel noise floor |

### Behavior Rules

- Noise floor monitoring **does NOT pause TX queues** (old hold behavior removed)
- Monitoring waits for TX-free windows with retry logic
- Results are persisted to database for API/UI use
- Per-channel values feed into LBT threshold decisions

### Fallback Chain

When spectral scan data is unavailable, the system falls back through:

1. **Spectral scan data** (primary — from SX1261)
2. **SX1261 RSSI point measurement** (secondary)
3. **RX packet-based estimation** (last resort)

> **Troubleshooting:** If all channels show `-120.0 dBm`, spectral scan data is not being collected. Check SX1261 availability, scan output files, and runtime permissions.

## CRC Errors

RX CRC errors indicate corrupted packets. Common causes:

- Interference from other transmitters
- Multi-path reflections
- Near-field overload (transmitter too close)
- AGC issues (see FEM/LNA/AGC section in hardware docs)

CRC errors are counted per channel and visible in the Status tab.

## TX/RX Echo

Echo detection prevents the system from processing its own transmitted packets:

### Self-Echo (Backend Level)
When a packet is transmitted, its hash is stored for 30 seconds. If the same hash appears on RX (own TX bounced back via antenna), it is discarded.

### Multi-Demod Dedup (Backend Level)
The SX1302 can demodulate the same packet via multiple IF chains simultaneously. A 2-second cache prevents duplicate dispatch.

### Cross-Channel Dedup (Bridge Level)
The bridge engine maintains a 15-second hash cache to catch the same packet received on different channels.

## Related Documents

- [`architecture.md`](./architecture.md) — System architecture
- [`channel_e_sx1261.md`](./channel_e_sx1261.md) — Channel E details
- [`lbt_cad.md`](./lbt_cad.md) — LBT and CAD behavior
- [`tx_queue.md`](./tx_queue.md) — TX queue system
- [`hardware.md`](./hardware.md) — Hardware details
