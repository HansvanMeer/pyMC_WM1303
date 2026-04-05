#!/bin/bash
# =============================================================================
# pyMC_WM1303 One-Line Upgrade Bootstrap
# =============================================================================
#
# Usage:
#   curl -sSL https://raw.githubusercontent.com/HansvanMeer/pyMC_WM1303/main/upgrade_bootstrap.sh | sudo bash
#
# This script:
#   1. Installs git (if not present)
#   2. Clones the pyMC_WM1303 repository to a temp directory
#   3. Runs the upgrade script
#   4. Cleans up the temp directory
# =============================================================================

set -e

REPO_URL="https://github.com/HansvanMeer/pyMC_WM1303.git"
TMP_DIR=$(mktemp -d /tmp/wm1303_upgrade.XXXXXX)

cleanup() {
    rm -rf "${TMP_DIR}"
}
trap cleanup EXIT

echo ""
echo "  ╔══════════════════════════════════════════════════════════╗"
echo "  ║     pyMC_WM1303 One-Line Upgrade                         ║"
echo "  ╚══════════════════════════════════════════════════════════╝"
echo ""

# Ensure running as root
if [ "$(id -u)" -ne 0 ]; then
    echo "  ✗ This script must be run as root (use sudo)"
    exit 1
fi

# Check that installation exists
if [ ! -d "/opt/pymc_repeater" ]; then
    echo "  ✗ No existing installation found at /opt/pymc_repeater"
    echo "  ℹ Run the installer first:"
    echo "    curl -sSL https://raw.githubusercontent.com/HansvanMeer/pyMC_WM1303/main/bootstrap.sh | sudo bash"
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

# Clone repository to temp directory
echo "  ℹ Fetching latest release..."
git clone --depth 1 "${REPO_URL}" "${TMP_DIR}/pyMC_WM1303" 2>/dev/null
echo "  ✓ Repository fetched"

# Run upgrade
echo ""
echo "  ℹ Starting upgrade..."
echo ""
cd "${TMP_DIR}/pyMC_WM1303"
bash upgrade.sh

# Cleanup is handled by trap
echo ""
echo "  ✓ Temporary files cleaned up"
