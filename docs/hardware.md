# Hardware & HAL

> WM1303 concentrator module, SenseCAP M1 integration, and HAL modifications

> **Disclaimer:** Use of this software and hardware modifications is at your own risk. The authors accept no responsibility for any damage to hardware, including but not limited to the WM1303 module, SenseCAP M1, or connected equipment.

## WM1303 Module Overview

The WM1303 is a LoRa concentrator module based on the Semtech SX1302/SX1303 baseband processor. It is mounted on a Pi HAT (Hardware Attached on Top) designed for the SenseCAP M1, which is essentially a Raspberry Pi 4 in a custom enclosure.

### Chip Components

| Chip | Role | SPI Device | Description |
|------|------|------------|-------------|
| **SX1302/SX1303** | Baseband processor | spidev0.0 | Digital baseband: 8 multi-SF demodulators, 1 LoRa service, 1 FSK. Handles RX demodulation and TX modulation. |
| **SX1250_0** | RF chain 0 radio | (via SX1302) | Dual-band radio transceiver. Connected to SKY66420 FEM. Supports TX and RX. |
| **SX1250_1** | RF chain 1 radio | (via SX1302) | Dual-band radio transceiver. RX only path (no PA connection). |
| **SX1261** | Companion chip | spidev0.1 | Independent LoRa transceiver used for spectral scanning, LBT RSSI measurements, and CAD. |
| **SKY66420** | Front-End Module (FEM) | (via GPIO) | Contains PA (Power Amplifier), LNA (Low Noise Amplifier), and RF switch. Controls TX/RX path switching. |
| **AD5338R** | DAC (optional) | (via I2C) | Digital-to-Analog Converter. Present on some WM1302/WM1303 HATs. Used in CN490 full-duplex reference designs. Not actively used in this project (see investigation notes below). |

### Block Diagram

```
                    Antenna
                       │
                  ┌────┴────┐
                  │SKY66420 │
                  │  FEM    │
                  │ PA+LNA  │
                  │RF Switch│
                  └┬───────┬┘
                   │       │
            TX/RX path  RX-only path
                   │       │
            ┌──────┴──┐ ┌──┴──────┐
            │SX1250_0 │ │SX1250_1 │
            │ RF0     │ │ RF1     │
            │(TX+RX)  │ │(RX only)│
            └────┬────┘ └────┬────┘
                 │            │
            ┌────┴────────────┴────┐
            │     SX1302/SX1303    │
            │   Baseband Processor │
            │                      │
            │  IF0-IF3 (RF0 demod) │
            │  IF4-IF7 (RF1 demod) │
            │  IF8 (LoRa service)  │
            │  IF9 (FSK)           │
            └──────────┬───────────┘
                       │ SPI (spidev0.0, 2 MHz)
                       │
            ┌──────────┴───────────┐
            │    Raspberry Pi 4    │
            │    (SenseCAP M1)     │
            └──────────┬───────────┘
                       │ SPI (spidev0.1, 2 MHz)
                       │
            ┌──────────┴───────────┐
            │       SX1261         │
            │  Companion Chip      │
            │  (Spectral/LBT/CAD)  │
            └──────────────────────┘
```

## SenseCAP M1 Pi HAT Integration

The SenseCAP M1 uses a custom Pi HAT that connects the WM1303 module to the Raspberry Pi 4 via SPI and GPIO. The key integration points are:

- **SPI bus**: Two SPI chip selects — CS0 (spidev0.0) for SX1302, CS1 (spidev0.1) for SX1261
- **GPIO pins**: Reset, power enable, and chip-specific control signals
- **Power**: 3.3V and 5V from Pi HAT header
- **Antenna**: Single SMA connector shared via FEM RF switch

## SPI Configuration

The SPI bus runs at **2 MHz** (2,000,000 Hz), as defined in the HAL source:

```c
// From libloragw/inc/loragw_spi.h
#define SPI_SPEED       2000000
```

### SPI Devices

| Device | Chip | Clock Speed | Purpose |
|--------|------|-------------|----------|
| `/dev/spidev0.0` | SX1302 | 2 MHz | Main concentrator: RX + TX |
| `/dev/spidev0.1` | SX1261 | 2 MHz | Spectral scan, LBT, CAD |

### SPI Bus Bandwidth Analysis

At 2 MHz, the theoretical SPI throughput is 250 KB/s. The actual bus utilization is well within capacity:

| Activity | SPI Time per Cycle | Frequency | Bus Utilization |
|----------|--------------------|-----------|-----------------|
| RX polling | ~1 ms | 10x/sec | ~1% |
| TX transmit | ~2 ms | Incidental | <0.5% |
| Spectral scan | ~5 ms per freq step | 1x per 30s sweep | ~5-10% |
| **Total** | | | **< 15%** |

The 2 MHz SPI speed provides sufficient bandwidth for the current implementation. The SX1261 spectral scan (via spidev0.1) operates on a separate SPI device, so it does not contend with SX1302 operations on spidev0.0.

### Enabling SPI

SPI must be enabled in the Raspberry Pi boot configuration:

```
# /boot/firmware/config.txt
dtparam=spi=on
```

Verify SPI devices exist after reboot:

```bash
ls -la /dev/spidev0.*
# Should show: /dev/spidev0.0  /dev/spidev0.1
```

### Enabling I2C

I2C must be enabled for the WM1303 on-board temperature sensor and the AD5338R DAC. The installer handles this automatically, but for manual setup:

```
# /boot/firmware/config.txt
dtparam=i2c_arm=on
```

The `i2c-dev` kernel module must also be loaded at boot:

```bash
echo "i2c-dev" | sudo tee /etc/modules-load.d/i2c-dev.conf
sudo modprobe i2c-dev
sudo modprobe i2c-bcm2835
```

Verify I2C is available after reboot:

```bash
ls -la /dev/i2c-1
# Should show the I2C device
```

> **Note:** The HAL uses I2C to read the on-board temperature sensor for RSSI temperature compensation. If I2C is not available, the HAL falls back to a fixed temperature offset.


## GPIO Pin Mapping

The Raspberry Pi 4 (Bookworm) uses a GPIO base offset of **512** for sysfs access. The actual sysfs GPIO number is calculated as `BCM pin + 512`.

| Function | BCM Pin | Sysfs GPIO | Direction | Description |
|----------|---------|------------|-----------|-------------|
| SX1302 Reset | BCM 17 | 529 | Output | Concentrator logic reset (active high pulse) |
| SX1302 Power Enable | BCM 18 | 530 | Output | Concentrator power control (high = enabled) |
| SX1261 Reset | BCM 5 | 517 | Output | Companion chip reset (active low pulse) |
| AD5338R Reset | BCM 13 | 525 | Output | DAC reset (active low pulse) |

### GPIO Reset Sequence

The reset sequence (performed by `reset_lgw.sh`) follows this order:

1. **Export** all GPIO pins via sysfs
2. **Set direction** to output for all pins
3. **Power enable** (BCM18) → HIGH
4. **SX1302 reset**: HIGH → LOW (active high pulse)
5. **SX1261 reset**: LOW → HIGH (active low pulse)
6. **AD5338R reset**: LOW → HIGH (active low pulse)
7. **Wait** 1 second for stabilization

A full power cycle (`power_cycle_lgw.sh`) additionally:
1. Powers OFF the concentrator (BCM18 → LOW)
2. Waits **3 seconds** for capacitor discharge (critical for clearing SX1250 analog state)
3. Powers ON and performs the logic reset sequence

### GPIO Configuration in wm1303_ui.json

GPIO pins are configurable via the SSOT configuration file:

```json
"gpio_pins": {
    "sx1302_reset": 17,
    "sx1302_power_en": 18,
    "sx1261_reset": 5,
    "ad5338r_reset": 13,
    "gpio_base_offset": 512
}
```

## HAL Modifications

The Semtech SX1302 HAL (version 2.10) has been modified in three key areas. These changes are maintained as overlay files applied during installation, keeping the fork repository clean.

### 1. AGC Debounce (loragw_hal.c)

**What was changed:** Added a debounce mechanism to the Automatic Gain Control (AGC) reload that occurs after each TX transmission.

**Why:** The original HAL reloads AGC firmware after every TX→RX transition. With rapid successive transmissions (common in bridge forwarding to multiple channels), this caused AGC oscillation and RX sensitivity degradation. Multiple AGC reloads within milliseconds led to unstable receiver gain.

**How it works:**

```c
// POST-TX AGC RELOAD: detect TX->TX_FREE transition (debounced)
{
    static uint8_t _ptx = TX_FREE;
    static struct timeval _last_agc_reload = {0, 0};
    uint8_t _ctx = sx1302_tx_status(CONTEXT_BOARD.clksrc);
    if (_ptx != TX_FREE && _ctx == TX_FREE) {
        struct timeval now;
        gettimeofday(&now, NULL);
        long elapsed_ms = (now.tv_sec - _last_agc_reload.tv_sec) * 1000 +
                          (now.tv_usec - _last_agc_reload.tv_usec) / 1000;
        if (elapsed_ms >= 500 || _last_agc_reload.tv_sec == 0) {
            // Perform AGC reload
            sx1302_agc_reload(CONTEXT_RF_CHAIN[CONTEXT_BOARD.clksrc].type);
            _last_agc_reload = now;
        } else {
            // Skip — too soon since last reload
        }
    }
    _ptx = _ctx;
}
```

The debounce enforces a minimum **500 ms** interval between AGC reloads, preventing the oscillation problem while still maintaining receiver sensitivity recovery after TX.

### 2. FEM/LNA Register Management (loragw_sx1302.c)

**What was changed:** Modified the PA (Power Amplifier) and LNA (Low Noise Amplifier) Look-Up Table (LUT) register values in the AGC start function.

**Why:** The default register values caused suboptimal FEM (Front-End Module) behavior:
- The LNA would shut down during TX→RX transitions, causing a brief period of no RX sensitivity
- The PA LUT values needed adjustment for the specific SKY66420 FEM on the WM1303 HAT

**Register values:**

| Register | Value | Purpose |
|----------|-------|---------|
| `LUT_TABLE_A_PA_LUT` | `0x04` (half-duplex) / `0x0C` (full-duplex) | PA enable: RADIO_CTRL[2] high when PA_EN=1 |
| `LUT_TABLE_A_LNA_LUT` | `0x03` | LNA: RADIO_CTRL[1] HIGH in IDLE+RX — prevents FEM shutdown during TX→RX transition |
| `LUT_TABLE_B_PA_LUT` | `0x04` | PA for RF chain B |
| `LUT_TABLE_B_LNA_LUT` | `0x03` | LNA for RF chain B |

The critical change is `LNA_LUT = 0x03`, which keeps the LNA active during idle and RX states. This was determined through extensive A/B testing:

| LNA LUT Value | Result |
|---------------|--------|
| `0x02` | ~1 dB better gain but 2 dB more noise |
| `0x03` | Best compromise: -3 dB noise figure, +12 dB gain |
| `0x0F` | Maximum gain but excessive noise, unstable |

**RSSI filter configuration:**

```c
// RSSI baseband and decimation filter settings
RSSI_BB_FILTER_ALPHA_RADIO_A  = 0x03
RSSI_DEC_FILTER_ALPHA_RADIO_A = 0x07
RSSI_BB_FILTER_ALPHA_RADIO_B  = 0x03
RSSI_DEC_FILTER_ALPHA_RADIO_B = 0x07
RSSI_DB_DEFAULT_VALUE_A       = 23
```

### 3. Spectral Scan Exposure (loragw_sx1302.h)

**What was changed:** The header file exposes additional constants and types needed by the Python backend for proper SX1302 model detection and spectral scan configuration.

**Why:** The Python ctypes wrapper needs access to chip model identification (SX1302 vs SX1303) and IF chain type definitions for proper configuration.

```c
typedef enum {
    CHIP_MODEL_ID_SX1302 = 0x02,
    CHIP_MODEL_ID_SX1303 = 0x03,
    CHIP_MODEL_ID_UNKNOWN
} sx1302_model_id_t;
```

### 4. Packet Forwarder Modifications (lora_pkt_fwd.c)

**What was changed:** The standard Semtech packet forwarder has been adapted for use as a local UDP bridge rather than a cloud-connected LoRaWAN forwarder.

**Why:** The original packet forwarder is designed to forward packets to a LoRaWAN Network Server over the internet. In this project, it serves as a local intermediary between the HAL and the Python backend, communicating exclusively via localhost UDP.

Key modifications include:
- Removal of cloud server connectivity
- Optimized local UDP handling for low-latency bridge operation
- Spectral scan thread integration for SX1261 data
- Configuration loading from `global_conf.json` with WM1303-specific settings

## FEM/LNA/AGC/PA Issues and Solutions

During development, several hardware-level challenges were encountered and resolved:

### AGC Instability After TX

**Problem:** After each TX transmission, the AGC (Automatic Gain Control) would recalibrate. With bridge forwarding to 3 channels, rapid AGC reloads caused oscillation, degrading RX sensitivity by up to 10 dB.

**Solution journey:**
1. Initially tried disabling AGC entirely — stable but suboptimal RX sensitivity
2. Tried custom AGC recovery logic — complex and unreliable
3. Implemented MCU firmware reload after AGC corruption — too slow
4. **Final solution:** AGC debounce (500 ms minimum interval) in the HAL C code. This allows normal AGC operation while preventing the rapid-fire reloads that caused instability.

### FEM LNA Register Values

**Problem:** After TX, the FEM's LNA would briefly shut down during the TX→RX transition, creating a "blind window" where RX sensitivity dropped significantly.

**Solution journey:**
1. Tested `LNA_LUT = 0x0F` (maximum gain) — too much noise
2. Tested `LNA_LUT = 0x02` — better gain but higher noise floor
3. **Final solution:** `LNA_LUT = 0x03` — keeps LNA active in IDLE and RX states, preventing shutdown during transitions

### PA LUT Configuration

**Problem:** TX power output did not match configured values. The Power Amplifier LUT mapping was incorrect for the SKY66420 FEM.

**Solution:** `PA_LUT = 0x04` for half-duplex operation (which the WM1303 uses). This correctly enables RADIO_CTRL[2] when PA_EN=1 for proper TX power delivery.

The TX gain LUT in `global_conf.json` maps requested RF power (12-27 dBm) to specific `pa_gain` and `pwr_idx` combinations.

### SX1250 Desensitization After Prolonged TX

**Problem:** After extended periods of bridge operation with frequent TX, the SX1250 analog frontend would become desensitized, reducing RX performance.

**Solution:** The `power_cycle_lgw.sh` script performs a full power cycle with a **3-second** power-off period to allow capacitors to fully discharge and reset the SX1250 analog state. This is more thorough than a simple logic reset.

## AD5338R DAC Investigation

The AD5338R is a dual-channel, 10-bit DAC present on some WM1302/WM1303 Pi HAT variants. It appears in the reference design documentation with `AD5338R_RESET_PIN=13` (BCM13).

**Current status:** Not actively used in this project. Investigation revealed:

- The DAC is referenced in CN490 full-duplex reference designs
- GPIO 13 (BCM) is configured as a reset pin but not otherwise driven
- Potential use cases include fine-grained gain control or noise floor calibration
- The reset script toggles the AD5338R reset pin as part of the standard sequence, ensuring a clean state

Future investigation may explore whether the DAC can be used for:
- Dynamic TX power fine-tuning
- Noise floor offset calibration
- Automatic gain compensation

## SX1261 as Spectrum Analyzer

The SX1261 companion chip serves as an independent RF spectrum analyzer, operating on its own SPI device (spidev0.1) without interfering with the main SX1302 concentrator.

### Capabilities

| Function | Description | Usage |
|----------|-------------|-------|
| **Spectral Scan** | Sweeps frequency band measuring RSSI at each frequency step (dynamic range based on RF chain center freq) | Noise floor monitoring (every 30s) |
| **CAD (Channel Activity Detection)** | Hardware CAD with preamble correlation via SX1261 opcodes, plus software spectral analysis | Pre-TX activity check (HW + SW implementation) |
| **RSSI Measurement** | Point measurements at specific frequencies | LBT (Listen Before Talk) per-channel checks |

### Spectral Scan Configuration

Configured in `global_conf.json`:

```json
"SX1261_conf": {
    "spi_path": "/dev/spidev0.1",
    "rssi_offset": 0,
    "spectral_scan": {
        "enable": true,
        "freq_start": 863000000,
        "freq_hz_stop": 870000000,
        "nb_chan": 36,
        "pace_s": 1
    },
    "lbt": {
        "enable": false
    }
}
```

Note: HAL-level LBT is explicitly disabled (`lbt.enable: false`). The software LBT implementation in the Python backend provides per-channel control and uses the spectral scan data instead. See [LBT & CAD](lbt_cad.md) for details.

### How Spectral Scan Data Is Used

1. **NoiseFloorMonitor** triggers a scan harvest every 30 seconds
2. The monitor waits for a TX-free window using retry logic (0% TX overhead)
3. The SX1261 sweeps the band via spidev0.1
4. Results are written to `/tmp/pymc_spectral_results.json`
5. Per-channel frequency matching extracts RSSI values for each channel
6. Values feed into rolling buffers (20 samples) for noise floor averaging
7. The Spectrum tab in the UI visualizes the scan results

### Hardware CAD Capabilities

In addition to spectral scanning, the SX1261 supports **hardware Channel Activity Detection (CAD)** with preamble correlation. This provides more accurate LoRa signal detection than software-only spectral analysis.

**HAL-level implementation:**

- **Register definitions** — CAD opcodes and configuration registers are defined in `sx1261_defs.h` in the HAL overlay
- **CAD scan functions** — `loragw_sx1261.h` and `loragw_sx1261.c` implement the hardware CAD scan with preamble correlation support
- **Integration with spectral sweep** — CAD execution is triggered after each spectral sweep cycle in the packet forwarder (`lora_pkt_fwd.c`)
- **Result reporting** — CAD results are written into the `spectral_results` JSON file alongside RSSI data, for backend consumption
- **Per-channel configuration** — CAD can be configured with per-channel frequency and spreading factor parameters

**HW vs SW CAD comparison:**

| Method | Detection Technique | Accuracy | Speed |
|--------|-------------------|----------|-------|
| Hardware CAD | SX1261 preamble correlation (register-level) | Higher — detects actual LoRa preambles | Fast — hardware-accelerated |
| Software CAD | Spectral histogram analysis of RSSI data | Lower — energy pattern matching only | Slower — post-processing of scan data |

The backend tracks both HW and SW CAD results with per-channel counters (`cad_hw_clear`, `cad_hw_detected`, `cad_sw_clear`, `cad_sw_detected`, `cad_last_source`). The TX queue scheduler reads the `cad_result` source field and increments the appropriate HW/SW counter. See [LBT & CAD](lbt_cad.md) for the full CAD integration details.

---

*See also: [Radio Configuration](radio.md) | [LBT & CAD](lbt_cad.md) | [System Architecture](architecture.md)*
