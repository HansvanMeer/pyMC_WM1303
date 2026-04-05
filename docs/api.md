# WM1303 API

> REST API endpoints for WM1303 concentrator management

## Overview

The WM1303 API is a CherryPy-based REST interface mounted at `/api/wm1303/`. It provides endpoints for monitoring, configuration, and control of the WM1303 concentrator system.

### Base URL

```
http://<pi-ip-address>:8000/api/wm1303/
```

### Authentication

All API endpoints require JWT (JSON Web Token) authentication.

**Obtaining a token:**

```bash
curl -X POST http://<pi-ip>:8000/api/auth/login \
  -H "Content-Type: application/json" \
  -d '{"password": "<admin_password>"}'
```

Response:
```json
{
    "token": "eyJhbGciOiJIUzI1NiIs...",
    "expires_in": 604800
}
```

**Using the token:**

```bash
curl http://<pi-ip>:8000/api/wm1303/status \
  -H "Authorization: Bearer <token>"
```

Tokens expire after 7 days (configurable via `jwt_expiry_minutes` in config.yaml).

### Response Format

All endpoints return JSON with `Content-Type: application/json`. CORS headers are included for cross-origin access.

## Endpoint Reference

### Status

#### GET /api/wm1303/status

Returns comprehensive system status including radio state, channel statistics, and service health.

**Response:**

```json
{
    "service": {
        "running": true,
        "uptime_seconds": 86400,
        "version": "v0.9.315"
    },
    "radio": {
        "spi_path": "/dev/spidev0.0",
        "model": "SX1302",
        "hal_version": "2.10"
    },
    "channels": [
        {
            "name": "ch-1",
            "friendly_name": "Channel A",
            "frequency": 869461000,
            "active": true,
            "rx_count": 1523,
            "tx_count": 892,
            "last_rssi": -87.5,
            "avg_rssi": -92.3,
            "last_snr": 8.2,
            "noise_floor": -93.5
        }
    ],
    "totals": {
        "rx_total": 4521,
        "tx_total": 2668,
        "crc_ok": 4490,
        "crc_bad": 31
    }
}
}
```

#### GET /api/wm1303/version

Returns the current software version from the VERSION file (`/etc/pymc_repeater/version`).

**Response:**

```json
{
    "version": "0.10.5"
}
```

### Channels

#### GET /api/wm1303/channels

Returns channel configuration from the SSOT (`wm1303_ui.json`).

**Response:**

```json
{
    "channels": [
        {
            "name": "ch-1",
            "friendly_name": "Channel A",
            "frequency": 869461000,
            "bandwidth": 125000,
            "spreading_factor": 8,
            "coding_rate": "4/5",
            "tx_enabled": true,
            "tx_power": 14,
            "active": true,
            "preamble_length": 17,
            "lbt_enabled": true,
            "cad_enabled": false,
            "lbt_rssi_target": -80
        }
    ]
}
```

#### POST /api/wm1303/channels

Update channel configuration. Changes are saved to the SSOT and take effect within 5 seconds (cache TTL).

**Request body:**

```json
{
    "channels": [
        {
            "name": "ch-1",
            "frequency": 869461000,
            "bandwidth": 125000,
            "spreading_factor": 8,
            "coding_rate": "4/5",
            "tx_enabled": true,
            "tx_power": 14,
            "active": true,
            "preamble_length": 17
        }
    ]
}
```

**Response:** `{"ok": true}`

The response may include a `warnings` array when channel configuration issues are detected:

```json
{
    "ok": true,
    "warnings": [
        "Channel 'Channel B' IF offset 850000 Hz exceeds ±730 kHz limit — channel force-disabled",
        "Channel 'Channel D' is out of IF range — channel force-disabled"
    ]
}
```

**IF range validation rules:**
- Center frequency is calculated from **active channels only** (inactive channels are excluded from the calculation)
- IF range limit is **±730 kHz** (matching SX1302 HAL demodulator bandwidth), corrected from the earlier ±490 kHz limit
- Channels whose IF offset exceeds the limit are **gracefully force-disabled** instead of crashing the service
- Warnings are returned in the API response to inform the user which channels were affected

#### GET /api/wm1303/channels/live

Returns live channel statistics including real-time RX/TX counts, signal quality, noise floor, TX queue depths, and LBT/CAD status.

**Response:**

```json
{
    "channels": {
        "ch-1": {
            "rx_count": 1523,
            "tx_count": 892,
            "tx_sent": 890,
            "tx_failed": 2,
            "tx_dropped": 5,
            "last_rssi": -87.5,
            "avg_rssi": -92.3,
            "last_snr": 8.2,
            "noise_floor": -112.0,
            "queue_depth": 0,
            "lbt_pass": 887,
            "lbt_block": 3,
            "cad_clear": 885,
            "cad_detected": 5
        }
    },
    "dedup": {
        "total": 156,
        "window_seconds": 300
    },
    "watchdog": {
        "restarts": 0,
        "last_rx_age_seconds": 3.2
    }
}
```

### IF Chains

#### GET /api/wm1303/ifchains

Returns the IF chain configuration derived from `global_conf.json`, including frequency offsets, enable status, and RF chain assignment.

**Response:**

```json
{
    "if_chains": [
        {
            "index": 0,
            "enabled": true,
            "radio": 0,
            "if_offset": 73750,
            "frequency": 869461000,
            "channel": "ch-1"
        }
    ]
}
```

### Bridge

#### GET /api/wm1303/bridge

Returns bridge rule configuration from the SSOT.

**Response:**

```json
{
    "rules": [
        {
            "id": "r1774374816452",
            "source": "channel_a",
            "handler": "repeater",
            "target": "channel_d",
            "packet_types": ["all"]
        }
    ]
}
```

#### POST /api/wm1303/bridge

Update bridge rules. Saves to SSOT and regenerates `bridge_conf.json`.

**Request body:**

```json
{
    "rules": [
        {
            "source": "channel_a",
            "handler": "repeater",
            "target": "channel_d",
            "packet_types": ["FLOOD", "TFLOOD", "ADVERT"]
        }
    ]
}
```

**Response:** `{"ok": true}`

### RF Chains

#### GET /api/wm1303/rfchains

Returns RF chain configuration including radio type, frequency, TX enable, RSSI offset, and TX gain LUT.

**Response:**

```json
{
    "rf_chains": [
        {
            "index": 0,
            "enabled": true,
            "type": "SX1250",
            "frequency": 869387250,
            "tx_enable": true,
            "rssi_offset": -215.4,
            "role": "TX + RX",
            "if_channels": [0, 1, 2]
        },
        {
            "index": 1,
            "enabled": true,
            "type": "SX1250",
            "frequency": 869387250,
            "tx_enable": false,
            "rssi_offset": -215.4,
            "role": "RX only",
            "if_channels": []
        }
    ]
}
```

#### POST /api/wm1303/rfchains

Update RF chain configuration (frequency, RSSI offset). Triggers `global_conf.json` regeneration.

### TX Queues

#### GET /api/wm1303/tx_queues

Returns per-channel TX queue status and statistics.

**Response:**

```json
{
    "queues": {
        "ch-1": {
            "depth": 0,
            "max_size": 15,
            "ttl_seconds": 5,
            "sent": 890,
            "failed": 2,
            "dropped_ttl": 3,
            "dropped_overflow": 2,
            "lbt_blocked": 3,
            "cad_blocked": 1
        }
    },
    "scheduler": {
        "inter_packet_gap_ms": 50,
        "active": true
    }
}
```

### Spectrum

#### GET /api/wm1303/spectrum

Returns the most recent SX1261 spectral scan results and SX1261 configuration.

**Response:**

```json
{
    "scan": {
        "freq_start": 863000000,
        "freq_stop": 870000000,
        "nb_chan": 36,
        "results": [
            {"freq": 863000000, "rssi": -118.5},
            {"freq": 863194444, "rssi": -117.2}
        ],
        "timestamp": "2026-04-03T14:30:00"
    },
    "sx1261_spi": "/dev/spidev0.1",
    "spectral_scan_enabled": true
}
```

#### POST /api/wm1303/spectrum

Trigger an on-demand spectral scan or update scan configuration.

**Request body (trigger scan):**

```json
{
    "action": "scan"
}
```

**Request body (update config):**

```json
{
    "spectral_scan": {
        "enable": true,
        "freq_start": 863000000,
        "freq_hz_stop": 870000000,
        "nb_chan": 36
    }
}
```

### Dedup Events

#### GET /api/wm1303/dedup_events

Returns deduplication event history from the SQLite database.

**Query parameters:**

| Parameter | Default | Description |
|-----------|---------|-------------|
| `hours` | 24 | Time window in hours |
| `limit` | 1000 | Maximum number of events |

**Response:**

```json
{
    "events": [
        {
            "timestamp": "2026-04-03T14:25:00",
            "channel": "ch-1",
            "packet_hash": "a1b2c3...",
            "action": "blocked"
        }
    ],
    "total": 156
}
```

### History Endpoints

These endpoints retrieve historical data from the SQLite database for chart rendering.

#### GET /api/wm1303/spectrum_history

Spectral scan history for trend analysis.

**Query parameters:** `hours` (default: 24)

#### GET /api/wm1303/lbt_history

LBT (Listen Before Talk) decision history per channel from channel_stats_history.

**Query parameters:** `hours` (default: 24)

**Response:**

```json
{
    "channels": {
        "ch-1": [
            {
                "timestamp": "2026-04-03T14:00:00",
                "lbt_pass": 45,
                "lbt_block": 2,
                "rssi_at_decision": -108.5
            }
        ]
    }
}
```

#### GET /api/wm1303/cad_history

CAD (Channel Activity Detection) event history per channel.

**Query parameters:** `hours` (default: 24)

#### GET /api/wm1303/cad_stats

Returns CAD statistics with HW/SW breakdown per channel. The `cad_events` database table stores separate columns for hardware and software CAD results. A periodic recorder writes HW/SW delta stats per minute.

**Query parameters:** `hours` (default: 24)

**Response:**

```json
{
    "channels": {
        "ch-1": {
            "summary": {
                "hw_clear": 1200,
                "hw_detected": 15,
                "sw_clear": 1180,
                "sw_detected": 35,
                "skipped": 3
            },
            "buckets": [
                {
                    "timestamp": "2026-04-03T14:00:00",
                    "hw_clear": 60,
                    "hw_detected": 1,
                    "sw_clear": 59,
                    "sw_detected": 2,
                    "skipped": 0
                }
            ]
        }
    }
}
```

#### GET /api/wm1303/signal_quality

Per-channel RSSI, SNR, and noise floor history. Now reads noise floor data from the `noise_floor_history` database table (instead of in-memory buffers), providing persistent historical data across service restarts.

**Query parameters:** `hours` (default: 24)

**Response:**

```json
{
    "channels": {
        "ch-1": [
            {
                "timestamp": "2026-04-03T14:00:00",
                "avg_rssi": -92.3,
                "avg_snr": 8.5,
                "rx_count": 127
            }
        ]
    }
}
```

#### GET /api/recent_packets

Returns recently bridged packets from the SQLite `packets` table. Used by the dashboard to display recent packet activity.

**Query parameters:** `limit` (default: 100)

**Response:**

```json
{
    "ok": true,
    "data": [
        {
            "timestamp": 1712160000.123,
            "source_channel": "ch-1",
            "target_channel": "ch-2",
            "rssi": -87.5,
            "snr": 8.2,
            "payload_type": "TXT",
            "route_type": "FLOOD",
            "size": 42
        }
    ],
    "count": 1
}
```

#### GET /api/wm1303/noise_floor_history

Returns noise floor measurement history from the SQLite `noise_floor` table. Used for noise floor trend charts.

**Query parameters:** `hours` (default: 24), `limit` (optional)

**Response:**

```json
{
    "ok": true,
    "data": [
        {
            "timestamp": 1712160000.0,
            "noise_floor_dbm": -93.5
        },
        {
            "timestamp": 1712160030.0,
            "noise_floor_dbm": -94.1
        }
    ]
}
```


### Logs

#### GET /api/wm1303/logs

Returns recent system log entries from the pyMC Repeater service.

**Response:**

```json
{
    "logs": [
        "2026-04-03 14:30:00 [INFO] RX on ch-1: rssi=-87.5 snr=8.2",
        "2026-04-03 14:30:01 [INFO] Bridge: ch-1 → ch-2 (FLOOD)"
    ]
}
```

### Control

#### POST /api/wm1303/control

System control actions (restart service, reset radio, etc.).

**Request body:**

```json
{
    "action": "restart_pkt_fwd"
}
```

**Available actions:**

| Action | Description |
|--------|-------------|
| `restart_pkt_fwd` | Restart the packet forwarder process |
| `reset_radio` | Execute GPIO reset sequence |
| `power_cycle` | Full power cycle (3-second off period) |
| `restart_service` | Restart the entire pymc-repeater service |

**Response:** `{"ok": true, "action": "restart_pkt_fwd"}`

## Error Responses

All endpoints return standard HTTP status codes:

| Code | Meaning |
|------|----------|
| 200 | Success |
| 400 | Bad request (invalid parameters) |
| 401 | Unauthorized (missing or invalid JWT) |
| 404 | Endpoint not found |
| 500 | Internal server error |

Error response format:

```json
{
    "error": "Description of the error",
    "code": 400
}
```

## API Usage Examples

### Monitor Channel Health (bash)

```bash
#!/bin/bash
TOKEN="<your-jwt-token>"
PI_IP="192.168.1.100"

# Get live channel stats
curl -s "http://${PI_IP}:8000/api/wm1303/channels/live" \
  -H "Authorization: Bearer ${TOKEN}" | python3 -m json.tool
```

### Update Bridge Rules (Python)

```python
import requests

base = "http://192.168.1.100:8000"
token = "<your-jwt-token>"
headers = {"Authorization": f"Bearer {token}"}

# Add a new bridge rule
rules = requests.get(f"{base}/api/wm1303/bridge", headers=headers).json()
rules["rules"].append({
    "source": "channel_a",
    "handler": "repeater",
    "target": "channel_b",
    "packet_types": ["FLOOD", "TFLOOD"]
})
requests.post(f"{base}/api/wm1303/bridge", json=rules, headers=headers)
```

### Trigger Spectral Scan (curl)

```bash
curl -X POST "http://${PI_IP}:8000/api/wm1303/spectrum" \
  -H "Authorization: Bearer ${TOKEN}" \
  -H "Content-Type: application/json" \
  -d '{"action": "scan"}'
```

---

*See also: [WM1303 Manager UI](ui.md) | [Software Components](software.md) | [Configuration](configuration.md)*
