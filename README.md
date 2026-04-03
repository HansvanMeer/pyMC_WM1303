# pyMC_WM1303

**WM1303 (SX1302/SX1303) LoRa Concentrator Module for MeshCore**

A complete installation and management system for running a [WM1303 LoRa concentrator](https://www.seeedstudio.com/WM1303-LoRaWAN-Gateway-Module-SX1303-p-5154.html) with [MeshCore](https://meshcore.co) ([pyMC_core](https://github.com/rightup/pyMC_core) & [pyMC_Repeater](https://github.com/rightup/pyMC_Repeater)) on a SenseCAP M1 / Raspberry Pi.

![WM1303 Manager — Status Dashboard](screenshots/status.jpg)

---

## Overview

This project integrates the Semtech SX1302/SX1303-based WM1303 LoRa concentrator HAT with the MeshCore mesh networking stack, providing:

- **Multi-channel LoRa gateway** — Up to 4 simultaneous receive channels across 2 RF chains
- **MeshCore mesh repeater** — Bridge packets between channels, forward across the mesh network
- **Web-based management UI** — Real-time monitoring, channel configuration, spectrum analysis
- **REST API** — Full programmatic control of all gateway functions
- **Automated installation** — Single-script setup from bare Raspberry Pi OS

The system uses an **overlay approach**: unmodified forks of the upstream repositories are cloned during installation, then WM1303-specific modifications are applied on top. This keeps the forks clean while allowing custom hardware integration.

## Features

### Radio & Hardware
- Dual RF chain support (RF0 + RF1) with independent frequency and spreading factor configuration
- Up to 8 IF demodulators (4 per RF chain) for multi-SF reception
- SX1261 companion chip integration for spectral scanning, noise floor monitoring, and Listen Before Talk (LBT)
- GPIO-based hardware control (power, reset, SX1261, AD5338R) with configurable pin mapping
- Automatic AGC management with debounce protection
- FEM (Front-End Module) LNA/PA register management

### Software
- Bridge engine with configurable rules (Single Source of Truth) for inter-channel packet forwarding
- Global TX scheduler with round-robin queuing, batch windows, and echo prevention
- Software LBT/CAD (Listen Before Talk / Channel Activity Detection) per channel
- RX watchdog with 3 automatic detection modes and packet forwarder recovery
- Packet deduplication with configurable TTL
- SQLite database for metrics, signal quality history, and dashboard data
- JWT-based authentication for API and web interface
- Systemd service with security hardening and auto-restart

### Management Interface
- **WM1303 Manager** — Single-page web application for real-time gateway management
- **Channels tab** — Per-channel status, statistics, and configuration
- **Spectrum tab** — Real-time spectral scan visualization with waterfall display
- **Bridge tab** — Visual bridge rule configuration between channels
- **Signal Quality** — RSSI/SNR charts with historical data
- **LBT/CAD charts** — Listen Before Talk decision visualization
- **Advanced Config** — GPIO pins, IF chains, RF chain parameters

## Hardware Requirements

| Component | Specification |
|-----------|---------------|
| **Single Board Computer** | SenseCAP M1 or Raspberry Pi 3B+/4/5 |
| **LoRa Module** | WM1303 LoRaWAN Gateway Module (SX1302/SX1303 + SX1261 + dual SX1250) |
| **HAT/Interface** | SenseCAP M1 Pi HAT or compatible SPI interface |
| **OS** | Raspberry Pi OS Lite (Bookworm or newer) |
| **SPI** | Must be enabled in `/boot/firmware/config.txt` |
| **Internet** | Required during installation for package downloads |

## Quick Start

### 1. Install

**One-line installer** (recommended):

```bash
curl -sSL https://raw.githubusercontent.com/HansvanMeer/pyMC_WM1303/main/bootstrap.sh | sudo bash
```

**Or manually**:

```bash
git clone https://github.com/HansvanMeer/pyMC_WM1303.git
cd pyMC_WM1303
sudo bash install.sh
```

The installation script handles everything:
- System package updates and build tool installation
- SPI configuration verification
- Repository cloning (HAL, pyMC_core, pyMC_Repeater)
- Overlay application (WM1303-specific modifications)
- HAL and packet forwarder compilation
- Python virtual environment setup
- Configuration file deployment
- GPIO reset script generation
- Systemd service installation and startup
- NTP synchronization verification

### 2. Setup Wizard

After installation, open the pyMC Repeater web interface:

```
http://<your-pi-ip>:8000
```

The setup wizard will guide you through the initial configuration. When prompted for hardware type, select **WM1303 LoRa Concentrator**.

> ⚠️ **Important:** The pyMC Repeater dashboard has a **Configure → Radio Settings** page. These radio settings are **NOT used** when running with the WM1303 concentrator. All radio configuration is managed exclusively through the **WM1303 Manager** interface (see next steps). You can safely ignore the Radio Settings page.

### 3. Configure Channels

Once the setup wizard is complete, open the **WM1303 Manager**:

```
http://<your-pi-ip>:8000/wm1303.html
```

Navigate to the **Channels** tab to configure your RF chains and channel assignments:

![WM1303 Manager — Channels: RF Chain Configuration](screenshots/channels-1.jpg)

In the upper section you configure the **RF Chain** settings:
- **RF Center Frequency** — Set the center frequency for your RF chain (e.g., `869.5245 MHz` for EU868). Both SX1250 radios share this center frequency.
- The **Analog Bandwidth Coverage** visualization shows you exactly which frequencies your IF chains cover.

Scroll down to configure the **IF Chains** and **Channel Definitions**:

![WM1303 Manager — Channels: IF Chain & Channel Configuration](screenshots/channels-2.jpg)

In the lower section:
- **IF Chain Configuration** — Maps IF demodulators to RF chains with frequency offsets. Each active IF chain becomes a receive slot.
- **Channel Definition Table** — Configure each channel with:
  - **Channel Name** — Friendly name (e.g., `sf8`, `sf7`)
  - **Frequency** — Exact RX/TX frequency in MHz
  - **Spreading Factor (SF)** — SF7 through SF12
  - **Bandwidth** — Typically 125 kHz
  - **Coding Rate** — Typically 4/5
  - **Preamble** — LoRa preamble length
  - **LBT / CAD** — Enable/disable Listen Before Talk and Channel Activity Detection per channel
  - **TX Power** — Transmit power in dBm
  - **Active** — Enable/disable the channel
- **Radio Summary** — Shows live RX/TX statistics including duty cycle
- **Channel Status** — Real-time per-channel metrics (RX/TX packets, pending, sent, failed, TX duty)

Click **Save & Restart** after making changes to apply the new channel configuration.

### 4. Configure Bridge Rules

Navigate to the **Bridge Rules** tab to define how packets are forwarded between channels:

![WM1303 Manager — Bridge Rules](screenshots/bridge-rules.jpg)

Bridge rules define the **forwarding paths** between your channels and the MeshCore repeater:
- **Source → Target** — Each rule specifies a source (left) and a target (right)
- For a basic 2-channel repeater setup, create 4 rules:
  1. `SF8 (869.461 MHz)` → `Repeater` — Forward packets received on channel A to the mesh repeater
  2. `SF7 (869.588 MHz)` → `Repeater` — Forward packets received on channel B to the mesh repeater
  3. `Repeater` → `SF8 (869.461 MHz)` — Forward mesh repeater packets out on channel A
  4. `Repeater` → `SF7 (869.588 MHz)` — Forward mesh repeater packets out on channel B
- **Pkt Types** — Optionally filter which packet types are forwarded per rule
- **TX Delay** — Add a delay (in ms) before forwarding
- Use the **toggle** on each rule to enable/disable it

Click **Save & Restart** to activate the bridge rules.

### 5. Verify Operation

Go back to the **Status** tab to verify everything is running:
- Both services (`lora_pkt_fwd` and `pymc-repeater`) should show **RUNNING**
- The **Packet Activity** chart should start showing RX/TX activity
- Active channels should be counted in the summary cards

See [docs/installation.md](docs/installation.md) for detailed installation instructions and troubleshooting.

## Upgrading

```bash
cd pyMC_WM1303
git pull
sudo bash upgrade.sh
```

Options:
- `--rebuild` — Force HAL/packet forwarder rebuild
- `--force-config` — Overwrite existing configuration with templates
- `--skip-pull` — Skip pulling from fork repositories

The upgrade script automatically backs up your configuration before making changes.

## WM1303 Manager UI

The WM1303 Manager is a single-page web application accessible at `http://<your-pi-ip>:8000/wm1303.html`. It provides real-time monitoring and configuration across multiple tabs:

### Status
Hardware overview, service health, packet activity charts, and active channel summary.

![Status Dashboard](screenshots/status.jpg)

### Spectrum
SX1261-based spectral scanning, per-channel signal quality (RSSI/SNR), channel activity monitoring, and frequency spectrum visualization.

![Spectrum & Signal Quality](screenshots/spectrum-1.jpg)

### Deduplication
Packet deduplication statistics showing forwarded, duplicate, echo, and filtered packets with historical charts.

![Deduplication Statistics](screenshots/dedup.jpg)

### Channels
RF chain configuration, IF chain-to-channel mapping, per-channel settings, and live radio statistics.

See [Quick Start — Step 3](#3-configure-channels) above for details.

### Bridge Rules
Visual bridge rule editor for inter-channel packet forwarding.

See [Quick Start — Step 4](#4-configure-bridge-rules) above for details.

## Repository Structure

```
pyMC_WM1303/
├── install.sh              # Full installation script (12 phases)
├── upgrade.sh              # Upgrade script (8 phases)
├── README.md               # This file
├── TODO.md                 # Development task tracking
├── LICENSE                 # MIT License
├── config/                 # Configuration templates
│   ├── config.yaml.template    # Main application config
│   ├── wm1303_ui.json          # UI & channel config (SSOT)
│   ├── global_conf.json        # HAL configuration
│   ├── pymc-repeater.service   # Systemd service file
│   ├── reset_lgw.sh            # GPIO reset script template
│   └── power_cycle_lgw.sh      # Power cycle script template
├── overlay/                # Source code modifications
│   ├── hal/                    # HAL & packet forwarder patches
│   │   ├── libloragw/          #   AGC, FEM, LNA, spectral scan
│   │   └── packet_forwarder/   #   TX/RX handling modifications
│   ├── pymc_core/              # MeshCore core library additions
│   │   └── hardware/           #   WM1303Backend, TXQueue, SX1261, etc.
│   └── pymc_repeater/          # MeshCore repeater modifications
│       └── repeater/           #   Bridge engine, API, UI, config
├── screenshots/            # UI screenshots
├── docs/                   # Comprehensive documentation
│   ├── architecture.md         # System architecture overview
│   ├── hardware.md             # Hardware & HAL details
│   ├── radio.md                # Radio configuration guide
│   ├── software.md             # Software components
│   ├── ui.md                   # WM1303 Manager UI guide
│   ├── api.md                  # REST API reference
│   ├── lbt_cad.md              # LBT & CAD documentation
│   ├── tx_queue.md             # TX queue & scheduling
│   ├── configuration.md        # Configuration file reference
│   ├── installation.md         # Installation & upgrade guide
│   └── repositories.md         # Repository information
└── scripts/                # Utility scripts
```

## Used Fork Repositories

This project uses unmodified forks of the upstream repositories. **Do not modify the forks directly** — all changes are managed through the overlay directory.

| Repository | Branch | Purpose |
|------------|--------|---------|
| [sx1302_hal](https://github.com/HansvanMeer/sx1302_hal) | `master` | Semtech HAL v2.10 for SX1302/SX1303 |
| [pyMC_core](https://github.com/HansvanMeer/pyMC_core) | `dev` | MeshCore core library (radio drivers, mesh protocol) |
| [pyMC_Repeater](https://github.com/HansvanMeer/pyMC_Repeater) | `dev` | MeshCore repeater (bridge, web UI, API, service) |

See [docs/repositories.md](docs/repositories.md) for details on the overlay approach.

## Documentation

| Document | Description |
|----------|-------------|
| [Architecture](docs/architecture.md) | System architecture, component relationships, data flow |
| [Hardware](docs/hardware.md) | WM1303 module, SPI, GPIO, HAL modifications, FEM/LNA/AGC |
| [Radio](docs/radio.md) | RF/IF chains, channels, frequency planning, sync word |
| [Software](docs/software.md) | Backend components, bridge engine, watchdog, database |
| [UI Guide](docs/ui.md) | WM1303 Manager web interface |
| [API Reference](docs/api.md) | REST API endpoints and authentication |
| [LBT & CAD](docs/lbt_cad.md) | Listen Before Talk and Channel Activity Detection |
| [TX Queue](docs/tx_queue.md) | TX processing flow, queue management, scheduling |
| [Configuration](docs/configuration.md) | All configuration files explained |
| [Installation](docs/installation.md) | Installation, upgrade, and troubleshooting guide |
| [Repositories](docs/repositories.md) | Fork repos, branches, and overlay approach |

## Technology Stack

| Layer | Technology |
|-------|------------|
| **Hardware Abstraction** | C (Semtech HAL v2.10, libloragw, lora_pkt_fwd) |
| **Backend** | Python 3 (pyMC_core, pyMC_Repeater, CherryPy) |
| **Frontend** | HTML5, JavaScript, CSS (single-page application) |
| **Charts** | Chart.js with chartjs-adapter-date-fns |
| **Database** | SQLite (metrics, signal history, dedup events) |
| **Service** | systemd (pymc-repeater.service) |
| **Communication** | SPI (HAL ↔ SX1302), UDP (pkt_fwd ↔ backend), WebSocket (backend ↔ UI) |

## Service Management

```bash
# Start / stop / restart
sudo systemctl start pymc-repeater
sudo systemctl stop pymc-repeater
sudo systemctl restart pymc-repeater

# View logs
journalctl -u pymc-repeater -f

# Check status
sudo systemctl status pymc-repeater
```

## Disclaimer

> **⚠️ Use at your own risk.** This software interacts directly with radio hardware via SPI and GPIO.
> The authors accept no responsibility for any damage to hardware, loss of data, or regulatory
> non-compliance resulting from the use of this software. Ensure your radio configuration complies
> with local regulations (e.g., EU868 duty cycle limits).

## License

This project is licensed under the MIT License — see the [LICENSE](LICENSE) file for details.
