# Installation & Upgrade Guide

> Step-by-step instructions for installing and upgrading the WM1303 system

> **Disclaimer:** This software interacts directly with radio hardware via SPI and GPIO. Incorrect configuration, particularly of GPIO pins or TX power settings, may damage your WM1303 concentrator module or Raspberry Pi. Proceed at your own risk. The authors accept no responsibility for hardware damage resulting from the use of this software.

## Prerequisites

### Hardware

- **SenseCAP M1** or compatible Raspberry Pi 4 with WM1303 Pi HAT
- Antenna connected to the WM1303 module (never transmit without an antenna)
- Stable power supply (5V/3A recommended)

See [Hardware Overview](hardware.md) for detailed hardware specifications.

### Software

- **Raspberry Pi OS Lite** (Bookworm or newer, 64-bit recommended)
- **SPI enabled** in `/boot/firmware/config.txt`
- **I2C enabled** in `/boot/firmware/config.txt` (for temperature sensor and AD5338R DAC)
- **Internet connectivity** for package installation and repository cloning
- **SSH access** to the Raspberry Pi

### Enable SPI

SPI must be enabled before installation. If not already configured:

```bash
# Method 1: raspi-config
sudo raspi-config
# Navigate to: Interface Options → SPI → Enable

# Method 2: Manual edit
sudo nano /boot/firmware/config.txt
# Add or uncomment:
dtparam=spi=on

# Reboot to apply
sudo reboot
```

Verify SPI is working after reboot:

```bash
ls -la /dev/spidev0.*
# Expected output:
# /dev/spidev0.0  (SX1302 concentrator)
# /dev/spidev0.1  (SX1261 companion chip)
```

### Enable I2C

I2C is required for the WM1303 temperature sensor and AD5338R DAC. The installer enables this automatically, but for manual setup:

```bash
# Method 1: raspi-config
sudo raspi-config
# Navigate to: Interface Options → I2C → Enable

# Method 2: Manual edit
sudo nano /boot/firmware/config.txt
# Add or uncomment:
dtparam=i2c_arm=on

# Ensure kernel module loads at boot
echo "i2c-dev" | sudo tee /etc/modules-load.d/i2c-dev.conf

# Reboot to apply
sudo reboot
```

Verify I2C is working after reboot:

```bash
ls -la /dev/i2c-1
# Should show the I2C device node
```


---

## Quick Start

### One-Line Install

The fastest way to install on a fresh Raspberry Pi OS Lite system:

```bash
curl -sSL https://raw.githubusercontent.com/HansvanMeer/pyMC_WM1303/main/bootstrap.sh | sudo bash
```

This bootstrap script automatically installs git, clones the repository to `/home/pi/pyMC_WM1303`, and runs the full installation.

### Manual Install

Alternatively, clone and install manually:

```bash
git clone https://github.com/HansvanMeer/pyMC_WM1303.git
cd pyMC_WM1303
sudo bash install.sh
```

The installation takes approximately 10-15 minutes depending on internet speed and Pi model. After completion:

1. Open a browser and navigate to `http://<pi-ip-address>:8000/wm1303.html`
2. Log in with the admin password (shown during installation or auto-generated)
3. Configure channels and bridge rules via the [Web UI](ui.md)
4. Edit `/etc/pymc_repeater/config.yaml` to set `node_name` and other preferences

> **⚠️ Important:** The **Configure → Radio Settings** page in the pyMC Repeater dashboard is **NOT used** with the WM1303 setup. All radio configuration (channels, frequencies, TX power, LBT/CAD) is managed exclusively through the **WM1303 Manager** UI at `http://<pi-ip>:8000/wm1303.html`. Changing radio settings in the pyMC Repeater dashboard will have no effect on the WM1303 hardware.


---

## Detailed Installation Phases

The `install.sh` script executes 12 phases. Each phase displays clear progress indicators with colored output.

### Command-Line Options

```bash
sudo bash install.sh [--skip-update] [--skip-build]
```

| Option | Description |
|--------|-------------|
| `--skip-update` | Skip `apt update/upgrade` (faster install if system is already up to date) |
| `--skip-build` | Skip HAL and packet forwarder compilation (use existing binaries) |

### Phase 1: System Prerequisites

Updates the system and installs required packages.

**Actions:**
- `apt-get update` and `apt-get upgrade` (unless `--skip-update`)
- Install build tools: `build-essential`, `gcc`, `make`, `git`
- Install Python: `python3`, `python3-dev`, `python3-pip`, `python3-venv`, `python3-setuptools`
- Install libraries: `libffi-dev`, `libssl-dev`
- Install utilities: `jq`, `ntpdate`, `ntp`
- Install RRDtool: `rrdtool`, `librrd-dev`, `python3-rrdtool`

**Packages installed:**

| Package | Purpose |
|---------|---------|
| `build-essential`, `gcc`, `make` | Compile HAL and packet forwarder (C code) |
| `python3`, `python3-dev`, `python3-venv` | Python runtime and virtual environment |
| `libffi-dev`, `libssl-dev` | Cryptographic libraries for JWT and identity |
| `jq` | JSON processing for GPIO script generation |
| `ntp`, `ntpdate` | Time synchronization |
| `rrdtool`, `librrd-dev`, `python3-rrdtool` | Round-robin database for time-series data storage |

### Phase 2: SPI & I2C Configuration Check

Verifies SPI and I2C are enabled and device nodes exist.

**SPI actions:**
- Check for `spi_bcm2835` or `spidev` kernel modules
- Verify `/dev/spidev0.0` and `/dev/spidev0.1` exist
- If missing, attempt to enable SPI in boot config
- Warn if reboot is needed

**I2C actions:**
- Check for `/dev/i2c-1` device node
- Load `i2c-dev` and `i2c-bcm2835` kernel modules if needed
- Enable `dtparam=i2c_arm=on` in boot config if not present
- Configure `i2c-dev` module to load at boot
- Warn if reboot is needed for I2C activation

### Phase 3: Directory Structure Creation

Creates all required directories with correct ownership.

| Directory | Purpose |
|-----------|---------|
| `/opt/pymc_repeater/` | Base installation directory |
| `/opt/pymc_repeater/repos/` | Cloned repository storage |
| `/opt/pymc_repeater/venv/` | Python virtual environment |
| `/etc/pymc_repeater/` | Configuration files |
| `/home/pi/wm1303_pf/` | Packet forwarder runtime directory |
| `/home/pi/sx1302_hal/` | HAL source and build directory |
| `/var/log/pymc_repeater/` | Application logs |
| `/var/lib/pymc_repeater/` | Runtime data (database, state) |
| `/home/pi/backups/` | Upgrade backup storage |

All directories are owned by the `pi` user.

### Phase 4: Clone Repositories

Clones the three fork repositories. If already present, pulls the latest changes.

| Repository | Branch | Target |
|------------|--------|--------|
| [sx1302_hal](https://github.com/HansvanMeer/sx1302_hal) | `master` | `/home/pi/sx1302_hal/` |
| [pyMC_core](https://github.com/HansvanMeer/pyMC_core) | `dev` | `/opt/pymc_repeater/repos/pyMC_core/` |
| [pyMC_Repeater](https://github.com/HansvanMeer/pyMC_Repeater) | `dev` | `/opt/pymc_repeater/repos/pyMC_Repeater/` |

See [Repository Information](repositories.md) for details on the fork strategy.

### Phase 5: Apply Overlay Modifications

Copies modified files from the `overlay/` directory on top of the cloned repositories. This is how WM1303-specific changes are applied without modifying the upstream forks.

**Overlay targets:**

| Overlay Source | Target | Files |
|---------------|--------|-------|
| `overlay/hal/` | `/home/pi/sx1302_hal/` | `loragw_hal.c`, `loragw_sx1302.c`, `loragw_sx1302.h`, `lora_pkt_fwd.c`, Makefiles |
| `overlay/pymc_core/` | `/opt/pymc_repeater/repos/pyMC_core/` | `wm1303_backend.py`, `sx1302_hal.py`, `tx_queue.py`, `sx1261_driver.py`, `signal_utils.py`, `virtual_radio.py` |
| `overlay/pymc_repeater/` | `/opt/pymc_repeater/repos/pyMC_Repeater/` | `bridge_engine.py`, `config_manager.py`, `engine.py`, `main.py`, `wm1303_api.py`, `wm1303.html`, and more |

See [Repository Information](repositories.md) for the overlay approach explained.

### Phase 6: Build HAL & Packet Forwarder

Compiles the SX1302 HAL library and packet forwarder binary from C source code.

**Actions:**
- Clean previous build artifacts
- Build `libloragw` (HAL shared library)
- Build `lora_pkt_fwd` (packet forwarder binary)
- Install binary to `/home/pi/wm1303_pf/lora_pkt_fwd`

Build uses all available CPU cores (`make -j$(nproc)`) for faster compilation.

### Phase 7: Python Virtual Environment & Package Installation

Sets up an isolated Python environment with all required packages.

**Actions:**
- Create virtual environment at `/opt/pymc_repeater/venv/`
- Upgrade `pip`, `setuptools`, and `wheel`
- Install `pyMC_core` in editable (dev) mode
- Install `pyMC_Repeater` in editable (dev) mode
- Install additional dependencies: `spidev`, `RPi.GPIO`, `pyyaml`, `cherrypy`, `pyjwt`, `cryptography`, `aiohttp`
- Clean Python `__pycache__` directories from installation and venv paths to prevent stale bytecode issues

### Phase 8: Install Configuration Files

Copies configuration templates to their target locations. **Existing files are preserved** — they are never overwritten during installation.

| Source | Target | Notes |
|--------|--------|-------|
| `config/config.yaml.template` | `/etc/pymc_repeater/config.yaml` | Only if not present |
| `config/wm1303_ui.json` | `/etc/pymc_repeater/wm1303_ui.json` | Only if not present |
| `config/global_conf.json` | `/home/pi/wm1303_pf/global_conf.json` | Only if not present |

See [Configuration Reference](configuration.md) for detailed documentation of all config files.

**Automatic identity key generation:**

After deploying `config.yaml`, the installer checks whether a `repeater.identity_key` is present. If not, it generates a unique 32-byte random key using Python's `secrets.token_bytes(32)` and writes it into `config.yaml`. This key provides the repeater with a stable mesh identity. It is preserved during upgrades and should not be manually edited.

### Phase 9: Generate GPIO Reset Scripts

Reads GPIO pin configuration from `wm1303_ui.json` and generates hardware-specific reset scripts.

**Generated files:**
- `/home/pi/wm1303_pf/reset_lgw.sh` — Logic reset for concentrator startup
- `/home/pi/wm1303_pf/power_cycle_lgw.sh` — Full power cycle for hardware recovery

GPIO pins and base offset are read from `wm1303_ui.json` → `gpio_pins`. See [Configuration Reference](configuration.md) for GPIO pin mapping details.

### Phase 10: Install Systemd Service

Installs and enables the service unit file.

**Actions:**
- Stop existing service (if running)
- Copy `pymc-repeater.service` to `/etc/systemd/system/`
- Reload systemd daemon
- Enable service for auto-start on boot

### Phase 11: NTP Time Synchronization

Ensures accurate time synchronization, which is important for deduplication TTLs, JWT tokens, and log timestamps.

**Actions:**
- Check `systemd-timesyncd` status
- Enable NTP if not active
- Display current system time for verification

### Phase 12: Start and Verify Service

Starts the service and performs basic health checks.

**Actions:**
- Start `pymc-repeater` service
- Wait 3 seconds for initialization
- Check service status via systemd
- Test web interface availability on configured port (default: 8000)

---

## Post-Installation

After successful installation, configure the system for your environment:

### 1. Set Node Name

Edit `/etc/pymc_repeater/config.yaml`:

```yaml
repeater:
  node_name: my-wm1303-repeater  # Choose a descriptive name
```

### 2. Configure Channels

Open the Web UI at `http://<pi-ip>:8000/wm1303.html` and configure:

- Channel frequencies, spreading factors, and bandwidths
- LBT/CAD settings per channel
- Bridge rules for packet forwarding between channels

See [Web UI](ui.md) for the interface guide.

### 3. Set Admin Password

If not set during installation, the system auto-generates a password. To change it, edit `config.yaml`:

```yaml
repeater:
  security:
    admin_password: your-new-password
```

Then restart the service:

```bash
sudo systemctl restart pymc-repeater
```

### 4. Verify Operation

```bash
# Check service is running
sudo systemctl status pymc-repeater

# Watch live logs
journalctl -u pymc-repeater -f

# Check web interface
curl -s http://localhost:8000/api/wm1303/status
```

---

## Upgrade Procedure

The `upgrade.sh` script updates an existing installation with the latest code from the fork repositories and re-applies overlay modifications.

### Command-Line Options

```bash
sudo bash upgrade.sh [--rebuild] [--force-config] [--skip-pull]
```

| Option | Description |
|--------|-------------|
| `--rebuild` | Force rebuild of HAL and packet forwarder (even if no changes detected) |
| `--force-config` | Overwrite existing config files with templates (use with caution) |
| `--skip-pull` | Skip pulling from remote repositories (use local overlay only) |

### Upgrade Phases

#### Phase 1: Pre-upgrade Backup

Creates a timestamped backup before making any changes.

**Backed up:**
- `/etc/pymc_repeater/` → Configuration files
- `/home/pi/wm1303_pf/` → Packet forwarder directory (including binary)
- Current git commit hashes → `version_info.txt`

**Backup location:** `/home/pi/backups/pre-upgrade-YYYYMMDD_HHMMSS/`

#### Phase 2: Stop Service

Gracefully stops the running service to prevent conflicts during the update.

#### Phase 3: Update Repositories

Pulls the latest changes from all three fork repositories:

- Discards local changes (overlay will be re-applied)
- Fetches all remote branches
- Checks out the configured branch
- Pulls latest commits
- Reports whether each repo was updated

#### Phase 4: Re-apply Overlay Modifications

Re-applies all overlay files from the `overlay/` directory. This ensures the WM1303-specific modifications are present on top of any upstream changes.

#### Phase 5: Rebuild HAL & Packet Forwarder

Rebuilds the C code if:
- The HAL repository was updated, OR
- `--rebuild` flag was specified

Otherwise skips the build (overlay changes to Python files do not require recompilation).

#### Phase 6: Update Python Packages

Reinstalls Python packages in editable mode if their repositories were updated.

#### Phase 7: Update Configuration Files

By default, existing configuration files are **preserved**. If `--force-config` is specified:
- `wm1303_ui.json` is overwritten with the template
- `config.yaml` is overwritten with the template
- `global_conf.json` is overwritten with the template

The systemd service file and GPIO reset scripts are always updated.

#### Phase 8: Restart and Verify Service

Restarts the service and performs the same health checks as during installation.

### Typical Upgrade Workflow

```bash
# Standard upgrade (preserves config, only rebuilds if needed)
cd ~/pyMC_WM1303
git pull
sudo bash upgrade.sh

# Force rebuild after HAL changes
sudo bash upgrade.sh --rebuild

# Reset all config to defaults (backup created automatically)
sudo bash upgrade.sh --force-config

# Apply only local overlay changes (no git pull)
sudo bash upgrade.sh --skip-pull
```

---

## Troubleshooting

### SPI Not Found

**Symptoms:** Installation reports "SPI device nodes not found" or service fails with SPI errors.

**Solutions:**
1. Verify SPI is enabled:
   ```bash
   grep -i spi /boot/firmware/config.txt
   # Should show: dtparam=spi=on
   ```
2. Check device nodes:
   ```bash
   ls -la /dev/spidev0.*
   ```
3. Load SPI module manually:
   ```bash
   sudo modprobe spidev
   ```
4. Reboot if SPI was just enabled:
   ```bash
   sudo reboot
   ```

### Build Errors

**Symptoms:** HAL or packet forwarder compilation fails.

**Solutions:**
1. Ensure build dependencies are installed:
   ```bash
   sudo apt-get install build-essential gcc make
   ```
2. Check for disk space:
   ```bash
   df -h /
   ```
3. Clean and rebuild:
   ```bash
   cd /home/pi/sx1302_hal
   make clean
   cd libloragw && make
   cd ../packet_forwarder && make
   ```
4. Check compiler output for specific errors

### Service Won't Start

**Symptoms:** `systemctl start pymc-repeater` fails or service crashes immediately.

**Solutions:**
1. Check logs for error details:
   ```bash
   journalctl -u pymc-repeater -n 50 --no-pager
   ```
2. Verify config file syntax:
   ```bash
   python3 -c "import yaml; yaml.safe_load(open('/etc/pymc_repeater/config.yaml'))"
   ```
3. Check if another process is using SPI:
   ```bash
   sudo fuser /dev/spidev0.0
   ```
4. Verify file permissions:
   ```bash
   ls -la /etc/pymc_repeater/
   ls -la /home/pi/wm1303_pf/
   ```
5. Test manual start for detailed output:
   ```bash
   sudo -u pi /opt/pymc_repeater/venv/bin/python3 -m repeater.main --config /etc/pymc_repeater/config.yaml
   ```

### Web Interface Not Responding

**Symptoms:** Cannot reach `http://<pi-ip>:8000/`.

**Solutions:**
1. Verify service is running:
   ```bash
   systemctl status pymc-repeater
   ```
2. Check port configuration:
   ```bash
   grep port /etc/pymc_repeater/config.yaml
   ```
3. Test locally:
   ```bash
   curl -s http://127.0.0.1:8000/
   ```
4. Check firewall:
   ```bash
   sudo iptables -L -n | grep 8000
   ```

### GPIO Permission Errors

**Symptoms:** Packet forwarder fails with GPIO export errors.

**Solutions:**
1. Check GPIO access:
   ```bash
   ls -la /sys/class/gpio/
   ```
2. Verify GPIO base offset (newer Pi kernels use 512):
   ```bash
   cat /sys/class/gpio/gpiochip*/base
   ```
3. Manual GPIO test:
   ```bash
   sudo /home/pi/wm1303_pf/reset_lgw.sh start
   ```
4. If base offset differs from 512, update `gpio_pins.gpio_base_offset` in `wm1303_ui.json` and re-run the install script to regenerate GPIO scripts

---

## Rollback Procedure

If an upgrade causes issues, use the pre-upgrade backup to restore the previous state:

```bash
# Find the most recent backup
ls -la /home/pi/backups/

# Restore configuration
sudo cp -a /home/pi/backups/pre-upgrade-YYYYMMDD_HHMMSS/pymc_repeater_config/* /etc/pymc_repeater/

# Restore packet forwarder (including binary)
sudo cp -a /home/pi/backups/pre-upgrade-YYYYMMDD_HHMMSS/wm1303_pf/* /home/pi/wm1303_pf/

# Optionally restore the packet forwarder binary specifically
sudo cp /home/pi/backups/pre-upgrade-YYYYMMDD_HHMMSS/lora_pkt_fwd.bak /home/pi/wm1303_pf/lora_pkt_fwd

# Restart service
sudo systemctl restart pymc-repeater

# Verify
sudo systemctl status pymc-repeater
journalctl -u pymc-repeater -f
```

Each upgrade creates a separate backup directory with a timestamp, so you can roll back to any previous version.

### View Upgrade History

Each backup contains a `version_info.txt` with the git commit hashes at the time of the upgrade:

```bash
cat /home/pi/backups/pre-upgrade-*/version_info.txt
```

---

## Uninstallation

To completely remove the WM1303 system:

```bash
# Stop and disable service
sudo systemctl stop pymc-repeater
sudo systemctl disable pymc-repeater

# Remove service file
sudo rm /etc/systemd/system/pymc-repeater.service
sudo systemctl daemon-reload

# Remove installation directories
sudo rm -rf /opt/pymc_repeater
sudo rm -rf /etc/pymc_repeater
sudo rm -rf /var/log/pymc_repeater
sudo rm -rf /var/lib/pymc_repeater
sudo rm -rf /home/pi/wm1303_pf
sudo rm -rf /home/pi/sx1302_hal

# Optionally remove backups
sudo rm -rf /home/pi/backups
```

---

## Related Documentation

- [Configuration Reference](configuration.md) — All configuration file parameters
- [Repository Information](repositories.md) — Fork strategy and overlay approach
- [Hardware Overview](hardware.md) — Hardware requirements and SPI/GPIO details
- [Web UI](ui.md) — Post-installation channel configuration
- [System Architecture](architecture.md) — Overall system design
