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

# Log file for verbose output
LOG_FILE="/tmp/wm1303_install.log"
rm -f "${LOG_FILE}"
touch "${LOG_FILE}"

phase() {
    phase_num=$((phase_num + 1))
    step_count=0
    echo -e "\n${BOLD}${BLUE}═══════════════════════════════════════════════════════════════${NC}"
    echo -e "${BOLD}${BLUE}  Phase ${phase_num}: $1${NC}"
    echo -e "${BOLD}${BLUE}═══════════════════════════════════════════════════════════════${NC}"
}

step() {
    step_count=$((step_count + 1))
    echo -ne "  ${CYAN}[${phase_num}.${step_count}]${NC} $1 ... "
}

ok() {
    echo -e "${GREEN}✓${NC} $1"
}

warn() {
    echo -e "${YELLOW}⚠${NC} $1"
}

fail() {
    echo -e "${RED}✗${NC} $1"
    echo -e "  ${RED}See ${LOG_FILE} for details${NC}"
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

# Installation state tracking
REBOOT_REQUIRED=false
INSTALL_SUCCESS=false

# Trap to handle installation failures
cleanup_on_failure() {
    if [ "$INSTALL_SUCCESS" = false ]; then
        echo ""
        echo -e "  ${BOLD}${RED}╔══════════════════════════════════════════════════════════╗${NC}"
        echo -e "  ${BOLD}${RED}║     Installation FAILED!                                 ║${NC}"
        echo -e "  ${BOLD}${RED}╚══════════════════════════════════════════════════════════╝${NC}"
        echo ""
        echo -e "  ${RED}The installation encountered an error and could not complete.${NC}"
        echo -e "  ${RED}Check ${LOG_FILE} for detailed output.${NC}"
        echo ""
        echo -e "  ${BOLD}To retry:${NC}  sudo bash install.sh"
        echo -e "  ${BOLD}For help:${NC}  Check the documentation in docs/installation.md"
        echo ""
    fi
}
trap cleanup_on_failure EXIT

# ---------------------------------------------------------------------------
# Pre-flight checks
# ---------------------------------------------------------------------------
echo -e "${BOLD}${GREEN}"
echo "  ╔══════════════════════════════════════════════════════════╗"
echo "  ║     pyMC_WM1303 Installation                             ║"
echo "  ║     WM1303 LoRa Concentrator + MeshCore Repeater         ║"
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
info "Log file: ${LOG_FILE}"

# =============================================================================
# Phase 1: System Prerequisites
# =============================================================================
phase "System Prerequisites"

if [ "$SKIP_UPDATE" = false ]; then
    step "Updating package lists"
    if ! apt-get update -y >> "${LOG_FILE}" 2>&1; then
        fail "Package list update failed"
    fi
    ok "Done"

    step "Upgrading installed packages"
    if ! apt-get upgrade -y >> "${LOG_FILE}" 2>&1; then
        fail "Package upgrade failed"
    fi
    ok "Done"
else
    step "Skipping system update (--skip-update)"
    ok "Skipped"
fi

step "Installing build tools and dependencies"
if ! apt-get install -y \
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
    i2c-tools \
    rrdtool \
    librrd-dev \
    python3-rrdtool \
    >> "${LOG_FILE}" 2>&1; then
    fail "Dependency installation failed"
fi
ok "Done"

# NTP packages (optional - Debian 13+ uses systemd-timesyncd)
step "Installing NTP client"
if apt-get install -y ntpdate ntp >> "${LOG_FILE}" 2>&1; then
    ok "NTP packages installed"
else
    ok "Using systemd-timesyncd"
fi

step "Verifying Python 3 version"
PYTHON_VERSION=$(python3 --version 2>&1)
ok "${PYTHON_VERSION}"

# =============================================================================
# Phase 2: SPI & I2C Configuration
# =============================================================================
phase "SPI & I2C Configuration Check"

step "Checking SPI kernel module"
if lsmod | grep -q spi_bcm2835 || lsmod | grep -q spidev; then
    ok "SPI kernel module loaded"
else
    modprobe spidev 2>/dev/null || true
    warn "SPI kernel module not detected (loaded spidev)"
fi

step "Checking SPI device nodes"
if [ -e /dev/spidev0.0 ] && [ -e /dev/spidev0.1 ]; then
    ok "SPI devices found"
else
    BOOT_CONFIG="/boot/firmware/config.txt"
    [ ! -f "$BOOT_CONFIG" ] && BOOT_CONFIG="/boot/config.txt"

    if [ -f "$BOOT_CONFIG" ]; then
        if grep -q "^dtparam=spi=on" "$BOOT_CONFIG"; then
            warn "SPI enabled in config.txt but devices not present. Reboot required."
            REBOOT_REQUIRED=true
        else
            sed -i '/^#.*dtparam=spi/d' "$BOOT_CONFIG"
            if grep -q '^\[' "$BOOT_CONFIG"; then
                sed -i '0,/^\[/{s/^\[/dtparam=spi=on\n\n[/}' "$BOOT_CONFIG"
            else
                echo "dtparam=spi=on" >> "$BOOT_CONFIG"
            fi
            ok "SPI enabled in config.txt"
            warn "Reboot required for SPI!"
            REBOOT_REQUIRED=true
        fi
    else
        fail "Cannot find boot config file. Please enable SPI manually."
    fi
fi

step "Checking SPI overlay for SenseCAP M1 Pi HAT"
BOOT_CONFIG="/boot/firmware/config.txt"
[ ! -f "$BOOT_CONFIG" ] && BOOT_CONFIG="/boot/config.txt"
if [ -f "$BOOT_CONFIG" ]; then
    if ! grep -q "dtoverlay=spi0-1cs" "$BOOT_CONFIG" && ! grep -q "dtoverlay=spi0-2cs" "$BOOT_CONFIG"; then
        ok "Default SPI configuration"
    else
        ok "SPI overlay configured"
    fi
fi

step "Checking I2C for WM1303 temperature sensor and AD5338R DAC"
if [ -e /dev/i2c-1 ]; then
    ok "I2C device found"
else
    modprobe i2c-dev 2>/dev/null || true
    modprobe i2c-bcm2835 2>/dev/null || true
    echo "i2c-dev" > /etc/modules-load.d/i2c-dev.conf

    BOOT_CONFIG="/boot/firmware/config.txt"
    [ ! -f "$BOOT_CONFIG" ] && BOOT_CONFIG="/boot/config.txt"

    if [ -f "$BOOT_CONFIG" ]; then
        if grep -q "^dtparam=i2c_arm=on" "$BOOT_CONFIG"; then
            warn "I2C enabled but /dev/i2c-1 not present. Reboot required."
            REBOOT_REQUIRED=true
        else
            sed -i '/^#.*dtparam=i2c_arm/d' "$BOOT_CONFIG"
            if grep -q '^\[' "$BOOT_CONFIG"; then
                sed -i '0,/^\[/{s/^\[/# enable I2C for WM1303 temperature sensor and AD5338R DAC\ndtparam=i2c_arm=on\n\n[/}' "$BOOT_CONFIG"
            else
                echo "# enable I2C for WM1303 temperature sensor and AD5338R DAC" >> "$BOOT_CONFIG"
                echo "dtparam=i2c_arm=on" >> "$BOOT_CONFIG"
            fi
            ok "I2C enabled in config.txt"
            warn "Reboot required for I2C!"
            REBOOT_REQUIRED=true
        fi
    else
        fail "Cannot find boot config file. Please enable I2C manually."
    fi
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
ok "Created"

step "Setting directory ownership"
chown -R ${PI_USER}:${PI_USER} "${INSTALL_BASE}"
chown -R ${PI_USER}:${PI_USER} "${PKTFWD_DIR}"
chown -R ${PI_USER}:${PI_USER} "${LOG_DIR}"
chown -R ${PI_USER}:${PI_USER} "${DATA_DIR}"
chown -R ${PI_USER}:${PI_USER} "${CONFIG_DIR}"
ok "Ownership set"

# =============================================================================
# Phase 4: Clone Repositories
# =============================================================================
phase "Clone Repositories"

clone_or_update_repo() {
    local repo_url="$1"
    local target_dir="$2"
    local branch="$3"
    local name="$(basename "$target_dir")"

    # Fix git 'dubious ownership' error (CVE-2022-24765)
    git config --global --add safe.directory "${target_dir}" 2>/dev/null
    sudo -u ${PI_USER} git config --global --add safe.directory "${target_dir}" 2>/dev/null

    if [ -d "${target_dir}/.git" ]; then
        # Ensure proper ownership before git operations
        chown -R ${PI_USER}:${PI_USER} "${target_dir}"
        cd "${target_dir}"
        sudo -u ${PI_USER} git fetch --all >> "${LOG_FILE}" 2>&1
        sudo -u ${PI_USER} git checkout "${branch}" >> "${LOG_FILE}" 2>&1
        sudo -u ${PI_USER} git pull origin "${branch}" >> "${LOG_FILE}" 2>&1
        ok "${name} updated to latest ${branch}"
    else
        if ! sudo -u ${PI_USER} git clone -b "${branch}" "${repo_url}" "${target_dir}" >> "${LOG_FILE}" 2>&1; then
            fail "Failed to clone ${name}"
        fi
        ok "${name} cloned"
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

step "Applying HAL overlay"
cp "${OVERLAY_DIR}/hal/libloragw/src/loragw_hal.c"     "${HAL_DIR}/libloragw/src/" >> "${LOG_FILE}" 2>&1
cp "${OVERLAY_DIR}/hal/libloragw/src/loragw_sx1302.c"  "${HAL_DIR}/libloragw/src/" >> "${LOG_FILE}" 2>&1
cp "${OVERLAY_DIR}/hal/libloragw/src/loragw_sx1261.c"  "${HAL_DIR}/libloragw/src/" >> "${LOG_FILE}" 2>&1
cp "${OVERLAY_DIR}/hal/libloragw/inc/loragw_sx1302.h"  "${HAL_DIR}/libloragw/inc/" >> "${LOG_FILE}" 2>&1
cp "${OVERLAY_DIR}/hal/libloragw/inc/loragw_sx1261.h"  "${HAL_DIR}/libloragw/inc/" >> "${LOG_FILE}" 2>&1
cp "${OVERLAY_DIR}/hal/libloragw/inc/sx1261_defs.h"    "${HAL_DIR}/libloragw/inc/" >> "${LOG_FILE}" 2>&1
cp "${OVERLAY_DIR}/hal/libloragw/Makefile"             "${HAL_DIR}/libloragw/" >> "${LOG_FILE}" 2>&1
cp "${OVERLAY_DIR}/hal/packet_forwarder/src/lora_pkt_fwd.c" "${HAL_DIR}/packet_forwarder/src/" >> "${LOG_FILE}" 2>&1
cp "${OVERLAY_DIR}/hal/packet_forwarder/Makefile"      "${HAL_DIR}/packet_forwarder/" >> "${LOG_FILE}" 2>&1
ok "HAL overlay applied"

step "Applying pyMC_core overlay"
CORE_HW_DIR="${REPO_DIR}/pyMC_core/src/pymc_core/hardware"
for f in __init__.py wm1303_backend.py sx1302_hal.py tx_queue.py sx1261_driver.py signal_utils.py virtual_radio.py; do
    if [ -f "${OVERLAY_DIR}/pymc_core/src/pymc_core/hardware/${f}" ]; then
        cp "${OVERLAY_DIR}/pymc_core/src/pymc_core/hardware/${f}" "${CORE_HW_DIR}/" >> "${LOG_FILE}" 2>&1
    fi
done
ok "pyMC_core overlay applied"

step "Applying pyMC_Repeater overlay"
RPT_DIR="${REPO_DIR}/pyMC_Repeater"

# repeater/ level files
for f in bridge_engine.py config_manager.py engine.py main.py identity_manager.py config.py packet_router.py; do
    if [ -f "${OVERLAY_DIR}/pymc_repeater/repeater/${f}" ]; then
        cp "${OVERLAY_DIR}/pymc_repeater/repeater/${f}" "${RPT_DIR}/repeater/" >> "${LOG_FILE}" 2>&1
    fi
done

# repeater/web/ level files
for f in wm1303_api.py http_server.py spectrum_collector.py cad_calibration_engine.py api_endpoints.py; do
    if [ -f "${OVERLAY_DIR}/pymc_repeater/repeater/web/${f}" ]; then
        cp "${OVERLAY_DIR}/pymc_repeater/repeater/web/${f}" "${RPT_DIR}/repeater/web/" >> "${LOG_FILE}" 2>&1
    fi
done

# repeater/web/html/ files
if [ -f "${OVERLAY_DIR}/pymc_repeater/repeater/web/html/wm1303.html" ]; then
    cp "${OVERLAY_DIR}/pymc_repeater/repeater/web/html/wm1303.html" "${RPT_DIR}/repeater/web/html/" >> "${LOG_FILE}" 2>&1
fi

# repeater/data_acquisition/ files
for f in sqlite_handler.py storage_collector.py; do
    if [ -f "${OVERLAY_DIR}/pymc_repeater/repeater/data_acquisition/${f}" ]; then
        cp "${OVERLAY_DIR}/pymc_repeater/repeater/data_acquisition/${f}" "${RPT_DIR}/repeater/data_acquisition/" >> "${LOG_FILE}" 2>&1
    fi
done

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
    sudo -u ${PI_USER} make clean >> "${LOG_FILE}" 2>&1 || true
    ok "Cleaned"

    step "Building libtools"
    cd "${HAL_DIR}"
    if ! sudo -u ${PI_USER} make -C libtools -j$(nproc) >> "${LOG_FILE}" 2>&1; then
        fail "libtools build failed"
    fi
    ok "Built"

    step "Building libloragw"
    cd "${HAL_DIR}"
    if ! sudo -u ${PI_USER} make -C libloragw -j$(nproc) >> "${LOG_FILE}" 2>&1; then
        fail "libloragw build failed"
    fi
    ok "Built"

    step "Building lora_pkt_fwd"
    cd "${HAL_DIR}"
    if ! sudo -u ${PI_USER} make -C packet_forwarder -j$(nproc) >> "${LOG_FILE}" 2>&1; then
        fail "packet_forwarder build failed"
    fi
    ok "Built"

    step "Installing packet forwarder binary"
    cp "${HAL_DIR}/packet_forwarder/lora_pkt_fwd" "${PKTFWD_DIR}/" >> "${LOG_FILE}" 2>&1
    chown ${PI_USER}:${PI_USER} "${PKTFWD_DIR}/lora_pkt_fwd"
    chmod 755 "${PKTFWD_DIR}/lora_pkt_fwd"
    ok "Installed"

    step "Building spectral_scan utility"
    if ! sudo -u ${PI_USER} make -C util_spectral_scan -j$(nproc) >> "${LOG_FILE}" 2>&1; then
        fail "spectral_scan build failed"
    fi
    ok "Built"

    step "Installing spectral_scan binary"
    cp "${HAL_DIR}/util_spectral_scan/spectral_scan" "${PKTFWD_DIR}/" >> "${LOG_FILE}" 2>&1
    chown ${PI_USER}:${PI_USER} "${PKTFWD_DIR}/spectral_scan"
    chmod 755 "${PKTFWD_DIR}/spectral_scan"
    ok "Installed"

else
    step "Skipping HAL build (--skip-build)"
    ok "Skipped"
fi

# =============================================================================
# Phase 7: Python Virtual Environment & Package Installation
# =============================================================================
phase "Python Virtual Environment & Package Installation"

step "Creating Python virtual environment"
if [ ! -d "${VENV_DIR}" ]; then
    if ! sudo -u ${PI_USER} python3 -m venv "${VENV_DIR}" >> "${LOG_FILE}" 2>&1; then
        fail "venv creation failed"
    fi
    ok "Created"
else
    ok "Already exists"
fi

step "Upgrading pip and setuptools"
if ! sudo -u ${PI_USER} "${VENV_DIR}/bin/pip" install --upgrade pip setuptools wheel >> "${LOG_FILE}" 2>&1; then
    fail "pip upgrade failed"
fi
ok "Done"

step "Installing pyMC_core (editable/dev mode)"
cd "${REPO_DIR}/pyMC_core"
if ! sudo -u ${PI_USER} "${VENV_DIR}/bin/pip" install -e . >> "${LOG_FILE}" 2>&1; then
    fail "pyMC_core install failed"
fi
ok "Installed"

step "Installing pyMC_Repeater (editable/dev mode)"
cd "${REPO_DIR}/pyMC_Repeater"
if ! sudo -u ${PI_USER} "${VENV_DIR}/bin/pip" install -e . >> "${LOG_FILE}" 2>&1; then
    fail "pyMC_Repeater install failed"
fi
ok "Installed"

step "Installing additional Python dependencies"
if ! sudo -u ${PI_USER} "${VENV_DIR}/bin/pip" install \
    spidev \
    RPi.GPIO \
    pyyaml \
    cherrypy \
    pyjwt \
    cryptography \
    aiohttp \
    >> "${LOG_FILE}" 2>&1; then
    fail "Additional dependencies install failed"
fi
ok "Done"

# Verify overlay is accessible after all pip installs
step "Verifying pyMC_core overlay is accessible"
PYMC_CORE_IMPORT_PATH=$(sudo -u ${PI_USER} "${VENV_DIR}/bin/python3" -c "import pymc_core.hardware; print(pymc_core.hardware.__file__)" 2>/dev/null || echo "")
if echo "$PYMC_CORE_IMPORT_PATH" | grep -q "site-packages"; then
    SITE_HW_DIR=$(dirname "$PYMC_CORE_IMPORT_PATH")
    cp "${OVERLAY_DIR}/pymc_core/src/pymc_core/hardware/"*.py "${SITE_HW_DIR}/" >> "${LOG_FILE}" 2>&1
    chown -R ${PI_USER}:${PI_USER} "${SITE_HW_DIR}"
    ok "Re-applied overlay to site-packages"
else
    ok "Editable install active"
fi

# Also verify pyMC_Repeater overlay
step "Verifying pyMC_Repeater overlay is accessible"
REPEATER_IMPORT_PATH=$(sudo -u ${PI_USER} "${VENV_DIR}/bin/python3" -c "import repeater.config; print(repeater.config.__file__)" 2>/dev/null || echo "")
if echo "$REPEATER_IMPORT_PATH" | grep -q "site-packages"; then
    SITE_REPEATER_DIR=$(dirname "$REPEATER_IMPORT_PATH")
    cp -r "${OVERLAY_DIR}/pymc_repeater/repeater/"* "${SITE_REPEATER_DIR}/" >> "${LOG_FILE}" 2>&1
    chown -R ${PI_USER}:${PI_USER} "${SITE_REPEATER_DIR}"
    ok "Re-applied overlay to site-packages"
else
    ok "Editable install active"
fi

# Clean Python bytecode caches
step "Cleaning Python bytecode caches"
find ${INSTALL_BASE} -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
find ${VENV_DIR} -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
# Pre-create runtime /tmp files with correct ownership to prevent permission issues
for tmpf in /tmp/pymc_spectral_results.json /tmp/pymc_wm1303_bridge_conf.json /tmp/pymc_cad_config.json; do
    touch "$tmpf" 2>/dev/null || true
    chown ${PI_USER}:${PI_USER} "$tmpf" 2>/dev/null || true
    chmod 664 "$tmpf" 2>/dev/null || true
done
log "Pre-created runtime /tmp files with correct ownership"
ok "Cleaned"


# =============================================================================
# Phase 8: Install Configuration Files
# =============================================================================
phase "Install Configuration Files"

step "Installing wm1303_ui.json"
if [ ! -f "${CONFIG_DIR}/wm1303_ui.json" ]; then
    cp "${SCRIPT_DIR}/config/wm1303_ui.json" "${CONFIG_DIR}/wm1303_ui.json" >> "${LOG_FILE}" 2>&1
    ok "Installed from template"
else
    ok "Existing config preserved"
fi

step "Installing config.yaml"
if [ ! -f "${CONFIG_DIR}/config.yaml" ]; then
    cp "${SCRIPT_DIR}/config/config.yaml.template" "${CONFIG_DIR}/config.yaml" >> "${LOG_FILE}" 2>&1
    ok "Installed from template"
else
    ok "Existing config preserved"
fi

step "Generating mesh identity key"
if ! grep -q '^[^#]*identity_key:' "${CONFIG_DIR}/config.yaml" 2>/dev/null; then
    ${VENV_DIR}/bin/python3 -c "
import yaml, secrets, base64
with open('${CONFIG_DIR}/config.yaml') as f:
    cfg = yaml.safe_load(f) or {}
cfg.setdefault('repeater', {})['identity_key'] = secrets.token_bytes(32)
with open('${CONFIG_DIR}/config.yaml', 'w') as f:
    yaml.dump(cfg, f, default_flow_style=False, allow_unicode=True)
" >> "${LOG_FILE}" 2>&1
    ok "Identity key generated"
else
    ok "Existing key preserved"
fi

step "Installing global_conf.json"
if [ ! -f "${PKTFWD_DIR}/global_conf.json" ]; then
    cp "${SCRIPT_DIR}/config/global_conf.json" "${PKTFWD_DIR}/global_conf.json" >> "${LOG_FILE}" 2>&1
    ok "Installed"
else
    ok "Existing config preserved"
fi

step "Setting configuration file ownership"
chown -R ${PI_USER}:${PI_USER} "${CONFIG_DIR}"
chown -R ${PI_USER}:${PI_USER} "${PKTFWD_DIR}"
ok "Done"

step "Installing version file"
cp "${SCRIPT_DIR}/VERSION" "${CONFIG_DIR}/version" >> "${LOG_FILE}" 2>&1
chown ${PI_USER}:${PI_USER} "${CONFIG_DIR}/version"
ok "v$(cat ${SCRIPT_DIR}/VERSION)"


# =============================================================================
# Phase 9: Generate GPIO Reset Scripts
# =============================================================================
phase "Generate GPIO Reset Scripts"

step "Reading GPIO pin configuration"
UI_JSON="${CONFIG_DIR}/wm1303_ui.json"
if [ -f "${UI_JSON}" ] && command -v jq &>/dev/null; then
    GPIO_RESET=$(jq -r '.gpio_pins.sx1302_reset // 17' "${UI_JSON}")
    GPIO_POWER=$(jq -r '.gpio_pins.sx1302_power_en // 18' "${UI_JSON}")
    GPIO_SX1261=$(jq -r '.gpio_pins.sx1261_reset // 5' "${UI_JSON}")
    GPIO_AD5338R=$(jq -r '.gpio_pins.ad5338r_reset // 13' "${UI_JSON}")
    GPIO_BASE=$(jq -r '.gpio_pins.gpio_base_offset // 512' "${UI_JSON}")
else
    GPIO_RESET=17
    GPIO_POWER=18
    GPIO_SX1261=5
    GPIO_AD5338R=13
    GPIO_BASE=512
fi
ok "GPIO: reset=BCM${GPIO_RESET}, power=BCM${GPIO_POWER}, sx1261=BCM${GPIO_SX1261}"

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
ok "Generated"

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
ok "Generated"

# =============================================================================
# Phase 10: Install Systemd Service
# =============================================================================
phase "Install Systemd Service"

step "Stopping existing service (if running)"
systemctl stop pymc-repeater.service 2>/dev/null || true
ok "Done"

step "Installing systemd service file"
cp "${SCRIPT_DIR}/config/pymc-repeater.service" /etc/systemd/system/pymc-repeater.service >> "${LOG_FILE}" 2>&1
ok "Installed"

step "Reloading systemd daemon"
systemctl daemon-reload >> "${LOG_FILE}" 2>&1
ok "Reloaded"

step "Enabling service for auto-start"
systemctl enable pymc-repeater.service >> "${LOG_FILE}" 2>&1
ok "Enabled"

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
        ok "NTP client running (not yet synced)"
    else
        systemctl enable systemd-timesyncd >> "${LOG_FILE}" 2>&1 || true
        systemctl start systemd-timesyncd >> "${LOG_FILE}" 2>&1 || true
        ok "NTP client enabled"
    fi

    info "System time: $(date '+%Y-%m-%d %H:%M:%S %Z')"
else
    if systemctl is-active --quiet ntp 2>/dev/null; then
        ok "NTP daemon is running"
    else
        systemctl enable ntp >> "${LOG_FILE}" 2>&1 || true
        systemctl start ntp >> "${LOG_FILE}" 2>&1 || true
        ok "NTP daemon started"
    fi
fi

# =============================================================================
# Phase 12: Start and Verify Service
# =============================================================================
phase "Start and Verify Service"

if [ "$REBOOT_REQUIRED" = true ]; then
    step "SPI devices not yet available (reboot required)"
    ok "Service will start automatically after reboot"
else
    step "Starting pymc-repeater service"
    systemctl start pymc-repeater.service >> "${LOG_FILE}" 2>&1
    sleep 5
    ok "Started"

    step "Checking service status"
    if systemctl is-active --quiet pymc-repeater.service; then
        ok "pymc-repeater service is RUNNING"
    else
        warn "Service may not have started correctly"
        info "Check logs: journalctl -u pymc-repeater -f"
    fi

    step "Checking web interface availability"
    sleep 5
    WEB_PORT=$(grep -oP 'port:\s*\K[0-9]+' "${CONFIG_DIR}/config.yaml" 2>/dev/null || echo "8000")
    if command -v curl &>/dev/null; then
        if curl -s -o /dev/null -w "%{http_code}" "http://127.0.0.1:${WEB_PORT}/" 2>/dev/null | grep -q "200\|302\|401"; then
            ok "Web interface responding on port ${WEB_PORT}"
        else
            ok "Web interface not yet responding (may need a few more seconds)"
        fi
    else
        ok "curl not available, skipping check"
    fi
fi

# =============================================================================
# Installation Complete
# =============================================================================
INSTALL_SUCCESS=true

echo -e "\n${BOLD}${GREEN}"
echo "  ╔══════════════════════════════════════════════════════════╗"
echo "  ║     Installation Completed Successfully!                 ║"
echo "  ╚══════════════════════════════════════════════════════════╝"
echo -e "${NC}"
echo -e "  ${BOLD}Quick Reference:${NC}"
echo -e "  ─────────────────────────────────────────────────────────"
echo -e "  Service control:  ${CYAN}sudo systemctl {start|stop|restart} pymc-repeater${NC}"
echo -e "  Service logs:     ${CYAN}journalctl -u pymc-repeater -f${NC}"
echo -e "  Web interface:    ${CYAN}http://<this-pi-ip>:8000/wm1303.html${NC}"
echo -e "  Repeater UI:      ${CYAN}http://<this-pi-ip>:8000/${NC}"
echo -e "  Full log:         ${CYAN}${LOG_FILE}${NC}"
echo ""

if [ "$REBOOT_REQUIRED" = true ]; then
    echo -e "  ${BOLD}${YELLOW}╔══════════════════════════════════════════════════════════╗${NC}"
    echo -e "  ${BOLD}${YELLOW}║  REBOOT REQUIRED to activate SPI and start the service   ║${NC}"
    echo -e "  ${BOLD}${YELLOW}╚══════════════════════════════════════════════════════════╝${NC}"
    echo ""
    echo -e "  The service is installed and enabled. It will start automatically after reboot."
    echo ""
    read -r -p "  Press ENTER to reboot now (or Ctrl+C to cancel)... "
    echo -e "\n  ${CYAN}Rebooting...${NC}"
    reboot
else
    echo -e "  ${GREEN}The service is running. No reboot required.${NC}"
    echo ""
fi
