# WM1303 REST API

> API reference for the WM1303 Manager backend endpoints

## Overview

The WM1303 API provides REST endpoints for managing the concentrator, channels, bridge rules, spectral data, and system status. All endpoints are mounted under `/api/wm1303/` on the pyMC Repeater HTTP server (port 8000).

## Authentication

All API endpoints require a valid **JWT token** from the pyMC Repeater authentication system. The token is obtained during the initial pyMC Repeater setup and stored in the repeater configuration.

## Base URL

```
http://<pi-ip>:8000/api/wm1303/
```

## Status Endpoints

### GET `/api/wm1303/status`

Returns the current system status including all channel statistics.

**Response:**
```json
{
  "status": "running",
  "version": "2.0.5",
  "uptime": 86400,
  "pkt_fwd_status": "running",
  "channels": {
    "channel_a": {
      "active": true,
      "frequency": 869525000,
      "bandwidth": 125000,
      "spreading_factor": 12,
      "rx_packets": 1523,
      "tx_packets": 487,
      "rx_crc_errors": 12,
      "rssi_last": -78.5,
      "snr_last": 8.2,
      "noise_floor": -115.3,
      "tx_duty_cycle": 0.42
    },
    "channel_b": { ... },
    "channel_c": { ... },
    "channel_d": { ... },
    "channel_e": {
      "active": true,
      "frequency": 869525000,
      "bandwidth": 62500,
      "spreading_factor": 12,
      "rx_packets": 23,
      "tx_packets": 8,
      "noise_floor": -118.7,
      "rx_boost": true
    }
  },
  "active_channels": 5,
  "system": {
    "cpu_temp": 48.2,
    "memory_used_pct": 34.5,
    "disk_used_pct": 12.8,
    "uptime_seconds": 86400
  }
}
```

### GET `/api/wm1303/health`

Simple health check endpoint.

**Response:**
```json
{ "ok": true, "version": "2.0.5" }
```

## Channel Configuration Endpoints

### GET `/api/wm1303/channels`

Returns configuration of all channels (reads from `wm1303_ui.json` SSOT).

### PUT `/api/wm1303/channels/<channel_id>`

Update a channel's configuration.

**Path parameters:**
- `channel_id` — `channel_a` through `channel_e`

**Request body:**
```json
{
  "frequency": 869525000,
  "bandwidth": 125000,
  "spreading_factor": 12,
  "coding_rate": 8,
  "preamble": 32,
  "tx_power": 20,
  "lbt_enabled": true,
  "lbt_threshold": -75,
  "cad_enabled": true,
  "active": true,
  "name": "SF12-868"
}
```

Channel E additionally supports:
```json
{
  "rx_boost": true,
  "bandwidth": 62500
}
```

**Behavior:**
1. Writes to `wm1303_ui.json` (SSOT)
2. Syncs active channels to `config.yaml`
3. Changes take effect within 5 seconds (cache TTL auto-reload)

### POST `/api/wm1303/channels/<channel_id>/toggle`

Toggle a channel active/inactive.

## Bridge Rule Endpoints

### GET `/api/wm1303/bridge/rules`

Returns all bridge rules from the SSOT.

**Response:**
```json
{
  "rules": [
    {
      "id": "rule_1",
      "source": "channel_a",
      "target": "channel_b",
      "types": ["all"],
      "enabled": true
    }
  ]
}
```

### POST `/api/wm1303/bridge/rules`

Create a new bridge rule.

### PUT `/api/wm1303/bridge/rules/<rule_id>`

Update an existing bridge rule.

### DELETE `/api/wm1303/bridge/rules/<rule_id>`

Delete a bridge rule.

### POST `/api/wm1303/bridge/rules/<rule_id>/toggle`

Toggle a bridge rule enabled/disabled.

## Spectrum / Noise Floor Endpoints

### GET `/api/wm1303/spectrum`

Returns current spectral scan data for all channels.

**Response includes:**
- Per-channel RSSI values
- Per-channel noise floor estimates
- Scan timestamp
- Number of scan points

### GET `/api/wm1303/spectrum/history`

Returns noise floor history for charting.

**Query parameters:**
- `period` — Time window (`1h`, `6h`, `24h`, `7d`)
- `channel` — Optional channel filter (`channel_a` through `channel_e`)

### GET `/api/wm1303/spectrum/cad`

Returns CAD event history.

**Response includes:**
- Per-channel CAD events with timestamps
- Hardware vs software detection source
- Event count per channel

### GET `/api/wm1303/spectrum/lbt`

Returns LBT event history.

## Deduplication Endpoints

### GET `/api/wm1303/dedup/stats`

Returns deduplication statistics.

**Response:**
```json
{
  "total_seen": 15234,
  "total_dropped_duplicate": 892,
  "total_dropped_echo": 67,
  "total_forwarded": 14275,
  "per_channel": {
    "channel_a": { "rx": 5234, "dedup": 312, "forwarded": 4922 },
    "channel_b": { ... }
  }
}
```

### GET `/api/wm1303/dedup/events`

Returns recent dedup events for the dedup chart.

**Query parameters:**
- `period` — Time window
- `limit` — Max events

## System Endpoints

### GET `/api/wm1303/system/info`

Returns Raspberry Pi system information.

**Response:**
```json
{
  "hostname": "raspberrypi",
  "cpu_temp": 48.2,
  "memory_total_mb": 3904,
  "memory_used_pct": 34.5,
  "disk_total_gb": 29.7,
  "disk_used_pct": 12.8,
  "uptime_seconds": 86400,
  "kernel": "6.6.20+rpt-rpi-v8",
  "python_version": "3.11.2"
}
```

### POST `/api/wm1303/system/restart`

Restart the pymc-repeater service.

### POST `/api/wm1303/system/restart-pktfwd`

Restart only the lora_pkt_fwd process.

### POST `/api/wm1303/system/reset-hardware`

Trigger a hardware reset (GPIO power cycle of the SX1302).

## Configuration Endpoints

### GET `/api/wm1303/config`

Returns the full `wm1303_ui.json` configuration.

### PUT `/api/wm1303/config`

Replace the full `wm1303_ui.json` configuration.

> **Caution:** This replaces the entire configuration. Use channel-specific endpoints for partial updates.

### GET `/api/wm1303/config/rf-chains`

Returns RF chain configuration.

### GET `/api/wm1303/config/if-chains`

Returns IF chain configuration.

### GET `/api/wm1303/config/advanced`

Returns advanced settings (GPIO, SPI, paths).

## WebSocket

The system pushes real-time updates via WebSocket:

| Event | Data | Frequency |
|-------|------|-----------|
| `channel_stats` | Per-channel RX/TX/RSSI/SNR | ~1 second |
| `noise_floor` | Per-channel noise floor values | ~30 seconds |
| `dedup_event` | Individual dedup/echo events | Real-time |
| `cad_event` | CAD detection events | Real-time |
| `system_status` | pkt_fwd status, warnings | On change |

## Error Responses

```json
{
  "error": "Channel not found",
  "code": 404
}
```

| Code | Meaning |
|------|---------|
| 200 | Success |
| 400 | Invalid request / validation error |
| 401 | Missing or invalid JWT token |
| 404 | Resource not found |
| 500 | Internal server error |

## Rate Limiting

No rate limiting is applied. The API is intended for local network access only.

## Related Documents

- [`configuration.md`](./configuration.md) — Configuration files and SSOT model
- [`ui.md`](./ui.md) — WM1303 Manager UI (consumes this API)
- [`architecture.md`](./architecture.md) — System architecture
