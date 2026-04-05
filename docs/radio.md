# Radio Configuration

> RF chains, IF chains, channel setup, and LoRa radio parameters

## RF Chains

The SX1302/SX1303 baseband processor connects to two independent SX1250 radio transceivers, called RF chains. Each RF chain can be tuned to a different center frequency.

### RF Chain 0 (RF0)

- **Radio chip:** SX1250_0
- **Capabilities:** TX and RX
- **FEM connection:** Connected to SKY66420 (PA + LNA + RF switch)
- **TX enable:** Yes — all transmissions go through RF0
- **Typical center frequency:** 869.525 MHz (EU868 g3 band)

### RF Chain 1 (RF1)

- **Radio chip:** SX1250_1
- **Capabilities:** RX only
- **FEM connection:** SAW filter path only (no PA)
- **TX enable:** No
- **Default state:** Disabled (`enable: false`) — no IF channels reference it in the default configuration

### Important: TX Always Uses RF0

Regardless of which channel a packet needs to be transmitted on, **all TX goes through RF chain 0**. The HAL tunes the SX1250_0 radio to the target frequency for each transmission. RF chain 1 has no Power Amplifier connection and cannot transmit.

### RF Chain Configuration in global_conf.json

```json
"radio_0": {
    "enable": true,
    "type": "SX1250",
    "freq": 869525000,
    "rssi_offset": -215.4,
    "rssi_tcomp": {
        "coeff_a": 0, "coeff_b": 0,
        "coeff_c": 20.41, "coeff_d": 2162.56, "coeff_e": 0
    },
    "tx_enable": true,
    "tx_freq_min": 863000000,
    "tx_freq_max": 870000000,
    "tx_gain_lut": [ ... ]
},
"radio_1": {
    "enable": false,
    "type": "SX1250",
    "freq": 869525000,
    "rssi_offset": -215.4,
    "tx_enable": false
}
```

> **Note:** `radio_1` is disabled by default because no IF channels reference it. Enable it only if you configure IF channels that need a second RF chain with a different center frequency.

## IF Chains (Intermediate Frequency Demodulators)

The SX1302 has **10 IF (Intermediate Frequency) channels** that perform the actual LoRa and FSK demodulation:

| IF Chain | Type | Assignment | Description |
|----------|------|------------|-------------|
| IF0 | Multi-SF | RF0 | Channel 1 demodulator — can decode SF5-SF12 |
| IF1 | Multi-SF | RF0 | Channel 2 demodulator — can decode SF5-SF12 |
| IF2 | Multi-SF | RF0 | Channel 3 demodulator — can decode SF5-SF12 |
| IF3 | Multi-SF | RF0 | Channel 4 demodulator — can decode SF5-SF12 |
| IF4 | Multi-SF | RF0 | Reserved (disabled) |
| IF5 | Multi-SF | RF0 | Reserved (disabled) |
| IF6 | Multi-SF | RF0 | Reserved (disabled) |
| IF7 | Multi-SF | RF0 | Reserved (disabled) |
| IF8 | LoRa Service | RF0 | Single-SF demodulator (specific SF/BW) |
| IF9 | FSK | RF0 | FSK demodulator (disabled) |

### How IF Chains Work

Each IF chain is configured with a **frequency offset** relative to its parent RF chain's center frequency. The actual receive frequency is:

```
RX frequency = RF chain center freq + IF offset
```

For example, with RF0 center at 869,525,000 Hz:
- IF0 offset = 0 Hz → listens at 869,525,000 Hz (Channel A)
- IF1 offset = +100,000 Hz → listens at 869,625,000 Hz (Channel B)

### Multi-SF Demodulators

The multi-SF demodulators can simultaneously detect and decode any spreading factor from SF5 to SF12 within their bandwidth. This means a single IF chain can receive packets from nodes using different spreading factors on the same frequency.

### IF Chain Configuration in global_conf.json

```json
"chan_multiSF_0": { "enable": true,  "radio": 0, "if": 0 },
"chan_multiSF_1": { "enable": true,  "radio": 0, "if": 100000 },
"chan_multiSF_2": { "enable": false, "radio": 0, "if": 0 },
"chan_multiSF_3": { "enable": false, "radio": 0, "if": 0 },
"chan_multiSF_4": { "enable": false, "radio": 0, "if": 0 },
"chan_multiSF_5": { "enable": false, "radio": 0, "if": 0 },
"chan_multiSF_6": { "enable": false, "radio": 0, "if": 0 },
"chan_multiSF_7": { "enable": false, "radio": 0, "if": 0 }
```

### Fixed Channel-to-IF Mapping

The mapping between channel names and IF chains is fixed in the backend code and never changes:

```python
CHANNEL_IF_MAP = {
    'ch-1':    0,   # Channel A → chan_multiSF_0
    'ch-2':    1,   # Channel B → chan_multiSF_1
    'ch-new':  2,   # Channel D → chan_multiSF_2
    'ch-notu': 3,   # Channel C → chan_multiSF_3
}
```

This fixed mapping prevents configuration drift and ensures consistent behavior regardless of channel ordering in the UI.

## Channel Configuration

Channels are configured in `wm1303_ui.json` (the SSOT). Each channel has the following parameters:

| Parameter | Description | Typical Values |
|-----------|-------------|----------------|
| `name` | Internal channel identifier | `ch-1`, `ch-2`, `ch-new` |
| `friendly_name` | Display name in UI | `Channel A`, `Channel B` |
| `frequency` | Center frequency in Hz | 869461000, 869588000 |
| `bandwidth` | Channel bandwidth in Hz | 125000, 250000, 500000 |
| `spreading_factor` | LoRa spreading factor | 7, 8, 9, 10, 11, 12 |
| `coding_rate` | Forward Error Correction rate | "4/5", "4/6", "4/7", "4/8" |
| `tx_enabled` | Whether TX is allowed | true, false |
| `tx_power` | TX power in dBm | 2-27 |
| `active` | Whether channel is enabled | true, false |
| `preamble_length` | Number of preamble symbols | 8-65535 (typically 17) |
| `lbt_enabled` | Listen Before Talk per channel | true, false |
| `cad_enabled` | Channel Activity Detection | true, false |
| `lbt_rssi_target` | LBT RSSI threshold in dBm | -80, -90, etc. |

### Example Channel Configuration

```json
{
    "name": "ch-1",
    "frequency": 869525000,
    "bandwidth": 125000,
    "spreading_factor": 7,
    "coding_rate": "4/5",
    "tx_enabled": true,
    "tx_power": 14,
    "active": true,
    "preamble_length": 17,
    "friendly_name": "Channel A",
    "lbt_rssi_target": -80,
    "lbt_enabled": true,
    "cad_enabled": false
}
```

> **Note:** Channel friendly names are aliases for display purposes only. They should never be used as reference identifiers in code or configuration. Always use the internal `name` field (`ch-1`, `ch-2`, etc.) for programmatic references.

## Maximum 4 Channels

The system supports a maximum of **4 simultaneous channels**. While the SX1302 has 8 multi-SF demodulators, practical considerations limit the useful number of channels:

### Why Less Is Better / More Stable

1. **TX contention:** The SX1302 is half-duplex — during TX, all RX stops. More channels mean more bridge forwarding, more TX events, and longer RX blind windows.

2. **Frequency spread:** All IF chains share a single RF chain center frequency. The maximum IF offset is ±730 kHz (matching the SX1302 HAL digital bandwidth). The RF chain center frequency is automatically calculated from the center of all **active** channels only — inactive channels do not influence the center frequency. Channels whose IF offset would exceed ±730 kHz are gracefully force-disabled instead of causing a startup error.

3. **Bridge complexity:** With N channels and full bridge rules, the number of TX events per received packet grows as N-1. With 4 channels, a single RX can trigger 3 TX events.

4. **RX blind window calculation:**
   - 2 channels: ~200 ms per bridge event (~1.7% RX loss at 1 event/12s)
   - 3 channels: ~380 ms per bridge event (~3.2% RX loss)
   - 4 channels: ~490 ms per bridge event (~4.1% RX loss)

5. **Noise floor monitoring:** More channels with LBT/CAD means more spectral scan overhead.

**Recommendation:** Use 2-3 channels for optimal stability. 4 channels is the practical maximum for acceptable performance.

## MeshCore Sync Word

MeshCore uses a custom sync word to distinguish its packets from LoRaWAN and other LoRa traffic:

| Parameter | Value | Hex |
|-----------|-------|-----|
| Sync Word | 5156 | 0x1424 |

The sync word is configured in `config.yaml`:

```yaml
radio:
  sync_word: 5156
```

And in `global_conf.json`:

```json
"lorawan_public": false
```

Setting `lorawan_public: false` tells the HAL to use a private sync word rather than the standard LoRaWAN sync word (0x3444). The actual MeshCore sync word (0x1424) is configured at the application level.

## LoRaWAN Compatibility

This system is **not LoRaWAN compatible** by design:

- **Different sync word:** MeshCore uses 0x1424, LoRaWAN uses 0x3444 (public) or 0x1424 with different network identification
- **Different protocol:** MeshCore uses its own packet format with route types, payload types, hop counts, and path tracking
- **No LoRaWAN Network Server:** The packet forwarder runs locally without cloud connectivity
- **`lorawan_public: false`:** The HAL is configured for private network mode

MeshCore packets and LoRaWAN packets can coexist on the same frequencies because the different sync words prevent cross-reception. The SX1302 will only demodulate packets matching its configured sync word.

## Frequency Planning (EU868)

The system operates in the European 868 MHz ISM band. Key frequency regulations:

| Sub-band | Frequency Range | Duty Cycle | Max Power | Notes |
|----------|----------------|------------|-----------|-------|
| g1 | 868.0 - 868.6 MHz | 1% | 25 mW ERP | Limited airtime |
| g2 | 868.7 - 869.2 MHz | 0.1% | 25 mW ERP | Very limited |
| g3 | 869.4 - 869.65 MHz | 10% | 500 mW ERP | **Primary band** |
| g4 | 869.7 - 870.0 MHz | No limit | 5 mW ERP | Low power |

**Typical channel placement (EU868):**

```
868.0    868.5    869.0    869.4  869.65   870.0
  │        │        │   g3:  │ ████ │        │
  │        │        │  10%   │ch-a  │        │
  │        │        │ 500mW  │ch-b  │        │
  │        │        │        │ch-d  │        │
  ▼        ▼        ▼        ▼      ▼        ▼
```

The g3 sub-band (869.4-869.65 MHz) is preferred because it offers the highest allowed power (500 mW / 27 dBm) and most generous duty cycle (10%).

## TX/RX Architecture

The SX1302 is fundamentally a **half-duplex** system on each RF chain:

### During Normal RX Operation

```
SX1250_0 (RF0): RX active — antenna → FEM LNA → SX1250 → SX1302 baseband → IF demodulators
SX1250_1 (RF1): RX active — (via SAW filter path)
SX1261: Available for spectral scan (separate SPI bus)
```

### During TX

```
RX ──stops──┐
            ├── RF switch → TX mode          (~0.1 ms)
            ├── TX airtime                   (65-230 ms depending on SF/payload)
            ├── RF switch → RX mode          (~0.1 ms)
            ├── AGC recalibration             (30-50 ms)
RX ──resumes┘
```

### RX Blind Window per TX Event

| Channel Config | SF | Airtime (60B payload) | AGC Recovery | Total Blind Window |
|---------------|----|-----------------------|--------------|--------------------|
| SF7 / BW125k | 7 | ~65 ms | ~40 ms | ~105 ms |
| SF8 / BW125k | 8 | ~120 ms | ~40 ms | ~160 ms |
| SF11 / BW250k | 11 | ~200 ms | ~40 ms | ~240 ms |
| SF12 / BW125k | 12 | ~400 ms | ~40 ms | ~440 ms |

### TX Hold (Removed)

In earlier versions, a TX hold window paused transmissions for approximately 4 seconds every 30 seconds to allow uninterrupted spectral scanning and noise floor measurement. This introduced ~13% TX overhead — during the hold window, queued packets could not be transmitted.

**TX hold has been completely removed.** The current implementation achieves 0% TX overhead by using a retry-based approach for spectral scanning (see below). The TX mutex is released during scan retries to preserve maximum RX availability, consistent with the [design principle](../design-principles.promptinclude.md) that RX availability is the top priority.

### Spectral Scan Retry Logic

The SX1261 companion chip performs spectral scans on a separate SPI bus without interrupting SX1302 RX. However, spectral scans cannot run concurrently with TX (both share the RF front-end).

Instead of blocking TX with a hold window, the spectral scan now uses a **retry mechanism**:
1. The scan checks if a TX operation is in progress
2. If TX is active, the scan releases the TX mutex and waits briefly before retrying
3. This repeats until a TX-free window is found
4. The scan range is dynamically calculated based on the RF chain center frequency and active channel spread

This approach ensures spectral scans always complete without introducing any TX delay or RX blind windows beyond the scan itself.

## Noise Floor / RSSI / SNR Per Channel

The system maintains per-channel signal quality metrics:

### Noise Floor

Determined by the NoiseFloorMonitor using SX1261 spectral scan data:
- Updated every 30 seconds
- Rolling buffer of 20 samples per channel
- Frequency matching: channel frequency ± BW/2
- **TX echo filtering:** RSSI values > -50 dBm are filtered at the storage level to prevent the device's own TX transmissions from contaminating noise floor measurements. This ensures noise floor data reflects actual ambient RF conditions.
- Typical values: -90 to -120 dBm depending on environment

### RSSI (Received Signal Strength Indicator)

- Measured per received packet by the SX1302
- Includes RSSI temperature compensation (rssi_tcomp coefficients)
- RSSI offset configured per RF chain (-215.4 dB default)
- Available per-channel: last RSSI, average RSSI, min/max

### SNR (Signal-to-Noise Ratio)

- Computed by the SX1302 baseband processor per packet
- Typical good values: > 5 dB
- Minimum decodable: approximately -20 dB (depending on SF)
- Higher SF can decode at lower SNR

## RX CRC Errors

The SX1302 performs CRC (Cyclic Redundancy Check) validation on received packets:

- **CRC OK:** Packet payload is intact — forwarded to the backend
- **CRC BAD:** Packet payload is corrupted — **discarded** by the packet forwarder
- **CRC NONE:** No CRC in packet — depends on configuration (typically rejected)

### What CRC Errors Mean

1. **Occasional CRC errors are normal** — RF interference, weak signals at the edge of reception range, or collision with other transmitters can corrupt packets
2. **High CRC error rate** indicates problems:
   - Antenna issues (disconnected, damaged, wrong frequency band)
   - Excessive interference at the operating frequency
   - SX1302 receiving signals from a different LoRa network with different parameters
   - Hardware issue with the SX1250 radio or FEM
3. **CRC validation was initially disabled** in early development, causing corrupt packets to be processed. This was fixed as the `crc_fix` deployment.

CRC errors are counted in the packet forwarder statistics and visible in the RX Watchdog metrics.

---

*See also: [Hardware & HAL](hardware.md) | [LBT & CAD](lbt_cad.md) | [TX Queue](tx_queue.md)*
