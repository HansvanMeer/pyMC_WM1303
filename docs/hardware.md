# Hardware

> Hardware components, SPI layout, GPIO, and platform details for the WM1303 Pi HAT

## Platform

| Component | Details |
|-----------|--------|
| **Board** | SenseCAP M1 |
| **SoC** | Raspberry Pi 4 (BCM2711) |
| **OS** | Raspberry Pi OS Lite (Bookworm or newer) |
| **Concentrator** | WM1303 LoRa HAT |
| **Main Radio** | SX1302/SX1303 baseband + 2x SX1250 RF front-ends |
| **Companion Radio** | SX1261 |

## WM1303 Pi HAT Module

The WM1303 HAT sits on the Raspberry Pi GPIO header and contains:

### SX1302/SX1303 Baseband Processor
- 8 multi-SF IF chain demodulators
- 1 single-SF demodulator
- 1 FSK demodulator
- AGC (Automatic Gain Control) with debounced management
- Timestamp engine for precise RX timing
- Connected to two SX1250 RF front-ends

### SX1250_0 (RF0)
- RF front-end radio 0
- **TX + RX capable**
- Center frequency configurable
- Used as primary TX chain

### SX1250_1 (RF1)
- RF front-end radio 1
- **RX only**
- Center frequency configurable
- Provides additional RX coverage

### SX1261 Companion Chip
- Independent radio with its own SPI connection
- **Full RX/TX capability** (enabled since v2.0.0)
- Spectral scan engine
- LBT (Listen Before Talk) measurement
- CAD (Channel Activity Detection)
- Supports sub-125 kHz bandwidths (62.5 kHz)
- See [`channel_e_sx1261.md`](./channel_e_sx1261.md) for details

### AD5338R DAC
- I2C DAC on the HAT (GPIO 13 / `AD5338R_RESET_PIN`)
- Referenced in WM1302 CN490 full-duplex design
- **Not currently used** in this implementation
- Potential future use for gain control or TX power management

## SPI Layout

The WM1303 HAT uses two SPI chip-select lines:

| SPI Device | Connected To | Speed | Purpose |
|------------|-------------|-------|--------|
| `/dev/spidev0.0` | SX1302 + SX1250s | **16 MHz** | Main concentrator path (RX/TX for Channels A–D) |
| `/dev/spidev0.1` | SX1261 | Via HAL | Channel E RX/TX, spectral scan, LBT, CAD |

### SPI Split — Why It Matters

The separate SPI paths provide **isolation** between the concentrator and companion radio:

- SX1261 operations (scan, LBT, CAD, Channel E) run **independently** from the main RX/TX path
- The concentrator RX path is not disrupted by SX1261 activity
- TX latency from LBT checks remains low
- This supports the design principle of maximum RX availability

### SPI Optimizations (v2.0.1)

The SPI driver was optimized for significantly faster transfers:

| Parameter | Original | Optimized |
|-----------|---------|----------|
| `LGW_BURST_CHUNK` | 1024 bytes | **16,384 bytes** (16x) |
| `SPI_SPEED` | 2 MHz | **16 MHz** (8x) |

These changes reduce SPI transfer overhead and improve RX/TX timing.

### Kernel SPI Buffer (spidev bufsiz)

The kernel SPI buffer must be increased to support larger burst transfers:

```
spidev bufsiz = 32768 bytes (default: 4096)
```

Configured via two methods for maximum compatibility:
1. **modprobe.d** — `/etc/modprobe.d/spidev.conf` (older kernels)
2. **Kernel command line** — `spidev.bufsiz=32768` in `/boot/firmware/cmdline.txt` (Debian Trixie+)

A reboot is required after initial configuration.

### Why 16 MHz SPI is Sufficient

At 16 MHz SPI clock with 16 KB burst chunks:
- A full 256-byte LoRa packet transfer takes ~128 µs
- The concentrator RX buffer can be read in a single burst
- TX command latency is negligible compared to LoRa airtime (tens to hundreds of ms)
- The SPI bus is idle most of the time — LoRa is a low-duty-cycle protocol

## GPIO Configuration

| GPIO (BCM) | GPIO (sysfs) | Function | Direction |
|------------|-------------|----------|----------|
| BCM 17 | 529 | SX1302 Reset | Output |
| BCM 18 | 530 | SX1302 Power enable | Output |
| BCM 5 | 517 | SX1261 NSS (chip select) | Output |
| BCM 13 | 525 | AD5338R Reset | Output (unused) |

> **Note:** The GPIO base offset on Raspberry Pi 4 is 512. The sysfs GPIO number = BCM number + 512.

### Reset Sequence

The HAL initialization performs a hardware reset:

1. Assert power enable (BCM 18) LOW → power off
2. Wait 100ms
3. Assert power enable (BCM 18) HIGH → power on
4. Wait 100ms
5. Assert reset (BCM 17) LOW → reset active
6. Wait 100ms
7. Assert reset (BCM 17) HIGH → reset released
8. Wait 100ms → SX1302 ready

Scripts `reset_lgw.sh` and `power_cycle_lgw.sh` perform these sequences.

## FEM / LNA / AGC / PA

### FEM (Front End Module)
The WM1303 uses the SX1250 internal LNA (Low Noise Amplifier) and PA (Power Amplifier). No external FEM is present on the standard WM1303 HAT.

### AGC (Automatic Gain Control)
The SX1302 AGC adjusts receiver gain automatically. The implementation includes:

- **Debounced AGC management** — prevents rapid gain changes that cause signal quality issues
- **AGC reset recovery** — specific to SX1302, handles cases where AGC gets stuck
- Gain control via the `loragw_hal.c` register interface

### PA (Power Amplifier)
TX power is controlled through the SX1250 PA settings:

- Power range depends on the RF chain and antenna setup
- LUT (Look-Up Table) values define the PA DAC settings per power level
- Incorrect LUT values can cause:
  - Poor TX performance (too low)
  - Excessive power draw (too high)
  - PA damage (extreme values)

> **Warning:** Modifying PA/LUT settings without proper understanding can damage the radio hardware. No responsibility is taken for hardware damage resulting from incorrect configuration.

## Platform Preparation (Installation)

The installation process enables required platform interfaces:

1. **SPI** — enabled via `raspi-config` or `/boot/firmware/config.txt`
2. **I2C** — enabled for AD5338R DAC (even if not currently used)
3. **User permissions** — `pi` user added to `spi`, `i2c`, `gpio`, `dialout` groups
4. **Passwordless sudo** — configured via `/etc/sudoers.d/010_pi-nopasswd`
5. **spidev bufsiz** — kernel buffer increased to 32768 bytes

## Related Documents

- [`architecture.md`](./architecture.md) — System architecture
- [`channel_e_sx1261.md`](./channel_e_sx1261.md) — SX1261 companion radio
- [`installation.md`](./installation.md) — Installation guide
- [`radio.md`](./radio.md) — Radio architecture
