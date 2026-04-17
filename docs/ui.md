# WM1303 Manager UI

> Web-based management interface for the WM1303 concentrator

## Overview

The WM1303 Manager is a single-page web application (`wm1303.html`) that provides complete management of the WM1303 concentrator system. It is accessible at:

```
http://<pi-ip>:8000/wm1303.html
```

The UI communicates with the backend via the [WM1303 REST API](./api.md) and receives real-time updates via WebSocket.

## Tabs

### Status Tab

Real-time overview of system health and channel performance:

| Section | Contents |
|---------|----------|
| System Info | Hostname, CPU temp, memory, disk, uptime, kernel version |
| Active Channels | Count of active channels (up to 5 when Channel E is active) |
| Per-channel status | Frequency, BW, SF, RX/TX counts, RSSI, SNR, noise floor, duty cycle |
| Channel E status | Same metrics for the SX1261 channel |
| Signal Quality chart | Real-time per-channel RSSI and SNR |
| Packet Activity chart | Bar chart of per-channel RX/TX counts (Channel E in orange) |

### Channels Tab

Per-channel configuration interface:

#### IF Channels (A–D)
- Frequency (MHz)
- Bandwidth (kHz) — max 125 kHz
- Spreading Factor (SF7–SF12)
- Coding Rate (4/5–4/8)
- Preamble length
- TX Power (dBm)
- LBT enable + threshold
- CAD enable (greyed out when LBT is off)
- Active toggle
- Channel name (display alias only)

#### SX1261 Channel (E)
Same parameters as above, plus:
- RX Boost toggle
- Bandwidth limited to 62.5 kHz (since v2.0.5)
- Separate configuration section for clarity

All channels use a consistent grid layout (aligned since v2.0.0).

### Bridge Tab

Bridge rules management:

- List of all bridge rules
- Source → target channel mapping
- Packet type filter per rule (all, advert, text, position, path)
- Enable/disable toggle per rule
- Add / edit / delete rules
- Rules read from and written to `wm1303_ui.json` SSOT

### Spectrum Tab

Spectral analysis and channel monitoring charts:

| Chart | Description |
|-------|-------------|
| **RSSI History** | Per-channel RSSI values over time |
| **SNR History** | Per-channel SNR values over time |
| **Noise Floor** | Per-channel noise floor from spectral scan |
| **CAD Chart** | Per-channel CAD detection events (HW/SW source) |
| **LBT History** | Per-channel LBT events with threshold visualization |
| **TX Activity** | Per-channel TX counts and duty cycle |
| **Dedup Chart** | Deduplication event visualization |

Channel E is displayed in **orange** across all charts (since v2.0.1).

Extended color palette ensures all 5 channels are visually distinct.

### Advanced Config Tab

Advanced system configuration:

| Section | Contents |
|---------|----------|
| GPIO Pins | Reset, power, SX1261 NSS, AD5338R reset pin assignments |
| RF Chains | RF0/RF1 center frequencies |
| IF Chains | Per-IF-chain enable, frequency offset, radio assignment |
| SPI Settings | Speed, burst size (informational) |
| System Actions | Restart service, restart pkt_fwd, hardware reset |

## UI Behavior Notes

### Channel Names Are Aliases
Channel names shown in the UI are **display aliases only**. Internal logic and API responses use stable channel identifiers (`channel_a` through `channel_e`). Users can rename channels freely without affecting system operation.

### CAD Toggle Dependency
The CAD toggle for any channel is **greyed out and disabled** when LBT is turned off for that channel. This enforces the rule that CAD only operates when LBT is active. Enabling LBT automatically unlocks the CAD toggle.

### Configuration Persistence
- UI changes are saved to `wm1303_ui.json` (SSOT) via the API
- Changes take effect within **5 seconds** (cache TTL auto-reload)
- No service restart required for most settings
- Bridge rule changes take effect immediately

### Frequency Display
All frequencies are displayed in **MHz** throughout the UI. Internal storage uses Hz.

### Bandwidth Display
All bandwidths are displayed in **kHz** throughout the UI. Internal storage uses Hz.

### Channel E Bandwidth
Since v2.0.5, the Channel E bandwidth dropdown is limited to **62.5 kHz** only. The 125/250/500 kHz options were removed because the SX1261 RX path through the concentrator HAL only supports sub-125 kHz bandwidths.

### RX Boost
The RX Boost toggle (Channel E) enables enhanced RX sensitivity on the SX1261. It is positioned before the Active toggle for better workflow (since v2.0.0).

## Technology Stack

| Component | Technology |
|-----------|------------|
| UI framework | Vanilla HTML/JavaScript/CSS |
| Charts | Chart.js |
| Data transport | REST API + WebSocket |
| Auto-refresh | WebSocket-driven (no polling) |

## Screenshots

Screenshots of the interface are available in the `/screenshots/` directory:

| File | Shows |
|------|---------|
| `status.jpg` | Status tab with channel overview |
| `channels-1.jpg` | Channel configuration (IF channels) |
| `channels-2.jpg` | Channel configuration (SX1261 channel) |
| `bridge-rules.jpg` | Bridge rules management |
| `spectrum-1.jpg` | Spectrum tab with charts |
| `dedup.jpg` | Deduplication event chart |

## Related Documents

- [`api.md`](./api.md) — REST API reference
- [`configuration.md`](./configuration.md) — Configuration files
- [`lbt_cad.md`](./lbt_cad.md) — LBT/CAD behavior and UI interaction
