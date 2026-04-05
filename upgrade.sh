#!/bin/bash
# =============================================================================
# pyMC_WM1303 Upgrade Script
# =============================================================================
# Updates the WM1303 installation with the latest code from the fork
# repositories and re-applies overlay modifications.
#
# Usage: sudo bash upgrade.sh [--rebuild] [--force-config] [--skip-pull]
#
# Options:
#   --rebuild       Force rebuild of HAL and packet forwarder
#   --force-config  Overwrite existing config files with templates
#   --skip-pull     Skip pulling from remote repositories
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
NC='\033[0m'

phase_num=0
step_count=0

# Log file for verbose output
LOG_FILE="/tmp/wm1303_upgrade.log"
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

# Run a command silently, logging output, showing errors on failure
run_quiet() {
    if ! "$@" >> "${LOG_FILE}" 2>&1; then
        echo -e "${RED}✗ FAILED${NC}"
        echo -e "  ${RED}Command: $*${NC}"
        tail -20 "${LOG_FILE}" | sed 's/^/  /' >&2
        return 1
    fi
}

# ---------------------------------------------------------------------------
# Configuration (must match install.sh)
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
OVERLAY_DIR="${SCRIPT_DIR}/overlay"
BACKUP_DIR="/home/pi/backups"

PI_USER="pi"

# Branch configuration
HAL_BRANCH="master"
CORE_BRANCH="dev"
REPEATER_BRANCH="dev"

# Parse arguments
FORCE_REBUILD=false
FORCE_CONFIG=false
SKIP_PULL=false
for arg in "$@"; do
    case "$arg" in
        --rebuild)      FORCE_REBUILD=true ;;
        --force-config) FORCE_CONFIG=true ;;
        --skip-pull)    SKIP_PULL=true ;;
        --help|-h)
            echo "Usage: sudo bash upgrade.sh [--rebuild] [--force-config] [--skip-pull]"
            echo "  --rebuild       Force rebuild of HAL and packet forwarder"
            echo "  --force-config  Overwrite existing config files with templates"
            echo "  --skip-pull     Skip pulling from remote repositories"
            exit 0
            ;;
    esac
done

# ---------------------------------------------------------------------------
# Pre-flight checks
# ---------------------------------------------------------------------------
echo -e "${BOLD}${GREEN}"
echo "  ╔══════════════════════════════════════════════════════════╗"
echo "  ║     pyMC_WM1303 Upgrade                                  ║"
echo "  ║     Updating WM1303 LoRa Concentrator + MeshCore         ║"
echo "  ╚══════════════════════════════════════════════════════════╝"
echo -e "${NC}"

if [ "$(id -u)" -ne 0 ]; then
    fail "This script must be run as root (sudo bash upgrade.sh)"
fi

if [ ! -d "${INSTALL_BASE}" ]; then
    fail "Installation not found at ${INSTALL_BASE}. Run install.sh first."
fi

if [ ! -d "${OVERLAY_DIR}" ]; then
    fail "Overlay directory not found at ${OVERLAY_DIR}"
fi

info "Installation directory: ${INSTALL_BASE}"
info "Overlay directory: ${OVERLAY_DIR}"
info "Log file: ${LOG_FILE}"

# =============================================================================
# Phase 1: Pre-upgrade Backup
# =============================================================================
phase "Pre-upgrade Backup"

TIMESTAMP=$(date +%Y%m%d_%H%M%S)
UPGRADE_BACKUP="${BACKUP_DIR}/pre-upgrade-${TIMESTAMP}"

step "Creating pre-upgrade backup"
mkdir -p "${UPGRADE_BACKUP}"
if [ -d "${CONFIG_DIR}" ]; then
    cp -a "${CONFIG_DIR}" "${UPGRADE_BACKUP}/pymc_repeater_config/" >> "${LOG_FILE}" 2>&1
fi
if [ -d "${PKTFWD_DIR}" ]; then
    cp -a "${PKTFWD_DIR}" "${UPGRADE_BACKUP}/wm1303_pf/" >> "${LOG_FILE}" 2>&1
fi
if [ -f "${PKTFWD_DIR}/lora_pkt_fwd" ]; then
    cp "${PKTFWD_DIR}/lora_pkt_fwd" "${UPGRADE_BACKUP}/lora_pkt_fwd.bak" >> "${LOG_FILE}" 2>&1
fi
ok "Backup created"

step "Recording current version info"
{
    echo "Upgrade timestamp: ${TIMESTAMP}"
    echo ""
    if [ -d "${HAL_DIR}/.git" ]; then
        echo "sx1302_hal: $(cd ${HAL_DIR} && git rev-parse HEAD) ($(cd ${HAL_DIR} && git branch --show-current))"
    fi
    if [ -d "${REPO_DIR}/pyMC_core/.git" ]; then
        echo "pyMC_core:  $(cd ${REPO_DIR}/pyMC_core && git rev-parse HEAD) ($(cd ${REPO_DIR}/pyMC_core && git branch --show-current))"
    fi
    if [ -d "${REPO_DIR}/pyMC_Repeater/.git" ]; then
        echo "pyMC_Repeater: $(cd ${REPO_DIR}/pyMC_Repeater && git rev-parse HEAD) ($(cd ${REPO_DIR}/pyMC_Repeater && git branch --show-current))"
    fi
} > "${UPGRADE_BACKUP}/version_info.txt"
ok "Version info saved"

chown -R ${PI_USER}:${PI_USER} "${BACKUP_DIR}"

# =============================================================================
# Phase 2: Stop Service
# =============================================================================
phase "Stop Service"

step "Stopping pymc-repeater service"
SERVICE_WAS_RUNNING=false
if systemctl is-active --quiet pymc-repeater.service 2>/dev/null; then
    SERVICE_WAS_RUNNING=true
    systemctl stop pymc-repeater.service >> "${LOG_FILE}" 2>&1
    ok "Service stopped"
else
    ok "Service was not running"
fi

# =============================================================================
# Phase 3: Update Repositories
# =============================================================================
phase "Update Repositories"

HAL_UPDATED=false
CORE_UPDATED=false
REPEATER_UPDATED=false

update_repo() {
    local target_dir="$1"
    local branch="$2"
    local name="$(basename "$target_dir")"

    if [ "$SKIP_PULL" = true ]; then
        info "Skipping pull for ${name} (--skip-pull)"
        return 1
    fi

    if [ ! -d "${target_dir}/.git" ]; then
        warn "${name}: not a git repository, skipping pull"
        return 1
    fi

    cd "${target_dir}"
    local before=$(git rev-parse HEAD)

    # Discard local changes (overlay will be re-applied)
    sudo -u ${PI_USER} git checkout -- . >> "${LOG_FILE}" 2>&1 || true
    sudo -u ${PI_USER} git clean -fd >> "${LOG_FILE}" 2>&1 || true
    sudo -u ${PI_USER} git fetch --all >> "${LOG_FILE}" 2>&1
    sudo -u ${PI_USER} git checkout "${branch}" >> "${LOG_FILE}" 2>&1
    sudo -u ${PI_USER} git pull origin "${branch}" >> "${LOG_FILE}" 2>&1

    local after=$(git rev-parse HEAD)

    if [ "$before" != "$after" ]; then
        ok "Updated: ${before:0:8} → ${after:0:8}"
        return 0  # updated
    else
        ok "Already up to date (${after:0:8})"
        return 1  # no update
    fi
}

step "Updating sx1302_hal"
if update_repo "${HAL_DIR}" "${HAL_BRANCH}"; then
    HAL_UPDATED=true
fi

step "Updating pyMC_core"
if update_repo "${REPO_DIR}/pyMC_core" "${CORE_BRANCH}"; then
    CORE_UPDATED=true
fi

step "Updating pyMC_Repeater"
if update_repo "${REPO_DIR}/pyMC_Repeater" "${REPEATER_BRANCH}"; then
    REPEATER_UPDATED=true
fi

# =============================================================================
# Phase 4: Re-apply Overlay Modifications
# =============================================================================
phase "Re-apply Overlay Modifications"

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

chown -R ${PI_USER}:${PI_USER} "${HAL_DIR}"
chown -R ${PI_USER}:${PI_USER} "${REPO_DIR}"

# =============================================================================
# Phase 5: Rebuild HAL & Packet Forwarder (if needed)
# =============================================================================
phase "Rebuild HAL & Packet Forwarder"

if [ "$FORCE_REBUILD" = true ] || [ "$HAL_UPDATED" = true ]; then
    step "Cleaning previous build artifacts"
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
    step "Skipping HAL rebuild (no changes detected)"
    ok "Use --rebuild to force"
fi

# =============================================================================
# Phase 6: Update Python Packages
# =============================================================================
phase "Update Python Packages"

if [ "$CORE_UPDATED" = true ] || [ "$FORCE_REBUILD" = true ]; then
    step "Reinstalling pyMC_core"
    cd "${REPO_DIR}/pyMC_core"
    if ! sudo -u ${PI_USER} "${VENV_DIR}/bin/pip" install -e . >> "${LOG_FILE}" 2>&1; then
        fail "pyMC_core install failed"
    fi
    ok "Reinstalled"
else
    step "Skipping pyMC_core reinstall (no changes)"
    ok "Skipped"
fi

if [ "$REPEATER_UPDATED" = true ] || [ "$FORCE_REBUILD" = true ]; then
    step "Reinstalling pyMC_Repeater"
    cd "${REPO_DIR}/pyMC_Repeater"
    if ! sudo -u ${PI_USER} "${VENV_DIR}/bin/pip" install -e . >> "${LOG_FILE}" 2>&1; then
        fail "pyMC_Repeater install failed"
    fi
    ok "Reinstalled"
else
    step "Skipping pyMC_Repeater reinstall (no changes)"
    ok "Skipped"
fi

# Verify overlays are accessible after all pip installs
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
rm -f /tmp/pymc_spectral_results.json 2>/dev/null || true
ok "Caches cleaned"


# =============================================================================
# Phase 7: Update Configuration Files
# =============================================================================
phase "Update Configuration Files"

if [ "$FORCE_CONFIG" = true ]; then
    warn "--force-config: overwriting existing configuration files!"

    step "Updating wm1303_ui.json"
    cp "${SCRIPT_DIR}/config/wm1303_ui.json" "${CONFIG_DIR}/wm1303_ui.json" >> "${LOG_FILE}" 2>&1
    ok "Updated"

    step "Updating config.yaml"
    cp "${SCRIPT_DIR}/config/config.yaml.template" "${CONFIG_DIR}/config.yaml" >> "${LOG_FILE}" 2>&1
    ok "Updated"

    step "Updating global_conf.json"
    cp "${SCRIPT_DIR}/config/global_conf.json" "${PKTFWD_DIR}/global_conf.json" >> "${LOG_FILE}" 2>&1
    ok "Updated"
else
    step "Preserving existing configuration files"
    ok "Use --force-config to overwrite"
fi

step "Ensuring mesh identity key exists"
if ! grep -q '^[^#]*identity_key:' "${CONFIG_DIR}/config.yaml" 2>/dev/null; then
    ${VENV_DIR}/bin/python3 -c "
import yaml, secrets
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

step "Updating systemd service file"
cp "${SCRIPT_DIR}/config/pymc-repeater.service" /etc/systemd/system/pymc-repeater.service >> "${LOG_FILE}" 2>&1
systemctl daemon-reload >> "${LOG_FILE}" 2>&1
ok "Service file updated"

step "Regenerating GPIO reset scripts"
# Read GPIO config from wm1303_ui.json
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

SX1302_RESET_PIN=$((GPIO_BASE + GPIO_RESET))
SX1302_POWER_PIN=$((GPIO_BASE + GPIO_POWER))
SX1261_RESET_PIN=$((GPIO_BASE + GPIO_SX1261))
AD5338R_RESET_PIN=$((GPIO_BASE + GPIO_AD5338R))

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
ok "reset_lgw.sh regenerated"

step "Regenerating power_cycle_lgw.sh"
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
ok "power_cycle_lgw.sh regenerated"

chown -R ${PI_USER}:${PI_USER} "${CONFIG_DIR}"
chown -R ${PI_USER}:${PI_USER} "${PKTFWD_DIR}"


# =============================================================================
# Phase 7b: Database Cleanup
# =============================================================================
phase "Database Cleanup"

DB_PATH="${DATA_DIR}/repeater.db"
if [ -f "${DB_PATH}" ]; then
    step "Cleaning bogus TX echo data (avg_rssi > -50 dBm)"
    BOGUS_COUNT=$(${VENV_DIR}/bin/python3 -c "
import sqlite3
try:
    conn = sqlite3.connect('${DB_PATH}')
    cur = conn.cursor()
    cur.execute('''CREATE TABLE IF NOT EXISTS channel_stats_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        channel_id TEXT, timestamp REAL, avg_rssi REAL, avg_snr REAL,
        pkt_count INTEGER, noise_floor_dbm REAL
    )''')
    count = cur.execute('SELECT COUNT(*) FROM channel_stats_history WHERE avg_rssi > -50').fetchone()[0]
    if count > 0:
        cur.execute('UPDATE channel_stats_history SET avg_rssi = NULL, avg_snr = NULL WHERE avg_rssi > -50')
        conn.commit()
    print(count)
    conn.close()
except Exception as e:
    print(0)
" 2>/dev/null || echo "0")
    if [ "${BOGUS_COUNT}" -gt 0 ]; then
        ok "Cleaned ${BOGUS_COUNT} rows with bogus RSSI values"
    else
        ok "No bogus TX echo data found"
    fi

    step "Cleaning old channel_id formats from stats history"
    OLD_FORMAT_COUNT=$(${VENV_DIR}/bin/python3 -c "
import sqlite3
try:
    conn = sqlite3.connect('${DB_PATH}')
    cur = conn.cursor()
    total = 0
    for table in ['channel_stats_history', 'noise_floor_history']:
        try:
            cur.execute(f'''CREATE TABLE IF NOT EXISTS {table} (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                channel_id TEXT, timestamp REAL
            )''')
            count = cur.execute(f'SELECT COUNT(*) FROM {table} WHERE channel_id NOT LIKE \"channel_%\" AND channel_id NOT LIKE \"inactive_%\"').fetchone()[0]
            if count > 0:
                cur.execute(f'DELETE FROM {table} WHERE channel_id NOT LIKE \"channel_%\" AND channel_id NOT LIKE \"inactive_%\"')
                total += count
        except Exception:
            pass
    conn.commit()
    print(total)
    conn.close()
except Exception as e:
    print(0)
" 2>/dev/null || echo "0")
    if [ "${OLD_FORMAT_COUNT}" -gt 0 ]; then
        ok "Removed ${OLD_FORMAT_COUNT} rows with old channel_id formats"
    else
        ok "No old format channel IDs found"
    fi
else
    info "Database not found at ${DB_PATH}, skipping cleanup"
fi

# =============================================================================
# Phase 8: Restart and Verify Service
# =============================================================================
phase "Restart and Verify Service"

step "Starting pymc-repeater service"
systemctl start pymc-repeater.service >> "${LOG_FILE}" 2>&1
sleep 3
ok "Service start command issued"

step "Checking service status"
if systemctl is-active --quiet pymc-repeater.service; then
    ok "pymc-repeater service is RUNNING"
else
    warn "Service may not have started correctly"
    info "Check logs with: journalctl -u pymc-repeater -f"
fi

step "Checking web interface availability"
sleep 2
WEB_PORT=$(grep -oP 'port:\s*\K[0-9]+' "${CONFIG_DIR}/config.yaml" 2>/dev/null || echo "8000")
if command -v curl &>/dev/null; then
    if curl -s -o /dev/null -w "%{http_code}" "http://127.0.0.1:${WEB_PORT}/" 2>/dev/null | grep -q "200\|302\|401"; then
        ok "Web interface responding on port ${WEB_PORT}"
    else
        ok "Web interface not yet responding (may need a few more seconds)"
    fi
fi

# =============================================================================
# Upgrade Complete
# =============================================================================
echo -e "\n${BOLD}${GREEN}"
echo "  ╔══════════════════════════════════════════════════════════╗"
echo "  ║     Upgrade Complete!                                    ║"
echo "  ╚══════════════════════════════════════════════════════════╝"
echo -e "${NC}"
echo -e "  ${BOLD}Summary:${NC}"
echo -e "  ─────────────────────────────────────────────────────────"
echo -e "  Backup location:  ${CYAN}${UPGRADE_BACKUP}${NC}"
echo -e "  HAL updated:      ${CYAN}${HAL_UPDATED}${NC}"
echo -e "  pyMC_core updated: ${CYAN}${CORE_UPDATED}${NC}"
echo -e "  pyMC_Repeater updated: ${CYAN}${REPEATER_UPDATED}${NC}"
echo -e "  HAL rebuilt:      ${CYAN}$( [ "$FORCE_REBUILD" = true ] || [ "$HAL_UPDATED" = true ] && echo 'yes' || echo 'no')${NC}"
echo -e "  Full log:         ${CYAN}${LOG_FILE}${NC}"
echo ""
echo -e "  ${BOLD}Service control:${NC}"
echo -e "  Service control:  ${CYAN}sudo systemctl {start|stop|restart} pymc-repeater${NC}"
echo -e "  Service logs:     ${CYAN}journalctl -u pymc-repeater -f${NC}"
echo -e "  Web interface:    ${CYAN}http://<this-pi-ip>:${WEB_PORT}/wm1303.html${NC}"
echo ""
