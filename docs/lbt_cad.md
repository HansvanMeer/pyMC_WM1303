# LBT and CAD

> Listen Before Talk and Channel Activity Detection in the WM1303 system

## Overview

The WM1303 system implements two complementary channel-sensing mechanisms:

- **LBT (Listen Before Talk)** — Measures RSSI before transmitting; delays TX if channel is busy
- **CAD (Channel Activity Detection)** — Detects LoRa preamble activity on the channel

Both are configured **per channel** and apply to the per-channel TX queues.

## Core Rule: CAD Depends on LBT

**CAD is only active when LBT is enabled for the same channel.**

This dependency is enforced in:
- The backend TX queue logic
- The WM1303 Manager UI (CAD toggle greyed out when LBT is off)
- The API validation

Rationale: CAD without LBT provides incomplete information. LBT measures raw RSSI (any signal), while CAD specifically detects LoRa preambles. Together they provide a more complete picture.

## LBT — Listen Before Talk

### How It Works

1. Before transmitting a queued packet, the TX queue checks if LBT is enabled for that channel
2. If enabled, an RSSI measurement is performed
3. The measured RSSI is compared to the LBT threshold (configurable per channel)
4. If RSSI > threshold → channel is busy → TX is delayed
5. If RSSI ≤ threshold → channel is clear → TX proceeds

### Configuration (per channel)

| Parameter | Description | Typical Value |
|-----------|------------|---------------|
| `lbt_enabled` | Enable/disable LBT | `true` / `false` |
| `lbt_threshold` | RSSI threshold in dBm | `-80` to `-65` |

### RSSI Measurement Path

For Channels A–D:
- Uses noise floor data from the spectral scan (SX1261-derived)
- Updated every 30 seconds by the NoiseFloorMonitor

For Channel E:
- SX1261 can perform direct RSSI measurement on its own frequency
- Provides more immediate channel assessment

## CAD — Channel Activity Detection

### How It Works

1. Only checked if LBT is also enabled for this channel
2. CAD specifically detects LoRa preamble presence
3. A positive CAD detection means a LoRa transmission is in progress
4. The TX queue delays transmission until CAD clears

### Configuration (per channel)

| Parameter | Description |
|-----------|------------|
| `cad_enabled` | Enable/disable CAD (requires LBT enabled) |

### CAD Event Tracking

CAD events are tracked per channel with both hardware and software source attribution:

- **Hardware CAD** — Direct detection from the radio hardware
- **Software CAD** — Detection from signal analysis

Events are stored in SQLite and displayed in the Spectrum tab CAD chart.

## TX Hold Behavior (Current State)

The TX hold model has evolved. The **current** behavior is:

| Hold Type | Status | Duration | Purpose |
|-----------|--------|----------|--------|
| TX batch window | ✅ Active | 2 seconds | Group concurrent bridge sends |
| Noise floor hold | ❌ Removed | — | Was: pause TX for noise measurement |
| LBT hold | ✅ Active (if enabled) | Until channel clear | Wait for clear channel |
| CAD hold | ✅ Active (if LBT+CAD enabled) | Until no preamble | Wait for no activity |
| Queue depth hold | ✅ Active | 100ms (1 pkt) to 2s (batch) | Brief dedup window |

### What Changed

In earlier versions, the noise floor monitor would pause TX queues during measurement. This was removed because:

1. It violated the "TX ASAP" design principle
2. Noise floor monitoring happens every 30 seconds — pausing TX that often is unacceptable
3. The SX1261 spectral scan runs on a separate SPI path and does not require TX silence
4. The NoiseFloorMonitor now waits for TX-free windows with retry logic instead

## SPI Impact on LBT Latency

LBT RSSI checks add minimal TX latency because:

- The SX1261 is on a **separate SPI bus** (`/dev/spidev0.1`)
- RSSI reads are fast (single register read)
- The main concentrator path is not affected
- SPI optimizations (16 MHz) further reduce overhead

## UI Behavior

### LBT Controls
- Available on all channels (A–E)
- Toggle enable/disable + threshold slider
- Changes apply within 5 seconds (cache TTL auto-reload)

### CAD Controls
- **Disabled/greyed out** when LBT is off for that channel
- Enabling LBT unlocks the CAD toggle
- This dependency is enforced in the UI JavaScript and validated by the API

### Charts

The Spectrum tab includes:

| Chart | Shows |
|-------|-------|
| LBT History | Per-channel LBT events with threshold |
| CAD chart | Per-channel CAD detections (HW/SW source) |
| Noise Floor | Per-channel noise floor values over time |

Channel E is shown in **orange** in all charts (since v2.0.1).

## Noise Floor Interaction

Noise floor values are important for LBT because:

- They provide baseline RSSI for each channel
- LBT threshold can be set relative to the noise floor
- Per-channel noise floor tracking prevents cross-channel interference in threshold decisions

### Noise Floor Sources (Fallback Chain)

1. **Spectral scan data** — Primary, from SX1261 scan
2. **SX1261 RSSI point measurement** — Secondary
3. **RX packet-based estimation** — Last resort

All values are persisted to database and exposed through API/WebSocket.

## Troubleshooting

### LBT blocking all TX
- Check noise floor values — if stuck at `-120 dBm`, scan data is not being collected
- Verify SX1261 is operational
- Check LBT threshold — too low means TX will always be blocked

### CAD toggle unresponsive
- Verify LBT is enabled for that channel first
- Check browser console for API errors

### High LBT reject rate
- Channel may be genuinely busy
- Threshold may need adjustment
- Check for interference sources

## Related Documents

- [`tx_queue.md`](./tx_queue.md) — TX queue system
- [`radio.md`](./radio.md) — Radio architecture
- [`ui.md`](./ui.md) — WM1303 Manager UI
- [`configuration.md`](./configuration.md) — Configuration files
- [`channel_e_sx1261.md`](./channel_e_sx1261.md) — Channel E / SX1261
