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
CONFIG_DIR="/etc/openhop_repeater"
LOG_DIR="/var/log/openhop_repeater"
DATA_DIR="/var/lib/openhop_repeater"
# PKTFWD_DIR and HAL_DIR are set after user detection (see below)

# GitHub repositories (unmodified forks)
#
# Note: the official upstream of pyMC_core and pyMC_Repeater has moved to the
# openhop-dev GitHub organization:
#   - https://github.com/openhop-dev/openhop_core   (was pyMC-dev/pymc-core)
#   - https://github.com/openhop-dev/openhop_repeater (was pyMC-dev/pymc-repeater)
#
# The Hans van Meer forks below remain the WM1303 source of truth: they are
# kept in sync with the new upstream and apply any WM1303-specific patches
# before this installer consumes them. See docs/repositories.md for the full
# upstream/fork relationship.
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
FORCE_USER=""
for arg in "$@"; do
    case "$arg" in
        --skip-update) SKIP_UPDATE=true ;;
        --skip-build)  SKIP_BUILD=true ;;
        --user=*)      FORCE_USER="${arg#--user=}" ;;
        --help|-h)
            echo "Usage: sudo bash install.sh [--skip-update] [--skip-build] [--user=<username>]"
            echo "  --skip-update    Skip apt update/upgrade"
            echo "  --skip-build     Skip HAL/packet forwarder build"
            echo "  --user=<name>    Force install for specific user (default: auto-detect)"
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

# ---------------------------------------------------------------------------
# Detect target user (the non-root user who will own the installation)
# Priority: --user=<name> > SUDO_USER > first non-root user with a home dir
# ---------------------------------------------------------------------------
detect_user() {
    # 1. Explicit --user=<name> argument
    if [ -n "$FORCE_USER" ]; then
        if id "$FORCE_USER" &>/dev/null; then
            echo "$FORCE_USER"
            return
        else
            fail "Specified user '$FORCE_USER' does not exist."
        fi
    fi

    # 2. SUDO_USER (set by sudo when a regular user runs 'sudo bash install.sh')
    if [ -n "${SUDO_USER:-}" ] && [ "$SUDO_USER" != "root" ] && id "$SUDO_USER" &>/dev/null; then
        echo "$SUDO_USER"
        return
    fi

    # 3. Check for common default users
    for candidate in pi orangepi radxa rock dietpi; do
        if id "$candidate" &>/dev/null; then
            echo "$candidate"
            return
        fi
    done

    # 4. First non-root user with UID >= 1000 and a valid home directory
    local found
    found=$(awk -F: '$3 >= 1000 && $3 < 65534 && $6 != "/" && $7 !~ /nologin|false/ {print $1; exit}' /etc/passwd)
    if [ -n "$found" ] && id "$found" &>/dev/null; then
        echo "$found"
        return
    fi

    # 5. No suitable user found
    fail "Could not detect a non-root user. Please specify one with --user=<username>\n  Example: sudo bash install.sh --user=myuser"
}

PI_USER=$(detect_user)

# Resolve home directory safely (getent is more reliable than eval echo ~)
PI_HOME=$(getent passwd "${PI_USER}" 2>/dev/null | cut -d: -f6)
if [ -z "${PI_HOME}" ]; then
    # Fallback: try eval echo ~ (works on most systems)
    PI_HOME=$(eval echo ~"${PI_USER}" 2>/dev/null)
fi

if [ -z "${PI_HOME}" ] || [ "${PI_HOME}" = "~${PI_USER}" ]; then
    fail "Could not determine home directory for user '${PI_USER}'."
fi

if [ ! -d "${PI_HOME}" ]; then
    fail "Home directory '${PI_HOME}' for user '${PI_USER}' does not exist."
fi

info "Detected target user: ${PI_USER} (home: ${PI_HOME})"

# Set user-relative paths
PKTFWD_DIR="${PI_HOME}/wm1303_pf"
HAL_DIR="${PI_HOME}/sx1302_hal"

INSTALL_VERSION="unknown"
if [ -f "${SCRIPT_DIR}/VERSION" ]; then
    INSTALL_VERSION="v$(cat ${SCRIPT_DIR}/VERSION)"
fi
info "Installing version: ${INSTALL_VERSION}"
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
    python3-systemd \
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

step "Configuring passwordless sudo for ${PI_USER}"
if [ ! -f /etc/sudoers.d/010_pi-nopasswd ]; then
    echo "${PI_USER} ALL=(ALL) NOPASSWD: ALL" > /etc/sudoers.d/010_pi-nopasswd
    chmod 440 /etc/sudoers.d/010_pi-nopasswd
    ok "Configured"
else
    ok "Already configured"
fi

step "Adding ${PI_USER} to hardware access groups"
usermod -aG spi,i2c,gpio,dialout ${PI_USER} 2>/dev/null || true
ok "Done"


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

step "Configuring SPI buffer size (spidev bufsiz=32768)"
SPIDEV_CONF="/etc/modprobe.d/spidev.conf"
CMDLINE_FILE="/boot/firmware/cmdline.txt"
SPIDEV_PARAM="spidev.bufsiz=32768"

# Method 1: modprobe.d (works on older kernels)
if [ -f "$SPIDEV_CONF" ] && grep -q "bufsiz=32768" "$SPIDEV_CONF"; then
    ok "modprobe.d spidev bufsiz already configured"
else
    echo "options spidev bufsiz=32768" > "$SPIDEV_CONF"
    ok "modprobe.d spidev bufsiz set to 32768"
fi

# Method 2: kernel cmdline (required for Debian Trixie+ where spidev loads before modprobe.d)
if [ -f "$CMDLINE_FILE" ]; then
    if grep -q "$SPIDEV_PARAM" "$CMDLINE_FILE"; then
        ok "Kernel cmdline spidev.bufsiz already configured"
    else
        sudo sed -i "s/$/ ${SPIDEV_PARAM}/" "$CMDLINE_FILE"
        ok "Added spidev.bufsiz=32768 to kernel cmdline"
    fi
else
    warn "$CMDLINE_FILE not found — skipping kernel cmdline method"
fi

if [ "$(cat /sys/module/spidev/parameters/bufsiz 2>/dev/null)" != "32768" ]; then
    warn "Reboot required for spidev bufsiz change to take effect"
    REBOOT_REQUIRED=true
fi

step "Configuring VPU core_freq_min=500 (stable SPI clock)"
BOOT_CONFIG="/boot/firmware/config.txt"
[ ! -f "$BOOT_CONFIG" ] && BOOT_CONFIG="/boot/config.txt"
if [ -f "$BOOT_CONFIG" ]; then
    if grep -q "^core_freq_min=500" "$BOOT_CONFIG"; then
        ok "Already configured"
    elif grep -q "^core_freq_min=" "$BOOT_CONFIG"; then
        # Replace existing value
        sed -i 's/^core_freq_min=.*/core_freq_min=500/' "$BOOT_CONFIG"
        ok "Updated to 500 (was different)"
        REBOOT_REQUIRED=true
    else
        # Add before any [section] or at end
        if grep -q '^\[' "$BOOT_CONFIG"; then
            sed -i '0,/^\[/{s/^\[/# Lock VPU core clock for stable SPI bus timing\ncore_freq_min=500\n\n[/}' "$BOOT_CONFIG"
        else
            echo "" >> "$BOOT_CONFIG"
            echo "# Lock VPU core clock for stable SPI bus timing" >> "$BOOT_CONFIG"
            echo "core_freq_min=500" >> "$BOOT_CONFIG"
        fi
        ok "Added to config.txt"
        REBOOT_REQUIRED=true
    fi
    # Verify current runtime value
    CURRENT_CORE_FREQ=$(vcgencmd measure_clock core 2>/dev/null | grep -oP '=\K[0-9]+' || echo "unknown")
    if [ "$CURRENT_CORE_FREQ" != "unknown" ]; then
        CORE_MHZ=$((CURRENT_CORE_FREQ / 1000000))
        info "Current VPU core frequency: ${CORE_MHZ} MHz"
    fi
else
    warn "Boot config not found — please add core_freq_min=500 manually"
fi

step "Configuring gpu_mem=16 (headless optimisation)"
BOOT_CONFIG="/boot/firmware/config.txt"
[ ! -f "$BOOT_CONFIG" ] && BOOT_CONFIG="/boot/config.txt"
if [ -f "$BOOT_CONFIG" ]; then
    if grep -q "^gpu_mem=16" "$BOOT_CONFIG"; then
        ok "Already configured"
    elif grep -q "^gpu_mem=" "$BOOT_CONFIG"; then
        # Replace existing value
        sed -i 's/^gpu_mem=.*/gpu_mem=16/' "$BOOT_CONFIG"
        ok "Updated to 16 (was different)"
        REBOOT_REQUIRED=true
    else
        # Add before any [section] or at end
        if grep -q '^\[' "$BOOT_CONFIG"; then
            sed -i '0,/^\[/{s/^\[/# Minimise GPU memory for headless operation\ngpu_mem=16\n\n[/}' "$BOOT_CONFIG"
        else
            echo "" >> "$BOOT_CONFIG"
            echo "# Minimise GPU memory for headless operation" >> "$BOOT_CONFIG"
            echo "gpu_mem=16" >> "$BOOT_CONFIG"
        fi
        ok "Added to config.txt"
        REBOOT_REQUIRED=true
    fi
else
    warn "Boot config not found — please add gpu_mem=16 manually"
fi

step "Configuring SPI polling_limit_us=250 (persistent)"
SPI_BCM_CONF="/etc/modprobe.d/spi-bcm2835-opts.conf"
if [ -f "$SPI_BCM_CONF" ] && grep -q "polling_limit_us=250" "$SPI_BCM_CONF"; then
    ok "Already configured"
else
    echo "options spi_bcm2835 polling_limit_us=250" > "$SPI_BCM_CONF"
    ok "Set polling_limit_us=250"
fi
# Apply at runtime immediately if module is loaded
SPI_POLL_PARAM="/sys/module/spi_bcm2835/parameters/polling_limit_us"
if [ -f "$SPI_POLL_PARAM" ]; then
    CURRENT_POLL=$(cat "$SPI_POLL_PARAM" 2>/dev/null)
    if [ "$CURRENT_POLL" != "250" ]; then
        echo 250 > "$SPI_POLL_PARAM" 2>/dev/null
        info "Runtime polling_limit_us: ${CURRENT_POLL} -> 250"
    else
        info "Runtime polling_limit_us: already 250"
    fi
fi

step "Setting CPU governor to performance"
GOV_CHANGED=0
GOV_TOTAL=0
for gov_file in /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor; do
    [ -f "$gov_file" ] || continue
    GOV_TOTAL=$((GOV_TOTAL + 1))
    current=$(cat "$gov_file" 2>/dev/null)
    if [ "$current" != "performance" ]; then
        echo "performance" > "$gov_file" 2>/dev/null && GOV_CHANGED=$((GOV_CHANGED + 1))
    fi
done
if [ $GOV_TOTAL -eq 0 ]; then
    warn "No CPU governor files found"
elif [ $GOV_CHANGED -gt 0 ]; then
    ok "Set to 'performance' on ${GOV_CHANGED}/${GOV_TOTAL} cores"
else
    ok "Already 'performance' on all ${GOV_TOTAL} cores"
fi

step "Installing SPI optimization service script"
mkdir -p "${INSTALL_BASE}"
cp "${SCRIPT_DIR}/config/spi_optimize.sh" "${INSTALL_BASE}/spi_optimize.sh" >> "${LOG_FILE}" 2>&1 || \
    cp "${SCRIPT_DIR}/config/spi_optimize.sh" /opt/pymc_repeater/spi_optimize.sh >> "${LOG_FILE}" 2>&1
chmod 755 "${INSTALL_BASE}/spi_optimize.sh" 2>/dev/null || chmod 755 /opt/pymc_repeater/spi_optimize.sh 2>/dev/null
ok "Installed (runs at every service start)"



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


# ---------------------------------------------------------------------------
# WiFi Power Save Check (prevents SSH/network dropouts on wireless links)
# ---------------------------------------------------------------------------
step "Checking WiFi power save settings"
WIFI_IFACE=""
for iface in /sys/class/net/wlan*; do
    [ -d "$iface" ] && WIFI_IFACE=$(basename "$iface") && break
done

if [ -z "$WIFI_IFACE" ]; then
    ok "No WiFi interface detected — skipping"
else
    # Check current power save status
    PWRSAVE="unknown"
    if command -v iw >/dev/null 2>&1; then
        PWRSAVE=$(iw dev "$WIFI_IFACE" get power_save 2>/dev/null | awk '{print $NF}' || echo "unknown")
    fi

    if [ "$PWRSAVE" = "off" ]; then
        ok "WiFi power save already disabled on $WIFI_IFACE"
    else
        # Disable immediately
        if command -v iw >/dev/null 2>&1; then
            iw dev "$WIFI_IFACE" set power_save off >> "${LOG_FILE}" 2>&1 || true
        fi

        # Make persistent — try multiple methods for OS compatibility
        WIFI_PS_PERSISTED=false

        # Method 1: NetworkManager (Debian/Ubuntu with NM)
        if command -v nmcli >/dev/null 2>&1 && systemctl is-active --quiet NetworkManager 2>/dev/null; then
            NM_CONF_DIR="/etc/NetworkManager/conf.d"
            NM_CONF_FILE="${NM_CONF_DIR}/99-wifi-powersave-off.conf"
            if [ ! -f "$NM_CONF_FILE" ]; then
                mkdir -p "$NM_CONF_DIR"
                cat > "$NM_CONF_FILE" << 'NMEOF'
[connection]
wifi.powersave = 2
NMEOF
                # NM powersave: 2 = disable, 3 = enable, 0 = default
                systemctl restart NetworkManager >> "${LOG_FILE}" 2>&1 || true
            fi
            WIFI_PS_PERSISTED=true
            ok "WiFi power save disabled on $WIFI_IFACE (NetworkManager)"
        fi

        # Method 2: dhcpcd hook (Raspberry Pi OS Bookworm and older)
        if [ "$WIFI_PS_PERSISTED" = false ] && [ -d "/etc/dhcpcd.conf" ] || [ -f "/etc/dhcpcd.conf" ]; then
            DHCPCD_HOOK="/etc/dhcpcd.exit-hook"
            HOOK_LINE='command -v iw >/dev/null 2>&1 && iw dev wlan0 set power_save off 2>/dev/null || true'
            if [ -f "$DHCPCD_HOOK" ] && grep -q "power_save off" "$DHCPCD_HOOK" 2>/dev/null; then
                WIFI_PS_PERSISTED=true
                ok "WiFi power save disabled on $WIFI_IFACE (dhcpcd hook exists)"
            else
                echo "$HOOK_LINE" >> "$DHCPCD_HOOK"
                chmod +x "$DHCPCD_HOOK"
                WIFI_PS_PERSISTED=true
                ok "WiFi power save disabled on $WIFI_IFACE (dhcpcd hook)"
            fi
        fi

        # Method 3: udev rule (generic fallback for systemd-networkd or other setups)
        if [ "$WIFI_PS_PERSISTED" = false ]; then
            UDEV_RULE="/etc/udev/rules.d/70-wifi-powersave.rules"
            if [ ! -f "$UDEV_RULE" ]; then
                cat > "$UDEV_RULE" << 'UDEVEOF'
# Disable WiFi power save for network stability
ACTION=="add", SUBSYSTEM=="net", KERNEL=="wlan*", RUN+="/usr/sbin/iw dev %k set power_save off"
UDEVEOF
                udevadm control --reload-rules >> "${LOG_FILE}" 2>&1 || true
            fi
            WIFI_PS_PERSISTED=true
            ok "WiFi power save disabled on $WIFI_IFACE (udev rule)"
        fi

        if [ "$WIFI_PS_PERSISTED" = false ]; then
            warn "Could not persist WiFi power save setting — disable manually if needed"
        fi
    fi
fi


# =============================================================================
# Phase 3: Directory Structure
# =============================================================================
phase "Directory Structure Creation"

step "Creating installation directories"
mkdir -p "${INSTALL_BASE}"
mkdir -p "${REPO_DIR}"
# --- OpenHop config migration -------------------------------------------------
# Legacy config dir /etc/pymc_repeater is superseded by /etc/openhop_repeater.
# On a device that still has the old dir, copy its contents (preserving the JWT
# identity in config.yaml, the version file and wm1303_ui.json) into the new
# location WITHOUT overwriting anything already present. The old dir is left in
# place so a rollback remains possible.
LEGACY_CONFIG_DIR="/etc/pymc_repeater"
if [ -d "${LEGACY_CONFIG_DIR}" ] && [ "${LEGACY_CONFIG_DIR}" != "${CONFIG_DIR}" ]; then
    mkdir -p "${CONFIG_DIR}"
    # -a preserves perms/ownership/timestamps; -n never overwrites existing files
    cp -an "${LEGACY_CONFIG_DIR}/." "${CONFIG_DIR}/" >> "${LOG_FILE}" 2>&1 || true
    ok "Migrated legacy config ${LEGACY_CONFIG_DIR} -> ${CONFIG_DIR} (JWT/version/wm1303_ui.json preserved)"
fi
mkdir -p "${CONFIG_DIR}"
mkdir -p "${PKTFWD_DIR}"

# --- OpenHop var-tree physical migration --------------------------------------
# Legacy /var/log/pymc_repeater and /var/lib/pymc_repeater are superseded by
# /var/log/openhop_repeater and /var/lib/openhop_repeater. Physically move the
# legacy content into the new location so historic logs and the SQLite DBs
# (potentially several MB) are preserved WITHOUT duplicating them on disk.
# If the new dir already has content, fall back to a safe copy that never
# overwrites and leave the legacy dir intact for rollback.
_migrate_legacy_vardir() {
    local legacy="$1" new="$2" label="$3"
    [ -d "${legacy}" ] || return 0
    [ "${legacy}" = "${new}" ] && return 0
    mkdir -p "${new}"
    if [ -z "$(ls -A "${new}" 2>/dev/null || true)" ]; then
        # New dir empty -> physical move via rsync -a --remove-source-files
        # (portable, handles same-fs efficiently, works cross-fs, keeps dotfiles).
        if rsync -a --remove-source-files "${legacy}/" "${new}/" >> "${LOG_FILE}" 2>&1; then
            find "${legacy}" -depth -type d -empty -delete >> "${LOG_FILE}" 2>&1 || true
            ok "Migrated legacy ${label} ${legacy} -> ${new} (physical move)"
        else
            ok "Legacy ${label} ${legacy} present but rsync move failed (see log); left in place"
        fi
    else
        # New dir already has content -> safe merge without overwrite.
        cp -an "${legacy}/." "${new}/" >> "${LOG_FILE}" 2>&1 || true
        ok "Merged legacy ${label} ${legacy} -> ${new} (safe copy; legacy preserved for rollback)"
    fi
}
_migrate_legacy_vardir "/var/log/pymc_repeater" "${LOG_DIR}"  "log dir"
_migrate_legacy_vardir "/var/lib/pymc_repeater" "${DATA_DIR}" "data dir"

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
        # Use `git reset --hard origin/<branch>` after fetch instead of `git pull`.
        # Idempotent and avoids "Your local changes would be overwritten by merge"
        # errors if a previous run left overlay-modified tracked files behind.
        sudo -u ${PI_USER} git fetch --all >> "${LOG_FILE}" 2>&1
        sudo -u ${PI_USER} git checkout "${branch}" >> "${LOG_FILE}" 2>&1
        sudo -u ${PI_USER} git reset --hard "origin/${branch}" >> "${LOG_FILE}" 2>&1
        sudo -u ${PI_USER} git clean -fd >> "${LOG_FILE}" 2>&1 || true
        ok "${name} updated to latest ${branch}"
    else
        if ! sudo -u ${PI_USER} git clone -b "${branch}" "${repo_url}" "${target_dir}" >> "${LOG_FILE}" 2>&1; then
            fail "Failed to clone ${name}"
        fi
        ok "${name} cloned"
    fi
}

step "Cloning sx1302_hal (HAL v2.10)"
clone_or_update_repo "${HAL_REPO}" "${HAL_DIR}" "${HAL_BRANCH}"

step "Cloning pyMC_core (dev branch)"
clone_or_update_repo "${CORE_REPO}" "${REPO_DIR}/pyMC_core" "${CORE_BRANCH}"

step "Cloning pyMC_Repeater (dev branch)"
clone_or_update_repo "${REPEATER_REPO}" "${REPO_DIR}/pyMC_Repeater" "${REPEATER_BRANCH}"

# Sync upstream tags so setuptools-scm computes correct version numbers
step "Syncing upstream tags for version resolution"
for _sync_pair in "pyMC_core:https://github.com/rightup/pyMC_core.git" "pyMC_Repeater:https://github.com/rightup/pyMC_Repeater.git"; do
    _sync_name="${_sync_pair%%:*}"
    _sync_url="${_sync_pair#*:}"
    _sync_dir="${REPO_DIR}/${_sync_name}"
    if [ -d "${_sync_dir}/.git" ]; then
        cd "${_sync_dir}"
        if ! git remote get-url upstream >> "${LOG_FILE}" 2>&1; then
            sudo -u ${PI_USER} git remote add upstream "${_sync_url}" >> "${LOG_FILE}" 2>&1
        fi
        sudo -u ${PI_USER} git fetch upstream --tags >> "${LOG_FILE}" 2>&1 || true
    fi
done
ok "Upstream tags synced"

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
cp "${OVERLAY_DIR}/hal/libloragw/src/loragw_spi.c"     "${HAL_DIR}/libloragw/src/" >> "${LOG_FILE}" 2>&1
cp "${OVERLAY_DIR}/hal/libloragw/src/loragw_lbt.c"     "${HAL_DIR}/libloragw/src/" >> "${LOG_FILE}" 2>&1
cp "${OVERLAY_DIR}/hal/libloragw/src/loragw_aux.c"     "${HAL_DIR}/libloragw/src/" >> "${LOG_FILE}" 2>&1
cp "${OVERLAY_DIR}/hal/libloragw/src/sx1261_spi.c"      "${HAL_DIR}/libloragw/src/" >> "${LOG_FILE}" 2>&1
cp "${OVERLAY_DIR}/hal/libloragw/inc/loragw_sx1302.h"  "${HAL_DIR}/libloragw/inc/" >> "${LOG_FILE}" 2>&1
cp "${OVERLAY_DIR}/hal/libloragw/inc/loragw_sx1261.h"  "${HAL_DIR}/libloragw/inc/" >> "${LOG_FILE}" 2>&1
cp "${OVERLAY_DIR}/hal/libloragw/inc/loragw_hal.h"     "${HAL_DIR}/libloragw/inc/" >> "${LOG_FILE}" 2>&1
cp "${OVERLAY_DIR}/hal/libloragw/inc/sx1261_defs.h"    "${HAL_DIR}/libloragw/inc/" >> "${LOG_FILE}" 2>&1
cp "${OVERLAY_DIR}/hal/libloragw/inc/loragw_spi.h"     "${HAL_DIR}/libloragw/inc/" >> "${LOG_FILE}" 2>&1
cp "${OVERLAY_DIR}/hal/libloragw/inc/loragw_lbt.h"     "${HAL_DIR}/libloragw/inc/" >> "${LOG_FILE}" 2>&1
cp "${OVERLAY_DIR}/hal/libloragw/Makefile"             "${HAL_DIR}/libloragw/" >> "${LOG_FILE}" 2>&1
cp "${OVERLAY_DIR}/hal/packet_forwarder/src/lora_pkt_fwd.c" "${HAL_DIR}/packet_forwarder/src/" >> "${LOG_FILE}" 2>&1
cp "${OVERLAY_DIR}/hal/packet_forwarder/src/capture_thread.c" "${HAL_DIR}/packet_forwarder/src/" >> "${LOG_FILE}" 2>&1
mkdir -p "${HAL_DIR}/packet_forwarder/inc" >> "${LOG_FILE}" 2>&1
cp "${OVERLAY_DIR}/hal/packet_forwarder/inc/capture_thread.h" "${HAL_DIR}/packet_forwarder/inc/" >> "${LOG_FILE}" 2>&1
cp "${OVERLAY_DIR}/hal/packet_forwarder/Makefile"      "${HAL_DIR}/packet_forwarder/" >> "${LOG_FILE}" 2>&1
ok "HAL overlay applied"

step "Applying pyMC_core overlay"
CORE_HW_DIR="${REPO_DIR}/pyMC_core/src/openhop_core/hardware"
for f in __init__.py wm1303_backend.py sx1302_hal.py tx_queue.py sx1261_driver.py signal_utils.py virtual_radio.py region_config.py; do
    if [ -f "${OVERLAY_DIR}/pymc_core/src/openhop_core/hardware/${f}" ]; then
        cp "${OVERLAY_DIR}/pymc_core/src/openhop_core/hardware/${f}" "${CORE_HW_DIR}/" >> "${LOG_FILE}" 2>&1
    fi
done
# companion/ overlay files (Contact model with RSSI/SNR support)
CORE_COMPANION_DIR="${REPO_DIR}/pyMC_core/src/openhop_core/companion"
for f in models.py contact_store.py; do
    if [ -f "${OVERLAY_DIR}/pymc_core/src/openhop_core/companion/${f}" ]; then
        cp "${OVERLAY_DIR}/pymc_core/src/openhop_core/companion/${f}" "${CORE_COMPANION_DIR}/" >> "${LOG_FILE}" 2>&1
    fi
done
# --- WM1303 v2.6.3: sync root-level helper modules into editable source ---
# The pyMC_core repo is pip install -e'd, so the editable path is the actual
# import location on most devices. Helpers such as openhop_core/paths.py sit
# at the package root and must be copied explicitly, otherwise
# `from openhop_core.paths import resolve_config_path` fails at runtime on
# editable-install devices (the site-packages sync in Phase 6 only runs in
# the non-editable fallback branch).
CORE_ROOT_DIR="${REPO_DIR}/pyMC_core/src/openhop_core"
if [ -d "${CORE_ROOT_DIR}" ]; then
    for _f in "${OVERLAY_DIR}/pymc_core/src/openhop_core/"*.py; do
        if [ -f "$_f" ]; then
            cp "$_f" "${CORE_ROOT_DIR}/" >> "${LOG_FILE}" 2>&1
        fi
    done
fi
ok "pyMC_core overlay applied"

step "Applying pyMC_Repeater overlay"
RPT_DIR="${REPO_DIR}/pyMC_Repeater"

# repeater/ level files
for f in bridge_engine.py channel_e_bridge.py channel_f_bridge.py config_manager.py engine.py main.py identity_manager.py config.py packet_router.py metrics_retention.py uniform_tracer.py wm1303_telemetry_helper.py protocol_validator.py; do
    if [ -f "${OVERLAY_DIR}/pymc_repeater/repeater/${f}" ]; then
        cp "${OVERLAY_DIR}/pymc_repeater/repeater/${f}" "${RPT_DIR}/repeater/" >> "${LOG_FILE}" 2>&1
    fi
done

# repeater/companion/ overlay files (echo-filter node_name sync)
mkdir -p "${RPT_DIR}/repeater/companion" >> "${LOG_FILE}" 2>&1
for f in bridge.py; do
    if [ -f "${OVERLAY_DIR}/pymc_repeater/repeater/companion/${f}" ]; then
        cp "${OVERLAY_DIR}/pymc_repeater/repeater/companion/${f}" "${RPT_DIR}/repeater/companion/" >> "${LOG_FILE}" 2>&1
    fi
done

# repeater/handler_helpers/ level files (v2.4.11+ overlays)
mkdir -p "${RPT_DIR}/repeater/handler_helpers" >> "${LOG_FILE}" 2>&1
for f in mesh_cli.py advert.py; do
    if [ -f "${OVERLAY_DIR}/pymc_repeater/repeater/handler_helpers/${f}" ]; then
        cp "${OVERLAY_DIR}/pymc_repeater/repeater/handler_helpers/${f}" "${RPT_DIR}/repeater/handler_helpers/" >> "${LOG_FILE}" 2>&1
    fi
done

# repeater/web/ level files
for f in wm1303_api.py http_server.py spectrum_collector.py cad_calibration_engine.py api_endpoints.py debug_collector.py packet_trace.py tiered_query.py update_endpoints.py; do
    if [ -f "${OVERLAY_DIR}/pymc_repeater/repeater/web/${f}" ]; then
        cp "${OVERLAY_DIR}/pymc_repeater/repeater/web/${f}" "${RPT_DIR}/repeater/web/" >> "${LOG_FILE}" 2>&1
    fi
done

# repeater/data_acquisition/ level files (SQLite schema + storage; WM1303 tables:
# invalid_packets, packet_metrics, crc_error_rate, dedup_events, neighbour_samples,
# sx1261_health_events + store_packet_metric/store_invalid_packet methods).
mkdir -p "${RPT_DIR}/repeater/data_acquisition" >> "${LOG_FILE}" 2>&1
for f in sqlite_handler.py; do
    if [ -f "${OVERLAY_DIR}/pymc_repeater/repeater/data_acquisition/${f}" ]; then
        cp "${OVERLAY_DIR}/pymc_repeater/repeater/data_acquisition/${f}" "${RPT_DIR}/repeater/data_acquisition/" >> "${LOG_FILE}" 2>&1
    fi
done

# repeater/web/html/ files
if [ -f "${OVERLAY_DIR}/pymc_repeater/repeater/web/html/wm1303.html" ]; then
    cp "${OVERLAY_DIR}/pymc_repeater/repeater/web/html/wm1303.html" "${RPT_DIR}/repeater/web/html/" >> "${LOG_FILE}" 2>&1
fi

# repeater/presets/ YAML files (observer/broker templates served by /api/broker_presets)
if [ -d "${OVERLAY_DIR}/pymc_repeater/repeater/presets" ]; then
    mkdir -p "${RPT_DIR}/repeater/presets" >> "${LOG_FILE}" 2>&1
    for f in "${OVERLAY_DIR}/pymc_repeater/repeater/presets"/*.yaml "${OVERLAY_DIR}/pymc_repeater/repeater/presets"/__init__.py; do
        if [ -f "$f" ]; then
            cp "$f" "${RPT_DIR}/repeater/presets/" >> "${LOG_FILE}" 2>&1
        fi
    done
fi


chown -R ${PI_USER}:${PI_USER} "${HAL_DIR}"
chown -R ${PI_USER}:${PI_USER} "${REPO_DIR}"

# Post-build UI patch: fix Observer/MQTT-tab save bugs in the shipped (minified)
# SPA assets (port=0 default and disallowedInput field-name mismatch). The Vue
# source is not in this repo and there is no npm build step, so the built assets
# are patched in place. Idempotent; safe to run on every install/upgrade.
step "Patching UI assets (Observer/MQTT save fix)"
if [ -f "${SCRIPT_DIR}/_tools/patch_ui_observer_save.sh" ]; then
    bash "${SCRIPT_DIR}/_tools/patch_ui_observer_save.sh" "${RPT_DIR}" >> "${LOG_FILE}" 2>&1 || true
    ok "UI Observer save patch applied"
else
    ok "Patch script not found — skipped"
fi

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

step "Checking existing venv health"
VENV_REBUILD_NEEDED=false
if [ -d "${VENV_DIR}" ]; then
    if ! "${VENV_DIR}/bin/python3" --version &>/dev/null; then
        warn "Venv Python broken (system Python upgraded?) — will rebuild venv"
        VENV_REBUILD_NEEDED=true
    else
        ok "Existing venv is healthy"
    fi
else
    ok "No existing venv (fresh install)"
fi

if [ "$VENV_REBUILD_NEEDED" = true ]; then
    step "Removing broken venv"
    rm -rf "${VENV_DIR}"
    ok "Removed"
fi

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

step "Symlinking system rrdtool module into venv"
VENV_SITE=$(sudo -u ${PI_USER} "${VENV_DIR}/bin/python3" -c "import site; print(site.getsitepackages()[0])" 2>/dev/null)
SYS_RRD=$(python3 -c "import rrdtool; print(rrdtool.__file__)" 2>/dev/null || true)
if [ -n "${SYS_RRD}" ] && [ -f "${SYS_RRD}" ] && [ -n "${VENV_SITE}" ]; then
    sudo -u ${PI_USER} ln -sf "${SYS_RRD}" "${VENV_SITE}/"
    # Verify import works
    if sudo -u ${PI_USER} "${VENV_DIR}/bin/python3" -c "import rrdtool" 2>/dev/null; then
        ok "Symlinked $(basename ${SYS_RRD})"
    else
        warn "Symlink created but import failed - RRD metrics will be unavailable"
    fi
else
    warn "System rrdtool module not found - RRD metrics will be unavailable"
fi

step "Symlinking system systemd module into venv"
# python3-systemd is installed via apt but lives in the system dist-packages.
# The venv is created with include-system-site-packages=false, so the native
# binding is invisible inside the venv. Symlinking the package dir lets the
# service use the native sd_notify path (Type=notify watchdog keep-alive).
# The code keeps a pure-Python AF_UNIX fallback, so this is a robustness step.
SYS_SYSTEMD=$(python3 -c "import os, systemd; print(os.path.dirname(systemd.__file__))" 2>/dev/null || true)
if [ -n "${SYS_SYSTEMD}" ] && [ -d "${SYS_SYSTEMD}" ] && [ -n "${VENV_SITE}" ]; then
    if [ ! -e "${VENV_SITE}/systemd" ]; then
        sudo -u ${PI_USER} ln -s "${SYS_SYSTEMD}" "${VENV_SITE}/systemd"
    fi
    if sudo -u ${PI_USER} "${VENV_DIR}/bin/python3" -c "import systemd.daemon" 2>/dev/null; then
        ok "Native systemd binding available in venv"
    else
        warn "Symlink created but import failed - sd_notify will use pure-Python fallback"
    fi
else
    warn "System systemd module not found - sd_notify will use pure-Python fallback"
fi

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

# Verify overlay is accessible after all pip installs.
# On Python 3.13 `pip install -e .` may fall back to a non-editable install for
# pymc_core: site-packages then contains *copies* of the repo files and our
# overlay changes to e.g. hardware/__init__.py are invisible. The blocks below
# detect this case via the import path and rsync the full overlay tree on top.
step "Verifying pyMC_core overlay is accessible"
PYMC_CORE_IMPORT_PATH=$(sudo -u ${PI_USER} "${VENV_DIR}/bin/python3" -c "import openhop_core.hardware; print(openhop_core.hardware.__file__)" 2>/dev/null || echo "")
if echo "$PYMC_CORE_IMPORT_PATH" | grep -q "site-packages"; then
    SITE_HW_DIR=$(dirname "$PYMC_CORE_IMPORT_PATH")
    # Recursive rsync so sub-directories and non-.py files (e.g. __init__.py
    # with the WM1303Backend conditional import block) are always included.
    rsync -a "${OVERLAY_DIR}/pymc_core/src/openhop_core/hardware/" "${SITE_HW_DIR}/" >> "${LOG_FILE}" 2>&1
    # Also re-apply companion overlay to site-packages
    SITE_COMPANION_DIR=$(dirname "$SITE_HW_DIR")/companion
    if [ -d "${SITE_COMPANION_DIR}" ] && [ -d "${OVERLAY_DIR}/pymc_core/src/openhop_core/companion" ]; then
        rsync -a "${OVERLAY_DIR}/pymc_core/src/openhop_core/companion/" "${SITE_COMPANION_DIR}/" >> "${LOG_FILE}" 2>&1
    fi
    # --- WM1303 v2.6.2 / v2.7 refactor: sync root-level helper modules -------
    # Only hardware/ and companion/ are rsynced above. New helpers such as
    # openhop_core/paths.py (v2.7 central config-path resolver) sit at the
    # package root and must be copied explicitly, otherwise
    # `from openhop_core.paths import resolve_config_path` fails at runtime.
    SITE_CORE_DIR=$(dirname "$SITE_HW_DIR")
    if [ -d "${SITE_CORE_DIR}" ]; then
        for _f in "${OVERLAY_DIR}/pymc_core/src/openhop_core/"*.py; do
            if [ -f "$_f" ]; then
                cp "$_f" "${SITE_CORE_DIR}/" >> "${LOG_FILE}" 2>&1
            fi
        done
        chown -R ${PI_USER}:${PI_USER} "${SITE_CORE_DIR}"/*.py 2>/dev/null || true
    fi
    chown -R ${PI_USER}:${PI_USER} "${SITE_HW_DIR}"
    chown -R ${PI_USER}:${PI_USER} "${SITE_COMPANION_DIR}" 2>/dev/null || true
    ok "Re-applied overlay to site-packages (rsync)"
else
    ok "Editable install active"
fi

# Sanity check: WM1303Backend must be importable after re-apply, otherwise
# bridge/scheduler init will run in degraded mode at service start.
step "Verifying WM1303Backend import"
if sudo -u ${PI_USER} "${VENV_DIR}/bin/python3" -c 'from openhop_core.hardware import WM1303Backend; assert WM1303Backend is not None' >> "${LOG_FILE}" 2>&1; then
    ok "WM1303Backend importable"
else
    warn "WM1303Backend import failed — bridge/scheduler may run in degraded mode (check ${LOG_FILE})"
fi

# Also verify pyMC_Repeater overlay
step "Verifying pyMC_Repeater overlay is accessible"
REPEATER_IMPORT_PATH=$(sudo -u ${PI_USER} "${VENV_DIR}/bin/python3" -c "import repeater.config; print(repeater.config.__file__)" 2>/dev/null || echo "")
if echo "$REPEATER_IMPORT_PATH" | grep -q "site-packages"; then
    SITE_REPEATER_DIR=$(dirname "$REPEATER_IMPORT_PATH")
    rsync -a "${OVERLAY_DIR}/pymc_repeater/repeater/" "${SITE_REPEATER_DIR}/" >> "${LOG_FILE}" 2>&1
    chown -R ${PI_USER}:${PI_USER} "${SITE_REPEATER_DIR}"
    ok "Re-applied overlay to site-packages (rsync)"
else
    ok "Editable install active"
fi

# Clean Python bytecode caches
step "Cleaning Python bytecode caches"
find ${INSTALL_BASE} -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
find ${VENV_DIR} -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
# Pre-create runtime /tmp files with correct ownership to prevent permission issues
for tmpf in /tmp/pymc_spectral_results.json /tmp/pymc_wm1303_bridge_conf.json /tmp/pymc_cad_config.json /tmp/pymc_channel_e_bridge_conf.json; do
    touch "$tmpf" 2>/dev/null || true
    chown ${PI_USER}:${PI_USER} "$tmpf" 2>/dev/null || true
    chmod 664 "$tmpf" 2>/dev/null || true
done
ok "Cleaned"


# =============================================================================
# Phase 8: Install Configuration Files
# =============================================================================
phase "Install Configuration Files"

step "Installing presets.json (channel presets per region)"
# Always overwrite presets.json since it is read-only catalog data managed by the installer.
if [ -f "${SCRIPT_DIR}/config/presets.json" ]; then
    cp "${SCRIPT_DIR}/config/presets.json" "${CONFIG_DIR}/presets.json" >> "${LOG_FILE}" 2>&1
    ok "presets.json installed"
else
    warn "presets.json not found in config/, skipping"
fi

step "Installing wm1303_ui.json"
if [ ! -f "${CONFIG_DIR}/wm1303_ui.json" ]; then
    cp "${SCRIPT_DIR}/config/wm1303_ui.json" "${CONFIG_DIR}/wm1303_ui.json" >> "${LOG_FILE}" 2>&1
    ok "Installed from template"
else
    ok "Existing config preserved"
fi

step "Normalizing wm1303_ui.json (removing legacy field names)"
NORM_RESULT=$(${VENV_DIR}/bin/python3 << PYNORM 2>>${LOG_FILE}
import json, sys
try:
    path = "${CONFIG_DIR}/wm1303_ui.json"
    with open(path) as f:
        ui = json.load(f)
    fixes = []
    for ch in ui.get("channels", []):
        label = ch.get("friendly_name", ch.get("name", "?"))
        for short, full in [("sf", "spreading_factor"), ("bw", "bandwidth"), ("cr", "coding_rate")]:
            if short in ch and full in ch:
                fixes.append(f"{label}: removed {short}={ch[short]} (kept {full}={ch[full]})")
                del ch[short]
            elif short in ch:
                ch[full] = ch.pop(short)
                fixes.append(f"{label}: renamed {short} -> {full}={ch[full]}")
    che = ui.get("channel_e", {})
    for short, full in [("sf", "spreading_factor"), ("bw", "bandwidth"), ("cr", "coding_rate")]:
        if short in che and full in che:
            fixes.append(f"channel_e: removed {short}={che[short]} (kept {full}={che[full]})")
            del che[short]
        elif short in che:
            che[full] = che.pop(short)
            fixes.append(f"channel_e: renamed {short} -> {full}={che[full]}")
    if fixes:
        with open(path, "w") as f:
            json.dump(ui, f, indent=2)
        print("fixed: " + "; ".join(fixes))
    else:
        print("clean")
except Exception as e:
    print("error: " + str(e), file=sys.stderr)
    print("error")
PYNORM
)
if [ "${NORM_RESULT}" = "clean" ]; then
    ok "No legacy fields found"
elif echo "${NORM_RESULT}" | grep -q "^fixed:"; then
    ok "${NORM_RESULT}"
else
    warn "Normalization issue — see ${LOG_FILE}"
fi


step "Installing config.yaml"
if [ ! -f "${CONFIG_DIR}/config.yaml" ]; then
    cp "${SCRIPT_DIR}/config/config.yaml.template" "${CONFIG_DIR}/config.yaml" >> "${LOG_FILE}" 2>&1
    # Replace placeholders with detected paths
    sed -i "s|__PKTFWD_DIR__|${PKTFWD_DIR}|g" "${CONFIG_DIR}/config.yaml"
    ok "Installed from template (pktfwd_dir: ${PKTFWD_DIR})"
else
    # Ensure existing config has correct pktfwd_dir for detected user
    if grep -q '/home/pi/wm1303_pf' "${CONFIG_DIR}/config.yaml" 2>/dev/null && [ "${PI_USER}" != "pi" ]; then
        sed -i "s|/home/pi/wm1303_pf|${PKTFWD_DIR}|g" "${CONFIG_DIR}/config.yaml"
        info "Updated pktfwd_dir in existing config to ${PKTFWD_DIR}"
    fi
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
# NOTE: The v2.6.2 dual-write shim to ${LEGACY_CONFIG_DIR}/version was removed
# in v2.6.3 now that the overlay code reads via openhop_core.paths.
# resolve_config_path(), which handles both /etc/openhop_repeater/ (canonical)
# and /etc/pymc_repeater/ (legacy fallback). The Phase 1 legacy config-dir
# migration above (cp -an ${LEGACY_CONFIG_DIR}/. ${CONFIG_DIR}/) still ensures
# that devices upgraded from v2.5.x get their old version file copied into
# ${CONFIG_DIR}/ before this step overwrites it with the new value.
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
# GPIO reset script for WM1303 CoreCell
# BCM pins: reset=${GPIO_RESET}, power=${GPIO_POWER}, sx1261=${GPIO_SX1261}, ad5338r=${GPIO_AD5338R}
# GPIO base offset: ${GPIO_BASE}
#
# Usage:
#   reset_lgw.sh start         - Normal start (quick reset + power on)
#   reset_lgw.sh stop          - Power down and hold resets
#   reset_lgw.sh deep_reset    - Extended hardware drain (>60s power off)

SX1302_RESET_PIN=${SX1302_RESET_PIN}
SX1302_POWER_EN_PIN=${SX1302_POWER_PIN}
SX1261_RESET_PIN=${SX1261_RESET_PIN}
AD5338R_RESET_PIN=${AD5338R_RESET_PIN}

# Default drain time for deep_reset (seconds)
DRAIN_TIME=\${2:-60}

WAIT_GPIO() {
    sleep 0.1
}

init() {
    for pin in \${SX1302_RESET_PIN} \${SX1261_RESET_PIN} \${SX1302_POWER_EN_PIN} \${AD5338R_RESET_PIN}; do
        echo "\${pin}" > /sys/class/gpio/export 2>/dev/null || true; WAIT_GPIO
        echo "out" > /sys/class/gpio/gpio\${pin}/direction; WAIT_GPIO
    done
}

power_down() {
    echo "CoreCell power OFF through GPIO\${SX1302_POWER_EN_PIN} (BCM${GPIO_POWER})..."
    echo "0" > /sys/class/gpio/gpio\${SX1302_POWER_EN_PIN}/value; WAIT_GPIO

    echo "SX1302 RESET asserted through GPIO\${SX1302_RESET_PIN} (BCM${GPIO_RESET})..."
    echo "1" > /sys/class/gpio/gpio\${SX1302_RESET_PIN}/value; WAIT_GPIO

    echo "SX1261 RESET asserted through GPIO\${SX1261_RESET_PIN} (BCM${GPIO_SX1261})..."
    echo "1" > /sys/class/gpio/gpio\${SX1261_RESET_PIN}/value; WAIT_GPIO

    echo "AD5338R RESET asserted through GPIO\${AD5338R_RESET_PIN} (BCM${GPIO_AD5338R})..."
    echo "1" > /sys/class/gpio/gpio\${AD5338R_RESET_PIN}/value; WAIT_GPIO
}

power_up() {
    echo "Releasing resets..."
    echo "0" > /sys/class/gpio/gpio\${SX1302_RESET_PIN}/value; WAIT_GPIO
    echo "0" > /sys/class/gpio/gpio\${SX1261_RESET_PIN}/value; WAIT_GPIO
    echo "0" > /sys/class/gpio/gpio\${AD5338R_RESET_PIN}/value; WAIT_GPIO
    sleep 0.5

    echo "CoreCell power enable through GPIO\${SX1302_POWER_EN_PIN} (BCM${GPIO_POWER})..."
    echo "1" > /sys/class/gpio/gpio\${SX1302_POWER_EN_PIN}/value; WAIT_GPIO
    sleep 0.5

    echo "CoreCell reset through GPIO\${SX1302_RESET_PIN} (BCM${GPIO_RESET})..."
    echo "1" > /sys/class/gpio/gpio\${SX1302_RESET_PIN}/value; WAIT_GPIO
    echo "0" > /sys/class/gpio/gpio\${SX1302_RESET_PIN}/value; WAIT_GPIO

    echo "SX1261 reset through GPIO\${SX1261_RESET_PIN} (BCM${GPIO_SX1261})..."
    echo "1" > /sys/class/gpio/gpio\${SX1261_RESET_PIN}/value; WAIT_GPIO
    echo "0" > /sys/class/gpio/gpio\${SX1261_RESET_PIN}/value; WAIT_GPIO

    echo "AD5338R reset through GPIO\${AD5338R_RESET_PIN} (BCM${GPIO_AD5338R})..."
    echo "1" > /sys/class/gpio/gpio\${AD5338R_RESET_PIN}/value; WAIT_GPIO
    echo "0" > /sys/class/gpio/gpio\${AD5338R_RESET_PIN}/value; WAIT_GPIO
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
        init
        power_down
        ;;
    deep_reset)
        echo "=== Extended hardware drain reset ==="
        echo "Initializing GPIOs..."
        init

        echo "Powering down all components..."
        power_down

        echo "Holding all resets for \${DRAIN_TIME} seconds to clear hardware state..."
        ELAPSED=0
        while [ \$ELAPSED -lt \$DRAIN_TIME ]; do
            REMAINING=\$((DRAIN_TIME - ELAPSED))
            printf "\r  Draining... %d seconds remaining  " \$REMAINING
            sleep 10
            ELAPSED=\$((ELAPSED + 10))
        done
        printf "\r  Drain complete (%d seconds)          \n" \$DRAIN_TIME

        echo "Powering up with clean state..."
        power_up
        sleep 1

        echo "=== Hardware drain reset complete ==="
        ;;
    *)
        echo "Usage: \$0 {start|stop|deep_reset} [drain_seconds]"
        echo "  start       - Normal start (quick reset + power on)"
        echo "  stop        - Power down and hold resets"
        echo "  deep_reset  - Extended power-off drain (default 60s)"
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

step "Stopping existing service(s) (if running)"
# Legacy pymc-repeater.service is superseded by openhop-repeater.service. Stop
# and disable the old unit so both cannot run/enable simultaneously.
systemctl stop pymc-repeater.service 2>/dev/null || true
systemctl stop openhop-repeater.service 2>/dev/null || true
if systemctl list-unit-files 2>/dev/null | grep -q '^pymc-repeater.service'; then
    systemctl disable pymc-repeater.service >> "${LOG_FILE}" 2>&1 || true
    rm -f /etc/systemd/system/pymc-repeater.service 2>/dev/null || true
fi
ok "Done"

step "Installing systemd service file"
cp "${SCRIPT_DIR}/config/openhop-repeater.service" /etc/systemd/system/openhop-repeater.service >> "${LOG_FILE}" 2>&1
# Replace placeholders with detected user
sed -i "s|__PI_USER__|${PI_USER}|g" /etc/systemd/system/openhop-repeater.service
sed -i "s|__PI_HOME__|${PI_HOME}|g" /etc/systemd/system/openhop-repeater.service
ok "Installed (user: ${PI_USER})"

step "Reloading systemd daemon"
systemctl daemon-reload >> "${LOG_FILE}" 2>&1
ok "Reloaded"

step "Enabling service for auto-start"
systemctl enable openhop-repeater.service >> "${LOG_FILE}" 2>&1
ok "Enabled"

# =============================================================================
# Phase 10b: Hardware Watchdog (OS-level)
# =============================================================================
# Two-layer watchdog:
#  - OS layer (here): BCM2835 hardware watchdog + systemd RuntimeWatchdogSec.
#    If the whole OS freezes, the hardware timer reboots the Pi automatically.
#  - Service layer (in pymc-repeater.service): Type=notify + WatchdogSec; the
#    backend feeds sd_notify WATCHDOG=1 so a hung application is restarted.
phase "Hardware Watchdog"

step "Enabling BCM2835 hardware watchdog module"
WDT_MODCONF="/etc/modules-load.d/bcm2835_wdt.conf"
if [ -f "${WDT_MODCONF}" ] && grep -q '^bcm2835_wdt' "${WDT_MODCONF}" 2>/dev/null; then
    ok "Module already configured"
else
    echo "bcm2835_wdt" > "${WDT_MODCONF}"
    ok "Configured ${WDT_MODCONF}"
fi
modprobe bcm2835_wdt 2>/dev/null || warn "modprobe bcm2835_wdt failed (will load at next boot)"
if [ -e /dev/watchdog ]; then
    info "/dev/watchdog present"
else
    warn "/dev/watchdog not present yet (expected after reboot)"
fi

# Note: the BCM2835 hardware watchdog enforces a fixed 60s timeout and does not
# support shorter custom values (wdctl SETTIMEOUT=0). Lower RuntimeWatchdogSec
# values are clamped to 60s by the kernel, so we configure 60s to match the
# effective hardware behaviour. On a full OS freeze the Pi auto-reboots within ~60s.
step "Configuring systemd RuntimeWatchdogSec=60s (BCM2835 hardware limit)"
SYSTEMD_CONF="/etc/systemd/system.conf"
if [ -f "${SYSTEMD_CONF}" ]; then
    TMP_SC="$(mktemp)"
    # Remove any existing (commented or active) RuntimeWatchdogSec line, then append ours
    grep -viE '^[#[:space:]]*RuntimeWatchdogSec' "${SYSTEMD_CONF}" > "${TMP_SC}"
    echo "RuntimeWatchdogSec=60s" >> "${TMP_SC}"
    mv "${TMP_SC}" "${SYSTEMD_CONF}"
    ok "Set RuntimeWatchdogSec=60s in ${SYSTEMD_CONF}"
    step "Re-executing systemd manager to apply watchdog"
    systemctl daemon-reexec >> "${LOG_FILE}" 2>&1 || warn "daemon-reexec failed (applies after reboot)"
    ok "Applied"
else
    warn "${SYSTEMD_CONF} not found; skipped RuntimeWatchdogSec"
fi

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
# Phase 11b: Low-Memory Device Maintenance
# =============================================================================
phase "Low-Memory Device Maintenance"

step "Configuring weekly maintenance reboot (low-memory devices)"
MEM_TOTAL_MB=$(free -m | awk '/^Mem:/ {print $2}')
REBOOT_CRON_FILE="/etc/cron.d/openhop-repeater-weekly-reboot"
NO_REBOOT_MARKER="${CONFIG_DIR}/no-auto-reboot"
# Remove legacy cron file from the pre-openhop naming, if present.
rm -f /etc/cron.d/pymc-repeater-weekly-reboot 2>/dev/null || true

if [ -f "${NO_REBOOT_MARKER}" ]; then
    ok "Opt-out marker present (${NO_REBOOT_MARKER}); skipping auto-reboot cron"
elif [ "${MEM_TOTAL_MB}" -lt 700 ]; then
    cat > "${REBOOT_CRON_FILE}" << 'CRON_EOF'
# pyMC_WM1303 - Weekly maintenance reboot for low-memory devices
# Auto-installed by install.sh / upgrade.sh on devices with < 700 MB RAM.
# Rationale: clears kernel Slab caches and Python heap fragmentation that
# accumulate over days of uptime, keeping memory usage stable on 512 MB Pis.
#
# To disable one-off:  sudo rm /etc/cron.d/openhop-repeater-weekly-reboot
# To prevent re-install on next upgrade:
#   sudo touch /etc/openhop_repeater/no-auto-reboot

SHELL=/bin/sh
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

0 4 * * 0 root logger -t openhop-repeater "Weekly maintenance reboot" && /sbin/reboot
CRON_EOF
    chmod 0644 "${REBOOT_CRON_FILE}"
    ok "Detected ${MEM_TOTAL_MB} MB RAM; installed ${REBOOT_CRON_FILE} (Sun 04:00)"
else
    if [ -f "${REBOOT_CRON_FILE}" ]; then
        rm -f "${REBOOT_CRON_FILE}"
        ok "${MEM_TOTAL_MB} MB RAM detected; removed stale ${REBOOT_CRON_FILE}"
    else
        ok "${MEM_TOTAL_MB} MB RAM detected; skipping auto-reboot cron (not needed)"
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
    step "Performing extended hardware drain reset (60s)"
    sudo "${PKTFWD_DIR}/reset_lgw.sh" deep_reset 60 >> "${LOG_FILE}" 2>&1
    ok "Hardware drain reset complete"

    # --- Deploy-gap verification (added v2.7.2 to catch #211/#214-style bugs) ---
    # Compares overlay/pymc_repeater/repeater/*.py against ${RPT_DIR}/repeater/*.py
    # (fork-checkout, loaded via PYTHONPATH). Warns if any overlay file was NOT copied.
    # storage_collector.py is deliberately excluded (kept on fork version to avoid drift).
    step "Verifying overlay deploy coverage (repeater/*.py)"
    _COV_EXCLUDED='storage_collector\.py$'
    _COV_MISSING=$(comm -23 \
        <(cd "${OVERLAY_DIR}/pymc_repeater/repeater" && find . -name '*.py' -type f 2>/dev/null | sed 's|^\./||' | sort) \
        <(cd "${RPT_DIR}/repeater" && find . -name '*.py' -type f 2>/dev/null | sed 's|^\./||' | sort) \
        | grep -Ev "${_COV_EXCLUDED}" || true)
    if [ -n "${_COV_MISSING}" ]; then
        warn "Deploy-gap detected — the following overlay files were NOT copied to ${RPT_DIR}/repeater/:"
        echo "${_COV_MISSING}" | sed 's/^/      - /' | tee -a "${LOG_FILE}"
        warn "Add them to the appropriate for-loop in install.sh/upgrade.sh (see #211/#214 pattern)"
    else
        ok "Overlay deploy coverage complete (all *.py deployed, excluding intentional: storage_collector.py)"
    fi
    unset _COV_EXCLUDED _COV_MISSING

    step "Starting openhop-repeater service"
    systemctl start openhop-repeater.service >> "${LOG_FILE}" 2>&1
    sleep 5
    ok "Started"

    step "Checking service status"
    if systemctl is-active --quiet openhop-repeater.service; then
        ok "openhop-repeater service is RUNNING"
    else
        warn "Service may not have started correctly"
        info "Check logs: journalctl -u openhop-repeater -f"
    fi

    step "Checking web interface availability"
    sleep 5
    WEB_PORT=$(grep -oP '^\s*port:\s*\K[0-9]+' "${CONFIG_DIR}/config.yaml" 2>/dev/null | head -1)
    WEB_PORT=${WEB_PORT:-8000}
    if command -v curl &>/dev/null; then
        if curl -s -o /dev/null -w "%{http_code}" "http://127.0.0.1:${WEB_PORT}/" 2>/dev/null | grep -q "200\|302\|401"; then
            ok "Web interface responding on port ${WEB_PORT}"
        else
            ok "Web interface not yet responding (may need a few more seconds)"
        fi
    else
        ok "curl not available, skipping check"
    fi

    step "Checking concentrator module detection"
    sleep 10
    CONCENTRATOR_LOG=$(journalctl -u pymc-repeater --since '90 seconds ago' --no-pager 2>/dev/null || true)
    if echo "${CONCENTRATOR_LOG}" | grep -qi 'lora_pkt_fwd started\|pktfwd ready\|backend started'; then
        ok "SX1302 concentrator module detected and running"
    else
        if echo "${CONCENTRATOR_LOG}" | grep -qi 'Failed to set SX1250\|ERROR.*spi\|ERROR.*gpio\|pktfwd.*fail'; then
            warn "Concentrator module detection failed (SPI/GPIO errors found)"
        else
            warn "Concentrator module not yet confirmed (may need more time)"
        fi
        echo ""
        echo -e "  ${BOLD}${YELLOW}╔══════════════════════════════════════════════════════════╗${NC}"
        echo -e "  ${BOLD}${YELLOW}║  ⚠️  CONCENTRATOR MODULE NOT DETECTED                     ║${NC}"
        echo -e "  ${BOLD}${YELLOW}╚══════════════════════════════════════════════════════════╝${NC}"
        echo ""
        echo -e "  The installation completed successfully, but the SX1302 concentrator"
        echo -e "  module was not detected. This usually means:"
        echo ""
        echo -e "  1. GPIO pin numbers may not match your board"
        echo -e "  2. SPI device path may be different on your system"
        echo -e "  3. Power supply may be insufficient (ensure >= 3A)"
        echo ""
        echo -e "  ${BOLD}Next steps:${NC}"
        echo -e "  - Open the web UI: ${CYAN}http://<this-pi-ip>:${WEB_PORT}/wm1303.html${NC}"
        echo -e "  - Go to ${CYAN}Adv. Config → SPI Device Configuration${NC}"
        echo -e "  - Go to ${CYAN}Adv. Config → GPIO Pin Configuration${NC}"
        echo -e "  - Verify and adjust the SPI paths and GPIO pins for your board"
        echo -e "  - See: ${CYAN}https://github.com/HansvanMeer/pyMC_WM1303/blob/main/docs/spi-troubleshooting.md${NC}"
        echo ""
        echo -e "  The service is installed and will start automatically on boot."
        echo -e "  You can restart it after adjusting settings:"
        echo -e "  ${CYAN}sudo systemctl restart openhop-repeater${NC}"
        echo ""
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
echo -e "  Service control:  ${CYAN}sudo systemctl {start|stop|restart} openhop-repeater${NC}"
echo -e "  Service logs:     ${CYAN}journalctl -u openhop-repeater -f${NC}"
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
