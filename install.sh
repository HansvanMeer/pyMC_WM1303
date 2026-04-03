#!/bin/bash
# =============================================================================
# pyMC_WM1303 Installation Script
# =============================================================================
# Installs and configures WM1303 (SX1302/SX1303) LoRa concentrator module
# with MeshCore (pyMC_core & pyMC_Repeater) on SenseCAP M1 / Raspberry Pi.
#
# Usage: sudo bash install.sh [--skip-update] [--skip-build]
#
# Prerequisites:
#   - Raspberry Pi OS Lite (Bookworm or newer)
#   - SPI enabled in /boot/firmware/config.txt
#   - Internet connectivity for package installation
# =============================================================================

set -euo pipefail

# ---------------------------------------------------------------------------
# Colors and formatting
# ---------------------------------------------------------------------------
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m' # No Color

phase_num=0
step_count=0

phase() {
    phase_num=$((phase_num + 1))
    step_count=0
    echo -e "\n${BOLD}${BLUE}═══════════════════════════════════════════════════════════════${NC}"
    echo -e "${BOLD}${BLUE}  Phase ${phase_num}: $1${NC}"
    echo -e "${BOLD}${BLUE}═══════════════════════════════════════════════════════════════${NC}"
}

step() {
    step_count=$((step_count + 1))
    echo -e "\n${CYAN}  [${phase_num}.${step_count}]${NC} $1"
}

ok() {
    echo -e "  ${GREEN}✓${NC} $1"
}

warn() {
    echo -e "  ${YELLOW}⚠${NC} $1"
}

fail() {
    echo -e "  ${RED}✗${NC} $1"
    exit 1
}

info() {
    echo -e "  ${CYAN}ℹ${NC} $1"
}

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_BASE="/opt/pymc_repeater"
REPO_DIR="${INSTALL_BASE}/repos"
VENV_DIR="${INSTALL_BASE}/venv"
CONFIG_DIR="/etc/pymc_repeater"
PKTFWD_DIR="/home/pi/wm1303_pf"
HAL_DIR="/home/pi/sx1302_hal"
LOG_DIR="/var/log/pymc_repeater"
DATA_DIR="/var/lib/pymc_repeater"

# GitHub repositories (unmodified forks)
HAL_REPO="https://github.com/HansvanMeer/sx1302_hal.git"
CORE_REPO="https://github.com/HansvanMeer/pyMC_core.git"
REPEATER_REPO="https://github.com/HansvanMeer/pyMC_Repeater.git"

# Branch configuration
HAL_BRANCH="master"
CORE_BRANCH="dev"
REPEATER_BRANCH="dev"

# Parse arguments
SKIP_UPDATE=false
SKIP_BUILD=false
for arg in "$@"; do
    case "$arg" in
        --skip-update) SKIP_UPDATE=true ;;
        --skip-build)  SKIP_BUILD=true ;;
        --help|-h)
            echo "Usage: sudo bash install.sh [--skip-update] [--skip-build]"
            echo "  --skip-update  Skip apt update/upgrade"
            echo "  --skip-build   Skip HAL/packet forwarder build"
            exit 0
            ;;
    esac
done

# ---------------------------------------------------------------------------
# Pre-flight checks
# ---------------------------------------------------------------------------
echo -e "${BOLD}${GREEN}"
echo "  ╔══════════════════════════════════════════════════════════╗"
echo "  ║     pyMC_WM1303 Installation                           ║"
echo "  ║     WM1303 LoRa Concentrator + MeshCore Repeater       ║"
echo "  ╚══════════════════════════════════════════════════════════╝"
echo -e "${NC}"

if [ "$(id -u)" -ne 0 ]; then
    fail "This script must be run as root (sudo bash install.sh)"
fi

# Detect Pi user
PI_USER="pi"
if ! id "$PI_USER" &>/dev/null; then
    fail "User '$PI_USER' not found. This script is designed for Raspberry Pi OS."
fi
PI_HOME=$(eval echo ~${PI_USER})

info "Installation directory: ${INSTALL_BASE}"
info "Configuration directory: ${CONFIG_DIR}"
info "Packet forwarder directory: ${PKTFWD_DIR}"
info "HAL directory: ${HAL_DIR}"
info "Script source: ${SCRIPT_DIR}"

# =============================================================================
# Phase 1: System Prerequisites
# =============================================================================
phase "System Prerequisites"

if [ "$SKIP_UPDATE" = false ]; then
    step "Updating package lists"
    apt-get update -y 2>&1 | tail -1
    ok "Package lists updated"

    step "Upgrading installed packages"
    apt-get upgrade -y 2>&1 | tail -3
    ok "System packages upgraded"
else
    step "Skipping system update (--skip-update)"
    warn "Package update skipped by user request"
fi

step "Installing build tools and dependencies"
apt-get install -y \
    build-essential \
    gcc \
    make \
    git \
    python3 \
    python3-dev \
    python3-pip \
    python3-venv \
    python3-setuptools \
    libffi-dev \
    libssl-dev \
    jq \
    2>&1 | tail -3
ok "Build tools and dependencies installed"

# NTP packages (optional - Debian 13+ uses systemd-timesyncd)
step "Installing NTP client (if available)"
if apt-get install -y ntpdate ntp 2>/dev/null; then
    ok "NTP packages installed"
else
    info "NTP packages not available (Debian 13+ uses systemd-timesyncd)"
    ok "Will use systemd-timesyncd instead"
fi

step "Verifying Python 3 version"
PYTHON_VERSION=$(python3 --version 2>&1)
info "${PYTHON_VERSION}"
ok "Python 3 available"

# =============================================================================
# Phase 2: SPI Configuration
# =============================================================================
phase "SPI Configuration Check"

step "Checking SPI kernel module"
if lsmod | grep -q spi_bcm2835 || lsmod | grep -q spidev; then
    ok "SPI kernel module loaded"
else
    warn "SPI kernel module not detected"
    info "Attempting to load spidev module..."
    modprobe spidev 2>/dev/null || true
fi

step "Checking SPI device nodes"
if [ -e /dev/spidev0.0 ] && [ -e /dev/spidev0.1 ]; then
    ok "SPI devices found: /dev/spidev0.0, /dev/spidev0.1"
else
    warn "SPI device nodes not found!"
    info "Checking /boot/firmware/config.txt for SPI configuration..."
    BOOT_CONFIG="/boot/firmware/config.txt"
    [ ! -f "$BOOT_CONFIG" ] && BOOT_CONFIG="/boot/config.txt"

    if [ -f "$BOOT_CONFIG" ]; then
        if grep -q "^dtparam=spi=on" "$BOOT_CONFIG"; then
            warn "SPI is enabled in config.txt but devices not present. Reboot may be required."
        else
            info "Enabling SPI in ${BOOT_CONFIG}..."
            # Remove any commented-out SPI line
            sed -i '/^#.*dtparam=spi/d' "$BOOT_CONFIG"
            echo "dtparam=spi=on" >> "$BOOT_CONFIG"
            ok "SPI enabled in config.txt"
            warn "A REBOOT is required after installation for SPI to become active!"
        fi
    else
        fail "Cannot find boot config file. Please enable SPI manually."
    fi
fi

step "Checking SPI overlay for SenseCAP M1 Pi HAT"
BOOT_CONFIG="/boot/firmware/config.txt"
[ ! -f "$BOOT_CONFIG" ] && BOOT_CONFIG="/boot/config.txt"
if [ -f "$BOOT_CONFIG" ]; then
    # Ensure SPI speed is not limited and dual CS is available
    if ! grep -q "dtoverlay=spi0-1cs" "$BOOT_CONFIG" && ! grep -q "dtoverlay=spi0-2cs" "$BOOT_CONFIG"; then
        info "SPI CS overlay not explicitly set, default configuration should work"
    fi
    ok "Boot configuration checked"
fi

# =============================================================================
# Phase 3: Directory Structure
# =============================================================================
phase "Directory Structure Creation"

step "Creating installation directories"
mkdir -p "${INSTALL_BASE}"
mkdir -p "${REPO_DIR}"
mkdir -p "${CONFIG_DIR}"
mkdir -p "${PKTFWD_DIR}"
mkdir -p "${LOG_DIR}"
mkdir -p "${DATA_DIR}"
mkdir -p "${PI_HOME}/backups"
ok "Installation directories created"

step "Setting directory ownership"
chown -R ${PI_USER}:${PI_USER} "${INSTALL_BASE}"
chown -R ${PI_USER}:${PI_USER} "${PKTFWD_DIR}"
chown -R ${PI_USER}:${PI_USER} "${LOG_DIR}"
chown -R ${PI_USER}:${PI_USER} "${DATA_DIR}"
chown -R ${PI_USER}:${PI_USER} "${CONFIG_DIR}"
ok "Directory ownership set to ${PI_USER}"

# =============================================================================
# Phase 4: Clone Repositories
# =============================================================================
phase "Clone Repositories"

clone_or_update_repo() {
    local repo_url="$1"
    local target_dir="$2"
    local branch="$3"
    local name="$(basename "$target_dir")"

    if [ -d "${target_dir}/.git" ]; then
        info "${name} already cloned, updating..."
        cd "${target_dir}"
        sudo -u ${PI_USER} git fetch --all 2>&1 | tail -1
        sudo -u ${PI_USER} git checkout "${branch}" 2>&1 | tail -1
        sudo -u ${PI_USER} git pull origin "${branch}" 2>&1 | tail -1
        ok "${name} updated to latest ${branch}"
    else
        info "Cloning ${name} (${branch} branch)..."
        sudo -u ${PI_USER} git clone -b "${branch}" "${repo_url}" "${target_dir}" 2>&1 | tail -2
        ok "${name} cloned successfully"
    fi
}

step "Cloning sx1302_hal (HAL v2.1.0)"
clone_or_update_repo "${HAL_REPO}" "${HAL_DIR}" "${HAL_BRANCH}"

step "Cloning pyMC_core (dev branch)"
clone_or_update_repo "${CORE_REPO}" "${REPO_DIR}/pyMC_core" "${CORE_BRANCH}"

step "Cloning pyMC_Repeater (dev branch)"
clone_or_update_repo "${REPEATER_REPO}" "${REPO_DIR}/pyMC_Repeater" "${REPEATER_BRANCH}"

# =============================================================================
# Phase 5: Apply Overlay Modifications
# =============================================================================
phase "Apply Overlay Modifications"

OVERLAY_DIR="${SCRIPT_DIR}/overlay"

if [ ! -d "${OVERLAY_DIR}" ]; then
    fail "Overlay directory not found at ${OVERLAY_DIR}"
fi

step "Applying HAL overlay (loragw_hal.c, loragw_sx1302.c, loragw_sx1302.h, lora_pkt_fwd.c, Makefiles)"
cp -v "${OVERLAY_DIR}/hal/libloragw/src/loragw_hal.c"     "${HAL_DIR}/libloragw/src/" 2>&1
cp -v "${OVERLAY_DIR}/hal/libloragw/src/loragw_sx1302.c"  "${HAL_DIR}/libloragw/src/" 2>&1
cp -v "${OVERLAY_DIR}/hal/libloragw/inc/loragw_sx1302.h"  "${HAL_DIR}/libloragw/inc/" 2>&1
cp -v "${OVERLAY_DIR}/hal/libloragw/Makefile"             "${HAL_DIR}/libloragw/" 2>&1
cp -v "${OVERLAY_DIR}/hal/packet_forwarder/src/lora_pkt_fwd.c" "${HAL_DIR}/packet_forwarder/src/" 2>&1
cp -v "${OVERLAY_DIR}/hal/packet_forwarder/Makefile"      "${HAL_DIR}/packet_forwarder/" 2>&1
ok "HAL overlay applied"

step "Applying pyMC_core overlay (WM1303 hardware modules)"
CORE_HW_DIR="${REPO_DIR}/pyMC_core/src/pymc_core/hardware"
for f in __init__.py wm1303_backend.py sx1302_hal.py tx_queue.py sx1261_driver.py signal_utils.py virtual_radio.py; do
    if [ -f "${OVERLAY_DIR}/pymc_core/src/pymc_core/hardware/${f}" ]; then
        cp -v "${OVERLAY_DIR}/pymc_core/src/pymc_core/hardware/${f}" "${CORE_HW_DIR}/" 2>&1
    fi
done
ok "pyMC_core overlay applied"

step "Applying pyMC_Repeater overlay (WM1303 API, UI, bridge, engine, config)"
RPT_DIR="${REPO_DIR}/pyMC_Repeater"

# repeater/ level files
for f in bridge_engine.py config_manager.py engine.py main.py identity_manager.py config.py packet_router.py; do
    if [ -f "${OVERLAY_DIR}/pymc_repeater/repeater/${f}" ]; then
        cp -v "${OVERLAY_DIR}/pymc_repeater/repeater/${f}" "${RPT_DIR}/repeater/" 2>&1
    fi
done

# repeater/web/ level files
for f in wm1303_api.py http_server.py spectrum_collector.py cad_calibration_engine.py; do
    if [ -f "${OVERLAY_DIR}/pymc_repeater/repeater/web/${f}" ]; then
        cp -v "${OVERLAY_DIR}/pymc_repeater/repeater/web/${f}" "${RPT_DIR}/repeater/web/" 2>&1
    fi
done

# repeater/web/html/ files
if [ -f "${OVERLAY_DIR}/pymc_repeater/repeater/web/html/wm1303.html" ]; then
    cp -v "${OVERLAY_DIR}/pymc_repeater/repeater/web/html/wm1303.html" "${RPT_DIR}/repeater/web/html/" 2>&1
fi

# repeater/data_acquisition/ files
for f in sqlite_handler.py storage_collector.py; do
    if [ -f "${OVERLAY_DIR}/pymc_repeater/repeater/data_acquisition/${f}" ]; then
        cp -v "${OVERLAY_DIR}/pymc_repeater/repeater/data_acquisition/${f}" "${RPT_DIR}/repeater/data_acquisition/" 2>&1
    fi
done

# Optional repeater-level files
[ -f "${OVERLAY_DIR}/pymc_repeater/repeater/wm1303_api.py" ] && \
    cp -v "${OVERLAY_DIR}/pymc_repeater/repeater/wm1303_api.py" "${RPT_DIR}/repeater/" 2>&1
[ -f "${OVERLAY_DIR}/pymc_repeater/repeater/wm1303.html" ] && \
    cp -v "${OVERLAY_DIR}/pymc_repeater/repeater/wm1303.html" "${RPT_DIR}/repeater/" 2>&1

ok "pyMC_Repeater overlay applied"

# Set ownership after overlay
chown -R ${PI_USER}:${PI_USER} "${HAL_DIR}"
chown -R ${PI_USER}:${PI_USER} "${REPO_DIR}"

# =============================================================================
# Phase 6: Build HAL & Packet Forwarder
# =============================================================================
phase "Build HAL & Packet Forwarder"

if [ "$SKIP_BUILD" = false ]; then
    step "Cleaning previous HAL build artifacts"
    cd "${HAL_DIR}"
    sudo -u ${PI_USER} make clean 2>&1 || true
    ok "Build artifacts cleaned"

    step "Building libloragw (HAL library)"
    cd "${HAL_DIR}/libloragw"
    sudo -u ${PI_USER} make -j$(nproc) 2>&1 | tail -5
    ok "libloragw built successfully"

    step "Building lora_pkt_fwd (packet forwarder)"
    cd "${HAL_DIR}/packet_forwarder"
    sudo -u ${PI_USER} make -j$(nproc) 2>&1 | tail -5
    ok "lora_pkt_fwd built successfully"

    step "Installing packet forwarder binary"
    cp -v "${HAL_DIR}/packet_forwarder/lora_pkt_fwd" "${PKTFWD_DIR}/" 2>&1
    chown ${PI_USER}:${PI_USER} "${PKTFWD_DIR}/lora_pkt_fwd"
    chmod 755 "${PKTFWD_DIR}/lora_pkt_fwd"
    ok "Packet forwarder installed to ${PKTFWD_DIR}"
else
    step "Skipping HAL build (--skip-build)"
    warn "HAL build skipped by user request"
fi

# =============================================================================
# Phase 7: Python Virtual Environment & Package Installation
# =============================================================================
phase "Python Virtual Environment & Package Installation"

step "Creating Python virtual environment"
if [ ! -d "${VENV_DIR}" ]; then
    sudo -u ${PI_USER} python3 -m venv "${VENV_DIR}"
    ok "Virtual environment created at ${VENV_DIR}"
else
    info "Virtual environment already exists"
    ok "Using existing virtual environment"
fi

step "Upgrading pip and setuptools"
sudo -u ${PI_USER} "${VENV_DIR}/bin/pip" install --upgrade pip setuptools wheel 2>&1 | tail -2
ok "pip and setuptools upgraded"

step "Installing pyMC_core (editable/dev mode)"
cd "${REPO_DIR}/pyMC_core"
sudo -u ${PI_USER} "${VENV_DIR}/bin/pip" install -e . 2>&1 | tail -3
ok "pyMC_core installed"

step "Installing pyMC_Repeater (editable/dev mode)"
cd "${REPO_DIR}/pyMC_Repeater"
sudo -u ${PI_USER} "${VENV_DIR}/bin/pip" install -e . 2>&1 | tail -3
ok "pyMC_Repeater installed"

step "Installing additional Python dependencies"
sudo -u ${PI_USER} "${VENV_DIR}/bin/pip" install \
    spidev \
    RPi.GPIO \
    pyyaml \
    cherrypy \
    pyjwt \
    cryptography \
    aiohttp \
    2>&1 | tail -3
ok "Additional dependencies installed"

# =============================================================================
# Phase 8: Install Configuration Files
# =============================================================================
phase "Install Configuration Files"

step "Installing wm1303_ui.json"
if [ ! -f "${CONFIG_DIR}/wm1303_ui.json" ]; then
    cp -v "${SCRIPT_DIR}/config/wm1303_ui.json" "${CONFIG_DIR}/wm1303_ui.json" 2>&1
    ok "wm1303_ui.json installed (template)"
else
    info "wm1303_ui.json already exists, preserving current configuration"
    ok "Existing wm1303_ui.json preserved"
fi

step "Installing config.yaml"
if [ ! -f "${CONFIG_DIR}/config.yaml" ]; then
    cp -v "${SCRIPT_DIR}/config/config.yaml.template" "${CONFIG_DIR}/config.yaml" 2>&1
    ok "config.yaml installed from template"
    info "Edit ${CONFIG_DIR}/config.yaml to customize your setup"
else
    info "config.yaml already exists, preserving current configuration"
    ok "Existing config.yaml preserved"
fi

step "Installing global_conf.json (HAL configuration)"
if [ ! -f "${PKTFWD_DIR}/global_conf.json" ]; then
    cp -v "${SCRIPT_DIR}/config/global_conf.json" "${PKTFWD_DIR}/global_conf.json" 2>&1
    ok "global_conf.json installed"
else
    info "global_conf.json already exists, preserving current configuration"
    ok "Existing global_conf.json preserved"
fi

step "Setting configuration file ownership"
chown -R ${PI_USER}:${PI_USER} "${CONFIG_DIR}"
chown -R ${PI_USER}:${PI_USER} "${PKTFWD_DIR}"
ok "Configuration ownership set"

# =============================================================================
# Phase 9: Generate GPIO Reset Scripts
# =============================================================================
phase "Generate GPIO Reset Scripts"

step "Reading GPIO pin configuration from wm1303_ui.json"
UI_JSON="${CONFIG_DIR}/wm1303_ui.json"
if [ -f "${UI_JSON}" ] && command -v jq &>/dev/null; then
    GPIO_RESET=$(jq -r '.gpio_pins.sx1302_reset // 17' "${UI_JSON}")
    GPIO_POWER=$(jq -r '.gpio_pins.sx1302_power_en // 18' "${UI_JSON}")
    GPIO_SX1261=$(jq -r '.gpio_pins.sx1261_reset // 5' "${UI_JSON}")
    GPIO_AD5338R=$(jq -r '.gpio_pins.ad5338r_reset // 13' "${UI_JSON}")
    GPIO_BASE=$(jq -r '.gpio_pins.gpio_base_offset // 512' "${UI_JSON}")
else
    warn "Cannot read GPIO config, using defaults"
    GPIO_RESET=17
    GPIO_POWER=18
    GPIO_SX1261=5
    GPIO_AD5338R=13
    GPIO_BASE=512
fi

info "GPIO pins: reset=BCM${GPIO_RESET}, power=BCM${GPIO_POWER}, sx1261=BCM${GPIO_SX1261}, ad5338r=BCM${GPIO_AD5338R}"
info "GPIO base offset: ${GPIO_BASE}"

SX1302_RESET_PIN=$((GPIO_BASE + GPIO_RESET))
SX1302_POWER_PIN=$((GPIO_BASE + GPIO_POWER))
SX1261_RESET_PIN=$((GPIO_BASE + GPIO_SX1261))
AD5338R_RESET_PIN=$((GPIO_BASE + GPIO_AD5338R))

step "Generating reset_lgw.sh"
cat > "${PKTFWD_DIR}/reset_lgw.sh" << RESET_EOF
#!/bin/sh
# Auto-generated GPIO reset script for WM1303 CoreCell
# BCM pins: reset=${GPIO_RESET}, power=${GPIO_POWER}, sx1261=${GPIO_SX1261}, ad5338r=${GPIO_AD5338R}
# GPIO base offset: ${GPIO_BASE}

SX1302_RESET_PIN=${SX1302_RESET_PIN}
SX1302_POWER_EN_PIN=${SX1302_POWER_PIN}
SX1261_RESET_PIN=${SX1261_RESET_PIN}
AD5338R_RESET_PIN=${AD5338R_RESET_PIN}

WAIT_GPIO() {
    sleep 0.1
}

init() {
    for pin in \${SX1302_RESET_PIN} \${SX1261_RESET_PIN} \${SX1302_POWER_EN_PIN} \${AD5338R_RESET_PIN}; do
        echo "\${pin}" > /sys/class/gpio/export 2>/dev/null || true; WAIT_GPIO
        echo "out" > /sys/class/gpio/gpio\${pin}/direction; WAIT_GPIO
    done
}

reset() {
    echo "CoreCell power enable through GPIO\${SX1302_POWER_EN_PIN} (BCM${GPIO_POWER})..."
    echo "1" > /sys/class/gpio/gpio\${SX1302_POWER_EN_PIN}/value; WAIT_GPIO

    echo "CoreCell reset through GPIO\${SX1302_RESET_PIN} (BCM${GPIO_RESET})..."
    echo "1" > /sys/class/gpio/gpio\${SX1302_RESET_PIN}/value; WAIT_GPIO
    echo "0" > /sys/class/gpio/gpio\${SX1302_RESET_PIN}/value; WAIT_GPIO

    echo "SX1261 reset through GPIO\${SX1261_RESET_PIN} (BCM${GPIO_SX1261})..."
    echo "0" > /sys/class/gpio/gpio\${SX1261_RESET_PIN}/value; WAIT_GPIO
    echo "1" > /sys/class/gpio/gpio\${SX1261_RESET_PIN}/value; WAIT_GPIO

    echo "AD5338R reset through GPIO\${AD5338R_RESET_PIN} (BCM${GPIO_AD5338R})..."
    echo "0" > /sys/class/gpio/gpio\${AD5338R_RESET_PIN}/value; WAIT_GPIO
    echo "1" > /sys/class/gpio/gpio\${AD5338R_RESET_PIN}/value; WAIT_GPIO
}

term() {
    for pin in \${SX1302_RESET_PIN} \${SX1261_RESET_PIN} \${SX1302_POWER_EN_PIN} \${AD5338R_RESET_PIN}; do
        if [ -d /sys/class/gpio/gpio\${pin} ]; then
            echo "\${pin}" > /sys/class/gpio/unexport 2>/dev/null || true; WAIT_GPIO
        fi
    done
}

case "\$1" in
    start)
        term
        init
        reset
        sleep 1
        ;;
    stop)
        reset
        term
        ;;
    *)
        echo "Usage: \$0 {start|stop}"
        exit 1
        ;;
esac
exit 0
RESET_EOF
chmod 755 "${PKTFWD_DIR}/reset_lgw.sh"
chown ${PI_USER}:${PI_USER} "${PKTFWD_DIR}/reset_lgw.sh"
ok "reset_lgw.sh generated"

step "Generating power_cycle_lgw.sh"
cat > "${PKTFWD_DIR}/power_cycle_lgw.sh" << POWER_EOF
#!/bin/sh
# Auto-generated power cycle script for WM1303 CoreCell
# Full power cycle to clear SX1250 TX-induced desensitization

SX1302_RESET_PIN=${SX1302_RESET_PIN}
SX1302_POWER_EN_PIN=${SX1302_POWER_PIN}
SX1261_RESET_PIN=${SX1261_RESET_PIN}
AD5338R_RESET_PIN=${AD5338R_RESET_PIN}

for pin in \${SX1302_RESET_PIN} \${SX1261_RESET_PIN} \${SX1302_POWER_EN_PIN} \${AD5338R_RESET_PIN}; do
    echo "\${pin}" > /sys/class/gpio/export 2>/dev/null || true
    sleep 0.1
    echo "out" > /sys/class/gpio/gpio\${pin}/direction
    sleep 0.1
done

echo "Power OFF CoreCell..."
echo "0" > /sys/class/gpio/gpio\${SX1302_POWER_EN_PIN}/value
sleep 3

echo "Power ON CoreCell..."
echo "1" > /sys/class/gpio/gpio\${SX1302_POWER_EN_PIN}/value
sleep 0.5

echo "CoreCell reset..."
echo "1" > /sys/class/gpio/gpio\${SX1302_RESET_PIN}/value; sleep 0.1
echo "0" > /sys/class/gpio/gpio\${SX1302_RESET_PIN}/value; sleep 0.1

echo "SX1261 reset..."
echo "0" > /sys/class/gpio/gpio\${SX1261_RESET_PIN}/value; sleep 0.1
echo "1" > /sys/class/gpio/gpio\${SX1261_RESET_PIN}/value; sleep 0.1

echo "AD5338R reset..."
echo "0" > /sys/class/gpio/gpio\${AD5338R_RESET_PIN}/value; sleep 0.1
echo "1" > /sys/class/gpio/gpio\${AD5338R_RESET_PIN}/value; sleep 0.1

sleep 1
echo "Power cycle complete"
POWER_EOF
chmod 755 "${PKTFWD_DIR}/power_cycle_lgw.sh"
chown ${PI_USER}:${PI_USER} "${PKTFWD_DIR}/power_cycle_lgw.sh"
ok "power_cycle_lgw.sh generated"

# =============================================================================
# Phase 10: Install Systemd Service
# =============================================================================
phase "Install Systemd Service"

step "Stopping existing service (if running)"
systemctl stop pymc-repeater.service 2>/dev/null || true
ok "Existing service stopped (or was not running)"

step "Installing systemd service file"
cp -v "${SCRIPT_DIR}/config/pymc-repeater.service" /etc/systemd/system/pymc-repeater.service 2>&1
ok "Service file installed"

step "Reloading systemd daemon"
systemctl daemon-reload
ok "Systemd daemon reloaded"

step "Enabling service for auto-start"
systemctl enable pymc-repeater.service 2>&1
ok "Service enabled for auto-start on boot"

# =============================================================================
# Phase 11: NTP Time Synchronization
# =============================================================================
phase "NTP Time Synchronization"

step "Checking NTP synchronization status"
if command -v timedatectl &>/dev/null; then
    NTP_STATUS=$(timedatectl show --property=NTPSynchronized --value 2>/dev/null || echo "unknown")
    TIMESYNCD=$(timedatectl show --property=NTP --value 2>/dev/null || echo "unknown")

    if [ "$NTP_STATUS" = "yes" ]; then
        ok "NTP is synchronized"
    elif [ "$TIMESYNCD" = "yes" ]; then
        info "NTP service is active but not yet synchronized"
        ok "NTP client is running"
    else
        warn "NTP synchronization not active"
        info "Enabling systemd-timesyncd..."
        systemctl enable systemd-timesyncd 2>/dev/null || true
        systemctl start systemd-timesyncd 2>/dev/null || true
        ok "NTP client enabled"
    fi

    step "Current system time"
    info "$(date '+%Y-%m-%d %H:%M:%S %Z')"
else
    warn "timedatectl not available, checking ntpd..."
    if systemctl is-active --quiet ntp 2>/dev/null; then
        ok "NTP daemon is running"
    else
        info "Starting NTP daemon..."
        systemctl enable ntp 2>/dev/null || true
        systemctl start ntp 2>/dev/null || true
        ok "NTP daemon started"
    fi
fi

# =============================================================================
# Phase 12: Start and Verify Service
# =============================================================================
phase "Start and Verify Service"

step "Starting pymc-repeater service"
systemctl start pymc-repeater.service 2>&1
sleep 3
ok "Service start command issued"

step "Checking service status"
if systemctl is-active --quiet pymc-repeater.service; then
    ok "pymc-repeater service is RUNNING"
    info "$(systemctl status pymc-repeater.service --no-pager -l 2>&1 | head -5)"
else
    warn "Service may not have started correctly"
    info "Check logs with: journalctl -u pymc-repeater -f"
    info "$(systemctl status pymc-repeater.service --no-pager -l 2>&1 | head -10)"
fi

step "Checking web interface availability"
sleep 2
WEB_PORT=$(grep -oP 'port:\s*\K[0-9]+' "${CONFIG_DIR}/config.yaml" 2>/dev/null || echo "8000")
if command -v curl &>/dev/null; then
    if curl -s -o /dev/null -w "%{http_code}" "http://127.0.0.1:${WEB_PORT}/" 2>/dev/null | grep -q "200\|302\|401"; then
        ok "Web interface responding on port ${WEB_PORT}"
    else
        info "Web interface not yet responding (may need a few more seconds)"
    fi
else
    info "curl not available, skipping web interface check"
fi

# =============================================================================
# Installation Complete
# =============================================================================
echo -e "\n${BOLD}${GREEN}"
echo "  ╔══════════════════════════════════════════════════════════╗"
echo "  ║     Installation Complete!                              ║"
echo "  ╚══════════════════════════════════════════════════════════╝"
echo -e "${NC}"
echo -e "  ${BOLD}Quick Reference:${NC}"
echo -e "  ─────────────────────────────────────────────────────────"
echo -e "  Service control:  ${CYAN}sudo systemctl {start|stop|restart} pymc-repeater${NC}"
echo -e "  Service logs:     ${CYAN}journalctl -u pymc-repeater -f${NC}"
echo -e "  Web interface:    ${CYAN}http://<this-pi-ip>:8000/wm1303.html${NC}"
echo -e "  Repeater UI:      ${CYAN}http://<this-pi-ip>:8000/${NC}"
echo ""
