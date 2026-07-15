#!/bin/bash
# =============================================================================
# pyMC_WM1303 One-Line Installer / Upgrade Bootstrap (with Region+Preset Wizard)
# =============================================================================
#
# Usage:
#   curl -sSL https://raw.githubusercontent.com/HansvanMeer/pyMC_WM1303/main/bootstrap.sh | sudo bash
#
# Optional environment overrides (skip interactive prompts):
#   WM1303_REGION=EU868|US915|AU915|AS923|IN865|JP920|KR920|CUSTOM
#   WM1303_PRESET=<preset-name-from-presets.json>
#   WM1303_SYNC_WORD=private|public|<hex>   (device-wide LoRa network sync word;
#                                            hex example: 0x1234; default: private)
#
# Optional flag:
#   --non-interactive   skip all wizard prompts, fall back to EU-Default preset
#
# This script:
#   1. Installs git + jq (if not present)
#   2. Clones or updates the pyMC_WM1303 repository
#   3. On NEW install only: runs an interactive wizard to choose region + preset
#      and writes /etc/openhop_repeater/wm1303_ui.json with those defaults.
#   4. Detects existing installation:
#      - New install: runs install.sh
#      - Existing install: runs upgrade.sh (wizard is SKIPPED to preserve config)
# =============================================================================

set -e

REPO_URL="https://github.com/HansvanMeer/pyMC_WM1303.git"
BOOTSTRAP_RAW_URL="https://raw.githubusercontent.com/HansvanMeer/pyMC_WM1303/main/bootstrap.sh"
CONFIG_DIR="/etc/openhop_repeater"
UI_JSON="${CONFIG_DIR}/wm1303_ui.json"

# ---------------------------------------------------------------------------
# Detect target user
# Priority: SUDO_USER > common default users > first non-root user with UID>=1000
# ---------------------------------------------------------------------------
_detect_user() {
    if [ -n "${SUDO_USER:-}" ] && [ "$SUDO_USER" != "root" ] && id "$SUDO_USER" &>/dev/null; then
        echo "$SUDO_USER"; return
    fi
    for candidate in pi orangepi radxa rock dietpi; do
        if id "$candidate" &>/dev/null; then echo "$candidate"; return; fi
    done
    local found
    found=$(awk -F: '$3 >= 1000 && $3 < 65534 && $6 != "/" && $7 !~ /nologin|false/ {print $1; exit}' /etc/passwd)
    if [ -n "$found" ] && id "$found" &>/dev/null; then echo "$found"; return; fi
    echo ""
}

PI_USER=$(_detect_user)
if [ -z "$PI_USER" ]; then
    echo "  ✗ Could not detect a non-root user. Create one first or run install.sh with --user=<name>."
    exit 1
fi
# Resolve home directory safely (getent is more reliable than eval echo ~)
PI_HOME=$(getent passwd "${PI_USER}" 2>/dev/null | cut -d: -f6)
if [ -z "${PI_HOME}" ]; then
    PI_HOME=$(eval echo ~"${PI_USER}" 2>/dev/null)
fi
if [ -z "${PI_HOME}" ] || [ "${PI_HOME}" = "~${PI_USER}" ]; then
    echo "  ✗ Could not determine home directory for user '${PI_USER}'."
    exit 1
fi
if [ ! -d "${PI_HOME}" ]; then
    echo "  ✗ Home directory '${PI_HOME}' for user '${PI_USER}' does not exist."
    exit 1
fi
INSTALL_DIR="${PI_HOME}/pyMC_WM1303"

# --- Early parse of --non-interactive flag and WM1303_REGION env var ---------
# Must happen BEFORE self-reexec so we can skip reexec when not needed.
_EARLY_NON_INTERACTIVE=0
for arg in "$@"; do
    if [ "${arg}" = "--non-interactive" ] || [ "${arg}" = "-y" ]; then
        _EARLY_NON_INTERACTIVE=1
    fi
done
# If WM1303_REGION is set, the user already chose a region — no need for interactive wizard
if [ -n "${WM1303_REGION}" ]; then
    _EARLY_NON_INTERACTIVE=1
fi

# --- Self-reexec for interactive wizard when piped from curl -----------------
# When run as `curl ... | sudo bash`, stdin is the pipe (no TTY), so the
# interactive wizard cannot prompt the user. Fix: download ourselves to a temp
# file and re-exec with the real TTY as stdin.
# Skip reexec if: already reexeced, non-interactive requested, or region preset via env.
if [ ! -t 0 ] && [ -z "${_WM1303_REEXEC}" ] && [ "${_EARLY_NON_INTERACTIVE}" -eq 0 ]; then
    _SELF="/tmp/wm1303_bootstrap_$$.sh"
    # We're being piped — download a fresh copy for re-execution
    if command -v curl &>/dev/null; then
        curl -sSL "${BOOTSTRAP_RAW_URL}" -o "${_SELF}" 2>/dev/null
    elif command -v wget &>/dev/null; then
        wget -qO "${_SELF}" "${BOOTSTRAP_RAW_URL}" 2>/dev/null
    else
        _SELF=""
    fi
    # Only reexec if /dev/tty is truly readable (not just exists)
    if [ -n "${_SELF}" ] && [ -f "${_SELF}" ] && [ -r /dev/tty ] && bash -c 'echo ok </dev/tty' &>/dev/null; then
        chmod +x "${_SELF}"
        export _WM1303_REEXEC=1
        exec bash "${_SELF}" "$@" </dev/tty
    else
        # /dev/tty not usable — clean up and continue non-interactive
        rm -f "${_SELF}" 2>/dev/null
    fi
fi
# Clean up reexec marker
unset _WM1303_REEXEC 2>/dev/null || true

# --- Parse --non-interactive flag --------------------------------------------
NON_INTERACTIVE=0
for arg in "$@"; do
    if [ "${arg}" = "--non-interactive" ] || [ "${arg}" = "-y" ]; then
        NON_INTERACTIVE=1
    fi
done
# If WM1303_REGION is set via env, also non-interactive
if [ -n "${WM1303_REGION}" ]; then
    NON_INTERACTIVE=1
fi
# If stdin is STILL not a TTY after reexec attempt, fall back to non-interactive.
if [ ! -t 0 ]; then
    NON_INTERACTIVE=1
fi

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

# Install jq if not present (used by the wizard for JSON merging)
if ! command -v jq &>/dev/null; then
    echo "  ℹ Installing jq..."
    apt-get install -y -qq jq
    echo "  ✓ jq installed"
else
    echo "  ✓ jq already available"
fi

# Fix git 'dubious ownership' error (CVE-2022-24765)
git config --global --add safe.directory "${INSTALL_DIR}" 2>/dev/null
sudo -u ${PI_USER} git config --global --add safe.directory "${INSTALL_DIR}" 2>/dev/null

# Clone or update repository
if [ -d "${INSTALL_DIR}/.git" ]; then
    echo "  ℹ Repository already exists, pulling latest changes..."
    chown -R ${PI_USER}:${PI_USER} "${INSTALL_DIR}"
    cd "${INSTALL_DIR}"
    sudo -u ${PI_USER} git fetch origin
    sudo -u ${PI_USER} git reset --hard origin/main
    sudo -u ${PI_USER} git clean -fd
    echo "  ✓ Repository updated"
else
    echo "  ℹ Cloning repository..."
    sudo -u ${PI_USER} git clone "${REPO_URL}" "${INSTALL_DIR}"
    echo "  ✓ Repository cloned"
fi

cd "${INSTALL_DIR}"

# ---------------------------------------------------------------------------
# Detect existing installation up-front (we need this for wizard skip logic)
# ---------------------------------------------------------------------------
IS_UPGRADE=0
# Detect an existing install via either the new openhop-repeater unit or the
# legacy pymc-repeater unit (pre-openhop migration).
if [ -d "/opt/pymc_repeater" ] && { systemctl is-enabled openhop-repeater &>/dev/null || systemctl is-enabled pymc-repeater &>/dev/null; }; then
    IS_UPGRADE=1
fi

# ---------------------------------------------------------------------------
# Wizard: choose regulatory region + channel preset (new installs only)
# ---------------------------------------------------------------------------
run_wizard() {
    local presets_file="${INSTALL_DIR}/config/presets.json"
    if [ ! -f "${presets_file}" ]; then
        echo "  ⚠ presets.json not found at ${presets_file}, skipping wizard"
        return 0
    fi

    echo ""
    echo "  ╔══════════════════════════════════════════════════════════╗"
    echo "  ║  Installation Wizard: Region + Preset selection          ║"
    echo "  ╚══════════════════════════════════════════════════════════╝"
    echo ""

    # Track whether the user supplied ANY override env var. If none were set
    # and we're running non-interactively, we'll show a clear summary warning
    # at the end with the full override syntax (issue #7 Bug 1).
    local _all_defaulted=1
    [ -n "${WM1303_REGION}" ] && _all_defaulted=0
    [ -n "${WM1303_PRESET}" ] && _all_defaulted=0
    [ -n "${WM1303_SYNC_WORD}" ] && _all_defaulted=0

    # Build region list from presets.json (unique region codes)
    local regions
    regions=$(jq -r '.presets[].region' "${presets_file}" | sort -u)
    if [ -z "${regions}" ]; then
        echo "  ⚠ No regions found in presets.json, falling back to EU868/EU-Default"
        WM1303_REGION="${WM1303_REGION:-EU868}"
        WM1303_PRESET="${WM1303_PRESET:-EU-Default}"
        write_wizard_config
        return 0
    fi

    # --- Region selection -------------------------------------------------
    if [ -n "${WM1303_REGION}" ]; then
        echo "  ✓ Region from env: ${WM1303_REGION}"
    elif [ "${NON_INTERACTIVE}" -eq 1 ]; then
        WM1303_REGION="EU868"
        echo ""
        echo "  ╔══════════════════════════════════════════════════════════╗"
        echo "  ║  ⚠  NON-INTERACTIVE MODE                                ║"
        echo "  ║  Region defaulting to EU868.                             ║"
        echo "  ║  To set a different region, re-run with:                 ║"
        echo "  ║    WM1303_REGION=AU915 curl -sSL ... | sudo bash         ║"
        echo "  ║  Supported: EU868 US915 AU915 AS923 IN865 JP920 KR920    ║"
        echo "  ╚══════════════════════════════════════════════════════════╝"
        echo ""
    else
        echo "  Available regions:"
        local i=1
        local region_arr=()
        while IFS= read -r rg; do
            region_arr+=("${rg}")
            echo "    ${i}) ${rg}"
            i=$((i+1))
        done <<< "${regions}"
        echo ""
        read -rp "  Select region [1-${#region_arr[@]}, default 1=EU868]: " reg_choice
        reg_choice=${reg_choice:-1}
        if ! [[ "${reg_choice}" =~ ^[0-9]+$ ]] || [ "${reg_choice}" -lt 1 ] || [ "${reg_choice}" -gt "${#region_arr[@]}" ]; then
            echo "  ⚠ Invalid choice, defaulting to EU868"
            WM1303_REGION="EU868"
        else
            WM1303_REGION="${region_arr[$((reg_choice-1))]}"
        fi
    fi
    echo "  ► Selected region: ${WM1303_REGION}"

    # --- Preset auto-select (1 preset per region in presets v3) -----------
    # Presets v3 contain only region + rf_center_freq_mhz, no channel configs.
    # Preset name matches region code, so we auto-select.
    WM1303_PRESET="${WM1303_PRESET:-${WM1303_REGION}}"
    local preset_desc
    preset_desc=$(jq -r --arg n "${WM1303_PRESET}" \
        '.presets[] | select(.name==$n or .region==$n) | .description // ""' \
        "${presets_file}" | head -n1)
    if [ -n "${preset_desc}" ]; then
        echo "  ► Region preset: ${WM1303_PRESET} — ${preset_desc}"
    else
        echo "  ► Region preset: ${WM1303_PRESET}"
    fi

    # --- Device-wide LoRa network Sync Word ------------------------------
    # WM1303_SYNC_WORD env var accepts only 'private' or 'public'.
    # SX1302 hardware supports ONLY these two values via the board-level
    # lorawan_public flag (lgw_conf_board_t). Custom sync words are NOT
    # hardware-supported and any invalid value falls back to Private.
    WM1303_SYNC_WORD_MODE="private"
    WM1303_SYNC_WORD_VALUE=5156   # 0x1424 Private
    if [ -n "${WM1303_SYNC_WORD}" ]; then
        local _sw_in
        _sw_in=$(echo "${WM1303_SYNC_WORD}" | tr '[:upper:]' '[:lower:]')
        case "${_sw_in}" in
            private)
                WM1303_SYNC_WORD_MODE="private"; WM1303_SYNC_WORD_VALUE=5156 ;;
            public)
                WM1303_SYNC_WORD_MODE="public"; WM1303_SYNC_WORD_VALUE=13380 ;;
            *)
                echo "  ⚠ Invalid WM1303_SYNC_WORD='${WM1303_SYNC_WORD}' (only 'private' or 'public' supported), falling back to Private (0x1424)" ;;
        esac
        echo "  ✓ Sync word from env: ${WM1303_SYNC_WORD_MODE} (value=${WM1303_SYNC_WORD_VALUE})"
    elif [ "${NON_INTERACTIVE}" -eq 1 ]; then
        echo "  ℹ Non-interactive mode: defaulting sync word to Private (0x1424)"
    else
        echo ""
        echo "  LoRa Network Sync Word (device-wide):"
        echo "    1) Private (0x1424, default)  [most networks incl. MeshCore]"
        echo "    2) Public  (0x3444)           [LoRaWAN public networks]"
        echo ""
        read -rp "  Select sync word [1-2, default 1=Private]: " sw_choice
        sw_choice=${sw_choice:-1}
        case "${sw_choice}" in
            1)
                WM1303_SYNC_WORD_MODE="private"; WM1303_SYNC_WORD_VALUE=5156 ;;
            2)
                WM1303_SYNC_WORD_MODE="public"; WM1303_SYNC_WORD_VALUE=13380 ;;
            *)
                echo "  ⚠ Invalid choice, falling back to Private (0x1424)"
                WM1303_SYNC_WORD_MODE="private"; WM1303_SYNC_WORD_VALUE=5156 ;;
        esac
    fi
    printf "  ► Selected sync word: %s (0x%04X)\n" "${WM1303_SYNC_WORD_MODE}" "${WM1303_SYNC_WORD_VALUE}"

    # Issue #7 Bug 1 — Final summary warning when running fully unattended.
    # If the install is non-interactive AND the user did not supply any env
    # var, print a single prominent block showing what was defaulted and the
    # exact override command for next time. Keeps headless installs easy to
    # diagnose when the wrong region is silently selected.
    if [ "${NON_INTERACTIVE}" -eq 1 ] && [ "${_all_defaulted}" -eq 1 ]; then
        echo ""
        echo "  ╔══════════════════════════════════════════════════════════════╗"
        echo "  ║  ⚠  NON-INTERACTIVE DEFAULTS APPLIED                         ║"
        echo "  ║                                                              ║"
        printf "  ║    Region:    %-46s ║\n" "${WM1303_REGION}"
        printf "  ║    Preset:    %-46s ║\n" "${WM1303_PRESET}"
        printf "  ║    Sync word: %-46s ║\n" "${WM1303_SYNC_WORD_MODE} (0x$(printf %04X "${WM1303_SYNC_WORD_VALUE}"))"
        echo "  ║                                                              ║"
        echo "  ║  To install for a different region, re-run with:             ║"
        echo "  ║    WM1303_REGION=AU915 WM1303_SYNC_WORD=public \\           ║"
        echo "  ║      curl -sSL https://raw.githubusercontent.com/HansvanMeer/║"
        echo "  ║      pyMC_WM1303/main/bootstrap.sh | sudo -E bash            ║"
        echo "  ║                                                              ║"
        echo "  ║  Supported: EU868 US915 AU915 AS923 IN865 JP920 KR920 CUSTOM ║"
        echo "  ╚══════════════════════════════════════════════════════════════╝"
        echo ""
    fi

    write_wizard_config
}

# Write wizard output to /etc/openhop_repeater/wm1303_ui.json.
# Presets v3 contain only region + rf_center_freq_mhz (no channel configs).
# All channels start disabled — the user configures them via the UI after
# installation. The template wm1303_ui.json provides the skeleton with
# disabled channel defaults.
write_wizard_config() {
    local presets_file="${INSTALL_DIR}/config/presets.json"
    local template="${INSTALL_DIR}/config/wm1303_ui.json"
    if [ ! -f "${template}" ]; then
        echo "  ⚠ Template wm1303_ui.json missing, cannot write wizard config"
        return 0
    fi
    mkdir -p "${CONFIG_DIR}"

    # Start from template (all channels disabled)
    cp "${template}" "${UI_JSON}"

    # Extract rf_center_freq_mhz from the chosen preset (if available)
    local rf_center="null"
    if [ -f "${presets_file}" ]; then
        local _rc
        _rc=$(jq -r --arg n "${WM1303_PRESET:-${WM1303_REGION}}" \
            '.presets[] | select(.name==$n or .region==$n) | .rf_center_freq_mhz // empty' \
            "${presets_file}" | head -n1)
        if [ -n "${_rc}" ] && [ "${_rc}" != "null" ]; then
            rf_center="${_rc}"
        fi
    fi

    # Merge region, sync_word, and rf_center_freq_mhz into the config
    jq --arg code "${WM1303_REGION}" \
       --arg sw_mode "${WM1303_SYNC_WORD_MODE}" \
       --argjson sw_value "${WM1303_SYNC_WORD_VALUE}" \
       --argjson rf_center "${rf_center}" \
       '.region = {"code": $code, "tx_freq_min": null, "tx_freq_max": null}
        | .sync_word = {"value": $sw_value, "mode": $sw_mode}
        | if $rf_center != null then .rf_center_freq_mhz = $rf_center else . end' \
       "${UI_JSON}" > "${UI_JSON}.tmp" && mv "${UI_JSON}.tmp" "${UI_JSON}"

    chown ${PI_USER}:${PI_USER} "${UI_JSON}"
    if [ "${rf_center}" != "null" ]; then
        printf "  ✓ Wrote %s — region=%s, rf_center=%.3f MHz, sync_word=%s (0x%04X)\n" \
            "${UI_JSON}" "${WM1303_REGION}" "${rf_center}" \
            "${WM1303_SYNC_WORD_MODE}" "${WM1303_SYNC_WORD_VALUE}"
    else
        printf "  ✓ Wrote %s — region=%s, sync_word=%s (0x%04X)\n" \
            "${UI_JSON}" "${WM1303_REGION}" \
            "${WM1303_SYNC_WORD_MODE}" "${WM1303_SYNC_WORD_VALUE}"
    fi
    echo "  ℹ All channels start disabled. Configure channels via the WM1303 Manager UI."
}

if [ "${IS_UPGRADE}" -eq 0 ]; then
    if [ -f "${UI_JSON}" ]; then
        echo "  ℹ Existing ${UI_JSON} found — skipping wizard to preserve config"
    else
        run_wizard
    fi
else
    echo "  ℹ Upgrade detected — wizard skipped, existing config preserved"
fi

# ---------------------------------------------------------------------------
# OpenHop var-tree physical migration (defense-in-depth)
# ---------------------------------------------------------------------------
# Physically move legacy /var/log/pymc_repeater -> /var/log/openhop_repeater
# and /var/lib/pymc_repeater -> /var/lib/openhop_repeater. install.sh and
# upgrade.sh also perform this same migration, but running it here first
# guarantees the move happens even when a stale local install/upgrade script
# is somehow invoked. Uses rsync --remove-source-files when the destination
# is empty (physical move, no disk duplication); otherwise a safe cp -an
# merge without overwrite, leaving the legacy dir intact for rollback.
# Skipped silently when rsync is not installed yet (install.sh will run it).
_bootstrap_migrate_legacy_vardir() {
    local legacy="$1" new="$2" label="$3"
    [ -d "${legacy}" ] || return 0
    [ "${legacy}" = "${new}" ] && return 0
    mkdir -p "${new}"
    if [ -z "$(ls -A "${new}" 2>/dev/null || true)" ]; then
        if rsync -a --remove-source-files "${legacy}/" "${new}/" 2>/dev/null; then
            find "${legacy}" -depth -type d -empty -delete 2>/dev/null || true
            echo "  ✓ Migrated legacy ${label} ${legacy} -> ${new} (physical move)"
        else
            echo "  ⚠ Legacy ${label} ${legacy} rsync move failed; left in place"
        fi
    else
        cp -an "${legacy}/." "${new}/" 2>/dev/null || true
        echo "  ✓ Merged legacy ${label} ${legacy} -> ${new} (safe copy; legacy preserved)"
    fi
}
if command -v rsync >/dev/null 2>&1; then
    _bootstrap_migrate_legacy_vardir "/var/log/pymc_repeater" "/var/log/openhop_repeater" "log dir"
    _bootstrap_migrate_legacy_vardir "/var/lib/pymc_repeater" "/var/lib/openhop_repeater" "data dir"
else
    echo "  ℹ rsync not installed yet; deferring legacy /var tree migration to install/upgrade script"
fi

# ---------------------------------------------------------------------------
# SSH timeout protection: run install/upgrade with nohup so the process
# survives if the SSH session disconnects (HAL build can take >10 minutes).
# Output is logged and tailed so the user still sees live progress.
# ---------------------------------------------------------------------------
BOOTSTRAP_LOG="/tmp/wm1303_bootstrap.log"
rm -f "${BOOTSTRAP_LOG}"

run_protected() {
    local script="$1"
    echo "  ℹ Running ${script} (nohup-protected against SSH timeout)..."
    echo "  ℹ Log: ${BOOTSTRAP_LOG}"
    nohup bash "${script}" > "${BOOTSTRAP_LOG}" 2>&1 &
    BGPID=$!
    tail -f "${BOOTSTRAP_LOG}" &
    TAILPID=$!
    wait $BGPID
    EXIT_CODE=$?
    kill $TAILPID 2>/dev/null
    wait $TAILPID 2>/dev/null
    return $EXIT_CODE
}

if [ "${IS_UPGRADE}" -eq 1 ]; then
    echo ""
    echo "  ╔══════════════════════════════════════════════════════════╗"
    echo "  ║  Existing installation detected — running UPGRADE        ║"
    echo "  ╚══════════════════════════════════════════════════════════╝"
    echo ""
    run_protected upgrade.sh
else
    echo ""
    echo "  ╔══════════════════════════════════════════════════════════╗"
    echo "  ║  No installation found — running INSTALL                 ║"
    echo "  ╚══════════════════════════════════════════════════════════╝"
    echo ""
    run_protected install.sh
fi
