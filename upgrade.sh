#!/bin/bash
# =============================================================================
# pyMC_WM1303 Upgrade Script
# =============================================================================
# Updates the WM1303 installation with the latest code from the fork
# repositories and re-applies overlay modifications.
#
# Usage: sudo bash upgrade.sh [--force-rebuild] [--force-config] [--skip-pull]
#
# Options:
#   --force-rebuild  Force rebuild of HAL and packet forwarder
#   --force-config   Overwrite existing config files with templates
#   --skip-pull      Skip pulling from remote repositories
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
    if [ -n "${UPGRADE_BACKUP:-}" ] && [ -d "${UPGRADE_BACKUP:-}" ]; then
        echo -e "  ${YELLOW}Rollback: backups are at ${UPGRADE_BACKUP}${NC}"
        echo -e "  ${YELLOW}  Config: cp -a ${UPGRADE_BACKUP}/pymc_repeater_config/* ${CONFIG_DIR}/${NC}"
        echo -e "  ${YELLOW}  DB:     cp ${UPGRADE_BACKUP}/db/*.db ${DATA_DIR}/${NC}"
    fi
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
CONFIG_DIR="/etc/openhop_repeater"
LOG_DIR="/var/log/openhop_repeater"
DATA_DIR="/var/lib/openhop_repeater"
OVERLAY_DIR="${SCRIPT_DIR}/overlay"
# PKTFWD_DIR, HAL_DIR, BACKUP_DIR are set after user detection (see below)

REBOOT_REQUIRED=false
VENV_REBUILD_NEEDED=false

# Branch configuration
HAL_BRANCH="master"
CORE_BRANCH="dev"
REPEATER_BRANCH="dev"

# Parse arguments
FORCE_REBUILD=false
FORCE_CONFIG=false
SKIP_PULL=false
FORCE_USER=""
for arg in "$@"; do
    case "$arg" in
        --force-rebuild|--rebuild) FORCE_REBUILD=true ;;
        --force-config) FORCE_CONFIG=true ;;
        --skip-pull)    SKIP_PULL=true ;;
        --user=*)       FORCE_USER="${arg#--user=}" ;;
        --help|-h)
            echo "Usage: sudo bash upgrade.sh [--force-rebuild] [--force-config] [--skip-pull] [--user=<username>]"
            echo "  --force-rebuild  Force rebuild of HAL and packet forwarder"
            echo "  --force-config   Overwrite existing config files with templates"
            echo "  --skip-pull      Skip pulling from remote repositories"
            echo "  --user=<name>    Force upgrade for specific user (default: auto-detect)"
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

# ---------------------------------------------------------------------------
# Detect target user (must match install.sh logic)
# Priority: --user=<name> > existing service file > SUDO_USER > auto-detect
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

    # 2. Read from existing service file (preserves the user from initial install)
    #    Prefer the new openhop unit, fall back to the legacy pymc unit.
    local svc_file=""
    if [ -f /etc/systemd/system/openhop-repeater.service ]; then
        svc_file=/etc/systemd/system/openhop-repeater.service
    elif [ -f /etc/systemd/system/pymc-repeater.service ]; then
        svc_file=/etc/systemd/system/pymc-repeater.service
    fi
    if [ -n "$svc_file" ]; then
        local svc_user
        svc_user=$(grep -oP '^User=\K.+' "$svc_file" 2>/dev/null || true)
        if [ -n "$svc_user" ] && [ "$svc_user" != "root" ] && id "$svc_user" &>/dev/null; then
            echo "$svc_user"
            return
        fi
    fi

    # 3. SUDO_USER (set by sudo when a regular user runs 'sudo bash upgrade.sh')
    if [ -n "${SUDO_USER:-}" ] && [ "$SUDO_USER" != "root" ] && id "$SUDO_USER" &>/dev/null; then
        echo "$SUDO_USER"
        return
    fi

    # 4. Check for common default users
    for candidate in pi orangepi radxa rock dietpi; do
        if id "$candidate" &>/dev/null; then
            echo "$candidate"
            return
        fi
    done

    # 5. First non-root user with UID >= 1000 and a valid home directory
    local found
    found=$(awk -F: '$3 >= 1000 && $3 < 65534 && $6 != "/" && $7 !~ /nologin|false/ {print $1; exit}' /etc/passwd)
    if [ -n "$found" ] && id "$found" &>/dev/null; then
        echo "$found"
        return
    fi

    fail "Could not detect a non-root user. Please specify one with --user=<username>\n  Example: sudo bash upgrade.sh --user=myuser"
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

# Set user-relative paths
PKTFWD_DIR="${PI_HOME}/wm1303_pf"
HAL_DIR="${PI_HOME}/sx1302_hal"
BACKUP_DIR="${PI_HOME}/backups"

info "Detected target user: ${PI_USER} (home: ${PI_HOME})"

if [ ! -d "${INSTALL_BASE}" ]; then
    fail "Installation not found at ${INSTALL_BASE}. Run install.sh first."
fi

if [ ! -d "${OVERLAY_DIR}" ]; then
    fail "Overlay directory not found at ${OVERLAY_DIR}"
fi

UPGRADE_VERSION="unknown"
if [ -f "${SCRIPT_DIR}/VERSION" ]; then
    UPGRADE_VERSION="v$(cat ${SCRIPT_DIR}/VERSION)"
fi
CURRENT_VERSION="unknown"
if [ -f "${CONFIG_DIR}/version" ]; then
    CURRENT_VERSION="v$(cat ${CONFIG_DIR}/version)"
fi
info "Current version: ${CURRENT_VERSION}"
info "Upgrading to: ${UPGRADE_VERSION}"
info "Installation directory: ${INSTALL_BASE}"
info "Overlay directory: ${OVERLAY_DIR}"
info "Log file: ${LOG_FILE}"

# Refuse downgrade
if [ "${CURRENT_VERSION}" != "unknown" ] && [ "${UPGRADE_VERSION}" != "unknown" ]; then
    CURRENT_SORT=$(echo "${CURRENT_VERSION#v}" | tr -d '[:space:]')
    UPGRADE_SORT=$(echo "${UPGRADE_VERSION#v}" | tr -d '[:space:]')
    HIGHER=$(printf '%s\n%s\n' "${CURRENT_SORT}" "${UPGRADE_SORT}" | sort -V | tail -1)
    if [ "${HIGHER}" = "${CURRENT_SORT}" ] && [ "${CURRENT_SORT}" != "${UPGRADE_SORT}" ]; then
        fail "Downgrade refused: installed ${CURRENT_VERSION} is newer than upgrade target ${UPGRADE_VERSION}"
    fi
fi

# =============================================================================
# Phase 1: Pre-upgrade Backup
# =============================================================================
phase "Pre-upgrade Backup"

TIMESTAMP=$(date +%Y%m%d_%H%M%S)
UPGRADE_BACKUP="${BACKUP_DIR}/pre-upgrade-${TIMESTAMP}"

# --- OpenHop config migration -------------------------------------------------
# Legacy config dir /etc/pymc_repeater is superseded by /etc/openhop_repeater.
# On the first openhop upgrade the new dir may not exist yet; copy the legacy
# contents (preserving JWT identity in config.yaml, the version file and
# wm1303_ui.json) WITHOUT overwriting anything already present, BEFORE the
# backup runs so the JWT survives. The legacy dir is left intact for rollback.
LEGACY_CONFIG_DIR="/etc/pymc_repeater"
if [ -d "${LEGACY_CONFIG_DIR}" ] && [ "${LEGACY_CONFIG_DIR}" != "${CONFIG_DIR}" ]; then
    mkdir -p "${CONFIG_DIR}"
    cp -an "${LEGACY_CONFIG_DIR}/." "${CONFIG_DIR}/" >> "${LOG_FILE}" 2>&1 || true
    ok "Migrated legacy config ${LEGACY_CONFIG_DIR} -> ${CONFIG_DIR} (JWT/version/wm1303_ui.json preserved)"
fi

# --- OpenHop var-tree physical migration --------------------------------------
# Legacy /var/log/pymc_repeater and /var/lib/pymc_repeater are superseded by
# /var/log/openhop_repeater and /var/lib/openhop_repeater. Physically move the
# legacy content into the new location BEFORE the pre-upgrade backup runs so
# the backup captures the migrated state. Historic logs and the SQLite DBs
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
mkdir -p "${LOG_DIR}" "${DATA_DIR}"
_migrate_legacy_vardir "/var/log/pymc_repeater" "${LOG_DIR}"  "log dir"
_migrate_legacy_vardir "/var/lib/pymc_repeater" "${DATA_DIR}" "data dir"

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
step "Backing up databases"
mkdir -p "${UPGRADE_BACKUP}/db"
for dbfile in "${DATA_DIR}/repeater.db" "${DATA_DIR}/spectrum_history.db"; do
    if [ -f "${dbfile}" ]; then
        cp "${dbfile}" "${UPGRADE_BACKUP}/db/" >> "${LOG_FILE}" 2>&1
    fi
done
ok "Database backup created"

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

step "Stopping repeater service"
SERVICE_WAS_RUNNING=false
# During the first openhop migration the running unit may still be the legacy
# pymc-repeater.service; on subsequent upgrades it is openhop-repeater.service.
# Stop whichever is active.
if systemctl is-active --quiet openhop-repeater.service 2>/dev/null; then
    SERVICE_WAS_RUNNING=true
    systemctl stop openhop-repeater.service >> "${LOG_FILE}" 2>&1
    ok "openhop-repeater service stopped"
elif systemctl is-active --quiet pymc-repeater.service 2>/dev/null; then
    SERVICE_WAS_RUNNING=true
    systemctl stop pymc-repeater.service >> "${LOG_FILE}" 2>&1
    ok "legacy pymc-repeater service stopped"
else
    ok "Service was not running"
fi

# =============================================================================
# Phase 2b: System Prerequisites & Backwards Compatibility
# =============================================================================
phase "System Prerequisites & Backwards Compatibility"

step "Ensuring required directories exist"
mkdir -p "${INSTALL_BASE}" "${REPO_DIR}" "${CONFIG_DIR}" "${PKTFWD_DIR}" "${LOG_DIR}" "${DATA_DIR}" "${BACKUP_DIR}"
chown -R ${PI_USER}:${PI_USER} "${INSTALL_BASE}" "${LOG_DIR}" "${DATA_DIR}" "${CONFIG_DIR}" "${PKTFWD_DIR}" "${BACKUP_DIR}"
ok "All directories verified"

step "Checking passwordless sudo for ${PI_USER}"
if [ ! -f /etc/sudoers.d/010_pi-nopasswd ]; then
    echo "${PI_USER} ALL=(ALL) NOPASSWD: ALL" > /etc/sudoers.d/010_pi-nopasswd
    chmod 440 /etc/sudoers.d/010_pi-nopasswd
    ok "Configured"
else
    ok "Already configured"
fi

step "Ensuring ${PI_USER} in hardware access groups"
usermod -aG spi,i2c,gpio,dialout ${PI_USER} 2>/dev/null || true
ok "Done"

step "Checking venv health"
if [ -d "${VENV_DIR}" ]; then
    if ! "${VENV_DIR}/bin/python3" --version &>/dev/null; then
        warn "Venv Python broken (system Python upgraded?) — will rebuild venv"
        VENV_REBUILD_NEEDED=true
    else
        ok "Venv Python is healthy"
    fi
else
    warn "Venv not found at ${VENV_DIR} — will be created"
    VENV_REBUILD_NEEDED=true
fi



step "Checking required packages"
PKGS_NEEDED=""
for pkg in jq i2c-tools rrdtool librrd-dev python3-rrdtool python3-systemd; do
    if ! dpkg -s "$pkg" &>/dev/null; then
        PKGS_NEEDED="${PKGS_NEEDED} ${pkg}"
    fi
done
if [ -n "${PKGS_NEEDED}" ]; then
    apt-get install -y ${PKGS_NEEDED} >> "${LOG_FILE}" 2>&1 || warn "Failed to install:${PKGS_NEEDED}"
    ok "Installed:${PKGS_NEEDED}"
else
    ok "All required packages present"
fi

step "Ensuring rrdtool module available in venv"
if [ -d "${VENV_DIR}" ]; then
    VENV_SITE=$(sudo -u ${PI_USER} "${VENV_DIR}/bin/python3" -c "import site; print(site.getsitepackages()[0])" 2>/dev/null)
    SYS_RRD=$(python3 -c "import rrdtool; print(rrdtool.__file__)" 2>/dev/null || true)
    if [ -n "${SYS_RRD}" ] && [ -f "${SYS_RRD}" ] && [ -n "${VENV_SITE}" ]; then
        sudo -u ${PI_USER} ln -sf "${SYS_RRD}" "${VENV_SITE}/"
        if sudo -u ${PI_USER} "${VENV_DIR}/bin/python3" -c "import rrdtool" 2>/dev/null; then
            ok "Symlinked $(basename ${SYS_RRD})"
        else
            warn "Symlink created but import failed - RRD metrics will be unavailable"
        fi
    else
        warn "System rrdtool module not found - RRD metrics will be unavailable"
    fi
else
    warn "venv not found at ${VENV_DIR} - skipping rrdtool symlink"
fi

step "Ensuring systemd module available in venv"
if [ -d "${VENV_DIR}" ]; then
    VENV_SITE=$(sudo -u ${PI_USER} "${VENV_DIR}/bin/python3" -c "import site; print(site.getsitepackages()[0])" 2>/dev/null)
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
else
    warn "venv not found at ${VENV_DIR} - skipping systemd symlink"
fi

step "Checking NTP synchronization"
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
else
    if systemctl is-active --quiet ntp 2>/dev/null; then
        ok "NTP daemon is running"
    else
        systemctl enable ntp >> "${LOG_FILE}" 2>&1 || true
        systemctl start ntp >> "${LOG_FILE}" 2>&1 || true
        ok "NTP daemon started"
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
        warn "Cannot find boot config file. I2C may need manual configuration."
    fi
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

    # Fix git 'dubious ownership' error (CVE-2022-24765)
    git config --global --add safe.directory "${target_dir}" 2>/dev/null
    sudo -u ${PI_USER} git config --global --add safe.directory "${target_dir}" 2>/dev/null
    # Ensure proper ownership before git operations
    chown -R ${PI_USER}:${PI_USER} "${target_dir}"

    cd "${target_dir}"
    local before=$(git rev-parse HEAD)

    # Discard local changes (overlay will be re-applied below).
    # `git reset --hard origin/<branch>` after fetch is idempotent and avoids
    # "Your local changes would be overwritten by merge" errors that can occur
    # when the overlay-copy has modified tracked files (e.g. repeater/main.py,
    # repeater/config.py) since the last upgrade.
    sudo -u ${PI_USER} git fetch --all >> "${LOG_FILE}" 2>&1
    # Use `git checkout -B` to FORCE creating/resetting the local branch to track
    # origin/<branch>. Without `-B`, a plain `git checkout main` would silently fail
    # when the local main branch doesn't yet exist (because the repo was originally
    # cloned with `-b dev`), leaving the local branch pointer on 'dev' even though
    # `git reset --hard origin/main` advances HEAD to the correct commit. This
    # caused branch-name vs HEAD inconsistency in v2.5.0 first deployments.
    sudo -u ${PI_USER} git checkout -B "${branch}" "origin/${branch}" >> "${LOG_FILE}" 2>&1
    sudo -u ${PI_USER} git reset --hard "origin/${branch}" >> "${LOG_FILE}" 2>&1
    sudo -u ${PI_USER} git clean -fd >> "${LOG_FILE}" 2>&1 || true

    # Sync upstream tags so setuptools-scm computes correct version numbers.
    # Note: the official upstream of pyMC_core and pyMC_Repeater has moved to
    # the openhop-dev GitHub organization:
    #   - https://github.com/openhop-dev/openhop_core      (was pyMC-dev/pymc-core)
    #   - https://github.com/openhop-dev/openhop_repeater  (was pyMC-dev/pymc-repeater)
    # The Hans van Meer forks (HansvanMeer/pyMC_core, HansvanMeer/pyMC_Repeater)
    # remain the WM1303 source of truth and are kept in sync with upstream.
    # The rightup mirrors below are used purely for setuptools-scm tag resolution
    # and may be swapped for openhop-dev URLs if those repos start shipping
    # version tags. See docs/repositories.md for the full relationship.
    local upstream_url=""
    case "${name}" in
        pyMC_Repeater) upstream_url="https://github.com/rightup/pyMC_Repeater.git" ;;
        pyMC_core)     upstream_url="https://github.com/rightup/pyMC_core.git" ;;
    esac
    if [ -n "${upstream_url}" ]; then
        if ! git remote get-url upstream >> "${LOG_FILE}" 2>&1; then
            sudo -u ${PI_USER} git remote add upstream "${upstream_url}" >> "${LOG_FILE}" 2>&1
        fi
        sudo -u ${PI_USER} git fetch upstream --tags >> "${LOG_FILE}" 2>&1 || true
    fi

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

# Pre-check: detect overlay changes BEFORE copying (compare new overlay vs deployed files)
HAL_OVERLAY_CHANGED=false
step "Checking HAL overlay checksums (before apply)"
OVERLAY_DIFFS=0
for overlay_file in \
    "libloragw/src/loragw_hal.c" \
    "libloragw/src/loragw_sx1302.c" \
    "libloragw/src/loragw_sx1261.c" \
    "libloragw/src/loragw_spi.c" \
    "libloragw/src/loragw_lbt.c" \
    "libloragw/src/loragw_aux.c" \
    "libloragw/src/sx1261_spi.c" \
    "libloragw/inc/loragw_sx1302.h" \
    "libloragw/inc/loragw_hal.h" \
    "libloragw/inc/loragw_sx1261.h" \
    "libloragw/inc/sx1261_defs.h" \
    "libloragw/inc/loragw_spi.h" \
    "libloragw/inc/loragw_lbt.h" \
    "libloragw/Makefile" \
    "packet_forwarder/src/lora_pkt_fwd.c" \
    "packet_forwarder/src/capture_thread.c" \
    "packet_forwarder/inc/capture_thread.h" \
    "packet_forwarder/Makefile"; do
    src="${OVERLAY_DIR}/hal/${overlay_file}"
    dst="${HAL_DIR}/${overlay_file}"
    if [ -f "${src}" ] && [ -f "${dst}" ]; then
        if ! cmp -s "${src}" "${dst}"; then
            OVERLAY_DIFFS=$((OVERLAY_DIFFS + 1))
            echo "  Changed: ${overlay_file}" >> "${LOG_FILE}"
        fi
    elif [ -f "${src}" ]; then
        OVERLAY_DIFFS=$((OVERLAY_DIFFS + 1))
        echo "  New: ${overlay_file}" >> "${LOG_FILE}"
    fi
done
if [ ${OVERLAY_DIFFS} -gt 0 ]; then
    HAL_OVERLAY_CHANGED=true
    ok "${OVERLAY_DIFFS} file(s) differ from deployed version"
else
    ok "All overlay files match deployed version"
fi


# =============================================================================
# Phase 4: Re-apply Overlay Modifications
# =============================================================================
phase "Re-apply Overlay Modifications"

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
for f in bridge_engine.py channel_e_bridge.py channel_f_bridge.py config_manager.py engine.py main.py identity_manager.py config.py packet_router.py metrics_retention.py uniform_tracer.py wm1303_telemetry_helper.py; do
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
# Phase 5: Rebuild HAL & Packet Forwarder (if needed)
# =============================================================================
phase "Rebuild HAL & Packet Forwarder"


# Check if compiled binary is missing (e.g., after manual clean or first overlay install)
BINARY_MISSING=false
if [ ! -f "${PKTFWD_DIR}/lora_pkt_fwd" ] || [ ! -f "${HAL_DIR}/libloragw/libloragw.a" ]; then
    BINARY_MISSING=true
    info "HAL binary missing — rebuild required"
fi

if [ "$FORCE_REBUILD" = true ] || [ "$HAL_UPDATED" = true ] || [ "$HAL_OVERLAY_CHANGED" = true ] || [ "$BINARY_MISSING" = true ]; then
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
    ok "Use --force-rebuild to force"
fi

# =============================================================================
# Phase 6: Update Python Packages
# =============================================================================
phase "Update Python Packages"

# ---------------------------------------------------------------------------
# Venv rebuild: if system Python was upgraded, recreate the venv from scratch
# ---------------------------------------------------------------------------
if [ "$VENV_REBUILD_NEEDED" = true ]; then
    step "Removing broken/missing venv"
    rm -rf "${VENV_DIR}"
    ok "Removed"

    step "Creating new Python virtual environment"
    if ! sudo -u ${PI_USER} python3 -m venv "${VENV_DIR}" >> "${LOG_FILE}" 2>&1; then
        fail "venv creation failed"
    fi
    ok "Created with $(python3 --version)"

    step "Upgrading pip and setuptools"
    if ! sudo -u ${PI_USER} "${VENV_DIR}/bin/pip" install --upgrade pip setuptools wheel >> "${LOG_FILE}" 2>&1; then
        fail "pip upgrade failed"
    fi
    ok "Done"

    step "Reinstalling pyMC_core (venv rebuild)"
    cd "${REPO_DIR}/pyMC_core"
    if ! sudo -u ${PI_USER} "${VENV_DIR}/bin/pip" install -e . >> "${LOG_FILE}" 2>&1; then
        fail "pyMC_core install failed"
    fi
    ok "Reinstalled"

    step "Reinstalling pyMC_Repeater (venv rebuild)"
    cd "${REPO_DIR}/pyMC_Repeater"
    if ! sudo -u ${PI_USER} "${VENV_DIR}/bin/pip" install -e . >> "${LOG_FILE}" 2>&1; then
        fail "pyMC_Repeater install failed"
    fi
    ok "Reinstalled"

    step "Reinstalling additional Python dependencies (venv rebuild)"
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

    # Re-symlink system rrdtool into new venv
    step "Re-symlinking rrdtool into new venv"
    VENV_SITE=$(sudo -u ${PI_USER} "${VENV_DIR}/bin/python3" -c "import site; print(site.getsitepackages()[0])" 2>/dev/null)
    SYS_RRD=$(python3 -c "import rrdtool; print(rrdtool.__file__)" 2>/dev/null || true)
    if [ -n "${SYS_RRD}" ] && [ -f "${SYS_RRD}" ] && [ -n "${VENV_SITE}" ]; then
        sudo -u ${PI_USER} ln -sf "${SYS_RRD}" "${VENV_SITE}/"
        if sudo -u ${PI_USER} "${VENV_DIR}/bin/python3" -c "import rrdtool" 2>/dev/null; then
            ok "Symlinked $(basename ${SYS_RRD})"
        else
            warn "Symlink created but import failed"
        fi
    else
        warn "System rrdtool module not found"
    fi

    # Re-symlink system systemd module into new venv
    step "Re-symlinking systemd module into new venv"
    VENV_SITE=$(sudo -u ${PI_USER} "${VENV_DIR}/bin/python3" -c "import site; print(site.getsitepackages()[0])" 2>/dev/null)
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
else
    # Normal path: only reinstall packages that changed
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
fi  # end VENV_REBUILD_NEEDED

# Verify overlays are accessible after all pip installs.
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
ok "Caches cleaned"


# =============================================================================
# Phase 7: Update Configuration Files
# =============================================================================
phase "Update Configuration Files"

step "Installing presets.json (channel presets per region)"
# Always overwrite presets.json since it is read-only catalog data managed by the installer.
if [ -f "${SCRIPT_DIR}/config/presets.json" ]; then
    cp "${SCRIPT_DIR}/config/presets.json" "${CONFIG_DIR}/presets.json" >> "${LOG_FILE}" 2>&1
    chown ${PI_USER}:${PI_USER} "${CONFIG_DIR}/presets.json"
    ok "presets.json installed"
else
    warn "presets.json not found in config/, skipping"
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

step "Checking SPI buffer size (spidev bufsiz=32768)"
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

step "Checking VPU core_freq_min=500 (stable SPI clock)"
BOOT_CONFIG="/boot/firmware/config.txt"
[ ! -f "$BOOT_CONFIG" ] && BOOT_CONFIG="/boot/config.txt"
if [ -f "$BOOT_CONFIG" ]; then
    if grep -q "^core_freq_min=500" "$BOOT_CONFIG"; then
        ok "Already configured"
    elif grep -q "^core_freq_min=" "$BOOT_CONFIG"; then
        sed -i 's/^core_freq_min=.*/core_freq_min=500/' "$BOOT_CONFIG"
        ok "Updated to 500 (was different)"
        REBOOT_REQUIRED=true
    else
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
else
    warn "Boot config not found — please add core_freq_min=500 manually"
fi

step "Checking gpu_mem=16 (headless optimisation)"
BOOT_CONFIG="/boot/firmware/config.txt"
[ ! -f "$BOOT_CONFIG" ] && BOOT_CONFIG="/boot/config.txt"
if [ -f "$BOOT_CONFIG" ]; then
    if grep -q "^gpu_mem=16" "$BOOT_CONFIG"; then
        ok "Already configured"
    elif grep -q "^gpu_mem=" "$BOOT_CONFIG"; then
        sed -i 's/^gpu_mem=.*/gpu_mem=16/' "$BOOT_CONFIG"
        ok "Updated to 16 (was different)"
        REBOOT_REQUIRED=true
    else
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

step "Checking SPI polling_limit_us=250 (persistent)"
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

step "Updating SPI optimization service script"
cp "${SCRIPT_DIR}/config/spi_optimize.sh" "${INSTALL_BASE}/spi_optimize.sh" >> "${LOG_FILE}" 2>&1
chmod 755 "${INSTALL_BASE}/spi_optimize.sh" 2>/dev/null
ok "Updated (runs at every service start)"


if [ "$FORCE_CONFIG" = true ]; then
    warn "--force-config: overwriting existing configuration files!"

    step "Updating wm1303_ui.json"
    cp "${SCRIPT_DIR}/config/wm1303_ui.json" "${CONFIG_DIR}/wm1303_ui.json" >> "${LOG_FILE}" 2>&1
    ok "Overwritten"

    step "Updating config.yaml"
    cp "${SCRIPT_DIR}/config/config.yaml.template" "${CONFIG_DIR}/config.yaml" >> "${LOG_FILE}" 2>&1
    # Replace placeholders with detected paths
    sed -i "s|__PKTFWD_DIR__|${PKTFWD_DIR}|g" "${CONFIG_DIR}/config.yaml"
    ok "Overwritten (pktfwd_dir: ${PKTFWD_DIR})"

    step "Updating global_conf.json"
    cp "${SCRIPT_DIR}/config/global_conf.json" "${PKTFWD_DIR}/global_conf.json" >> "${LOG_FILE}" 2>&1
    ok "Overwritten"
else
    # Smart merge: add missing keys from template without overwriting existing values
    step "Merging wm1303_ui.json (adding missing keys)"
    MERGE_RESULT=$(${VENV_DIR}/bin/python3 << PYMERGE 2>>${LOG_FILE}
import json, sys
try:
    tmpl_path = "${SCRIPT_DIR}/config/wm1303_ui.json"
    live_path = "${CONFIG_DIR}/wm1303_ui.json"
    with open(tmpl_path) as f:
        tmpl = json.load(f)
    with open(live_path) as f:
        live = json.load(f)
    added = []
    for key in tmpl:
        if key not in live:
            live[key] = tmpl[key]
            added.append(key)
        elif isinstance(tmpl[key], dict) and isinstance(live[key], dict):
            # Deep merge: add missing sub-keys from template
            for subkey in tmpl[key]:
                if subkey not in live[key]:
                    live[key][subkey] = tmpl[key][subkey]
                    added.append(f"{key}.{subkey}")
    if added:
        with open(live_path, 'w') as f:
            json.dump(live, f, indent=2)
        print("added: " + ", ".join(added))
    else:
        print("up-to-date")
except FileNotFoundError:
    import shutil
    shutil.copy2(tmpl_path, live_path)
    print("installed-from-template")
except Exception as e:
    print("error: " + str(e), file=sys.stderr)
    print("error")
PYMERGE
    )
    if [ "${MERGE_RESULT}" = "up-to-date" ]; then
        ok "All keys present"
    elif echo "${MERGE_RESULT}" | grep -q "^added:"; then
        ok "${MERGE_RESULT}"
    elif [ "${MERGE_RESULT}" = "installed-from-template" ]; then
        ok "Installed from template (first upgrade)"
    else
        warn "Config merge issue — see ${LOG_FILE}"
    fi

    step "Normalizing wm1303_ui.json (removing legacy field names)"
    NORM_RESULT=$(${VENV_DIR}/bin/python3 << PYNORM 2>>${LOG_FILE}
import json, sys
try:
    path = "${CONFIG_DIR}/wm1303_ui.json"
    with open(path) as f:
        ui = json.load(f)
    fixes = []
    # Normalize channels: rename/remove legacy short field names
    for ch in ui.get("channels", []):
        label = ch.get("friendly_name", ch.get("name", "?"))
        for short, full in [("sf", "spreading_factor"), ("bw", "bandwidth"), ("cr", "coding_rate")]:
            if short in ch and full in ch:
                fixes.append(f"{label}: removed {short}={ch[short]} (kept {full}={ch[full]})")
                del ch[short]
            elif short in ch:
                ch[full] = ch.pop(short)
                fixes.append(f"{label}: renamed {short} -> {full}={ch[full]}")
    # Normalize channel_e
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


    step "Migrating config.yaml (key renames and value updates)"
    CONFIG_MIGRATE=$(${VENV_DIR}/bin/python3 << PYMIGRATE 2>>${LOG_FILE}
import yaml, sys

live_path = "${CONFIG_DIR}/config.yaml"
try:
    with open(live_path) as f:
        cfg = yaml.safe_load(f) or {}
except FileNotFoundError:
    print("skipped-no-config")
    sys.exit(0)

changes = []

# --- Bridge section ---
br = cfg.get('bridge', {})

# Migrate dedup_ttl -> dedup_ttl_seconds (SSOT key since v2.2.0)
# The canonical key is bridge.dedup_ttl_seconds (seconds, default 300).
# Legacy key bridge.dedup_ttl may exist from older installs.
if 'dedup_ttl' in br and 'dedup_ttl_seconds' not in br:
    old_val = br.pop('dedup_ttl')
    # Old configs may have had dedup_ttl in seconds already (300) or
    # in the legacy small-integer form (15). Accept the value as-is.
    br['dedup_ttl_seconds'] = int(old_val) if old_val else 300
    changes.append('bridge.dedup_ttl->dedup_ttl_seconds=%d' % br['dedup_ttl_seconds'])
elif 'dedup_ttl' in br and 'dedup_ttl_seconds' in br:
    # Both keys exist (broken state) — keep dedup_ttl_seconds, remove legacy
    br.pop('dedup_ttl')
    changes.append('bridge: removed duplicate dedup_ttl (kept dedup_ttl_seconds=%d)' % br['dedup_ttl_seconds'])

# Ensure dedup_ttl_seconds exists with correct default
if 'dedup_ttl_seconds' not in br:
    br['dedup_ttl_seconds'] = 300
    changes.append('bridge.dedup_ttl_seconds=300')
elif br['dedup_ttl_seconds'] < 300:
    old_val = br['dedup_ttl_seconds']
    br['dedup_ttl_seconds'] = 300
    changes.append('bridge.dedup_ttl_seconds: %d->300 (enforced minimum)' % old_val)

cfg['bridge'] = br

# --- Repeater section ---
rep = cfg.get('repeater', {})

# Ensure cache_ttl has a sane value (default 300 since v2.2.0)
if 'cache_ttl' not in rep:
    rep['cache_ttl'] = 300
    changes.append('repeater.cache_ttl=300')
elif rep['cache_ttl'] < 300:
    old_val = rep['cache_ttl']
    rep['cache_ttl'] = 300
    changes.append('repeater.cache_ttl: %d->300 (enforced minimum)' % old_val)

# Ensure max_cache_size exists (default 1000 since v2.2.0)
if 'max_cache_size' not in rep:
    rep['max_cache_size'] = 1000
    changes.append('repeater.max_cache_size=1000')
elif rep['max_cache_size'] < 1000:
    old_val = rep['max_cache_size']
    rep['max_cache_size'] = 1000
    changes.append('repeater.max_cache_size: %d->1000 (enforced minimum)' % old_val)

# tx_delay_factor: set to 0 (v2.1.0: CAD handles collision avoidance)
if rep.get('tx_delay_factor', 0) != 0:
    rep['tx_delay_factor'] = 0
    changes.append('repeater.tx_delay_factor=0')

# direct_tx_delay_factor: set to 0 (v2.1.0: CAD handles collision avoidance)
if rep.get('direct_tx_delay_factor', 0) != 0:
    rep['direct_tx_delay_factor'] = 0
    changes.append('repeater.direct_tx_delay_factor=0')

cfg['repeater'] = rep

# --- Delays section ---
dly = cfg.get('delays', {})
if dly.get('tx_delay_factor', 0) != 0:
    dly['tx_delay_factor'] = 0
    changes.append('delays.tx_delay_factor=0')
if dly.get('direct_tx_delay_factor', 0) != 0:
    dly['direct_tx_delay_factor'] = 0
    changes.append('delays.direct_tx_delay_factor=0')
cfg['delays'] = dly

# --- WM1303 TX Queue section ---
wm = cfg.get('wm1303', {})
tq = wm.get('tx_queue', {})
if tq.get('tx_delay_ms', 0) != 0:
    tq['tx_delay_ms'] = 0
    changes.append('wm1303.tx_queue.tx_delay_ms=0')
wm['tx_queue'] = tq
cfg['wm1303'] = wm

# --- Bridge rules: set tx_delay_ms to 0 on all rules ---
for section_key in ['bridge', 'wm1303']:
    section = cfg.get(section_key, {})
    rules = section.get('bridge_rules', [])
    if isinstance(rules, list):
        for rule in rules:
            if isinstance(rule, dict) and rule.get('tx_delay_ms', 0) != 0:
                rule['tx_delay_ms'] = 0
                changes.append(section_key + '.bridge_rules.' + rule.get('name', '?') + '.tx_delay_ms=0')
        section['bridge_rules'] = rules
        cfg[section_key] = section

if changes:
    with open(live_path, 'w') as f:
        yaml.dump(cfg, f, default_flow_style=False, allow_unicode=True)
    print('migrated: ' + ', '.join(changes))
else:
    print('up-to-date')
PYMIGRATE
    )
    if [ "${CONFIG_MIGRATE}" = "up-to-date" ]; then
        ok "No migrations needed"
    elif echo "${CONFIG_MIGRATE}" | grep -q "^migrated:"; then
        ok "${CONFIG_MIGRATE}"
    elif [ "${CONFIG_MIGRATE}" = "skipped-no-config" ]; then
        ok "Skipped (no config.yaml yet)"
    else
        warn "Config migration issue — see ${LOG_FILE}"
    fi

    step "Merging config.yaml (adding missing fields)"
    YAML_MERGE=$(${VENV_DIR}/bin/python3 << PYYAML 2>>${LOG_FILE}
import yaml, sys
def deep_merge(base, override):
    added = []
    for key, val in base.items():
        if key not in override:
            override[key] = val
            added.append(key)
        elif isinstance(val, dict) and isinstance(override.get(key), dict):
            sub = deep_merge(val, override[key])
            added.extend(key + "." + s for s in sub)
    return added
try:
    tmpl_path = "${SCRIPT_DIR}/config/config.yaml.template"
    live_path = "${CONFIG_DIR}/config.yaml"
    with open(tmpl_path) as f:
        tmpl = yaml.safe_load(f) or {}
    with open(live_path) as f:
        live = yaml.safe_load(f) or {}
    added = deep_merge(tmpl, live)
    if added:
        with open(live_path, 'w') as f:
            yaml.dump(live, f, default_flow_style=False, allow_unicode=True)
        print("added: " + ", ".join(added))
    else:
        print("up-to-date")
except FileNotFoundError:
    import shutil
    shutil.copy2(tmpl_path, live_path)
    print("installed-from-template")
except Exception as e:
    print("error: " + str(e), file=sys.stderr)
    print("error")
PYYAML
    )
    if [ "${YAML_MERGE}" = "up-to-date" ]; then
        ok "All fields present"
    elif echo "${YAML_MERGE}" | grep -q "^added:"; then
        ok "${YAML_MERGE}"
    elif [ "${YAML_MERGE}" = "installed-from-template" ]; then
        ok "Installed from template (first upgrade)"
    else
        warn "Config merge issue — see ${LOG_FILE}"
    fi

    step "Preserving bridge_config.yaml"
    ok "Preserved (never overwritten)"
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
# Legacy pymc-repeater.service is superseded by openhop-repeater.service.
# Disable+remove the old unit so both cannot enable simultaneously.
if systemctl list-unit-files 2>/dev/null | grep -q '^pymc-repeater.service'; then
    systemctl disable pymc-repeater.service >> "${LOG_FILE}" 2>&1 || true
    rm -f /etc/systemd/system/pymc-repeater.service 2>/dev/null || true
fi
cp "${SCRIPT_DIR}/config/openhop-repeater.service" /etc/systemd/system/openhop-repeater.service >> "${LOG_FILE}" 2>&1
# Replace placeholders with detected user
sed -i "s|__PI_USER__|${PI_USER}|g" /etc/systemd/system/openhop-repeater.service
sed -i "s|__PI_HOME__|${PI_HOME}|g" /etc/systemd/system/openhop-repeater.service
systemctl daemon-reload >> "${LOG_FILE}" 2>&1
systemctl enable openhop-repeater.service >> "${LOG_FILE}" 2>&1
ok "Service file updated (user: ${PI_USER})"

# --- Hardware Watchdog (OS-level) ---
# Keep upgrades consistent with install.sh: BCM2835 hardware watchdog module +
# systemd RuntimeWatchdogSec so a full OS freeze reboots the Pi automatically.
# The service-level watchdog (Type=notify + WatchdogSec) comes from the updated
# pymc-repeater.service file copied just above.
step "Enabling BCM2835 hardware watchdog module"
WDT_MODCONF="/etc/modules-load.d/bcm2835_wdt.conf"
if [ -f "${WDT_MODCONF}" ] && grep -q '^bcm2835_wdt' "${WDT_MODCONF}" 2>/dev/null; then
    ok "Module already configured"
else
    echo "bcm2835_wdt" > "${WDT_MODCONF}"
    ok "Configured ${WDT_MODCONF}"
fi
modprobe bcm2835_wdt 2>/dev/null || warn "modprobe bcm2835_wdt failed (will load at next boot)"

# Note: the BCM2835 hardware watchdog enforces a fixed 60s timeout (wdctl
# SETTIMEOUT=0); lower values are clamped to 60s by the kernel. We configure 60s
# to match the effective hardware behaviour (Pi auto-reboots within ~60s on freeze).
step "Configuring systemd RuntimeWatchdogSec=60s (BCM2835 hardware limit)"
SYSTEMD_CONF="/etc/systemd/system.conf"
if [ -f "${SYSTEMD_CONF}" ]; then
    if grep -qE '^RuntimeWatchdogSec=60s$' "${SYSTEMD_CONF}"; then
        ok "Already set"
    else
        TMP_SC="$(mktemp)"
        grep -viE '^[#[:space:]]*RuntimeWatchdogSec' "${SYSTEMD_CONF}" > "${TMP_SC}"
        echo "RuntimeWatchdogSec=60s" >> "${TMP_SC}"
        mv "${TMP_SC}" "${SYSTEMD_CONF}"
        systemctl daemon-reexec >> "${LOG_FILE}" 2>&1 || warn "daemon-reexec failed (applies after reboot)"
        ok "Set RuntimeWatchdogSec=60s"
    fi
else
    warn "${SYSTEMD_CONF} not found; skipped RuntimeWatchdogSec"
fi

step "Updating version file"
if [ -f "${SCRIPT_DIR}/VERSION" ]; then
    cp "${SCRIPT_DIR}/VERSION" "${CONFIG_DIR}/version" >> "${LOG_FILE}" 2>&1
    chown ${PI_USER}:${PI_USER} "${CONFIG_DIR}/version"
    # NOTE: The v2.6.2 dual-write shim to ${LEGACY_CONFIG_DIR}/version was
    # removed in v2.6.3 now that the overlay code reads via
    # openhop_core.paths.resolve_config_path(), which handles both
    # /etc/openhop_repeater/ (canonical) and /etc/pymc_repeater/ (legacy
    # fallback). The Phase 1 legacy config-dir migration earlier in this
    # script (cp -an ${LEGACY_CONFIG_DIR}/. ${CONFIG_DIR}/) still ensures
    # that devices upgraded from v2.5.x get their old version file copied
    # into ${CONFIG_DIR}/ before this step overwrites it with the new value.
    ok "v$(cat ${SCRIPT_DIR}/VERSION)"
else
    warn "VERSION file not found in repo"
fi


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
# Phase 7b: Database Schema Migration & Cleanup
# =============================================================================
phase "Database Schema Migration & Cleanup"

DB_PATH="${DATA_DIR}/repeater.db"
SPECTRUM_DB="${DATA_DIR}/spectrum_history.db"

if [ -f "${DB_PATH}" ]; then
    step "Running schema migration"
    MIGRATION_RESULT=$(${VENV_DIR}/bin/python3 << DBMIGRATE 2>>${LOG_FILE}
import sqlite3, sys, time

def migrate(db_path):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    changes = []

    # --- Create tables if missing ---
    tables = {
        'channel_stats_history': '''CREATE TABLE IF NOT EXISTS channel_stats_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            channel_id TEXT, timestamp REAL, avg_rssi REAL, avg_snr REAL,
            pkt_count INTEGER, noise_floor_dbm REAL
        )''',
        'noise_floor_history': '''CREATE TABLE IF NOT EXISTS noise_floor_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            channel_id TEXT, timestamp REAL, noise_floor_dbm REAL
        )''',
        'noise_floor': '''CREATE TABLE IF NOT EXISTS noise_floor (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp REAL NOT NULL,
            noise_floor_dbm REAL NOT NULL
        )''',
        'packets': '''CREATE TABLE IF NOT EXISTS packets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp REAL, direction TEXT, channel TEXT,
            frequency REAL, sf INTEGER, bw INTEGER,
            rssi REAL, snr REAL, payload BLOB,
            raw_hex TEXT, size INTEGER
        )''',
        'adverts': '''CREATE TABLE IF NOT EXISTS adverts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp REAL, node_id TEXT, short_name TEXT,
            long_name TEXT, rssi REAL, snr REAL, hops INTEGER
        )''',
        'crc_errors': '''CREATE TABLE IF NOT EXISTS crc_errors (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp REAL, channel TEXT, frequency REAL,
            sf INTEGER, bw INTEGER, rssi REAL, snr REAL
        )''',
        'dedup_events': '''CREATE TABLE IF NOT EXISTS dedup_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp REAL, channel TEXT, frequency REAL,
            payload_hash TEXT, action TEXT
        )''',
        'migrations': '''CREATE TABLE IF NOT EXISTS migrations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE NOT NULL,
            applied_at REAL NOT NULL
        )''',
        'packet_activity': '''CREATE TABLE IF NOT EXISTS packet_activity (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp REAL NOT NULL,
            channel_id TEXT NOT NULL,
            rx_count INTEGER DEFAULT 0,
            tx_count INTEGER DEFAULT 0
        )''',
        'cad_events': '''CREATE TABLE IF NOT EXISTS cad_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp REAL NOT NULL,
            channel_id TEXT NOT NULL,
            cad_clear INTEGER DEFAULT 0,
            cad_detected INTEGER DEFAULT 0,
            cad_skipped INTEGER DEFAULT 0,
            cad_hw_clear INTEGER DEFAULT 0,
            cad_hw_detected INTEGER DEFAULT 0,
            cad_sw_clear INTEGER DEFAULT 0,
            cad_sw_detected INTEGER DEFAULT 0
        )''',
    }
    for tname, ddl in tables.items():
        # Check if table exists
        exists = cur.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (tname,)).fetchone()
        if not exists:
            cur.execute(ddl)
            changes.append("created table " + tname)

    # --- Add missing columns ---
    def has_column(table, column):
        try:
            cols = [r[1] for r in cur.execute("PRAGMA table_info(" + table + ")").fetchall()]
            return column in cols
        except Exception:
            return True  # assume exists if check fails

    col_migrations = [
        ('adverts', 'zero_hop', 'BOOLEAN NOT NULL DEFAULT FALSE'),
        ('packets', 'lbt_attempts', 'INTEGER DEFAULT 0'),
        ('packets', 'lbt_backoff_delays_ms', 'TEXT'),
        ('packets', 'lbt_channel_busy', 'BOOLEAN DEFAULT FALSE'),
        ('channel_stats_history', 'noise_floor_dbm', 'REAL'),
        ('channel_stats_history', 'pkt_count', 'INTEGER'),
        # Defensive for pre-v2.1 installs (cad_events HW/SW split)
        ('cad_events', 'cad_hw_clear', 'INTEGER DEFAULT 0'),
        ('cad_events', 'cad_hw_detected', 'INTEGER DEFAULT 0'),
        ('cad_events', 'cad_sw_clear', 'INTEGER DEFAULT 0'),
        ('cad_events', 'cad_sw_detected', 'INTEGER DEFAULT 0'),
    ]
    for table, column, coldef in col_migrations:
        if not has_column(table, column):
            try:
                cur.execute("ALTER TABLE " + table + " ADD COLUMN " + column + " " + coldef)
                changes.append("added " + table + "." + column)
            except Exception as e:
                pass  # column may already exist

    # --- Create indexes ---
    indexes = [
        ('idx_noise_timestamp', 'noise_floor', 'timestamp'),
        ('idx_stats_channel_ts', 'channel_stats_history', 'channel_id, timestamp'),
        ('idx_packets_timestamp', 'packets', 'timestamp'),
        ('idx_pktact_ts', 'packet_activity', 'timestamp'),
        ('idx_cadevt_ts', 'cad_events', 'timestamp'),
    ]
    for idx_name, table, cols in indexes:
        try:
            cur.execute("CREATE INDEX IF NOT EXISTS " + idx_name + " ON " + table + "(" + cols + ")")
        except Exception:
            pass

    conn.commit()
    conn.close()
    return changes

try:
    result = migrate("${DB_PATH}")
    if result:
        print(str(len(result)) + " changes: " + ", ".join(result))
    else:
        print("up-to-date")
except Exception as e:
    print("error: " + str(e), file=sys.stderr)
    print("error")
DBMIGRATE
    )
    if [ "${MIGRATION_RESULT}" = "up-to-date" ]; then
        ok "Schema up to date"
    elif echo "${MIGRATION_RESULT}" | grep -q "changes:"; then
        ok "${MIGRATION_RESULT}"
    else
        warn "Migration issue — see ${LOG_FILE}"
    fi

    step "Cleaning bogus TX echo data (avg_rssi > -50 dBm)"
    BOGUS_COUNT=$(${VENV_DIR}/bin/python3 -c "
import sqlite3
try:
    conn = sqlite3.connect('${DB_PATH}')
    cur = conn.cursor()
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
        ok "Cleaned ${BOGUS_COUNT} rows"
    else
        ok "No bogus data found"
    fi

    step "Cleaning old channel_id formats"
    OLD_FORMAT_COUNT=$(${VENV_DIR}/bin/python3 -c "
import sqlite3
try:
    conn = sqlite3.connect('${DB_PATH}')
    cur = conn.cursor()
    total = 0
    for table in ['channel_stats_history', 'noise_floor_history']:
        try:
            count = cur.execute('SELECT COUNT(*) FROM ' + table + ' WHERE channel_id NOT LIKE \"channel_%\" AND channel_id NOT LIKE \"inactive_%\"').fetchone()[0]
            if count > 0:
                cur.execute('DELETE FROM ' + table + ' WHERE channel_id NOT LIKE \"channel_%\" AND channel_id NOT LIKE \"inactive_%\"')
                total += count
        except Exception:
            pass
    conn.commit()
    print(total)
    conn.close()
except Exception:
    print(0)
" 2>/dev/null || echo "0")
    if [ "${OLD_FORMAT_COUNT}" -gt 0 ]; then
        ok "Removed ${OLD_FORMAT_COUNT} rows"
    else
        ok "No old format IDs found"
    fi
else
    info "Database not found at ${DB_PATH}, skipping migration"
fi

# One-time VACUUM on upgrade (retention cutover from mixed days to uniform 8)
if [ -f "${DB_PATH}" ]; then
    step "Running one-time VACUUM (retention cutover cleanup)"
    ${VENV_DIR}/bin/python3 -c "
import sqlite3
try:
    conn = sqlite3.connect('${DB_PATH}')
    conn.execute('VACUUM')
    conn.close()
    print('ok')
except Exception as e:
    print('skip: ' + str(e))
" >> "${LOG_FILE}" 2>&1 && ok "VACUUM complete" || ok "VACUUM skipped"
fi

# Clean up orphaned tables in spectrum_history.db
# Since v2.x, CAD and LBT data is tracked in repeater.db by _packet_activity_recorder.
# The spectrum_collector no longer writes to lbt_events/cad_events in spectrum_history.db.
if [ -f "${SPECTRUM_DB}" ]; then
    step "Cleaning orphaned CAD/LBT data from spectrum_history.db"
    ORPHAN_COUNT=$(${VENV_DIR}/bin/python3 -c "
import sqlite3
try:
    conn = sqlite3.connect('${SPECTRUM_DB}')
    cur = conn.cursor()
    total = 0
    for table in ['lbt_events', 'cad_events']:
        try:
            count = cur.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0]
            if count > 0:
                cur.execute('DELETE FROM ' + table)
                total += count
        except Exception:
            pass
    conn.commit()
    print(total)
    conn.close()
except Exception:
    print(0)
" 2>/dev/null || echo "0")
    if [ "${ORPHAN_COUNT}" -gt 0 ]; then
        ok "Removed ${ORPHAN_COUNT} orphaned rows from spectrum_history.db"
    else
        ok "No orphaned data found"
    fi
else
    info "spectrum_history.db not found, skipping cleanup"
fi


# =============================================================================
# Phase 7c: Low-Memory Device Maintenance
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
# Phase 8: Restart and Verify Service
# =============================================================================
phase "Restart and Verify Service"

step "Performing extended hardware drain reset (60s)"
sudo "${PKTFWD_DIR}/reset_lgw.sh" deep_reset 60 >> "${LOG_FILE}" 2>&1
ok "Hardware drain reset complete"

step "Starting openhop-repeater service"
systemctl start openhop-repeater.service >> "${LOG_FILE}" 2>&1
sleep 3
ok "Service start command issued"

step "Checking service status"
if systemctl is-active --quiet openhop-repeater.service; then
    ok "openhop-repeater service is RUNNING"
else
    warn "Service may not have started correctly"
    info "Check logs with: journalctl -u openhop-repeater -f"
fi

step "Checking web interface availability"
sleep 2
WEB_PORT=$(grep -oP '^\s*port:\s*\K[0-9]+' "${CONFIG_DIR}/config.yaml" 2>/dev/null | head -1)
WEB_PORT=${WEB_PORT:-8000}
if command -v curl &>/dev/null; then
    # v2.5.6: retry loop (5x 2s = max 10s) to handle slow webserver startup
    WEB_OK=0
    WEB_TRY=0
    for WEB_TRY in 1 2 3 4 5; do
        if curl -s -o /dev/null -w "%{http_code}" "http://127.0.0.1:${WEB_PORT}/" 2>/dev/null | grep -q "200\|302\|401"; then
            WEB_OK=1
            break
        fi
        sleep 2
    done
    if [ "${WEB_OK}" = "1" ]; then
        ok "Web interface responding on port ${WEB_PORT} (ready after ${WEB_TRY} attempt(s))"
    else
        warn "Web interface not responding after 5 attempts (10s) - check: journalctl -u openhop-repeater"
    fi
fi

step "Checking journal for post-startup errors"
sleep 7
JOURNAL_ERRORS=$(journalctl -u openhop-repeater --since "30 seconds ago" -p err --no-pager 2>/dev/null | grep -v "^-- " | head -5)
if [ -z "${JOURNAL_ERRORS}" ]; then
    ok "No errors in journal"
else
    warn "Errors detected in journal after startup:"
    echo "${JOURNAL_ERRORS}" | head -5 | sed 's/^/    /'
    info "Review with: journalctl -u openhop-repeater --since '5 minutes ago'"
fi

step "Checking concentrator module detection"
sleep 10
CONCENTRATOR_LOG=$(journalctl -u openhop-repeater --since '90 seconds ago' --no-pager 2>/dev/null || true)
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
    echo -e "  The upgrade completed successfully, but the SX1302 concentrator"
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
    echo -e "  The service is running and will start automatically on boot."
    echo -e "  You can restart it after adjusting settings:"
    echo -e "  ${CYAN}sudo systemctl restart openhop-repeater${NC}"
    echo ""
fi

# =============================================================================
# Upgrade Complete
# =============================================================================
VERSION_STR="unknown"
if [ -f "${SCRIPT_DIR}/VERSION" ]; then
    VERSION_STR="v$(cat ${SCRIPT_DIR}/VERSION)"
fi

echo -e "\n${BOLD}${GREEN}"
echo "  ╔══════════════════════════════════════════════════════════╗"
printf "  ║%-58s║\n" "     Upgrade Complete!  ${VERSION_STR}"
echo "  ╚══════════════════════════════════════════════════════════╝"
echo -e "${NC}"
echo -e "  ${BOLD}Summary:${NC}"
echo -e "  ─────────────────────────────────────────────────────────"
echo -e "  Version:          ${CYAN}${VERSION_STR}${NC}"
echo -e "  Backup location:  ${CYAN}${UPGRADE_BACKUP}${NC}"
echo -e "  HAL updated:      ${CYAN}${HAL_UPDATED}${NC}"
echo -e "  HAL overlay diff: ${CYAN}${HAL_OVERLAY_CHANGED}${NC}"
echo -e "  pyMC_core updated: ${CYAN}${CORE_UPDATED}${NC}"
echo -e "  pyMC_Repeater updated: ${CYAN}${REPEATER_UPDATED}${NC}"
echo -e "  HAL rebuilt:      ${CYAN}$( [ "$FORCE_REBUILD" = true ] || [ "$HAL_UPDATED" = true ] || [ "$HAL_OVERLAY_CHANGED" = true ] || [ "$BINARY_MISSING" = true ] && echo 'yes' || echo 'no')${NC}"
echo -e "  Full log:         ${CYAN}${LOG_FILE}${NC}"
echo ""
echo -e "  ${BOLD}Service control:${NC}"
echo -e "  sudo systemctl {start|stop|restart} openhop-repeater"
echo -e "  journalctl -u openhop-repeater -f"
echo -e "  Web interface:    ${CYAN}http://<this-pi-ip>:${WEB_PORT}/wm1303.html${NC}"
echo ""

if [ "$REBOOT_REQUIRED" = true ]; then
    echo -e "  ${BOLD}${YELLOW}╔══════════════════════════════════════════════════════════╗${NC}"
    echo -e "  ${BOLD}${YELLOW}║  REBOOT RECOMMENDED to apply kernel/hardware changes     ║${NC}"
    echo -e "  ${BOLD}${YELLOW}╚══════════════════════════════════════════════════════════╝${NC}"
    echo ""
    echo -e "  ${YELLOW}Some changes (SPI buffer size, core_freq_min, gpu_mem, I2C) require a reboot to take effect.${NC}"
    echo -e "  ${YELLOW}Run: sudo reboot${NC}"
    echo ""
fi
