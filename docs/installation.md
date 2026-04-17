# Installation Guide

> Complete installation instructions for the WM1303 Bridge/Repeater system

## Prerequisites

### Hardware
- **SenseCAP M1** (Raspberry Pi 4 + WM1303 LoRa HAT) — or compatible Pi 4 with WM1303 HAT
- MicroSD card (16 GB+ recommended)
- Ethernet or Wi-Fi connection
- Antenna connected to the WM1303 HAT

### Software
- **Raspberry Pi OS Lite** (Bookworm or newer) — freshly flashed
- SSH access enabled
- Internet connectivity (for package installation and git clone)

## Fresh Installation

### Quick Start

```bash
git clone https://github.com/HansvanMeer/pyMC_WM1303.git
cd pyMC_WM1303
sudo bash install.sh
```

The installation script handles everything automatically. It will take 15–30 minutes depending on your internet speed.

### What the Install Script Does

The installation is divided into clearly labeled steps. All actions produce visible output during execution.

#### Step 1: System Update
- `apt update && apt upgrade`
- Ensures the system is up to date

#### Step 2: Install Build Dependencies
- Installs packages required for compiling C code and building Python packages:
  - `build-essential`, `gcc`, `make`, `cmake`
  - `git`, `python3-pip`, `python3-venv`
  - `libsqlite3-dev`, `libffi-dev`
  - And other required development packages

#### Step 3: Enable SPI Interface
- Enables SPI via `raspi-config` or `/boot/firmware/config.txt`
- Required for communication with SX1302 and SX1261

#### Step 4: Configure SPI Buffer Size
- Sets `spidev bufsiz=32768` (up from default 4096)
- Configured via **two methods** for maximum compatibility:
  1. `/etc/modprobe.d/spidev.conf` (older kernels)
  2. `/boot/firmware/cmdline.txt` (Debian Trixie and newer)
- Required for the optimized 16 KB SPI burst transfers
- **Reboot required** after installation for this to take effect

#### Step 5: Configure User Permissions
- Creates `/etc/sudoers.d/010_pi-nopasswd` for passwordless sudo
- Adds `pi` user to hardware groups: `spi`, `i2c`, `gpio`, `dialout`
- Required for GPIO reset, pkt_fwd management, and hardware access

#### Step 6: Clone Repositories
Clones the required upstream forks:

```
/opt/pymc_repeater/repos/pyMC_core/       ← dev branch
/opt/pymc_repeater/repos/pyMC_Repeater/   ← dev branch
/home/pi/sx1302_hal/                      ← HAL v2.10
```

#### Step 7: Apply Overlay Files
Copies overlay files from this repository into the cloned repos:

- HAL source files → `sx1302_hal/libloragw/` and `sx1302_hal/packet_forwarder/`
- pymc_core hardware files → `pyMC_core/src/pymc_core/hardware/`
- pymc_repeater files → `pyMC_Repeater/repeater/`

This is the core of the overlay strategy — upstream repos are not modified, only extended.

#### Step 8: Build HAL and Packet Forwarder
- Compiles `libloragw.a` (SX1302 HAL library)
- Compiles `lora_pkt_fwd` (packet forwarder binary)
- Copies binary to `/home/pi/wm1303_pf/`
- Copies configuration files

#### Step 9: Install pyMC Core and Repeater
- Creates Python virtual environment
- Installs pyMC_core (dev) in development mode
- Installs pyMC_Repeater (dev) in development mode
- Installs Python dependencies

#### Step 10: Copy Configuration Files
- `config.yaml.template` → `/etc/pymc_repeater/config.yaml`
- `wm1303_ui.json` → `/etc/pymc_repeater/wm1303_ui.json`
- `reset_lgw.sh` and `power_cycle_lgw.sh` → `/home/pi/wm1303_pf/`
- GPIO reset and power cycle scripts

#### Step 11: Install and Enable systemd Service
- Copies `pymc-repeater.service` to `/etc/systemd/system/`
- Enables auto-start on boot
- Starts the service

#### Step 12: JWT Token Generation
- The pyMC Repeater generates a JWT token on first start
- This token is used for API authentication

#### Step 13: Version Tracking
- Writes the installed version to `/etc/pymc_repeater/version`

#### Step 14: NTP Verification
- Checks that the NTP client is properly configured and syncing
- Accurate time is important for packet timestamps and log correlation

## Post-Installation

### Verify Installation

```bash
# Check service status
sudo systemctl status pymc-repeater

# Check version
cat /etc/pymc_repeater/version

# Check pkt_fwd is running
ps aux | grep lora_pkt_fwd

# Check logs
journalctl -u pymc-repeater -f
```

### Access the Web Interface

Open a browser and navigate to:
```
http://<pi-ip>:8000/wm1303.html
```

The WM1303 Manager should show the Status tab with channel information.

### Verify Channel E

In the Status tab, verify that Channel E appears with its own RSSI/SNR/noise floor values. In the Channels tab, verify that Channel E (SX1261) has a separate configuration section.

### Verify Bridge Configuration

Check that `bridge_conf.json` was generated correctly:

```bash
# Verify unused IF channels are disabled
sudo cat /home/pi/wm1303_pf/bridge_conf.json | python3 -c "
import sys, json
c = json.load(sys.stdin)
for i in range(8):
    k = f'chan_multiSF_{i}'
    if k in c.get('SX130x_conf', {}):
        ch = c['SX130x_conf'][k]
        print(f'{k}: enable={ch.get(\"enable\")}, if={ch.get(\"if\", 0)}')
"
```

Expected: only actively configured channels should show `enable: true`.

## Upgrade

### One-Liner Bootstrap Upgrade

```bash
curl -sSL https://raw.githubusercontent.com/HansvanMeer/pyMC_WM1303/main/upgrade_bootstrap.sh | sudo bash
```

### Manual Upgrade

```bash
cd /tmp
git clone https://github.com/HansvanMeer/pyMC_WM1303.git pymc_wm1303_upgrade
cd pymc_wm1303_upgrade
sudo bash upgrade.sh
```

### From Existing Clone

```bash
cd ~/pyMC_WM1303
git pull origin main
sudo bash upgrade.sh
```

### Force HAL Rebuild

If HAL overlay files changed (C code modifications):

```bash
sudo bash upgrade.sh --force-rebuild
```

A reboot is recommended after upgrades that change SPI configuration.

### What the Upgrade Script Does

1. Pulls latest code from all repositories
2. Applies updated overlay files
3. **Deep-merges** new config keys into existing `wm1303_ui.json` (preserving user settings)
4. Rebuilds HAL if overlay checksums changed (or if `--force-rebuild`)
5. Restarts the service
6. Updates `/etc/pymc_repeater/version`

### Configuration Preservation

During upgrade:
- **User settings are preserved** — the merge adds new keys without overwriting existing values
- **Deep merge** handles nested dictionaries (e.g., new Channel E sub-keys)
- **bridge_conf.json is regenerated** on service restart from the merged SSOT

## Troubleshooting

### Service fails to start
```bash
journalctl -u pymc-repeater -n 50
```
Common causes:
- Missing sudo permissions → check `/etc/sudoers.d/010_pi-nopasswd`
- SPI not enabled → check `/boot/firmware/config.txt` for `dtparam=spi=on`
- Missing Python dependencies → reinstall in venv

### pkt_fwd crashes on startup
- Check SPI device availability: `ls -la /dev/spidev0.*`
- Check GPIO permissions
- Verify HAL compilation was successful
- Check `bridge_conf.json` for valid configuration

### No RX packets
- Verify antenna is connected
- Check channel frequency matches your MeshCore network
- Check pkt_fwd stdout for RX activity
- Verify IF chain configuration in `bridge_conf.json`

### spidev bufsiz not applied
After installation, verify:
```bash
cat /sys/module/spidev/parameters/bufsiz
```
Should show `32768`. If not, reboot the Pi.

## Directory Structure (After Installation)

```
/opt/pymc_repeater/                    # Main installation
├── repos/
│   ├── pyMC_core/                     # Core library (dev)
│   └── pyMC_Repeater/                 # Repeater app (dev)
│
/etc/pymc_repeater/                    # Configuration
├── config.yaml
├── wm1303_ui.json
└── version
│
/home/pi/
├── sx1302_hal/                        # HAL source (compiled)
├── wm1303_pf/                         # Packet forwarder runtime
│   ├── lora_pkt_fwd
│   ├── bridge_conf.json
│   ├── global_conf.json
│   ├── reset_lgw.sh
│   └── power_cycle_lgw.sh
└── pyMC_WM1303/                       # This repository (if cloned here)
│
/etc/systemd/system/
└── pymc-repeater.service
│
/etc/sudoers.d/
└── 010_pi-nopasswd
│
/etc/modprobe.d/
└── spidev.conf                        # spidev bufsiz=32768
```

## Related Documents

- [`hardware.md`](./hardware.md) — Hardware requirements
- [`configuration.md`](./configuration.md) — Configuration files
- [`repositories.md`](./repositories.md) — Repository structure
- [`architecture.md`](./architecture.md) — System architecture
