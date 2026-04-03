# Configuration Reference

> Complete reference for all configuration files, their parameters, and interactions

## Overview

The WM1303 system uses several configuration files that work together:

| File | Location | Purpose |
|------|----------|--------|
| `config.yaml` | `/etc/pymc_repeater/` | Main service configuration |
| `wm1303_ui.json` | `/etc/pymc_repeater/` | UI state and channel config (SSOT) |
| `global_conf.json` | `/home/pi/wm1303_pf/` | HAL/packet forwarder configuration |
| `pymc-repeater.service` | `/etc/systemd/system/` | Systemd service unit |
| `reset_lgw.sh` | `/home/pi/wm1303_pf/` | GPIO reset script |
| `power_cycle_lgw.sh` | `/home/pi/wm1303_pf/` | GPIO power cycle script |

During installation, template files from the `config/` directory in the repository are copied to their target locations. Existing configuration files are **preserved** during upgrades unless `--force-config` is specified. See [Installation & Upgrade](installation.md) for details.

## Configuration Interaction

```
┌──────────────────────┐
│    wm1303_ui.json    │  ◄── SSOT (Single Source of Truth)
│  (channels, bridge,  │      Edited via Web UI
│   GPIO, adv config)  │
└──────────┬───────────┘
           │ generates at startup
           ▼
┌──────────────────────┐     ┌──────────────────────┐
│   global_conf.json   │     │     config.yaml      │
│  (HAL, RF chains,    │     │  (service settings,  │
│   IF chains, SX1261) │     │   JWT, mesh, radio)  │
└──────────┬───────────┘     └──────────┬───────────┘
           │                            │
           ▼                            ▼
┌──────────────────────┐     ┌──────────────────────┐
│  Packet Forwarder    │     │  pyMC Repeater       │
│  (lora_pkt_fwd)      │     │  (Python service)    │
└──────────────────────┘     └──────────────────────┘
```

Key relationships:
- `wm1303_ui.json` is the SSOT for channel configuration. Changes made in the Web UI are saved here.
- The backend regenerates `global_conf.json` RF/IF chain settings from `wm1303_ui.json` at service startup.
- `config.yaml` holds service-level settings that are not part of the UI.
- Both files are read by the pyMC Repeater service at startup and (for `wm1303_ui.json`) with a 5-second cache TTL during runtime.

---

## config.yaml

Main service configuration file. Installed from `config.yaml.template` during installation.

**Location:** `/etc/pymc_repeater/config.yaml`

### bridge

Controls the bridge engine that forwards packets between channels.

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `bridge_rules` | list | `[]` | List of bridge forwarding rules. Managed via UI; see [TX Queue](tx_queue.md) for rule format |
| `dedup_ttl_seconds` | integer | `300` | Duration (seconds) to remember packet hashes for deduplication. Prevents echo loops |

### delays

TX delay factors that control timing between transmissions.

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `direct_tx_delay_factor` | float | `0.5` | Multiplier for direct TX delay |
| `tx_delay_factor` | float | `1.0` | General TX delay multiplier |

### duty_cycle

EU regulatory duty cycle enforcement.

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `enforcement_enabled` | boolean | `false` | Enable/disable duty cycle enforcement |
| `max_airtime_per_minute` | integer | `54000` | Maximum airtime in ms per minute |
| `max_airtime_percent` | integer | `10` | Maximum duty cycle percentage |

### logging

Python logging configuration.

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `format` | string | `%(asctime)s %(name)s %(levelname)s %(message)s` | Log message format |
| `level` | string | `INFO` | Log level: DEBUG, INFO, WARNING, ERROR |

### mesh

MeshCore mesh network settings.

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `identity_key` | string | auto-generated | Unique mesh identity key. Generated on first run if not present |
| `path_hash_mode` | integer | `0` | Path hash algorithm mode |

### radio

Default radio parameters (used as fallback when channel-specific settings are not defined).

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `bandwidth` | integer | `125000` | Bandwidth in Hz (125000, 250000, 500000) |
| `coding_rate` | integer | `5` | Coding rate denominator (5=4/5, 6=4/6, 7=4/7, 8=4/8) |
| `crc_enabled` | boolean | `true` | Enable CRC checking |
| `frequency` | float | `869525000.0` | Default frequency in Hz |
| `preamble_length` | integer | `8` | Number of preamble symbols |
| `spreading_factor` | integer | `7` | Spreading factor (5-12) |
| `sync_word` | integer | `5156` | MeshCore sync word (0x1424). Distinguishes MeshCore from LoRaWAN |
| `tx_power` | integer | `14` | TX power in dBm |

### radio_type

Identifies the radio hardware type.

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `radio_type` | string | `wm1303` | Radio hardware identifier. Must be `wm1303` for this system |

### repeater

MeshCore repeater behavior settings.

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `advert_interval_seconds` | integer | `133200` | Advertisement interval in seconds (~37 hours) |
| `allow_discovery` | boolean | `true` | Allow node discovery by other mesh nodes |
| `cache_ttl` | integer | `60` | Route cache TTL in seconds |
| `direct_tx_delay_factor` | float | `0.5` | Direct TX delay multiplier |
| `identity_key` | string | auto-generated | Repeater identity key. Generated on first run |
| `initial_advert_delay_seconds` | integer | `15` | Delay before first advertisement after startup |
| `latitude` | float | `0.0` | GPS latitude for location reporting |
| `longitude` | float | `0.0` | GPS longitude for location reporting |
| `loop_detect` | boolean | `true` | Enable loop detection in forwarding |
| `mode` | string | `forward` | Repeater operating mode |
| `node_name` | string | `pyRepeater` | Human-readable node name displayed in the mesh network |
| `send_advert_interval_hours` | integer | `37` | Advertisement interval in hours |
| `tx_delay_factor` | float | `1.0` | General TX delay multiplier |

### repeater.security

Authentication and authorization settings for the [Web UI](ui.md) and [API](api.md).

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `admin_password` | string | auto-generated | Admin password for web UI login. Set during install or auto-generated |
| `jwt_secret` | string | auto-generated | Secret key for JWT token signing. Auto-generated on first run |
| `jwt_expiry_minutes` | integer | `10080` | JWT token validity period (default: 7 days) |

### sx1261_lbt

SX1261 companion chip GPIO and role configuration.

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `bus_id` | integer | `0` | SPI bus ID |
| `cs_id` | integer | `0` | SPI chip select ID |
| `cs_pin` | integer | `21` | Chip select GPIO pin |
| `busy_pin` | integer | `20` | Busy indicator GPIO pin |
| `irq_pin` | integer | `16` | Interrupt request GPIO pin |
| `reset_pin` | integer | `18` | Reset GPIO pin |
| `rxen_pin` | integer | `12` | RX enable GPIO pin |
| `txen_pin` | integer | `13` | TX enable GPIO pin |
| `rxled_pin` | integer | `-1` | RX LED GPIO pin (-1 = disabled) |
| `txled_pin` | integer | `-1` | TX LED GPIO pin (-1 = disabled) |
| `is_waveshare` | boolean | `true` | Waveshare-compatible hardware |
| `role` | string | `lbt_cad_spectrum_only` | SX1261 operating role. Only spectral scan/LBT/CAD |
| `tx_enabled` | boolean | `false` | SX1261 TX capability (disabled — TX goes through SX1302) |

### web

Web server configuration.

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `port` | integer | `8000` | HTTP server port for the [Web UI](ui.md) and [API](api.md) |
| `web_path` | string | `null` | Custom web root path (null = default) |

### wm1303

WM1303-specific hardware and channel configuration.

#### wm1303.channels

Defines the LoRa channels available for the bridge. Each channel is identified by a key (e.g., `channel_a`, `channel_b`).

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `frequency` | integer | varies | Center frequency in Hz |
| `bandwidth` | integer | `125000` | Bandwidth in Hz |
| `spreading_factor` | integer | varies | Spreading factor (7 or 8 typically) |
| `coding_rate` | string | `4/5` | Coding rate |
| `description` | string | varies | Human-readable channel description |
| `preamble_length` | integer | `17` | Preamble symbols |
| `tx_enable` | boolean | `true` | Enable TX on this channel |
| `tx_power` | integer | `14` | TX power in dBm |

Example:

```yaml
wm1303:
  channels:
    channel_a:
      frequency: 869461000
      bandwidth: 125000
      spreading_factor: 8
      coding_rate: 4/5
      description: MeshCore Channel A (SF8)
      tx_power: 14
    channel_b:
      frequency: 869588000
      spreading_factor: 7
      description: MeshCore Channel B (SF7)
      tx_power: 14
```

#### wm1303.pktfwd

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `pktfwd_bin` | string | `lora_pkt_fwd` | Packet forwarder binary name |
| `pktfwd_dir` | string | `/home/pi/wm1303_pf` | Packet forwarder working directory |
| `reset_script` | string | `reset_lgw.sh` | GPIO reset script filename |
| `spi_path` | string | `/dev/spidev0.0` | SX1302 SPI device path |
| `udp_port` | integer | `1730` | UDP port for packet forwarder communication |

#### wm1303.tx_queue

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `architecture` | string | `rf0_rx_rf1_tx` | TX architecture mode |
| `queue_size` | integer | `50` | Maximum TX queue size (per channel) |
| `tx_delay_ms` | integer | `20` | Delay between TX operations in ms |

---

## wm1303_ui.json

The Single Source of Truth (SSOT) for channel configuration, GPIO pins, bridge rules, and advanced parameters. All changes made through the [WM1303 Manager UI](ui.md) are saved to this file.

**Location:** `/etc/pymc_repeater/wm1303_ui.json`

### Top-Level Fields

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `rf_center_freq_mhz` | float | `869.525` | RF center frequency for display (MHz) |
| `advert_interval` | integer | `133200` | MeshCore advertisement interval (seconds) |

### channels

Array of channel configuration objects. Each channel defines frequency, modulation, and spectrum access settings.

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `name` | string | `ch-1`, `ch-2`, ... | Internal channel identifier (do not use as reference ID) |
| `friendly_name` | string | `Channel A`, ... | Display name (alias only — do not use as reference ID) |
| `frequency` | integer | varies | Center frequency in Hz |
| `bandwidth` | integer | `125000` | Bandwidth in Hz |
| `spreading_factor` | integer | varies | Spreading factor (5-12) |
| `coding_rate` | string | `4/5` | Coding rate |
| `tx_enabled` | boolean | `true` | Enable TX on this channel |
| `tx_power` | integer | `14` | TX power in dBm |
| `active` | boolean | `true` | Channel is active (receiving and transmitting) |
| `preamble_length` | integer | `17` | Number of preamble symbols |
| `lbt_enabled` | boolean | `false` | Enable LBT per channel. See [LBT & CAD](lbt_cad.md) |
| `lbt_rssi_target` | integer | `-115` | LBT RSSI target threshold (dBm) |
| `cad_enabled` | boolean | `false` | Enable CAD per channel. See [LBT & CAD](lbt_cad.md) |

Note: `friendly_name` values are aliases for display purposes only. They should never be used as reference identifiers in bridge rules or API calls. Use `name` for programmatic references.

### bridge

Bridge rule configuration managed through the UI.

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `bridge.rules` | array | `[]` | List of bridge forwarding rules |

### gpio_pins

GPIO pin mapping for the WM1303 Pi HAT. These values are used to generate `reset_lgw.sh` and `power_cycle_lgw.sh` during installation.

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `sx1302_reset` | integer | `17` | BCM pin for SX1302 reset |
| `sx1302_power_en` | integer | `18` | BCM pin for SX1302 power enable |
| `sx1261_reset` | integer | `5` | BCM pin for SX1261 reset |
| `ad5338r_reset` | integer | `13` | BCM pin for AD5338R DAC reset |
| `gpio_base_offset` | integer | `512` | GPIO base offset for sysfs (Raspberry Pi 4/5 with newer kernels) |

The actual GPIO sysfs numbers are calculated as `gpio_base_offset + BCM_pin`. For example, BCM pin 17 with offset 512 = sysfs GPIO 529.

See [Hardware Overview](hardware.md) for the GPIO wiring diagram.

### hal_advanced

Advanced HAL parameters for FEM (Front End Module) control.

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `force_host_fe_ctrl` | boolean | `false` | Force host-side FEM control (bypasses HAL auto-detect) |
| `lna_lut` | string | `0x03` | LNA lookup table value |
| `pa_lut` | string | `0x04` | PA (Power Amplifier) lookup table value |
| `agc_ana_gain` | string | `auto` | Analog AGC gain mode |
| `agc_dec_gain` | string | `auto` | Decimation AGC gain mode |
| `channelizer_fixed_gain` | boolean | `false` | Fix channelizer gain (disable AGC for channelizer) |

See [Hardware Overview](hardware.md) for details on FEM/LNA/AGC/PA configuration and the LUT tables.

### adv_config

Advanced operational parameters accessible via the Adv. Config tab in the [Web UI](ui.md).

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `max_cache_size` | integer | `1000` | Maximum dedup cache size |
| `tx_packet_ttl_seconds` | integer | `5` | TX queue packet TTL in seconds |
| `tx_overflow_policy` | string | `drop_oldest` | Queue overflow policy: `drop_oldest` |
| `noise_floor_interval_seconds` | integer | `30` | Noise floor measurement interval |
| `noise_floor_tx_hold_seconds` | integer | `4` | TX hold duration during noise floor scan |
| `noise_floor_buffer_size` | integer | `20` | Rolling buffer size for noise floor samples |

See [LBT & CAD](lbt_cad.md) for how these parameters affect spectrum access and [TX Queue](tx_queue.md) for queue behavior.

---

## global_conf.json

SX1302 HAL and packet forwarder configuration. This file controls the low-level radio hardware.

**Location:** `/home/pi/wm1303_pf/global_conf.json`

### SX130x_conf

Top-level concentrator configuration.

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `com_type` | string | `SPI` | Communication type (always SPI) |
| `com_path` | string | `/dev/spidev0.0` | SPI device path for SX1302 |
| `lorawan_public` | boolean | `false` | LoRaWAN public network mode. `false` for MeshCore (private sync word) |
| `clksrc` | integer | `0` | Clock source (0 = radio_0) |
| `antenna_gain` | integer | `0` | Antenna gain in dBi (for EIRP calculation) |
| `full_duplex` | boolean | `false` | Full duplex mode (not supported on WM1303) |

### radio_0 and radio_1

RF chain configuration. See [Radio Configuration](radio.md) for detailed RF chain documentation.

| Key | Type | Default (radio_0) | Description |
|-----|------|-------------------|-------------|
| `enable` | boolean | `true` | Enable this RF chain |
| `type` | string | `SX1250` | Radio chip type |
| `freq` | integer | `869387250` | Center frequency in Hz |
| `rssi_offset` | float | `-215.4` | RSSI calibration offset |
| `tx_enable` | boolean | `true` (radio_0 only) | Enable TX capability. Only radio_0 has PA connection |
| `tx_freq_min` | integer | `863000000` | Minimum TX frequency in Hz |
| `tx_freq_max` | integer | `870000000` | Maximum TX frequency in Hz |
| `tx_gain_lut` | array | 16 entries | TX power lookup table mapping rf_power to PA/pwr_idx |

Radio_1 is configured identically to radio_0 for center frequency but with `tx_enable: false`.

### TX Gain Lookup Table

The `tx_gain_lut` maps requested RF power (dBm) to hardware PA gain and power index settings:

| rf_power | pa_gain | pwr_idx | Notes |
|----------|---------|---------|-------|
| 12-17 | 0 | 15-22 | Low power range (PA bypass) |
| 18-27 | 1 | 1-14 | High power range (PA active) |

See [Hardware Overview](hardware.md) for FEM/PA configuration details and LUT table optimization.

### IF Channels (chan_multiSF_0 through chan_multiSF_7)

IF (Intermediate Frequency) chain configuration. Each IF channel demodulates at an offset from its parent RF chain center frequency.

| Key | Type | Description |
|-----|------|-------------|
| `enable` | boolean | Enable this IF channel |
| `radio` | integer | Parent RF chain (0 or 1) |
| `if` | integer | IF offset in Hz from RF chain center frequency |

Default configuration (3 active channels):

| IF Channel | Enabled | Radio | IF Offset | Resulting Frequency |
|-----------|---------|-------|-----------|--------------------|
| chan_multiSF_0 | true | 0 | +73,750 Hz | 869,461,000 Hz (ch-a) |
| chan_multiSF_1 | true | 0 | +200,750 Hz | 869,588,000 Hz (ch-b) |
| chan_multiSF_2 | true | 0 | -87,250 Hz | 869,300,000 Hz (ch-d) |
| chan_multiSF_3-7 | false | 0 | 0 | — (disabled) |

The SX1302 supports up to 8 multi-SF demodulators plus 1 LoRa service channel and 1 FSK channel. For MeshCore operation, using fewer channels (max 4) provides better stability.

See [Radio Configuration](radio.md) for the full IF chain architecture.

### sx1261_conf

SX1261 companion chip configuration for spectral scanning and LBT.

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `spi_path` | string | `/dev/spidev0.1` | SPI device path for SX1261 |
| `rssi_offset` | integer | `0` | RSSI calibration offset |

#### spectral_scan

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `enable` | boolean | `true` | Enable continuous spectral scanning |
| `freq_start` | integer | `863000000` | Scan start frequency in Hz |
| `nb_chan` | integer | `36` | Number of scan channels (~200 kHz steps) |
| `nb_scan` | integer | `100` | Scans per channel per sweep |
| `pace_s` | integer | `1` | Sweep pace in seconds |

#### lbt (HAL-level)

HAL-level LBT is **disabled** because it does not support multi-channel operation. Software LBT is used instead.

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `enable` | boolean | `false` | HAL LBT enable (keep disabled) |
| `rssi_target` | integer | `-80` | HAL LBT RSSI target (unused) |
| `nb_channel` | integer | `0` | Number of LBT channels (unused) |
| `channels` | array | `[]` | LBT channel definitions (unused) |

See [LBT & CAD](lbt_cad.md) for the software LBT implementation.

### gateway_conf

Packet forwarder gateway settings for the UDP protocol.

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `gateway_ID` | string | `AA555A0000000000` | Gateway identifier (used in UDP protocol headers) |
| `server_address` | string | `127.0.0.1` | UDP server address (localhost — the backend) |
| `serv_port_up` | integer | `1730` | Upstream UDP port (RX packets: forwarder → backend) |
| `serv_port_down` | integer | `1730` | Downstream UDP port (TX packets: backend → forwarder) |
| `keepalive_interval` | integer | `10` | Keepalive ping interval in seconds |
| `stat_interval` | integer | `30` | Statistics reporting interval in seconds |
| `push_timeout_ms` | integer | `100` | PUSH_DATA acknowledgment timeout in ms |
| `forward_crc_valid` | boolean | `true` | Forward packets with valid CRC |
| `forward_crc_error` | boolean | `true` | Forward packets with CRC errors (for monitoring) |
| `forward_crc_disabled` | boolean | `true` | Forward packets with CRC disabled |

---

## pymc-repeater.service

Systemd service unit file that manages the pyMC Repeater daemon.

**Location:** `/etc/systemd/system/pymc-repeater.service`

### Key Configuration

```ini
[Unit]
Description=pyMC Repeater Daemon
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=pi
Group=pi
WorkingDirectory=/opt/pymc_repeater/repos/pyMC_Repeater
Environment="VIRTUAL_ENV=/opt/pymc_repeater/venv"
Environment="PATH=/opt/pymc_repeater/venv/bin:/usr/local/sbin:..."
ExecStartPre=-/usr/bin/sudo /bin/systemctl stop lora_pkt_fwd.service
ExecStart=/opt/pymc_repeater/venv/bin/python3 -m repeater.main --config /etc/pymc_repeater/config.yaml
Restart=on-failure
RestartSec=5
MemoryHigh=256M
ReadWritePaths=/var/log/pymc_repeater /var/lib/pymc_repeater /etc/pymc_repeater /home/pi /tmp
```

### Notable Settings

| Setting | Value | Purpose |
|---------|-------|--------|
| `User=pi` | Runs as `pi` user | Service does not require root |
| `ExecStartPre` | Stops `lora_pkt_fwd.service` | Prevents conflict with standalone packet forwarder |
| `Restart=on-failure` | Auto-restart on crash | Ensures high availability |
| `RestartSec=5` | 5-second restart delay | Allows hardware to reset |
| `MemoryHigh=256M` | Soft memory limit | Prevents runaway memory usage |
| `ReadWritePaths` | Specific directories | Restricts write access for security |

### Service Commands

```bash
# Start / stop / restart
sudo systemctl start pymc-repeater
sudo systemctl stop pymc-repeater
sudo systemctl restart pymc-repeater

# View status
sudo systemctl status pymc-repeater

# View logs (live)
journalctl -u pymc-repeater -f

# View last 100 log lines
journalctl -u pymc-repeater -n 100

# Enable/disable auto-start on boot
sudo systemctl enable pymc-repeater
sudo systemctl disable pymc-repeater
```

---

## GPIO Scripts

GPIO reset and power cycle scripts are **auto-generated** during installation based on the `gpio_pins` section of `wm1303_ui.json`. They are regenerated during upgrades.

### reset_lgw.sh

**Location:** `/home/pi/wm1303_pf/reset_lgw.sh`

Performs a logic-level reset of the concentrator module. Called by the packet forwarder at startup.

**Sequence:**
1. `term` — Unexport all GPIO pins (cleanup)
2. `init` — Export pins and set as output
3. `reset` — Execute reset sequence:
   - Power enable HIGH (turn on concentrator)
   - SX1302 reset pulse (HIGH → LOW)
   - SX1261 reset pulse (LOW → HIGH)
   - AD5338R reset pulse (LOW → HIGH)
4. Sleep 1 second for stabilization

**Usage:**
```bash
# Performed automatically by packet forwarder
/home/pi/wm1303_pf/reset_lgw.sh start

# Manual cleanup
/home/pi/wm1303_pf/reset_lgw.sh stop
```

### power_cycle_lgw.sh

**Location:** `/home/pi/wm1303_pf/power_cycle_lgw.sh`

Performs a full power cycle of the concentrator. This is more aggressive than `reset_lgw.sh` — it cuts power completely to clear SX1250 TX-induced desensitization.

**Sequence:**
1. Export all GPIO pins
2. Power OFF (set power enable LOW)
3. Wait 3 seconds for capacitors to fully discharge
4. Power ON (set power enable HIGH)
5. Wait 0.5 seconds for power stabilization
6. Execute logic resets (SX1302, SX1261, AD5338R)
7. Wait 1 second for final stabilization

The 3-second power-off duration is critical — it ensures the SX1250 analog frontend fully resets, clearing any TX-induced desensitization state.

See [Hardware Overview](hardware.md) for details on the FEM/PA issue that necessitates power cycling.

### GPIO Pin Mapping

| Function | BCM Pin | Sysfs GPIO (offset 512) | Description |
|----------|---------|------------------------|-------------|
| SX1302 Reset | 17 | 529 | Concentrator baseband reset |
| SX1302 Power | 18 | 530 | Concentrator power enable |
| SX1261 Reset | 5 | 517 | Companion chip reset |
| AD5338R Reset | 13 | 525 | DAC reset |

---

## Auto-Generated Secrets

The following values are automatically generated on first run if not present in `config.yaml`:

| Secret | Purpose | Generation |
|--------|---------|------------|
| `mesh.identity_key` | Unique mesh network identity | Cryptographic random |
| `repeater.identity_key` | Repeater identity for the network | Cryptographic random |
| `repeater.security.jwt_secret` | JWT token signing key | Cryptographic random |
| `repeater.security.admin_password` | Web UI admin password | Set during install or random |

These secrets are stored in plaintext in `config.yaml`. Ensure appropriate file permissions:

```bash
# Verify permissions
ls -la /etc/pymc_repeater/config.yaml
# Should be: -rw-r--r-- 1 pi pi
```

---

## Related Documentation

- [Installation & Upgrade](installation.md) — How config files are installed and updated
- [Radio Configuration](radio.md) — RF chain and IF chain details
- [Hardware Overview](hardware.md) — GPIO wiring, SPI bus, FEM/PA configuration
- [LBT & CAD](lbt_cad.md) — Spectrum access configuration
- [TX Queue & Scheduling](tx_queue.md) — Queue parameters and bridge rules
- [Web UI](ui.md) — Configuration interface
- [API Reference](api.md) — REST API endpoints
