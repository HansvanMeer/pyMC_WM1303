# WM1303 Manager UI

> Web-based management interface for the WM1303 LoRa concentrator

## Overview

The WM1303 Manager is a single-page web application (`wm1303.html`) that provides real-time monitoring and configuration of the WM1303 concentrator system. It runs alongside the pyMC Console (Vue.js app) on the same HTTP server (port 8000).

### Access

```
http://<pi-ip-address>:8000/wm1303.html
```

The UI requires JWT authentication. On first access, log in with the admin password configured during installation.

### Technology Stack

| Component | Technology |
|-----------|------------|
| Frontend | Single HTML file with embedded CSS and JavaScript |
| Charts | Chart.js (dynamically loaded) with date adapter |
| Real-time | WebSocket (ws4py) for live data push |
| API | REST endpoints at `/api/wm1303/*` |
| Auth | JWT token-based authentication |

## Available Tabs

The UI is organized into tabs, each providing a focused view of system functionality:

### 1. Status Tab

Overview of the entire system:

| Section | Content |
|---------|----------|
| Version header | Software version (e.g., "v0.10.5"), dynamically loaded from `/api/wm1303/status` |
| Service status | Running/stopped, uptime, CPU usage |
| Radio summary | Total RX/TX across all channels |
| Channel status cards | Per-channel live status with signal indicators |
| RX/TX counters | Total packets received and transmitted |
| Error indicators | Red dot for active issues, green for healthy |

![Status Tab](../screenshots/status.jpg)


**Per-channel status card:**
- Channel name and frequency
- RX count, last RSSI, average RSSI
- TX count, last TX power
- Noise floor (color-coded: green < -110, yellow < -90, red >= -90 dBm). Per-channel noise floor sourced from `_channel_noise_floors` dict; global noise floor computed from `signal_quality` API
- Active/inactive indicator

### 2. Channels Tab

Detailed channel configuration and monitoring:

| Section | Content |
|---------|----------|
| Channel configuration | Frequency, SF, BW, CR, TX power, preamble length per channel |
| IF Chain Configuration | IF chain mapping, LBT/CAD enable toggles per channel |
| Radio Summary | Aggregated RX/TX statistics |
| TX airtime and duty cycle | Per-channel airtime and duty cycle percentage |
| Noise floor display | Per-channel noise floor with guard against -120 fallback |

![Channels Configuration](../screenshots/channels-1.jpg)

![IF Chain Configuration](../screenshots/channels-2.jpg)


**IF Chain Configuration block:**

Per-channel toggles for:
- LBT enable/disable
- CAD enable/disable — **CAD toggle is disabled (greyed out) when LBT is not active** for that channel. CAD depends on LBT being enabled to function correctly.
- LBT RSSI target threshold

Changes take effect within 5 seconds (auto-reload via cache TTL) without service restart.

**Channel matching:** Channel identification now requires both **frequency and spreading factor** equality. This fixes duplicate data display that occurred when two channels shared the same frequency but used different spreading factors.

### 3. Bridge Tab

Bridge rule management:

| Section | Content |
|---------|----------|
| Active rules list | Source → target channel mappings |
| Rule editor | Add/edit/delete bridge rules |
| Packet type filter | Select which MeshCore packet types to forward per rule |
| Handler selection | Repeater (modify hop/path) or Direct (pass-through) |
| Bridge statistics | Packets forwarded, duplicates detected |
| Save & restart | Save rules to SSOT and trigger service configuration reload |

![Bridge Rules](../screenshots/bridge-rules.jpg)


### 4. Spectrum Tab

RF spectrum visualization:

| Section | Content |
|---------|----------|
| Spectrum chart | Real-time spectral scan graph (863-870 MHz) |
| Waterfall view | Time-based frequency heatmap |
| Channel markers | Frequency markers for configured channels |
| Noise floor overlay | Per-channel noise floor levels |
| Zoom controls | Frequency and amplitude zoom |

The spectrum data comes from the SX1261 spectral scan (updated via NoiseFloorMonitor every 30 seconds).

![Spectrum View](../screenshots/spectrum-1.jpg)

### 5. Adv. Config Tab

Advanced configuration options for fine-tuning system behavior:

| Section | Content |
|---------|----------|
| GPIO pins | SX1302 reset, power enable, SX1261 reset, AD5338R reset, base offset — with **live sysfs GPIO state preview** showing the actual kernel GPIO numbers and current pin values |
| Dedup settings | Dedup TTL (time-to-live) for duplicate detection window |
| TX queue settings | Packet TTL, overflow policy, max cache size |
| Noise floor settings | Scan interval, TX hold duration, rolling buffer size |
| HAL advanced | Force host FE control, LNA LUT, PA LUT, AGC analog/digital gain, channelizer fixed gain |
| Advert interval | MeshCore advertisement interval |

> **Note:** The GPIO pin configuration includes a live preview panel that reads the actual sysfs GPIO state from the system. This shows the computed sysfs numbers (BCM pin + base offset) and their current HIGH/LOW state, useful for verifying hardware connections.

## Real-Time Updates

The UI uses WebSocket connections for live data:

```
ws://<pi-ip-address>:8000/ws
```

**WebSocket push events:**

| Event | Data | Frequency |
|-------|------|-----------|
| Channel stats | RX/TX counts, RSSI, SNR per channel | ~5 seconds |
| TX queue stats | Queue depth, sent/failed/dropped counts | ~5 seconds |
| Spectrum data | Spectral scan results | ~30 seconds |
| Bridge events | Forwarding activity, dedup events | Real-time |
| Noise floor | Per-channel noise floor updates | ~30 seconds |

The adaptive refresh system adjusts update frequency based on activity level — more frequent during high traffic, less frequent during idle periods.

## Chart.js Visualizations

The UI includes several interactive charts built with Chart.js:

### Signal Quality Chart

- RSSI and SNR over time per channel
- Color-coded by channel
- Time axis with auto-scaling
- Tooltip with exact values

### Spectrum Chart

- X-axis: Frequency (863-870 MHz)
- Y-axis: RSSI (dBm)
- Channel frequency markers as vertical lines
- Noise floor threshold line

### LBT Decisions Chart

- Pass/block decisions over time per channel
- RSSI values at decision points
- Threshold line visualization
- Helps tune LBT thresholds

### CAD Activity Chart

- Channel Activity Detection events per channel
- **5 datasets per channel**: HW Clear, SW Clear, HW Detected, SW Detected, Skipped
- Stat cards display **HW/SW badge** with per-source breakdown
- Data sourced from `cad_events` database table with HW/SW columns
- Periodic recorder writes HW/SW delta stats per minute per channel
- Correlation with TX queue behavior

### Dedup Chart

- Deduplication events over time
- Shows when and how often packets are detected as duplicates
- Data stored in SQLite for historical view
- Helps tune dedup window settings

![Dedup Events](../screenshots/dedup.jpg)


## Known UI Characteristics

- **Desktop-optimized layout** — Mobile responsive design is a future improvement
- **Single HTML file** — All CSS and JavaScript embedded for easy deployment
- **Chart.js race condition** — Charts wait for library load before initialization (fixed)
- **Decimal separator** — Frequency input fields use `type="text"` with `inputmode="decimal"` and automatic comma-to-dot replacement to ensure locale-independent decimal point input across all browsers
- **-120 dBm guard** — Noise floor display shows "--" instead of -120 when no data available

> **⚠️ Important:** The **Configure → Radio Settings** page in the pyMC Repeater dashboard is **NOT used** with the WM1303 setup. All radio configuration (channels, frequencies, TX power, LBT/CAD settings) is managed exclusively through the WM1303 Manager UI. Changing radio settings in the pyMC Repeater dashboard will have no effect on the WM1303 concentrator hardware.

---

*See also: [WM1303 API](api.md) | [Software Components](software.md) | [Configuration](configuration.md)*
