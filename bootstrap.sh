#!/bin/bash
# =============================================================================
# pyMC_WM1303 One-Line Installer / Upgrade Bootstrap
# =============================================================================
#
# Usage:
#   curl -sSL https://raw.githubusercontent.com/HansvanMeer/pyMC_WM1303/main/bootstrap.sh | sudo bash
#
# This script:
#   1. Installs git (if not present)
#   2. Clones or updates the pyMC_WM1303 repository
#   3. Detects existing installation:
#      - New install: runs install.sh
#      - Existing install: runs upgrade.sh
# =============================================================================

set -e

REPO_URL="https://github.com/HansvanMeer/pyMC_WM1303.git"
INSTALL_DIR="/home/pi/pyMC_WM1303"
PI_USER="pi"

echo ""
echo "  ╔══════════════════════════════════════════════════════════╗"
echo "  ║     pyMC_WM1303 Bootstrap                                ║"
echo "  ╚══════════════════════════════════════════════════════════╝"
echo ""

# Ensure running as root
if [ "$(id -u)" -ne 0 ]; then
    echo "  ✗ This script must be run as root (use sudo)"
    exit 1
fi

# Install git if not present
if ! command -v git &>/dev/null; then
    echo "  ℹ Installing git..."
    apt-get update -qq
    apt-get install -y -qq git
    echo "  ✓ git installed"
else
    echo "  ✓ git already available"
fi

# Fix git 'dubious ownership' error (CVE-2022-24765)
git config --global --add safe.directory "${INSTALL_DIR}" 2>/dev/null
sudo -u ${PI_USER} git config --global --add safe.directory "${INSTALL_DIR}" 2>/dev/null

# Clone or update repository
if [ -d "${INSTALL_DIR}/.git" ]; then
    echo "  ℹ Repository already exists, pulling latest changes..."
    chown -R ${PI_USER}:${PI_USER} "${INSTALL_DIR}"
    cd "${INSTALL_DIR}"
    sudo -u ${PI_USER} git pull
    echo "  ✓ Repository updated"
else
    echo "  ℹ Cloning repository..."
    sudo -u ${PI_USER} git clone "${REPO_URL}" "${INSTALL_DIR}"
    echo "  ✓ Repository cloned"
fi

cd "${INSTALL_DIR}"

# Detect existing installation
if [ -d "/opt/pymc_repeater" ] && systemctl is-enabled pymc-repeater &>/dev/null; then
    echo ""
    echo "  ╔══════════════════════════════════════════════════════════╗"
    echo "  ║  Existing installation detected — running UPGRADE        ║"
    echo "  ╚══════════════════════════════════════════════════════════╝"
    echo ""
    bash upgrade.sh
else
    echo ""
    echo "  ╔══════════════════════════════════════════════════════════╗"
    echo "  ║  No installation found — running INSTALL                 ║"
    echo "  ╚══════════════════════════════════════════════════════════╝"
    echo ""
    bash install.sh
fi
