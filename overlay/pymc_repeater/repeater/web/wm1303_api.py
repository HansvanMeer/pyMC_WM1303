from collections import deque

def _load_global_conf() -> dict:
    import re, json
    from pathlib import Path
    _ACTIVE = Path("/tmp/pymc_wm1303_bridge_conf.json")
    src = _ACTIVE if _ACTIVE.exists() else _GLOBAL_CONF
    if not src.exists(): return {}
    text = src.read_text()
    text = re.sub(r'/[*].*?[*]/', '', text, flags=re.DOTALL)
    text = re.sub(r'//[^\n]*', '', text)
    try: return json.loads(text)
    except: return {}

def _load_bridge_conf() -> dict:
    """Load the RUNNING bridge config (what lora_pkt_fwd actually uses)."""
    import re as _re, json as _json
    from pathlib import Path
    candidates = [
        Path("/tmp/pymc_wm1303_bridge_conf.json"),
        _PKTFWD_DIR / "bridge_conf.json",
        _PKTFWD_DIR / "global_conf.json",
    ]
    for src in candidates:
        if src.exists():
            text = src.read_text()
            text = _re.sub(r'/[*].*?[*]/', '', text, flags=_re.DOTALL)
            text = _re.sub(r'//[^\n]*', '', text)
            try:
                return _json.loads(text)
            except Exception:
                continue
    return {}


"""
WM1303 API - CherryPy REST endpoints for WM1303 management + Spectrum Analyzer
"""
import cherrypy
try:
    from .spectrum_collector import get_collector
    _COLLECTOR_AVAILABLE = True
except ImportError:
    _COLLECTOR_AVAILABLE = False
import json
import os
import subprocess
import logging
import time
from pathlib import Path
from contextlib import contextmanager as _contextmanager
import threading

from repeater.web.tiered_query import (
    tiered_packet_activity_query, tiered_noise_floor_query,
    tiered_channel_stats_query, tiered_dedup_query,
    tiered_cad_events_query, tiered_crc_error_rate_query,
    tiered_packet_metrics_query, auto_bucket_seconds,
)

_STATUS_CACHE={}
_STATUS_CACHE_TTL=8


logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Detect PKTFWD_DIR dynamically (supports non-pi users and alternate distros)
# ---------------------------------------------------------------------------
def _detect_pktfwd_dir() -> Path:
    """Resolve the packet forwarder directory at import time."""
    # 1. From config.yaml
    try:
        import yaml as _yaml
        with open('/etc/pymc_repeater/config.yaml') as _f:
            _cfg = _yaml.safe_load(_f) or {}
        _pdir = _cfg.get('wm1303', {}).get('pktfwd_dir', '')
        if _pdir and Path(_pdir).is_dir():
            return Path(_pdir)
    except Exception:
        pass
    # 2. From systemd service User=
    try:
        import subprocess as _sp
        _svc_user = _sp.check_output(
            ['systemctl', 'show', 'pymc-repeater', '-p', 'User', '--value'],
            text=True, timeout=3
        ).strip()
        if _svc_user and _svc_user != 'root':
            import pwd as _pwd
            _home = Path(_pwd.getpwnam(_svc_user).pw_dir)
            _candidate = _home / 'wm1303_pf'
            if _candidate.is_dir():
                return _candidate
    except Exception:
        pass
    # 3. Scan common user home directories
    try:
        import pwd as _pwd
        for _pw in _pwd.getpwall():
            if _pw.pw_uid >= 1000 and _pw.pw_uid < 65534 and _pw.pw_dir != '/':
                _candidate = Path(_pw.pw_dir) / 'wm1303_pf'
                if _candidate.is_dir():
                    return _candidate
    except Exception:
        pass
    # 4. Fallback
    return Path('/home/pi/wm1303_pf')

_PKTFWD_DIR  = _detect_pktfwd_dir()

# Paths
_SVC_NAME    = "pymc-repeater"
_UI_JSON     = Path("/etc/pymc_repeater/wm1303_ui.json")
_GLOBAL_CONF = _PKTFWD_DIR / "global_conf.json"
_SPECTRAL_BIN = _PKTFWD_DIR / "spectral_scan"
_SPECTRAL_RES = Path("/tmp/pymc_spectral_results.json")

# Spectrum-scan region presets (Issue #7.1).
# Maps `region.code` (from wm1303_ui.json) to a (start_hz, stop_hz, step_hz)
# tuple used by the SX1261 spectrum scan and the simulated-fallback range.
# Selecting CUSTOM uses `region.tx_freq_min` / `region.tx_freq_max` from
# wm1303_ui.json directly. Unknown / empty region.code falls back to EU868.
#
# These ranges are display/scan-only and do NOT change regulatory behaviour;
# real regulatory enforcement still lives in per-channel duty-cycle and
# LBT/CAD logic.
SPECTRUM_REGION_PRESETS = {
    "EU868": (863_000_000, 870_000_000, 200_000),
    "US915": (902_000_000, 928_000_000, 500_000),
    "AU915": (915_000_000, 928_000_000, 500_000),
    "AS923": (920_000_000, 928_000_000, 200_000),
    "IN865": (865_000_000, 867_000_000, 200_000),
    "JP920": (920_000_000, 928_000_000, 200_000),
    "KR920": (920_000_000, 923_000_000, 200_000),
}
SPECTRUM_DEFAULT_REGION = "EU868"
SPECTRUM_DEFAULT_STEP_HZ = 200_000  # fallback step for CUSTOM when not specified

# v2.5.7: Neighbour RSSI/SNR sample persistence.
# Samples are written to the neighbour_samples SQLite table on every
# _neighbours_get() call so signal-quality charts survive service restarts.
# The in-memory ring buffer is kept as a fast read-path cache; it is
# re-populated from the DB on first access after a restart.
_NEIGHBOURS_HISTORY_MAX = 200          # samples retained per node (in-memory + DB)
_NEIGHBOURS_HISTORY = {}                # node_id -> list[(ts, rssi, snr)]
_NEIGHBOURS_HISTORY_TIMEOUT_S = 86400  # drop nodes whose last sample is older than 24h


def _get_spectrum_scan_range():
    """Resolve the spectrum-scan frequency range from wm1303_ui.json.

    Reads top-level ``region`` from wm1303_ui.json and looks ``region.code``
    up in ``SPECTRUM_REGION_PRESETS``. Falls back to ``SPECTRUM_DEFAULT_REGION``
    (EU868) when the code is empty / unknown. For ``CUSTOM`` region, uses
    ``region.tx_freq_min`` and ``region.tx_freq_max`` from the same block;
    step defaults to ``SPECTRUM_DEFAULT_STEP_HZ`` (200 kHz).

    Returns:
        Tuple ``(start_hz, stop_hz, step_hz, region_code_resolved)`` where
        ``region_code_resolved`` is the code that actually applied (after any
        fallback) so the UI can show it in the spectrum-tab header.
    """
    try:
        ui_data = json.loads(_UI_JSON.read_text()) if _UI_JSON.exists() else {}
    except (OSError, ValueError) as _e:
        logger.debug("_get_spectrum_scan_range: read failed: %s", _e)
        ui_data = {}
    region = ui_data.get("region") or {}
    code = (region.get("code") or "").strip().upper() or SPECTRUM_DEFAULT_REGION
    if code == "CUSTOM":
        fmin = region.get("tx_freq_min")
        fmax = region.get("tx_freq_max")
        if (isinstance(fmin, (int, float)) and isinstance(fmax, (int, float))
                and fmax > fmin):
            return (int(fmin), int(fmax), SPECTRUM_DEFAULT_STEP_HZ, "CUSTOM")
        logger.debug("_get_spectrum_scan_range: CUSTOM without valid "
                     "tx_freq_min/max -> fallback to %s", SPECTRUM_DEFAULT_REGION)
        code = SPECTRUM_DEFAULT_REGION
    preset = SPECTRUM_REGION_PRESETS.get(code)
    if preset is None:
        logger.debug("_get_spectrum_scan_range: unknown region %r -> fallback to %s",
                     code, SPECTRUM_DEFAULT_REGION)
        code = SPECTRUM_DEFAULT_REGION
        preset = SPECTRUM_REGION_PRESETS[code]
    return (preset[0], preset[1], preset[2], code)


def _safe_write(path: Path, content: str) -> bool:
    """Write content to path with automatic permission recovery."""
    try:
        path.write_text(content)
        return True
    except PermissionError:
        try:
            subprocess.run(['sudo', 'chown', 'pi:pi', str(path)], timeout=5, check=False)
            path.write_text(content)
            return True
        except OSError as e:
            logger.warning('_safe_write: permission recovery failed for %s: %s', path, e)
            return False
    except OSError as e:
        logger.warning('_safe_write: failed to write %s: %s', path, e)
        return False

def _j(obj):
    cherrypy.response.headers["Content-Type"] = "application/json"
    return json.dumps(_sanitize_json(obj)).encode()


def _get_backend():
    """Get WM1303Backend instance via module-level reference."""
    try:
        from pymc_core.hardware.wm1303_backend import _active_backend
        return _active_backend
    except Exception:
        return None

def _body():
    try:
        raw = cherrypy.request.body.read()
        return json.loads(raw) if raw else {}
    except Exception:
        return {}

def _migrate_ui_config(data: dict) -> tuple[dict, bool]:
    """Migrate legacy wm1303_ui.json schemas to the current format.

    Returns (migrated_data, changed_flag). When changed_flag is True the caller
    should persist the data so subsequent reads use the canonical schema.

    Currently handles:
    - region: legacy plain string (e.g. "EU868") -> nested dict
      {"code": "EU868", "tx_freq_min": null, "tx_freq_max": null}
      (Credit: @fahimshariff-au, issue #7 — v2.4.10 startup error on upgrade)
    - region: missing key -> empty nested dict so the UI region selector loads
      cleanly instead of crashing.
    """
    changed = False
    if not isinstance(data, dict):
        return data, changed
    region_val = data.get("region")
    if isinstance(region_val, str):
        # Legacy: region was a plain string code
        logger.warning("_migrate_ui_config: migrating legacy region string %r to nested dict", region_val)
        data["region"] = {
            "code": region_val,
            "tx_freq_min": None,
            "tx_freq_max": None,
        }
        changed = True
    elif region_val is None and "region" not in data:
        # Missing: initialize empty so downstream code never KeyErrors
        data["region"] = {"code": "", "tx_freq_min": None, "tx_freq_max": None}
        changed = True
    return data, changed


def _load_ui() -> dict:
    if _UI_JSON.exists():
        try:
            data = json.loads(_UI_JSON.read_text())
            data, changed = _migrate_ui_config(data)
            if changed:
                try:
                    _safe_write(_UI_JSON, json.dumps(data, indent=2))
                    logger.info("_load_ui: persisted migrated wm1303_ui.json schema")
                except Exception as ex:
                    logger.warning("_load_ui: failed to persist migration: %s", ex)
            return data
        except Exception:
            pass
    return {"channels": [], "bridge": {"rules": []}, "region": {"code": "", "tx_freq_min": None, "tx_freq_max": None}}

def _save_ui(data: dict):
    _safe_write(_UI_JSON, json.dumps(data, indent=2))



def _get_ui_channel_id_map():
    """Map each UI channel index to its backend channel key.
    Active UI channels are mapped by position to channel_a..channel_d,
    matching the backend's _CHANNEL_ID_BY_INDEX in get_radios().
    Inactive channels get a non-colliding 'inactive_N' key."""
    _CHANNEL_ID_BY_INDEX = ['channel_a', 'channel_b', 'channel_c', 'channel_d']
    ui_chs = _load_ui().get('channels', [])
    id_map = {}
    active_pos = 0
    for ui_idx, ch in enumerate(ui_chs):
        if ch.get('active', False):
            if active_pos < len(_CHANNEL_ID_BY_INDEX):
                key = _CHANNEL_ID_BY_INDEX[active_pos]
            else:
                key = 'channel_' + chr(97 + active_pos)
            id_map[ui_idx] = key
            active_pos += 1
        else:
            id_map[ui_idx] = 'inactive_' + str(ui_idx)
    return id_map


def _sync_config_yaml_channels(channels: list) -> None:
    """Sync active channels from SSOT to config.yaml wm1303.channels section.

    Maps active channels to channel_a..channel_d by index (same as backend)
    and writes them to config.yaml so it stays in sync with wm1303_ui.json.
    """
    import yaml
    _CHANNEL_ID_BY_INDEX = ['channel_a', 'channel_b', 'channel_c', 'channel_d']
    cfg_path = Path('/etc/pymc_repeater/config.yaml')
    try:
        with open(cfg_path) as f:
            cfg = yaml.safe_load(f) or {}
        if 'wm1303' not in cfg:
            cfg['wm1303'] = {}
        new_channels = {}
        active_idx = 0
        for ch in channels:
            if not ch.get('active', False):
                continue
            if active_idx >= len(_CHANNEL_ID_BY_INDEX):
                break
            ch_id = _CHANNEL_ID_BY_INDEX[active_idx]
            new_channels[ch_id] = {
                'frequency': int(ch.get('frequency', 0)),
                'spreading_factor': int(ch.get('spreading_factor', 7)),
                'bandwidth': int(ch.get('bandwidth', 125000)),
                'coding_rate': ch.get('coding_rate', '4/5'),
                'preamble_length': int(ch.get('preamble_length', 17)),
                'tx_power': int(ch.get('tx_power', 14)),
                'tx_enable': ch.get('tx_enabled', True),
                'description': 'MeshCore {} (SF{})'.format(
                    ch.get('name', ch.get('friendly_name', 'Channel ' + chr(65 + active_idx))),
                    ch.get('spreading_factor', 7)),
            }
            active_idx += 1
        cfg['wm1303']['channels'] = new_channels
        with open(cfg_path, 'w') as f:
            yaml.safe_dump(cfg, f, default_flow_style=False, sort_keys=False)
    except Exception as e:
        logger.warning('_sync_config_yaml_channels: failed to sync config.yaml: %s', e)

def sync_global_conf():
    """Regenerate bridge_conf.json from wm1303_ui.json using the backend's
    _generate_bridge_conf() (single code path, SSOT).

    bridge_conf.json is AUTHORITATIVE. global_conf.json is a copy.
    """
    from pymc_core.hardware.wm1303_backend import _generate_bridge_conf

    ui = _load_ui()
    channels = ui.get('channels', [])
    if not channels:
        logger.warning('sync_global_conf: no channels in UI config, skipping')
        return {'status': 'skipped', 'reason': 'no channels'}

    # Build a minimal channels dict for the fallback path in _generate_bridge_conf.
    # The function reads wm1303_ui.json directly for fixed IF mapping,
    # but needs a non-empty dict to avoid the "No channels" error.
    ch_dict = {}
    for ch in channels:
        if ch.get('active', False):
            ch_dict[ch.get('name', f'ch_{len(ch_dict)}')] = ch
    # If no active channels, pass all channels so center freq is still computed
    if not ch_dict:
        for ch in channels:
            ch_dict[ch.get('name', f'ch_{len(ch_dict)}')] = ch

    try:
        conf = _generate_bridge_conf(ch_dict)
    except Exception as ex:
        logger.error('sync_global_conf: _generate_bridge_conf failed: %s', ex)
        return {'status': 'error', 'reason': str(ex)}

    # Write bridge_conf.json (authoritative)
    _BRIDGE_CONF_PATH = _PKTFWD_DIR / 'bridge_conf.json'
    try:
        _BRIDGE_CONF_PATH.write_text(json.dumps(conf, indent=2))
        logger.info('sync_global_conf: wrote bridge_conf.json')
    except Exception as ex:
        logger.warning('sync_global_conf: could not write bridge_conf.json: %s', ex)

    # Copy to global_conf.json
    try:
        _GLOBAL_CONF.write_text(json.dumps(conf, indent=2))
        logger.info('sync_global_conf: wrote global_conf.json (copy of bridge_conf.json)')
    except Exception as ex:
        logger.warning('sync_global_conf: could not write global_conf.json: %s', ex)

    # Write active runtime copy
    active_conf = Path('/tmp/pymc_wm1303_bridge_conf.json')
    try:
        active_conf.write_text(json.dumps(conf, indent=2))
    except Exception:
        pass

    # Restart lora_pkt_fwd
    try:
        subprocess.Popen(
            ['sudo', 'systemctl', 'restart', 'lora_pkt_fwd'],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True)
        logger.info('sync_global_conf: restarted lora_pkt_fwd')
    except Exception as ex:
        logger.warning('sync_global_conf: could not restart lora_pkt_fwd: %s', ex)

    # Extract center freq for response
    center_hz = conf.get('SX130x_conf', {}).get('radio_0', {}).get('freq', 0)
    center_mhz = round(center_hz / 1e6, 4) if center_hz else 0

    return {'status': 'ok', 'center_mhz': center_mhz}



def _load_global_conf_ORIG() -> dict:
    """Load global_conf.json stripping C/C++ style comments."""
    import re
    if not _GLOBAL_CONF.exists():
        return {}
    text = _GLOBAL_CONF.read_text()
    # Strip block comments /* ... */
    text = re.sub(r'/\*.*?\*/', '', text, flags=re.DOTALL)
    # Strip line comments // ...
    text = re.sub(r'//[^\n]*', '', text)
    try:
        return json.loads(text)
    except Exception:
        return {}

def _build_if_channels(conf: dict) -> list:
    """Extract IF chain layout from SX130x_conf."""
    sx = conf.get("SX130x_conf", {})
    rf = {
        0: sx.get("radio_0", {}).get("freq", 867500000),
        1: sx.get("radio_1", {}).get("freq", 868500000),
    }
    channels = []
    for i in range(8):
        ch = sx.get("chan_multiSF_{}".format(i), {})
        if not ch.get("enable", False):
            continue
        radio = ch.get("radio", 0)
        offset = ch.get("if", 0)
        freq = rf.get(radio, 0) + offset
        channels.append({
            "if_chain": i, "type": "multi_sf",
            "frequency_hz": freq,
            "frequency_mhz": round(freq / 1e6, 4),
            "radio": radio, "offset_hz": offset, "bandwidth_khz": 125,
        })
    std = sx.get("chan_Lora_std", {})
    if std.get("enable", False):
        radio = std.get("radio", 0)
        offset = std.get("if", 0)
        freq = rf.get(radio, 0) + offset
        channels.append({"if_chain": 8, "type": "lora_std",
            "frequency_hz": freq, "frequency_mhz": round(freq / 1e6, 4),
            "radio": radio, "offset_hz": offset,
            "bandwidth_khz": std.get("bandwidth", 250000) // 1000})
    fsk = sx.get("chan_FSK", {})
    if fsk.get("enable", False):
        radio = fsk.get("radio", 0)
        offset = fsk.get("if", 0)
        freq = rf.get(radio, 0) + offset
        channels.append({"if_chain": 9, "type": "fsk",
            "frequency_hz": freq, "frequency_mhz": round(freq / 1e6, 4),
            "radio": radio, "offset_hz": offset, "bandwidth_khz": 125})
    return channels

def _toggle_spectral_scan(enable: bool) -> bool:
    """Toggle spectral_scan enable in global_conf.json."""
    import re
    if not _GLOBAL_CONF.exists():
        return False
    text = _GLOBAL_CONF.read_text()
    lines = text.splitlines()
    result = []
    in_spec = False
    done = False
    for line in lines:
        if not done and 'spectral_scan' in line and '{' in line:
            in_spec = True
        if in_spec and not done and '"enable"' in line:
            new_val = 'true' if enable else 'false'
            line = re.sub(r'(:\s*)(true|false)', r'\g<1>' + new_val, line, count=1)
            done = True
            in_spec = False
        result.append(line)
    _safe_write(_GLOBAL_CONF, '\n'.join(result))
    return True



def _sanitize_json(obj):
    """Recursively convert non-JSON-serializable types."""
    if isinstance(obj, bytes):
        return obj.hex()
    if isinstance(obj, set):
        return list(obj)
    if isinstance(obj, dict):
        return {str(k): _sanitize_json(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_sanitize_json(v) for v in obj]
    return obj


def _regenerate_gpio_scripts(gpio: dict):
    """Regenerate reset_lgw.sh and power_cycle_lgw.sh with updated GPIO pin assignments."""
    import os, stat
    base = gpio.get('gpio_base_offset', 512)
    sx1302_rst_bcm = gpio.get('sx1302_reset', 17)
    sx1302_pwr_bcm = gpio.get('sx1302_power_en', 18)
    sx1261_rst_bcm = gpio.get('sx1261_reset', 5)
    ad5338r_rst_bcm = gpio.get('ad5338r_reset', 13)
    sx1302_rst = sx1302_rst_bcm + base
    sx1302_pwr = sx1302_pwr_bcm + base
    sx1261_rst = sx1261_rst_bcm + base
    ad5338r_rst = ad5338r_rst_bcm + base

    script_dir = str(_PKTFWD_DIR)
    os.makedirs(script_dir, exist_ok=True)

    # --- reset_lgw.sh ---
    reset_script = f'''#!/bin/sh
# Auto-generated by WM1303 Manager - DO NOT EDIT MANUALLY
# GPIO base={base}, BCM pins: reset={sx1302_rst_bcm}, power={sx1302_pwr_bcm}, sx1261={sx1261_rst_bcm}, adc={ad5338r_rst_bcm}

SX1302_RESET_PIN={sx1302_rst}
SX1302_POWER_EN_PIN={sx1302_pwr}
SX1261_RESET_PIN={sx1261_rst}
AD5338R_RESET_PIN={ad5338r_rst}

WAIT_GPIO() {{
    sleep 0.1
}}

init() {{
    echo "$SX1302_RESET_PIN" > /sys/class/gpio/export 2>/dev/null || true; WAIT_GPIO
    echo "$SX1261_RESET_PIN" > /sys/class/gpio/export 2>/dev/null || true; WAIT_GPIO
    echo "$SX1302_POWER_EN_PIN" > /sys/class/gpio/export 2>/dev/null || true; WAIT_GPIO
    echo "$AD5338R_RESET_PIN" > /sys/class/gpio/export 2>/dev/null || true; WAIT_GPIO

    echo "out" > /sys/class/gpio/gpio${{SX1302_RESET_PIN}}/direction; WAIT_GPIO
    echo "out" > /sys/class/gpio/gpio${{SX1261_RESET_PIN}}/direction; WAIT_GPIO
    echo "out" > /sys/class/gpio/gpio${{SX1302_POWER_EN_PIN}}/direction; WAIT_GPIO
    echo "out" > /sys/class/gpio/gpio${{AD5338R_RESET_PIN}}/direction; WAIT_GPIO
}}

reset() {{
    echo "CoreCell power enable through GPIO${{SX1302_POWER_EN_PIN}} (BCM{sx1302_pwr_bcm})..."
    echo "1" > /sys/class/gpio/gpio${{SX1302_POWER_EN_PIN}}/value; WAIT_GPIO

    echo "CoreCell reset through GPIO${{SX1302_RESET_PIN}} (BCM{sx1302_rst_bcm})..."
    echo "1" > /sys/class/gpio/gpio${{SX1302_RESET_PIN}}/value; WAIT_GPIO
    echo "0" > /sys/class/gpio/gpio${{SX1302_RESET_PIN}}/value; WAIT_GPIO

    echo "SX1261 reset through GPIO${{SX1261_RESET_PIN}} (BCM{sx1261_rst_bcm})..."
    echo "0" > /sys/class/gpio/gpio${{SX1261_RESET_PIN}}/value; WAIT_GPIO
    echo "1" > /sys/class/gpio/gpio${{SX1261_RESET_PIN}}/value; WAIT_GPIO

    echo "AD5338R reset through GPIO${{AD5338R_RESET_PIN}} (BCM{ad5338r_rst_bcm})..."
    echo "0" > /sys/class/gpio/gpio${{AD5338R_RESET_PIN}}/value; WAIT_GPIO
    echo "1" > /sys/class/gpio/gpio${{AD5338R_RESET_PIN}}/value; WAIT_GPIO
}}

term() {{
    for pin in $SX1302_RESET_PIN $SX1261_RESET_PIN $SX1302_POWER_EN_PIN $AD5338R_RESET_PIN; do
        if [ -d /sys/class/gpio/gpio${{pin}} ]; then
            echo "${{pin}}" > /sys/class/gpio/unexport 2>/dev/null || true; WAIT_GPIO
        fi
    done
}}

case "$1" in
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
        echo "Usage: $0 {{start|stop}}"
        exit 1
        ;;
esac

exit 0
'''

    # --- power_cycle_lgw.sh ---
    power_script = f'''#!/bin/sh
# Auto-generated by WM1303 Manager - DO NOT EDIT MANUALLY
# Full power cycle script for WM1303 CoreCell
# GPIO base={base}, BCM pins: reset={sx1302_rst_bcm}, power={sx1302_pwr_bcm}, sx1261={sx1261_rst_bcm}, adc={ad5338r_rst_bcm}

SX1302_RESET_PIN={sx1302_rst}
SX1302_POWER_EN_PIN={sx1302_pwr}
SX1261_RESET_PIN={sx1261_rst}
AD5338R_RESET_PIN={ad5338r_rst}

# Export GPIOs
for pin in $SX1302_RESET_PIN $SX1261_RESET_PIN $SX1302_POWER_EN_PIN $AD5338R_RESET_PIN; do
    echo "$pin" > /sys/class/gpio/export 2>/dev/null || true
    sleep 0.1
    echo "out" > /sys/class/gpio/gpio${{pin}}/direction
    sleep 0.1
done

# FULL POWER CYCLE
echo "Power OFF CoreCell..."
echo "0" > /sys/class/gpio/gpio${{SX1302_POWER_EN_PIN}}/value
sleep 3  # Wait for caps to FULLY discharge (SX1250 analog reset)

echo "Power ON CoreCell..."
echo "1" > /sys/class/gpio/gpio${{SX1302_POWER_EN_PIN}}/value
sleep 0.5  # Wait for power stabilization

# Logic resets
echo "CoreCell reset (GPIO${{SX1302_RESET_PIN}}, BCM{sx1302_rst_bcm})..."
echo "1" > /sys/class/gpio/gpio${{SX1302_RESET_PIN}}/value; sleep 0.1
echo "0" > /sys/class/gpio/gpio${{SX1302_RESET_PIN}}/value; sleep 0.1

echo "SX1261 reset (GPIO${{SX1261_RESET_PIN}}, BCM{sx1261_rst_bcm})..."
echo "0" > /sys/class/gpio/gpio${{SX1261_RESET_PIN}}/value; sleep 0.1
echo "1" > /sys/class/gpio/gpio${{SX1261_RESET_PIN}}/value; sleep 0.1

echo "AD5338R reset (GPIO${{AD5338R_RESET_PIN}}, BCM{ad5338r_rst_bcm})..."
echo "0" > /sys/class/gpio/gpio${{AD5338R_RESET_PIN}}/value; sleep 0.1
echo "1" > /sys/class/gpio/gpio${{AD5338R_RESET_PIN}}/value; sleep 0.1

sleep 1  # Final stabilization
echo "Power cycle complete"
'''

    reset_path = os.path.join(script_dir, 'reset_lgw.sh')
    power_path = os.path.join(script_dir, 'power_cycle_lgw.sh')

    with open(reset_path, 'w') as f:
        f.write(reset_script)
    os.chmod(reset_path, stat.S_IRWXU | stat.S_IRGRP | stat.S_IXGRP | stat.S_IROTH | stat.S_IXOTH)

    with open(power_path, 'w') as f:
        f.write(power_script)
    os.chmod(power_path, stat.S_IRWXU | stat.S_IRGRP | stat.S_IXGRP | stat.S_IROTH | stat.S_IXOTH)

    logger.info("GPIO scripts regenerated: %s, %s", reset_path, power_path)





class _SharedConn:
    """Module-level shared SQLite connection with thread-safe access."""

    def __init__(self, path):
        self._path = str(path)
        self._conn = None
        self._lock = threading.RLock()

    def _ensure_conn(self):
        if self._conn is None:
            import sqlite3 as _sq3
            self._conn = _sq3.connect(
                self._path, timeout=10, check_same_thread=False,
            )
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.execute("PRAGMA busy_timeout=5000")
            self._conn.execute("PRAGMA cache_size=-512")
            self._conn.execute("PRAGMA mmap_size=0")
            self._conn.execute("PRAGMA temp_store=MEMORY")
        return self._conn

    def __enter__(self):
        self._lock.acquire()
        return self._ensure_conn()

    def __exit__(self, exc_type, exc_val, exc_tb):
        try:
            if self._conn:
                if exc_type is None:
                    self._conn.commit()
                else:
                    self._conn.rollback()
        finally:
            self._lock.release()
        return False


# Module-level shared connection registry (Python 3.13 compatible)
_shared_conn_instances = {}  # path -> _SharedConn
_shared_conn_lock = threading.Lock()


def _get_shared_conn(path):
    """Get or create a shared connection for the given DB path."""
    key = str(path)
    if key not in _shared_conn_instances:
        with _shared_conn_lock:
            if key not in _shared_conn_instances:
                _shared_conn_instances[key] = _SharedConn(path)
    return _shared_conn_instances[key]


@_contextmanager
def _db_conn(path, timeout=5):
    """Thread-safe access to a shared persistent SQLite connection."""
    shared = _get_shared_conn(path)
    with shared as conn:
        yield conn



class WM1303API:
    exposed = True

    def __init__(self, daemon=None):
        self.daemon = daemon
        # Initialize debug bundle collector
        from .debug_collector import DebugCollector
        self._debug_collector = DebugCollector(
            config={},
            backend=None,
            bridge_engine=None,
            repeater_engine=None,
        )

    @cherrypy.expose
    @cherrypy.tools.json_out()
    def ifchains(self):
        """Return IF chain layout derived from bridge_conf.json (actual running config)."""
        try:
            # Read actual running config
            conf = _load_bridge_conf()
            sx = conf.get("SX130x_conf", {})

            # Get radio center frequencies
            r0_freq = sx.get("radio_0", {}).get("freq", 0)
            r1_freq = sx.get("radio_1", {}).get("freq", 0)
            r0_tx = sx.get("radio_0", {}).get("tx_enable", False)
            r1_tx = sx.get("radio_1", {}).get("tx_enable", False)

            rf_center_mhz = round(max(r0_freq, r1_freq) / 1e6, 4) if max(r0_freq, r1_freq) > 0 else 0

            # Load UI channels for friendly name matching
            ui = _load_ui()
            ui_channels = ui.get("channels", [])

            # Build frequency -> friendly name lookup from UI channels
            freq_to_name = {}
            for ch in ui_channels:
                ch_freq = int(ch.get("frequency", 0))
                friendly = ch.get("name", ch.get("friendly_name", "Unknown"))
                freq_to_name[ch_freq] = friendly

            result = []

            # Process multi-SF IF chains 0-7
            for i in range(8):
                ch_conf = sx.get(f"chan_multiSF_{i}", {})
                enabled = ch_conf.get("enable", False)
                radio = ch_conf.get("radio", 0)
                if_offset = ch_conf.get("if", 0)

                # Calculate actual frequency
                center_freq = r0_freq if radio == 0 else r1_freq
                actual_freq_hz = center_freq + if_offset if enabled else 0
                actual_freq_mhz = round(actual_freq_hz / 1e6, 4) if actual_freq_hz else 0

                # Determine role based on radio's tx_enable
                radio_has_tx = r0_tx if radio == 0 else r1_tx
                if enabled:
                    if radio_has_tx:
                        role = "rx_tx"
                    else:
                        role = "rx_only"
                else:
                    role = "disabled"

                # Match to UI channel by frequency
                friendly = freq_to_name.get(actual_freq_hz, None)
                if friendly:
                    if role == "rx_tx":
                        channel_name = f"{friendly} (RX+TX)"
                    elif role == "rx_only":
                        channel_name = f"{friendly} (RX)"
                    else:
                        channel_name = f"{friendly} (disabled)"
                else:
                    if enabled:
                        channel_name = f"IF{i} ({actual_freq_mhz} MHz)"
                    else:
                        channel_name = "(unused)"

                result.append({
                    "if_chain": i,
                    "type": "multi_sf",
                    "frequency_hz": actual_freq_hz,
                    "frequency_mhz": actual_freq_mhz,
                    "radio": radio,
                    "offset_hz": if_offset if enabled else 0,
                    "bandwidth_khz": 125 if enabled else 0,
                    "role": role,
                    "channel_name": channel_name,
                })

            # Process LoRa standard channel (IF chain 8)
            lora_std = sx.get("chan_Lora_std", {})
            std_enabled = lora_std.get("enable", False)
            std_radio = lora_std.get("radio", 0)
            std_offset = lora_std.get("if", 0)
            std_center = r0_freq if std_radio == 0 else r1_freq
            std_freq = std_center + std_offset if std_enabled else 0
            result.append({
                "if_chain": 8,
                "type": "lora_std",
                "frequency_hz": std_freq,
                "frequency_mhz": round(std_freq / 1e6, 4) if std_freq else 0,
                "radio": std_radio,
                "offset_hz": std_offset if std_enabled else 0,
                "bandwidth_khz": int(lora_std.get("bandwidth", 250000)) // 1000 if std_enabled else 0,
                "role": "rx_only" if std_enabled else "disabled",
                "channel_name": "LoRa Standard" if std_enabled else "(unused)",
            })

            # Process FSK channel (IF chain 9)
            fsk = sx.get("chan_FSK", {})
            fsk_enabled = fsk.get("enable", False)
            fsk_radio = fsk.get("radio", 0)
            fsk_offset = fsk.get("if", 0)
            fsk_center = r0_freq if fsk_radio == 0 else r1_freq
            fsk_freq = fsk_center + fsk_offset if fsk_enabled else 0
            result.append({
                "if_chain": 9,
                "type": "fsk",
                "frequency_hz": fsk_freq,
                "frequency_mhz": round(fsk_freq / 1e6, 4) if fsk_freq else 0,
                "radio": fsk_radio,
                "offset_hz": fsk_offset if fsk_enabled else 0,
                "bandwidth_khz": int(fsk.get("bandwidth", 125000)) // 1000 if fsk_enabled else 0,
                "role": "rx_only" if fsk_enabled else "disabled",
                "channel_name": "FSK" if fsk_enabled else "(unused)",
            })

            return {"ifchains": result, "source": "bridge_conf.json (running config)", "count": len(result),
                    "rf_center_mhz": rf_center_mhz}
        except Exception as ex:
            logger.error("ifchains error: %s", ex)
            return {"ifchains": [], "error": str(ex)}


    @cherrypy.expose
    def default(self, resource="status", *args, **params):
        method = cherrypy.request.method.upper()
        cherrypy.response.headers["Access-Control-Allow-Origin"] = "*"
        cherrypy.response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
        cherrypy.response.headers["Access-Control-Allow-Headers"] = "Content-Type"
        if method == "OPTIONS":
            return b"{}"

        if resource == "status":
            return self._status()

        if resource == "channels":
            sub = args[0] if args else ""
            if sub == "live" and method == "GET":
                return self._channels_live_get()
            if method == "GET":
                return self._channels_get()
            if method == "POST":
                return self._channels_post()

        if resource == "bridge":
            if method == "GET":
                return self._bridge_get()
            if method == "POST":
                return self._bridge_post()

        # -- rfchains --
        if resource == "rfchains":
            if method == "GET":
                return self._rfchains_get()
            if method == "POST":
                return self._rfchains_post()

        # -- tx_queues --
        if resource == "tx_queues":
            if method == "GET":
                return self._tx_queues_get()

        # -- spectrum --
        if resource == "spectrum":
            if method == "GET":
                return self._spectrum_get()
            if method == "POST":
                return self._spectrum_post()

        # -- neighbours (v2.5.7: mesh neighbour list with persistence) --
        if resource == "neighbours":
            if method == "GET":
                return self._neighbours_get()
            if method == "DELETE" or (method == "POST" and _body().get("action") == "delete"):
                return self._neighbours_delete()

        # -- neighbours_history (v2.5.7: persistent per-node RSSI/SNR history) --
        if resource == "neighbours_history":
            return self._neighbours_history_get(params.get("node_id", ""))

        # -- repeater_location (v2.5.7: own repeater GPS coordinates) --
        if resource == "repeater_location":
            if method == "GET":
                return self._repeater_location_get()
            if method in ("POST", "PUT"):
                return self._repeater_location_post()

        # -- neighbours_filter (v2.5.7: hide-test-nodes config) --
        if resource == "neighbours_filter":
            if method == "GET":
                return self._neighbours_filter_get()
            if method in ("POST", "PUT"):
                return self._neighbours_filter_post()

        # -- logs --
        if resource == "logs":
            return self._logs()

        # -- control --
        if resource == "control":
            return self._control()


        # -- signal quality (per-channel RSSI/SNR from packets) --
        if resource == "signal_quality":
            return _j(self.signal_quality(**params))

        # -- noise floor history --
        if resource == "noise_floor_history":
            return self.noise_floor_history(**params)


        # -- LBT history (TX events from packets table) --
        if resource == "lbt_history":
            return _j(self.lbt_history(**params))


        # -- TX activity per channel --

        # -- packet_activity --
        if resource == "packet_activity":
            return self._packet_activity(**params)

        # -- crc_error_rate (per-channel CRC error rate tracking) --
        if resource == "crc_error_rate":
            return self._crc_error_rate(**params)

        # -- packet_metrics (per-packet RX/TX detail for spectrum-tab charts) --
        if resource == "packet_metrics":
            return self._packet_metrics(**params)

        if resource == "tx_activity":
            return self.tx_activity(**params)

        # -- dedup events (bridge engine dedup visualization) --
        if resource == "dedup":
            sub = args[0] if args else ""
            if method == "GET":
                return self._dedup_events_get(**params)

        # -- packet traces (packet flow tracing) --
        if resource == "packet_traces":
            if method == "GET":
                return self._packet_traces_get(**params)



        # -- per-channel noise floor (enhanced) --
        if resource == "noise_floor":
            if method == "GET":
                return self._noise_floor_get(**params)

        # -- CAD stats --
        if resource == "cad_stats":
            if method == "GET":
                return self._cad_stats_get(**params)

        # -- cache_stats (internal buffer diagnostics, v2.5.3 follow-up) --
        # GET /api/wm1303/cache_stats -> snapshot of all in-memory caches
        # (dedup, TX echo, multi-demod RX, TX ACK, per-channel config caches)
        # with sizes, TTLs, age statistics, and engine counters. Helps diagnose
        # state-accumulation drift on long-running deployments (#24/#25).
        if resource == "cache_stats":
            if method == "GET":
                return self._cache_stats_get(**params)

                # -- adv_config (advanced configuration) --
        if resource == "adv_config":
            if method == "GET":
                return self._adv_config_get()
            if method == "POST":
                return self._adv_config_post()

        # -- channel_e (Channel E / LoRa RX configuration) --
        if resource == "channel_e":
            if method == "GET":
                return self._channel_e_get()
            if method == "POST":
                return self._channel_e_post()

        # -- channel_f (Channel F / chan_Lora_std on RF0, parallel to A-D) --
        # UI-only persistence: backend (wm1303_backend.py) reads channel_f from
        # wm1303_ui.json and generates chan_Lora_std dynamically. No global_conf
        # writes here (unlike channel_e which also writes SX1261 lora_rx block).
        if resource == "channel_f":
            if method == "GET":
                return self._channel_f_get()
            if method == "POST":
                return self._channel_f_post()

        # -- debug bundle --
        if resource == "debug":
            sub = args[0] if args else "status"
            if sub == "status" and method == "GET":
                return self._debug_status()
            if sub == "generate" and method == "POST":
                return self._debug_generate()
            if sub == "download" and method == "GET":
                return self._debug_download()
            raise cherrypy.HTTPError(404, "Unknown debug sub-resource: {}".format(sub))

        # -- regions (Issue #4 multi-region support) --
        # GET /api/wm1303/regions -> list all available regions and their metadata
        if resource == "regions":
            if method == "GET":
                return self._regions_get()

        # -- region (current selected region from UI config) --
        # GET /api/wm1303/region -> current region
        # POST/PUT /api/wm1303/region -> update region (body: {"code":..., optional tx_freq_min/max for CUSTOM})
        if resource == "region":
            if method == "GET":
                return self._region_get()
            if method in ("POST", "PUT"):
                return self._region_post()

        # -- sync_word (device-wide LoRa network sync word) --
        # GET /api/wm1303/sync_word -> current sync_word (value, mode, hex)
        # POST/PUT /api/wm1303/sync_word -> update sync_word (body: {"mode":"private|public"})
        # Note: Custom sync_word is NOT hardware-supported on SX1302 (lorawan_public
        # is a board-level flag). Only Private (0x1424) and Public (0x3444) accepted.
        if resource == "sync_word":
            if method == "GET":
                return self._sync_word_get()
            if method in ("POST", "PUT"):
                return self._sync_word_post()

        # -- presets (community channel presets for MeshCore frequencies) --
        # GET /api/wm1303/presets -> list available channel presets per region
        if resource == "presets":
            if method == "GET":
                return self._presets_get()

        raise cherrypy.HTTPError(404, "Unknown resource: {}".format(resource))

    def _status(self):
        import time as _time
        _now=_time.time()
        if _STATUS_CACHE.get('ts') and _now-_STATUS_CACHE['ts']<_STATUS_CACHE_TTL:
            return _STATUS_CACHE['data']
        # Check pymc-repeater service
        try:
            r = subprocess.run(
                ["sudo", "systemctl", "is-active", _SVC_NAME],
                capture_output=True, text=True, timeout=5
            )
            svc_active = r.stdout.strip() == "active"
        except Exception:
            svc_active = False

        # Check lora_pkt_fwd process
        try:
            rp = subprocess.run(
                ["pgrep", "-f", "lora_pkt_fwd"],
                capture_output=True, text=True, timeout=3
            )
            pkt_fwd_pids = [p for p in rp.stdout.strip().splitlines() if p.strip()]
            pkt_fwd_running = len(pkt_fwd_pids) > 0
            pkt_fwd_pid = int(pkt_fwd_pids[0]) if pkt_fwd_pids else None
        except Exception:
            pkt_fwd_running = False
            pkt_fwd_pid = None

        # Get service uptime
        uptime_str = None
        try:
            ru = subprocess.run(
                ["sudo", "systemctl", "show", _SVC_NAME,
                 "--property=ActiveEnterTimestamp"],
                capture_output=True, text=True, timeout=5
            )
            for line in ru.stdout.splitlines():
                if line.startswith("ActiveEnterTimestamp="):
                    uptime_str = line.split("=", 1)[1].strip()
                    break
        except Exception:
            pass

        eui_str = None
        try:
            rm = subprocess.run(['ip', 'link', 'show', 'eth0'], capture_output=True, text=True, timeout=3)
            for ln in rm.stdout.splitlines():
                ln = ln.strip()
                if 'link/ether' in ln:
                    mac = ln.split()[1]
                    p = mac.split(':')
                    ep = p[:3] + ['ff', 'fe'] + p[3:]
                    ep[0] = format(int(ep[0], 16) ^ 0x02, '02x')
                    eui_str = ''.join(ep).upper()
                    break
        except Exception:
            pass
        temperature = None
        try:
            with open('/sys/class/thermal/thermal_zone0/temp') as tf:
                temperature = round(int(tf.read().strip()) / 1000.0, 1)
        except Exception:
            pass
        # Read concentrator (SX1302) temperature from pkt_fwd status file
        concentrator_temp = None
        try:
            with open('/tmp/concentrator_temp') as ctf:
                concentrator_temp = round(float(ctf.read().strip()), 1)
        except Exception:
            pass
        # Sum packet counts from per-channel backend stats
        _pkt_rx = 0
        _pkt_tx = 0
        _pkt_fwd = 0
        try:
            _bk_s = _get_backend()
            if _bk_s:
                _ch_stats_s = _bk_s.get_channel_stats()
                for _cn_s, _cs_s in _ch_stats_s.items():
                    _pkt_rx += _cs_s.get("rx_count", 0)
                    _pkt_tx += _cs_s.get("tx_count", 0)
                _pkt_fwd = _pkt_tx
        except Exception:
            pass
        # Read version from VERSION file (deployed by install/upgrade scripts)
        _version = "0.10.0"
        try:
            _vf = Path("/etc/pymc_repeater/version")
            if _vf.exists():
                _version = _vf.read_text().strip()
        except Exception:
            pass
        # Channel counts including Channel E (SX1261) and Channel F (chan_Lora_std)
        _ui_data = _load_ui()
        _if_active = sum(1 for ch in _ui_data.get("channels", []) if ch.get("active", False))
        _che_active = 1 if _ui_data.get("channel_e", {}).get("enabled", False) else 0
        _chf_active = 1 if _ui_data.get("channel_f", {}).get("enabled", False) else 0
        _total_ch = 4 + 1 + 1  # 4 hardware IF slots (A-D) + Channel E + Channel F
        _active_ch = _if_active + _che_active + _chf_active
        _inactive_ch = _total_ch - _active_ch
        # ── Raspberry Pi system info ──────────────────────────
        _pi_info = {}
        try:
            # Pi Model
            try:
                with open('/proc/device-tree/model') as _mf:
                    _pi_info['model'] = _mf.read().strip().rstrip('\x00')
            except Exception:
                try:
                    with open('/proc/cpuinfo') as _cf:
                        for _cl in _cf:
                            if _cl.startswith('Model'):
                                _pi_info['model'] = _cl.split(':', 1)[1].strip()
                                break
                except Exception:
                    _pi_info['model'] = None
            # Memory: MemTotal and MemAvailable from /proc/meminfo
            try:
                _mem_total = 0
                _mem_avail = 0
                with open('/proc/meminfo') as _mif:
                    for _ml in _mif:
                        if _ml.startswith('MemTotal:'):
                            _mem_total = int(_ml.split()[1])  # kB
                        elif _ml.startswith('MemAvailable:'):
                            _mem_avail = int(_ml.split()[1])  # kB
                _mem_used = _mem_total - _mem_avail
                _pi_info['mem_total_mb'] = round(_mem_total / 1024)
                _pi_info['mem_used_mb'] = round(_mem_used / 1024)
            except Exception:
                _pi_info['mem_total_mb'] = None
                _pi_info['mem_used_mb'] = None
            # CPU temperature (already read above as 'temperature', reuse)
            _pi_info['cpu_temp'] = temperature
            # CPU usage from /proc/stat (instant snapshot)
            try:
                with open('/proc/stat') as _sf:
                    _cpu1 = _sf.readline().split()
                import time as _ctime
                _ctime.sleep(0.1)
                with open('/proc/stat') as _sf:
                    _cpu2 = _sf.readline().split()
                _idle1 = int(_cpu1[4]) + int(_cpu1[5])
                _idle2 = int(_cpu2[4]) + int(_cpu2[5])
                _total1 = sum(int(x) for x in _cpu1[1:])
                _total2 = sum(int(x) for x in _cpu2[1:])
                _d_total = _total2 - _total1
                _d_idle = _idle2 - _idle1
                _pi_info['cpu_usage'] = round((1 - _d_idle / _d_total) * 100) if _d_total > 0 else 0
            except Exception:
                _pi_info['cpu_usage'] = None
            # Disk usage of root filesystem
            try:
                _df = subprocess.run(
                    ['df', '-B1', '/'],
                    capture_output=True, text=True, timeout=5
                )
                _df_lines = _df.stdout.strip().splitlines()
                if len(_df_lines) >= 2:
                    _df_parts = _df_lines[1].split()
                    _pi_info['disk_total_gb'] = round(int(_df_parts[1]) / (1024**3), 2)
                    _pi_info['disk_used_gb'] = round(int(_df_parts[2]) / (1024**3), 2)
            except Exception:
                _pi_info['disk_total_gb'] = None
                _pi_info['disk_used_gb'] = None
        except Exception:
            pass
        _r=_j({
            "version": _version,
            "service": "active" if svc_active else "inactive",
            "pkt_fwd_running": pkt_fwd_running,
            "last_restart": uptime_str,
            "pkt_fwd_pid": pkt_fwd_pid,
            "chip": "WM1303 (SX1302/SX1303)",
            "chip_version": "v1.2",
            "spi_path": _load_ui().get("spi_devices", {}).get("sx1302_spi_path", "/dev/spidev0.0"),
            "sx1261_spi": _load_ui().get("spi_devices", {}).get("sx1261_spi_path", "/dev/spidev0.1"),
            "uptime": uptime_str,
            "eui": eui_str,
            "temperature": concentrator_temp if concentrator_temp is not None else temperature,
            "packets_received": _pkt_rx,
            "packets_sent": _pkt_tx,
            "packets_forwarded": _pkt_fwd,
            "active_channels": _active_ch,
            "total_channels": _total_ch,
            "inactive_channels": _inactive_ch,
            "pi_info": _pi_info,
            "timestamp": time.time(),
        })
        _STATUS_CACHE['data'] = _r
        _STATUS_CACHE['ts'] = _time.time()
        return _r

    def _channels_get(self):
        chs = _load_ui().get("channels", [])
        _abc = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
        for i, ch in enumerate(chs):
            ch.setdefault("preamble_length", 17)
            ch.setdefault("friendly_name", "Channel " + (_abc[i] if i < len(_abc) else str(i + 1)))
            ch.setdefault("lbt_threshold", ch.get("lbt_rssi_target", -80))
            ch.setdefault("lbt_rssi_target", ch.get("lbt_threshold", -80))
            ch.setdefault("tx_power", 14)
            ch.setdefault("lbt_enabled", False)
            ch.setdefault("cad_enabled", False)
        return _j(chs)

    def _channels_post(self):
        body = _body()
        # Support both list and dict formats
        if isinstance(body, list):
            channels = body
            do_restart = False
        else:
            channels = body.get("channels", [])
            do_restart = body.get("restart", False)

        # Pre-save validation: check IF range constraints
        # SX1302 HAL constant: LGW_RF_RX_BANDWIDTH_125KHZ = 1600000
        RF_RX_BW = 1_600_000
        warnings = []
        active_freqs = [int(ch.get('frequency', 0))
                        for ch in channels
                        if ch.get('active', False) and ch.get('frequency', 0)]
        if active_freqs:
            center = sum(active_freqs) // len(active_freqs)
            for ch in channels:
                f = int(ch.get('frequency', 0))
                bw = int(ch.get('bandwidth', 125000))
                max_if = (RF_RX_BW // 2) - (bw // 2) - 7500
                if f and abs(f - center) > max_if:
                    ch_name = ch.get('name', ch.get('friendly_name', '?'))
                    delta_khz = abs(f - center) / 1000
                    max_khz = max_if / 1000
                    warnings.append(
                        f"Channel '{ch_name}' at {f/1e6:.3f} MHz is {delta_khz:.1f} kHz "
                        f"from center {center/1e6:.3f} MHz (max {max_khz:.1f} kHz for "
                        f"BW {bw/1000:.0f} kHz). It will be force-disabled at startup.")

        # Defensive cleanup: sync_word is device-wide, never per-channel.
        # Strip any sync_word field that may have leaked in from older UI builds
        # or third-party callers. The device-wide value lives at the top level
        # of wm1303_ui.json and is managed via /api/wm1303/sync_word.
        for _ch in channels:
            if isinstance(_ch, dict):
                _ch.pop("sync_word", None)

        # GitHub issue #7 Bug E -- LBT threshold UI/HAL field sync.
        # The WM1303 UI "LBT (dBm)" field writes `lbt_rssi_target`, but
        # lora_pkt_fwd reads `lbt_threshold` for the actual TX-blocking
        # decision. Historically the two fields diverged: bootstrap initialised
        # `lbt_threshold=-80` directly, so the UI-driven `lbt_rssi_target`
        # changes never propagated to the field that the HAL actually uses,
        # making the UI control cosmetic.
        #
        # On every channel save we now force both fields to the same value,
        # treating `lbt_rssi_target` as the source-of-truth (it is what the
        # UI writes). If only `lbt_threshold` is present (legacy callers),
        # it is mirrored back into `lbt_rssi_target` so future reads stay
        # consistent. If neither is set the channel is left untouched.
        for _ch in channels:
            if not isinstance(_ch, dict):
                continue
            _src = _ch.get("lbt_rssi_target")
            _dst = _ch.get("lbt_threshold")
            if _src is not None:
                _ch["lbt_threshold"] = _src
            elif _dst is not None:
                _ch["lbt_rssi_target"] = _dst

        ui = _load_ui()
        ui["channels"] = channels
        _save_ui(ui)
        # Sync config.yaml wm1303.channels so it stays in sync with SSOT
        _sync_config_yaml_channels(channels)
        # SSOT: sync IF chains in global_conf.json
        sync_result = sync_global_conf()
        result = {"status": "ok", "sync": sync_result}
        if warnings:
            result["warnings"] = warnings

        if do_restart:
            try:
                subprocess.Popen(
                    ["sudo", "systemctl", "restart", _SVC_NAME],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True)
                result["service_restarted"] = True
            except Exception as e:
                result["restart_error"] = str(e)
        return _j(result)


    # -- channels/live -------------------------------------------------------
    def _channels_live_get(self):
        """Return aggregated live operational data per channel.

        Uses wm1303_ui.json (SSOT) as the channel source — NOT config.yaml.
        Active channels are mapped to channel_a..channel_d by index, matching
        the backend's _CHANNEL_ID_BY_INDEX mapping in get_radios().
        """
        import time as _t
        import urllib.request, json as _json2

        _CHANNEL_ID_BY_INDEX = ['channel_a', 'channel_b', 'channel_c', 'channel_d']
        _abc = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ'

        # Load channels from SSOT (wm1303_ui.json) — same source as backend
        _ui_chs = _load_ui().get('channels', [])

        # Get per-channel stats from backend (direct reference)
        channel_stats = {}
        tx_stats = {}
        try:
            _bk = _get_backend()
            if _bk:
                channel_stats = _bk.get_channel_stats()
                if _bk._tx_queue_manager:
                    tx_stats = _bk._tx_queue_manager.get_status()
        except Exception:
            pass

        if not tx_stats:
            try:
                _resp = urllib.request.urlopen('http://127.0.0.1:8000/api/wm1303/tx_queues', timeout=2)
                _tq = _json2.loads(_resp.read())
                tx_stats = _tq.get('queues', {})
            except Exception:
                pass

        # Sum packet counts from per-channel backend stats
        total_rx = 0
        total_tx = 0
        for _cn_t, _cs_t in channel_stats.items():
            total_rx += _cs_t.get('rx_count', 0)
            total_tx += _cs_t.get('tx_count', 0)

        # Get service uptime in seconds
        uptime_seconds = 0
        try:
            import subprocess as _sp
            ru = _sp.run(
                ['sudo', 'systemctl', 'show', _SVC_NAME, '--property=ActiveEnterTimestamp'],
                capture_output=True, text=True, timeout=5
            )
            for line in ru.stdout.splitlines():
                if line.startswith('ActiveEnterTimestamp='):
                    ts_str = line.split('=', 1)[1].strip()
                    if ts_str:
                        from datetime import datetime as _dtc
                        try:
                            dt = _dtc.strptime(ts_str, '%a %Y-%m-%d %H:%M:%S %Z')
                            uptime_seconds = int(_t.time() - dt.timestamp())
                        except Exception:
                            pass
        except Exception:
            pass

        # Get noise floor from spectrum scan results
        noise_data = {}
        try:
            if _SPECTRAL_RES.exists():
                scan = json.loads(_SPECTRAL_RES.read_text())
                scan_points = scan.get('scan_points', [])
                for pt in scan_points:
                    freq_hz = int(pt.get('freq_hz', 0))
                    rssi = pt.get('rssi_dbm', -120)
                    noise_data[freq_hz] = rssi
        except Exception:
            pass

        # Also try spectrum collector DB
        if not noise_data and _COLLECTOR_AVAILABLE:
            try:
                collector = get_collector()
                recent = collector.get_spectrum_history(hours=1)
                for pt in recent:
                    freq_mhz = pt.get('freq_mhz', 0)
                    freq_hz = int(freq_mhz * 1e6)
                    rssi = pt.get('rssi_dbm', -120)
                    if freq_hz not in noise_data or rssi < noise_data[freq_hz]:
                        noise_data[freq_hz] = rssi
            except Exception:
                pass

        def _find_noise(freq_hz, tolerance=150000):
            best = -120.0
            best_dist = float('inf')
            for nf_hz, nf_rssi in noise_data.items():
                dist = abs(nf_hz - freq_hz)
                if dist < tolerance and dist < best_dist:
                    best = nf_rssi
                    best_dist = dist
            result = round(best, 1)
            return result if result != 0 else -120.0

        # Build per-channel live data from SSOT
        # Map active channels to channel_a..channel_d by index (same as backend)
        channels_live = []
        active_idx = 0
        for ui_idx, uch in enumerate(_ui_chs):
            if not uch.get('active', False):
                continue
            if active_idx >= len(_CHANNEL_ID_BY_INDEX):
                break
            ch_id = _CHANNEL_ID_BY_INDEX[active_idx]
            freq = int(uch.get('frequency', 0))
            if not freq:
                active_idx += 1
                continue
            friendly_name = uch.get('friendly_name',
                                    'Channel ' + (_abc[ui_idx] if ui_idx < len(_abc) else str(ui_idx + 1)))

            # Get real per-channel stats from backend using mapped channel_id
            ch_st = channel_stats.get(ch_id, {})
            ch_tx = tx_stats.get(ch_id, {})
            rx_count = ch_st.get('rx_count', 0)
            tx_sent = ch_st.get('tx_count', 0) or ch_tx.get('total_sent', 0)
            tx_failed = ch_st.get('tx_failed', 0) or ch_tx.get('total_failed', 0)
            last_tx = ch_st.get('last_tx_time') or ch_tx.get('last_tx_time')
            last_rx = ch_st.get('last_rx_time')
            rssi_last = ch_st.get('last_rssi', -120.0)
            rssi_avg = ch_st.get('rssi_avg', -120.0)
            snr_last = ch_st.get('last_snr', 0.0)
            # Noise floor: prefer LBT RSSI rolling average from TX queue, fallback to spectral scan
            nf_lbt_avg = ch_tx.get('noise_floor_lbt_avg')
            noise_floor = _find_noise(freq)
            if noise_floor == 0 or noise_floor is None: noise_floor = -120.0
            # If LBT rolling RSSI average available and spectral gave fallback, use LBT
            if nf_lbt_avg is not None and nf_lbt_avg > -119.0:
                noise_floor = nf_lbt_avg
            channels_live.append({
                'name': uch.get('name', ch_id),
                'friendly_name': friendly_name,
                'frequency': freq,
                'bandwidth': int(uch.get('bandwidth', 125000)),
                'spreading_factor': int(uch.get('spreading_factor', 7)),
                'coding_rate': uch.get('coding_rate', '4/5'),
                'rx_packets': rx_count,
                'tx_packets': tx_sent,
                'tx_failed': tx_failed,
                'last_rx': last_rx,
                'last_tx': last_tx,
                'rssi_last': rssi_last,
                'rssi_avg': rssi_avg,
                'snr_last': snr_last,
                'noise_floor': noise_floor,
                # TX timing stats
                'avg_tx_airtime_ms': ch_st.get('avg_tx_airtime_ms', 0),
                'avg_tx_send_ms': ch_st.get('avg_tx_send_ms', 0),
                'avg_tx_wait_ms': ch_st.get('avg_tx_wait_ms', 0),
                'last_tx_airtime_ms': ch_st.get('last_tx_airtime_ms', 0),
                'total_tx_airtime_ms': ch_st.get('total_tx_airtime_ms', 0),
                'total_tx_send_ms': ch_st.get('total_tx_send_ms', 0),
                'tx_bytes': ch_st.get('tx_bytes', 0),
                'tx_duty_pct': ch_st.get('tx_duty_pct', 0),
                # Software LBT stats (prefer tx_queue stats, fallback to backend stats)
                'lbt_blocked': ch_tx.get('lbt_blocked', 0) or ch_st.get('lbt_blocked', 0),
                'lbt_passed': ch_tx.get('lbt_passed', 0) or ch_st.get('lbt_passed', 0),
                'lbt_skipped': ch_tx.get('lbt_skipped', 0) or ch_st.get('lbt_skipped', 0),
                'lbt_last_blocked_at': ch_tx.get('lbt_last_blocked_at') or ch_st.get('lbt_last_blocked_at'),
                'lbt_last_rssi': ch_tx.get('lbt_last_rssi') or ch_st.get('lbt_last_rssi'),
                # LBT RSSI noise floor estimates (rolling buffer of last 20 measurements)
                'noise_floor_lbt_avg': ch_tx.get('noise_floor_lbt_avg'),
                'noise_floor_lbt_min': ch_tx.get('noise_floor_lbt_min'),
                'noise_floor_lbt_max': ch_tx.get('noise_floor_lbt_max'),
                'noise_floor_lbt_samples': ch_tx.get('noise_floor_lbt_samples', 0),
                # TX noisefloor (pre-CAD FSK-RX) rolling estimates
                'tx_noisefloor_avg': ch_tx.get('tx_noisefloor_avg'),
                'tx_noisefloor_min': ch_tx.get('tx_noisefloor_min'),
                'tx_noisefloor_max': ch_tx.get('tx_noisefloor_max'),
                'tx_noisefloor_last': ch_tx.get('tx_noisefloor_last'),
                'tx_noisefloor_samples': ch_tx.get('tx_noisefloor_samples', 0),
                # Queue health stats
                'queue_pending': ch_tx.get('pending', 0),
                'queue_size': ch_tx.get('queue_size', 15),
                'dropped_overflow': ch_tx.get('dropped_overflow', 0),
                'dropped_ttl': ch_tx.get('dropped_ttl', 0),
                # CAD stats
                'cad_clear': ch_tx.get('cad_clear', 0),
                'cad_detected': ch_tx.get('cad_detected', 0),
            })
            active_idx += 1

        # --- Include Channel E (SX1261 dedicated LoRa RX/TX) if enabled ---
        _che_ui = _load_ui().get("channel_e", {})
        if _che_ui.get("enabled", False):
            ch_e_st = channel_stats.get("channel_e", {})
            ch_e_tx = tx_stats.get("channel_e", {})
            _che_freq = int(_che_ui.get("frequency", 0))
            _che_rx = ch_e_st.get("rx_count", 0)
            _che_tx_sent = ch_e_st.get("tx_count", 0) or ch_e_tx.get("total_sent", 0)
            _che_tx_failed = ch_e_st.get("tx_failed", 0) or ch_e_tx.get("total_failed", 0)
            _che_last_tx = ch_e_st.get("last_tx_time") or ch_e_tx.get("last_tx_time")
            _che_last_rx = ch_e_st.get("last_rx_time")
            _che_rssi = ch_e_st.get("last_rssi", -120.0)
            _che_rssi_avg = ch_e_st.get("rssi_avg", -120.0)
            _che_snr = ch_e_st.get("last_snr", 0.0)
            _che_nf_lbt = ch_e_tx.get("noise_floor_lbt_avg")
            _che_nf = _find_noise(_che_freq)
            if _che_nf == 0 or _che_nf is None: _che_nf = -120.0
            if _che_nf_lbt is not None and _che_nf_lbt > -119.0:
                _che_nf = _che_nf_lbt
            _che_total_airtime_ms = ch_e_st.get("total_tx_airtime_ms", 0)
            _che_duty = round((_che_total_airtime_ms / 1000.0 / uptime_seconds) * 100, 3) if uptime_seconds > 0 else 0
            channels_live.append({
                "name": _che_ui.get("name", _che_ui.get("friendly_name", "Channel E")),
                "friendly_name": _che_ui.get("friendly_name", "Channel E"),
                "frequency": _che_freq,
                "bandwidth": int(_che_ui.get("bandwidth", 62500)),
                "spreading_factor": int(_che_ui.get("spreading_factor", 8)),
                "coding_rate": _che_ui.get("coding_rate", "4/5"),
                "is_sx1261": True,
                "rx_packets": _che_rx,
                "tx_packets": _che_tx_sent,
                "tx_failed": _che_tx_failed,
                "last_rx": _che_last_rx,
                "last_tx": _che_last_tx,
                "rssi_last": _che_rssi,
                "rssi_avg": _che_rssi_avg,
                "snr_last": _che_snr,
                "noise_floor": _che_nf,
                "avg_tx_airtime_ms": ch_e_st.get("avg_tx_airtime_ms", 0),
                "avg_tx_send_ms": ch_e_st.get("avg_tx_send_ms", 0),
                "avg_tx_wait_ms": ch_e_st.get("avg_tx_wait_ms", 0),
                "last_tx_airtime_ms": ch_e_st.get("last_tx_airtime_ms", 0),
                "total_tx_airtime_ms": _che_total_airtime_ms,
                "total_tx_send_ms": ch_e_st.get("total_tx_send_ms", 0),
                "tx_bytes": ch_e_st.get("tx_bytes", 0),
                "tx_duty_pct": _che_duty,
                "lbt_blocked": ch_e_tx.get("lbt_blocked", 0) or ch_e_st.get("lbt_blocked", 0),
                "lbt_passed": ch_e_tx.get("lbt_passed", 0) or ch_e_st.get("lbt_passed", 0),
                "lbt_skipped": ch_e_tx.get("lbt_skipped", 0) or ch_e_st.get("lbt_skipped", 0),
                "lbt_last_blocked_at": ch_e_tx.get("lbt_last_blocked_at") or ch_e_st.get("lbt_last_blocked_at"),
                "lbt_last_rssi": ch_e_tx.get("lbt_last_rssi") or ch_e_st.get("lbt_last_rssi"),
                "noise_floor_lbt_avg": ch_e_tx.get("noise_floor_lbt_avg"),
                "noise_floor_lbt_min": ch_e_tx.get("noise_floor_lbt_min"),
                "noise_floor_lbt_max": ch_e_tx.get("noise_floor_lbt_max"),
                "noise_floor_lbt_samples": ch_e_tx.get("noise_floor_lbt_samples", 0),
                "queue_pending": ch_e_tx.get("pending", 0),
                "queue_size": ch_e_tx.get("queue_size", 15),
                "dropped_overflow": ch_e_tx.get("dropped_overflow", 0),
                "dropped_ttl": ch_e_tx.get("dropped_ttl", 0),
                # CAD stats
                "cad_clear": ch_e_tx.get("cad_clear", 0),
                "cad_detected": ch_e_tx.get("cad_detected", 0),
            })
            total_rx += _che_rx
            total_tx += _che_tx_sent

        # --- Include Channel F (chan_Lora_std on SX1302 RF0) if enabled ---
        # Channel F runs in PARALLEL with channels A-D on the SX1302 (BW125/250/500).
        _chf_ui = _load_ui().get("channel_f", {})
        if _chf_ui.get("enabled", False):
            ch_f_st = channel_stats.get("channel_f", {})
            ch_f_tx = tx_stats.get("channel_f", {})
            _chf_freq = int(_chf_ui.get("frequency", 0))
            _chf_rx = ch_f_st.get("rx_count", 0)
            _chf_tx_sent = ch_f_st.get("tx_count", 0) or ch_f_tx.get("total_sent", 0)
            _chf_tx_failed = ch_f_st.get("tx_failed", 0) or ch_f_tx.get("total_failed", 0)
            _chf_last_tx = ch_f_st.get("last_tx_time") or ch_f_tx.get("last_tx_time")
            _chf_last_rx = ch_f_st.get("last_rx_time")
            _chf_rssi = ch_f_st.get("last_rssi", -120.0)
            _chf_rssi_avg = ch_f_st.get("rssi_avg", -120.0)
            _chf_snr = ch_f_st.get("last_snr", 0.0)
            _chf_nf_lbt = ch_f_tx.get("noise_floor_lbt_avg")
            _chf_nf = _find_noise(_chf_freq)
            if _chf_nf == 0 or _chf_nf is None:
                _chf_nf = -120.0
            if _chf_nf_lbt is not None and _chf_nf_lbt > -119.0:
                _chf_nf = _chf_nf_lbt
            _chf_total_airtime_ms = ch_f_st.get("total_tx_airtime_ms", 0)
            _chf_duty = round((_chf_total_airtime_ms / 1000.0 / uptime_seconds) * 100, 3) if uptime_seconds > 0 else 0
            channels_live.append({
                "name": _chf_ui.get("name", _chf_ui.get("friendly_name", "Channel F")),
                "friendly_name": _chf_ui.get("friendly_name", "Channel F"),
                "frequency": _chf_freq,
                "bandwidth": int(_chf_ui.get("bandwidth", 250000)),
                "spreading_factor": int(_chf_ui.get("spreading_factor", 9)),
                "coding_rate": _chf_ui.get("coding_rate", "4/5"),
                "is_chan_lora_std": True,
                "rx_packets": _chf_rx,
                "tx_packets": _chf_tx_sent,
                "tx_failed": _chf_tx_failed,
                "last_rx": _chf_last_rx,
                "last_tx": _chf_last_tx,
                "rssi_last": _chf_rssi,
                "rssi_avg": _chf_rssi_avg,
                "snr_last": _chf_snr,
                "noise_floor": _chf_nf,
                "avg_tx_airtime_ms": ch_f_st.get("avg_tx_airtime_ms", 0),
                "avg_tx_send_ms": ch_f_st.get("avg_tx_send_ms", 0),
                "avg_tx_wait_ms": ch_f_st.get("avg_tx_wait_ms", 0),
                "last_tx_airtime_ms": ch_f_st.get("last_tx_airtime_ms", 0),
                "total_tx_airtime_ms": _chf_total_airtime_ms,
                "total_tx_send_ms": ch_f_st.get("total_tx_send_ms", 0),
                "tx_bytes": ch_f_st.get("tx_bytes", 0),
                "tx_duty_pct": _chf_duty,
                "lbt_blocked": ch_f_tx.get("lbt_blocked", 0) or ch_f_st.get("lbt_blocked", 0),
                "lbt_passed": ch_f_tx.get("lbt_passed", 0) or ch_f_st.get("lbt_passed", 0),
                "lbt_skipped": ch_f_tx.get("lbt_skipped", 0) or ch_f_st.get("lbt_skipped", 0),
                "lbt_last_blocked_at": ch_f_tx.get("lbt_last_blocked_at") or ch_f_st.get("lbt_last_blocked_at"),
                "lbt_last_rssi": ch_f_tx.get("lbt_last_rssi") or ch_f_st.get("lbt_last_rssi"),
                "noise_floor_lbt_avg": ch_f_tx.get("noise_floor_lbt_avg"),
                "noise_floor_lbt_min": ch_f_tx.get("noise_floor_lbt_min"),
                "noise_floor_lbt_max": ch_f_tx.get("noise_floor_lbt_max"),
                "noise_floor_lbt_samples": ch_f_tx.get("noise_floor_lbt_samples", 0),
                "queue_pending": ch_f_tx.get("pending", 0),
                "queue_size": ch_f_tx.get("queue_size", 15),
                "dropped_overflow": ch_f_tx.get("dropped_overflow", 0),
                "dropped_ttl": ch_f_tx.get("dropped_ttl", 0),
                "cad_clear": ch_f_tx.get("cad_clear", 0),
                "cad_detected": ch_f_tx.get("cad_detected", 0),
            })
            total_rx += _chf_rx
            total_tx += _chf_tx_sent


        return _j({
            "channels": channels_live,
            "uptime_seconds": uptime_seconds,
            "total_rx": total_rx,
            "total_tx": total_tx,
            "timestamp": _t.time(),
        })


    def _bridge_get(self):
        """Return bridge rules from wm1303_ui.json (Single Source of Truth)."""
        ui = _load_ui()
        bridge_data = ui.get("bridge", {})
        rules = bridge_data.get("rules", [])
        return _j({"rules": rules})

    def _bridge_post(self):
        """Save bridge rules to wm1303_ui.json (Single Source of Truth) and hot-reload bridge engine."""
        body = _body()
        rules = body.get("rules", [])

        # Save to UI JSON (SSOT)
        ui = _load_ui()
        ui["bridge"] = {"rules": rules}
        _save_ui(ui)
        logger.info("SSOT: saved %d bridge rules to wm1303_ui.json", len(rules))

        # Hot-reload bridge engine rules
        reload_count = -1
        try:
            from repeater.main import RepeaterDaemon
            import gc
            for obj in gc.get_referrers(RepeaterDaemon):
                if isinstance(obj, RepeaterDaemon) and hasattr(obj, 'reload_bridge_rules'):
                    reload_count = obj.reload_bridge_rules()
                    logger.info("SSOT: bridge engine hot-reloaded %d rules", reload_count)
                    break
            else:
                logger.warning("SSOT: could not find RepeaterDaemon instance for hot-reload")
        except Exception as e:
            logger.warning("SSOT: bridge engine hot-reload failed: %s", e)

        # --- optional service restart (Save & Restart button) ---
        restarted = False
        if body.get("restart"):
            import subprocess as _sp
            try:
                _sp.Popen(['sudo', 'systemctl', 'restart', _SVC_NAME])
                restarted = True
                logger.info("Service %s restart triggered via bridge Save & Restart", _SVC_NAME)
            except Exception as e:
                logger.error("Service restart failed: %s", e)

        resp = {"status": "ok", "saved": len(rules), "reloaded": reload_count}
        if restarted:
            resp["service_restarted"] = True
        return _j(resp)

    def _dedup_events_get(self, **params):
        """Return dedup events with time-range aggregation for charting.

        Query params:
          range  - '1h','6h','24h','3d','7d' (default '1h')
          bucket - aggregation bucket in minutes (auto if omitted)
          since  - unix timestamp (custom range start)
          until  - unix timestamp (custom range end)
          raw    - 'true' to return individual events instead of buckets
        """
        import time as _time
        import sqlite3 as _sqlite3

        bridge = None
        try:
            from repeater.bridge_engine import _active_bridge
            bridge = _active_bridge
        except Exception:
            pass

        # --- Parse time range ---
        now = _time.time()
        range_str = params.get('range', '1h')
        AUTO_BUCKETS = {
            '1h':  (3600,       1),    # 1 min buckets -> 60 points
            '6h':  (6*3600,     5),    # 5 min buckets -> 72 points
            '24h': (24*3600,   15),    # 15 min buckets -> 96 points
            '3d':  (3*24*3600, 60),    # 1 hour buckets -> 72 points
            '7d':  (7*24*3600, 120),   # 2 hour buckets -> 84 points
        }

        if 'since' in params:
            since_ts = float(params['since'])
            until_ts = float(params.get('until', now))
            span = until_ts - since_ts
            # Auto bucket based on span
            if span <= 3600:
                bucket_min = 1
            elif span <= 6*3600:
                bucket_min = 5
            elif span <= 24*3600:
                bucket_min = 15
            elif span <= 3*24*3600:
                bucket_min = 60
            else:
                bucket_min = 120
        else:
            span_secs, bucket_min = AUTO_BUCKETS.get(range_str, (3600, 1))
            since_ts = now - span_secs
            until_ts = now

        if 'bucket' in params:
            bucket_min = int(params['bucket'])

        want_raw = params.get('raw', 'false').lower() == 'true'

        # --- Gather bridge stats (always from live bridge) ---
        live_stats = {}
        if bridge is not None:
            st = bridge.get_stats()
            live_stats = {
                "total_forwarded": st.get('forwarded_packets', 0),
                "total_duplicate": st.get('dropped_duplicate', 0),
                "total_tx_echo": st.get('fwd_echo_detected', 0),
                "total_filtered": st.get('dropped_filtered', 0),
                "dedup_seen_active": st.get('dedup_seen_active', 0),
                "dedup_events_buffered": st.get('dedup_events_buffered', 0),
            }

        # --- Try SQLite for historical data ---
        db_path = "/var/lib/pymc_repeater/repeater.db"
        buckets = []
        period_stats = {"total_forwarded": 0, "total_duplicate": 0,
                        "total_tx_echo": 0, "total_filtered": 0,
                        "total_hal_tx_echo": 0, "total_hal_mesh_echo": 0,
                        "total_hal_unknown_echo": 0, "total_multi_demod": 0,
                        "total_companion_dedup": 0,
                        "period_start": since_ts, "period_end": until_ts,
                        "dedup_ratio": 0.0}

        try:
            import os as _os
            if _os.path.exists(db_path):
                with _db_conn(db_path) as conn:
                    conn.row_factory = _sqlite3.Row

                    if want_raw:
                        # Return individual events
                        rows = conn.execute(
                            "SELECT ts, event_type, source, pkt_hash, pkt_size, pkt_type "
                            "FROM dedup_events WHERE ts >= ? AND ts <= ? ORDER BY ts ASC LIMIT 10000",
                            (since_ts, until_ts)
                        ).fetchall()
                        events = [{"ts": r["ts"], "type": r["event_type"], "src": r["source"],
                                   "hash": r["pkt_hash"], "size": r["pkt_size"],
                                   "pkt_type": r["pkt_type"]} for r in rows]
                        # Compute stats from raw events
                        for e in events:
                            k = "total_" + e["type"]
                            if k in period_stats:
                                period_stats[k] += 1
                        total = len(events)
                        if total > 0:
                            # Dedup ratio = (all non-forwarded events) / total.
                            # Includes duplicates, echoes (bridge/HAL/mesh/unknown),
                            # filtered, multi-demod, and companion-dedup.
                            _dedup_sum = (
                                period_stats.get("total_duplicate", 0)
                                + period_stats.get("total_tx_echo", 0)
                                + period_stats.get("total_filtered", 0)
                                + period_stats.get("total_hal_tx_echo", 0)
                                + period_stats.get("total_hal_mesh_echo", 0)
                                + period_stats.get("total_hal_unknown_echo", 0)
                                + period_stats.get("total_multi_demod", 0)
                                + period_stats.get("total_companion_dedup", 0)
                            )
                            period_stats["dedup_ratio"] = round(_dedup_sum / total, 4)
                        period_stats.update(live_stats)
                        return _j({"events": events, "stats": period_stats, "mode": "raw"})

                    # Aggregated buckets
                    # Aggregated buckets via tiered query
                    bucket_secs = bucket_min * 60
                    _EVENT_TYPES = ('forwarded', 'duplicate', 'tx_echo', 'filtered',
                                    'hal_tx_echo', 'hal_mesh_echo', 'hal_unknown_echo',
                                    'multi_demod', 'companion_dedup')
                    tiered_rows = tiered_dedup_query(conn, since_ts, until_ts, bucket_secs)
                    # Pivot: one row per (bucket_ts, event_type) -> one dict per bucket_ts
                    bkt_map = {}
                    for r in tiered_rows:
                        bts = int(r["bucket_ts"])
                        if bts not in bkt_map:
                            bkt_map[bts] = {"ts": bts, "forwarded": 0, "duplicate": 0,
                                            "tx_echo": 0, "filtered": 0, "hal_tx_echo": 0,
                                            "hal_mesh_echo": 0, "hal_unknown_echo": 0,
                                            "multi_demod": 0, "companion_dedup": 0}
                        et = r.get("event_type", "")
                        cnt = r.get("sample_count") or 0
                        if et in bkt_map[bts]:
                            bkt_map[bts][et] += cnt
                    buckets = [bkt_map[k] for k in sorted(bkt_map.keys())]

                    # Compute period totals from the pivoted buckets
                    for b in buckets:
                        for et in _EVENT_TYPES:
                            k = "total_" + et
                            if k in period_stats:
                                period_stats[k] += b.get(et, 0)
                    total = sum(period_stats.get("total_" + et, 0) for et in _EVENT_TYPES)
                    if total > 0:
                        # Dedup ratio = (all non-forwarded events) / total.
                        _dedup_sum = (
                            period_stats.get("total_duplicate", 0)
                            + period_stats.get("total_tx_echo", 0)
                            + period_stats.get("total_filtered", 0)
                            + period_stats.get("total_hal_tx_echo", 0)
                            + period_stats.get("total_hal_mesh_echo", 0)
                            + period_stats.get("total_hal_unknown_echo", 0)
                            + period_stats.get("total_multi_demod", 0)
                            + period_stats.get("total_companion_dedup", 0)
                        )
                        period_stats["dedup_ratio"] = round(_dedup_sum / total, 4)

        except Exception as _e:
            import logging
            logging.getLogger("WM1303API").warning("dedup SQLite query error: %s", _e)
            # Fall back to in-memory deque if SQLite fails
            if bridge is not None:
                events = bridge.get_dedup_events(since=since_ts, limit=500)
                # Build simple buckets from in-memory events
                bucket_secs = bucket_min * 60
                bkt_map = {}
                for ev in events:
                    bts = int(ev['ts'] / bucket_secs) * bucket_secs
                    if bts not in bkt_map:
                        bkt_map[bts] = {"ts": bts, "forwarded": 0, "duplicate": 0, "tx_echo": 0,
                                        "filtered": 0, "hal_tx_echo": 0, "hal_mesh_echo": 0,
                                        "hal_unknown_echo": 0, "multi_demod": 0,
                                        "companion_dedup": 0}
                    et = ev.get('type', '')
                    if et in bkt_map[bts]:
                        bkt_map[bts][et] += 1
                for ev in events:
                    k = "total_" + ev.get('type', '')
                    if k in period_stats:
                        period_stats[k] += 1
                total = len(events)
                if total > 0:
                    # Dedup ratio = (all non-forwarded events) / total.
                    _dedup_sum = (
                        period_stats.get("total_duplicate", 0)
                        + period_stats.get("total_tx_echo", 0)
                        + period_stats.get("total_filtered", 0)
                        + period_stats.get("total_hal_tx_echo", 0)
                        + period_stats.get("total_hal_mesh_echo", 0)
                        + period_stats.get("total_hal_unknown_echo", 0)
                        + period_stats.get("total_multi_demod", 0)
                        + period_stats.get("total_companion_dedup", 0)
                    )
                    period_stats["dedup_ratio"] = round(_dedup_sum / total, 4)

        period_stats.update(live_stats)
        return _j({
            "buckets": buckets,
            "stats": period_stats,
            "range": range_str,
            "bucket_minutes": bucket_min,
        })

    # -- packet traces ---------------------------------------------------------
    def _packet_traces_get(self, **params):
        """Return recent packet traces from the in-memory ring buffer."""
        try:
            from repeater.web.packet_trace import get_traces
            limit = int(params.get('limit', 50))
            status = params.get('status', '')
            channel = params.get('channel', '')
            traces = get_traces(limit=limit, status=status, channel=channel)
            return _j({"traces": traces})
        except Exception as e:
            import traceback
            return _j({"error": str(e), "detail": traceback.format_exc()})

    # -- rfchains --------------------------------------------------------------
    def _rfchains_get(self):
        """Return RF0 and RF1 config dynamically from bridge_conf.json."""
        ui = _load_ui()
        conf = _load_bridge_conf()
        sx = conf.get("SX130x_conf", {})

        # Read actual radio configs
        r0 = sx.get("radio_0", {})
        r1 = sx.get("radio_1", {})

        # Determine center frequency from UI (SSOT) or fallback to config
        rf_center_mhz = ui.get("rf_center_freq_mhz", 0)
        if not rf_center_mhz:
            rf_center_mhz = round(max(r0.get("freq", 0), r1.get("freq", 0)) / 1e6, 4)
        rf_center_hz = int(round(rf_center_mhz * 1e6))

        # Count IF chains per radio
        rf0_chains = []
        rf1_chains = []
        for i in range(8):
            ch = sx.get(f"chan_multiSF_{i}", {})
            if ch.get("enable", False):
                radio = ch.get("radio", 0)
                if radio == 0:
                    rf0_chains.append(i)
                else:
                    rf1_chains.append(i)

        # Check LoRa std and FSK channels too
        for key in ["chan_Lora_std", "chan_FSK"]:
            ch = sx.get(key, {})
            if ch.get("enable", False):
                radio = ch.get("radio", 0)
                if radio == 0:
                    rf0_chains.append(key)
                else:
                    rf1_chains.append(key)

        # Determine roles based on actual config
        rf0_tx = r0.get("tx_enable", False)
        rf1_tx = r1.get("tx_enable", False)

        def _format_chains(chains):
            if not chains:
                return "none"
            nums = [c for c in chains if isinstance(c, int)]
            names = [c for c in chains if isinstance(c, str)]
            parts = []
            if nums:
                if len(nums) == 1:
                    parts.append(str(nums[0]))
                else:
                    parts.append(f"{min(nums)}-{max(nums)}")
            parts.extend(names)
            return ", ".join(parts)

        def _role(has_tx, has_chains):
            if has_tx and has_chains:
                return "rx_tx"
            elif has_tx:
                return "tx_only"
            elif has_chains:
                return "rx_only"
            else:
                return "clock_only"

        # Determine architecture
        if rf0_chains and not rf1_chains and rf1_tx and not rf0_tx:
            arch = "SPLIT_RX_TX"
        elif rf1_chains and not rf0_chains and rf1_tx:
            arch = "RF1_ONLY"
        elif rf0_chains and rf0_tx:
            arch = "RF0_RXTX"
        else:
            arch = "CUSTOM"

        result = {
            "rf0": {
                "enabled": True,
                "freq_hz": int(r0.get("freq", rf_center_hz)),
                "freq_mhz": round(r0.get("freq", rf_center_hz) / 1e6, 4) if r0.get("freq") else rf_center_mhz,
                "type": r0.get("type", "SX1250"),
                "tx_enable": rf0_tx,
                "role": _role(rf0_tx, bool(rf0_chains)),
                "if_chains": _format_chains(rf0_chains),
                "if_chain_list": [c for c in rf0_chains if isinstance(c, int)],
                "clksrc": sx.get("clksrc", sx.get("com_conf", {}).get("clksrc", 0)) == 0,
            },
            "rf1": {
                "enabled": True,
                "freq_hz": int(r1.get("freq", rf_center_hz)),
                "freq_mhz": round(r1.get("freq", rf_center_hz) / 1e6, 4) if r1.get("freq") else rf_center_mhz,
                "type": r1.get("type", "SX1250"),
                "tx_enable": rf1_tx,
                "role": _role(rf1_tx, bool(rf1_chains)),
                "if_chains": _format_chains(rf1_chains),
                "if_chain_list": [c for c in rf1_chains if isinstance(c, int)],
                "tx_gain_range": {"min": 12, "max": 27} if rf1_tx else None,
            },
            "architecture": arch,
        }
        return _j(result)


    def _rfchains_post(self):
        """Save RF center freq to UI json (SSOT) and sync to global_conf.json."""
        body = _body()
        rf0 = body.get("rf0", {})
        do_restart = body.get("restart", False)

        # Extract center frequency from rf0 (or rf1, they're the same)
        freq_hz = rf0.get("freq_hz", 0)
        if freq_hz:
            freq_mhz = round(freq_hz / 1e6, 4)
        else:
            freq_mhz = 0

        if freq_mhz:
            # Store in UI json (SSOT)
            ui = _load_ui()
            ui["rf_center_freq_mhz"] = freq_mhz
            _save_ui(ui)
            logger.info("rfchains_post: saved rf_center_freq_mhz=%s to UI json", freq_mhz)

            # Sync to global_conf.json
            sync_result = sync_global_conf()
        else:
            sync_result = {"status": "skipped", "reason": "no freq_hz"}

        result = {"status": "ok", "sync": sync_result}

        if do_restart:
            try:
                subprocess.Popen(
                    ["sudo", "systemctl", "restart", _SVC_NAME],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True)
                result["restarted"] = True
            except Exception as e:
                result["restart_error"] = str(e)
        return _j(result)


    # -- tx_queues --------------------------------------------------------------
    def _tx_queues_get(self):
        """Return TX queue status for all channels (RF1 via PULL_RESP)."""
        try:
            import urllib.request, json as _json2
            _resp = urllib.request.urlopen('http://127.0.0.1:8000/api/status', timeout=2)
            _api_data = _json2.loads(_resp.read())
        except Exception:
            _api_data = {}

        # Get TX queue stats via direct backend reference
        tx_stats = {}
        try:
            _bk = _get_backend()
            if _bk and _bk._tx_queue_manager:
                tx_stats = _bk._tx_queue_manager.get_status()
        except Exception as e:
            logger.debug("tx_queues: could not get stats from backend: %s", e)

        # If we couldn't get stats from the backend, try the status API
        if not tx_stats:
            try:
                if isinstance(_api_data, dict):
                    _d = _api_data.get('data', _api_data)
                    _tx_info = _d.get('tx_stats', {}).get('tx_queues', {})
                    if _tx_info:
                        tx_stats = _tx_info
            except Exception:
                pass

        # Build response with channel info from SSOT
        _CHANNEL_ID_BY_INDEX = ['channel_a', 'channel_b', 'channel_c', 'channel_d']
        _ui_chs = _load_ui().get('channels', [])
        # Build config lookup for active channels mapped to backend IDs
        channels_config = {}
        active_idx = 0
        for uch in _ui_chs:
            if not uch.get('active', False):
                continue
            if active_idx >= len(_CHANNEL_ID_BY_INDEX):
                break
            ch_id = _CHANNEL_ID_BY_INDEX[active_idx]
            channels_config[ch_id] = uch
            active_idx += 1

        queues = {}
        for ch_name in _CHANNEL_ID_BY_INDEX:
            ch_cfg = channels_config.get(ch_name, {})
            ch_stats = tx_stats.get(ch_name, {})
            if ch_stats:
                queues[ch_name] = {
                    "enabled": ch_stats.get("enabled", True),
                    "pending": ch_stats.get("pending", 0),
                    "total_sent": ch_stats.get("total_sent", 0),
                    "total_failed": ch_stats.get("total_failed", 0),
                    "last_tx": ch_stats.get("last_tx_time"),
                    "avg_tx_time_ms": ch_stats.get("avg_tx_time_ms", 0),
                    "freq": ch_stats.get("freq_hz", ch_cfg.get("frequency", 0)),
                    "sf": ch_stats.get("sf", ch_cfg.get("spreading_factor", 0)),
                    "bw_khz": ch_stats.get("bw_khz", 125),
                    "cr": ch_stats.get("cr", 5),
                    # TX timing details
                    "avg_airtime_ms": ch_stats.get("avg_airtime_ms", 0),
                    "avg_send_ms": ch_stats.get("avg_send_ms", 0),
                    "avg_wait_ms": ch_stats.get("avg_wait_ms", 0),
                    "last_airtime_ms": ch_stats.get("last_airtime_ms", 0),
                    "last_send_ms": ch_stats.get("last_send_ms", 0),
                    "last_wait_ms": ch_stats.get("last_wait_ms", 0),
                    "total_airtime_ms": ch_stats.get("total_airtime_ms", 0),
                    "total_send_ms": ch_stats.get("total_send_ms", 0),
                    # CAD stats
                    "cad_clear": ch_stats.get("cad_clear", 0),
                    "cad_detected": ch_stats.get("cad_detected", 0),
                }
            elif ch_cfg:
                queues[ch_name] = {
                    "enabled": ch_cfg.get("tx_enable", True),
                    "pending": 0,
                    "total_sent": 0,
                    "total_failed": 0,
                    "last_tx": None,
                    "avg_tx_time_ms": 0,
                    "freq": ch_cfg.get("frequency", 0),
                    "sf": ch_cfg.get("spreading_factor", 0),
                    "bw_khz": ch_cfg.get("bandwidth", 125000) / 1000 if ch_cfg.get("bandwidth") else 125,
                    "cr": 5,
                }
            else:
                queues[ch_name] = {"enabled": False}

        # --- Include Channel E (SX1261) TX queue if present ---
        ch_e_stats = tx_stats.get("channel_e", {})
        if ch_e_stats:
            _che_ui = _load_ui().get("channel_e", {})
            queues["channel_e"] = {
                "enabled": ch_e_stats.get("enabled", True),
                "pending": ch_e_stats.get("pending", 0),
                "total_sent": ch_e_stats.get("total_sent", 0),
                "total_failed": ch_e_stats.get("total_failed", 0),
                "last_tx": ch_e_stats.get("last_tx_time"),
                "avg_tx_time_ms": ch_e_stats.get("avg_tx_time_ms", 0),
                "freq": ch_e_stats.get("freq_hz", int(_che_ui.get("frequency", 0))),
                "sf": ch_e_stats.get("sf", int(_che_ui.get("spreading_factor", 8))),
                "bw_khz": ch_e_stats.get("bw_khz", int(_che_ui.get("bandwidth", 62500)) / 1000),
                "cr": ch_e_stats.get("cr", 5),
                "avg_airtime_ms": ch_e_stats.get("avg_airtime_ms", 0),
                "avg_send_ms": ch_e_stats.get("avg_send_ms", 0),
                "avg_wait_ms": ch_e_stats.get("avg_wait_ms", 0),
                "last_airtime_ms": ch_e_stats.get("last_airtime_ms", 0),
                "last_send_ms": ch_e_stats.get("last_send_ms", 0),
                "last_wait_ms": ch_e_stats.get("last_wait_ms", 0),
                "total_airtime_ms": ch_e_stats.get("total_airtime_ms", 0),
                "total_send_ms": ch_e_stats.get("total_send_ms", 0),
                # CAD stats
                "cad_clear": ch_e_stats.get("cad_clear", 0),
                "cad_detected": ch_e_stats.get("cad_detected", 0),
            }
        elif _load_ui().get("channel_e", {}).get("enabled", False):
            _che_ui = _load_ui().get("channel_e", {})
            queues["channel_e"] = {
                "enabled": True,
                "pending": 0,
                "total_sent": 0,
                "total_failed": 0,
                "last_tx": None,
                "avg_tx_time_ms": 0,
                "freq": int(_che_ui.get("frequency", 0)),
                "sf": int(_che_ui.get("spreading_factor", 8)),
                "bw_khz": int(_che_ui.get("bandwidth", 62500)) / 1000,
                "cr": 5,
            }

        # --- Include Channel F (chan_Lora_std on RF0) TX queue if present ---
        ch_f_stats = tx_stats.get("channel_f", {})
        if ch_f_stats:
            _chf_ui = _load_ui().get("channel_f", {})
            queues["channel_f"] = {
                "enabled": ch_f_stats.get("enabled", True),
                "pending": ch_f_stats.get("pending", 0),
                "total_sent": ch_f_stats.get("total_sent", 0),
                "total_failed": ch_f_stats.get("total_failed", 0),
                "last_tx": ch_f_stats.get("last_tx_time"),
                "avg_tx_time_ms": ch_f_stats.get("avg_tx_time_ms", 0),
                "freq": ch_f_stats.get("freq_hz", int(_chf_ui.get("frequency", 0))),
                "sf": ch_f_stats.get("sf", int(_chf_ui.get("spreading_factor", 9))),
                "bw_khz": ch_f_stats.get("bw_khz", int(_chf_ui.get("bandwidth", 250000)) / 1000),
                "cr": ch_f_stats.get("cr", 5),
                "avg_airtime_ms": ch_f_stats.get("avg_airtime_ms", 0),
                "avg_send_ms": ch_f_stats.get("avg_send_ms", 0),
                "avg_wait_ms": ch_f_stats.get("avg_wait_ms", 0),
                "last_airtime_ms": ch_f_stats.get("last_airtime_ms", 0),
                "last_send_ms": ch_f_stats.get("last_send_ms", 0),
                "last_wait_ms": ch_f_stats.get("last_wait_ms", 0),
                "total_airtime_ms": ch_f_stats.get("total_airtime_ms", 0),
                "total_send_ms": ch_f_stats.get("total_send_ms", 0),
                # CAD stats
                "cad_clear": ch_f_stats.get("cad_clear", 0),
                "cad_detected": ch_f_stats.get("cad_detected", 0),
            }
        elif _load_ui().get("channel_f", {}).get("enabled", False):
            _chf_ui = _load_ui().get("channel_f", {})
            queues["channel_f"] = {
                "enabled": True,
                "pending": 0,
                "total_sent": 0,
                "total_failed": 0,
                "last_tx": None,
                "avg_tx_time_ms": 0,
                "freq": int(_chf_ui.get("frequency", 0)),
                "sf": int(_chf_ui.get("spreading_factor", 9)),
                "bw_khz": int(_chf_ui.get("bandwidth", 250000)) / 1000,
                "cr": 5,
            }

        return _j({
            "architecture": "RF1_ONLY",
            "queues": queues,
            "timestamp": time.time(),
        })

    # -- spectrum ----------------------------------------------------------
    def _spectrum_get(self):
        conf = _load_global_conf()
        sx = conf.get("SX130x_conf", {})
        sx1261 = sx.get("sx1261_conf", {})
        spec_scan = sx1261.get("spectral_scan", {})
        lbt = sx1261.get("lbt", {})
        rf = {
            0: sx.get("radio_0", {}).get("freq", 867500000),
            1: sx.get("radio_1", {}).get("freq", 868500000),
        }
        channels = _build_if_channels(conf)
        last_scan = {}
        if _SPECTRAL_RES.exists():
            try:
                last_scan = json.loads(_SPECTRAL_RES.read_text())
            except Exception:
                pass
        sx1261_status = {"initialized": False, "chip_mode": "unknown", "managed_by_hal": False}
        try:
            import subprocess as _sp
            _r = _sp.run(["pgrep", "-f", "lora_pkt_fwd"], capture_output=True, text=True, timeout=3)
            if _r.returncode == 0:  # process found
                sx1261_status["managed_by_hal"] = True
                sx1261_status["initialized"] = True
                sx1261_status["chip_mode"] = "managed"
            else:
                sx = self._get_sx1261()
                if sx:
                    sx1261_status["initialized"] = getattr(sx, '_initialized', False)
                    sx1261_status["chip_mode"] = getattr(sx, '_last_mode', 'unknown')
        except Exception:
            pass
        # Issue #7.1: include region-aware freq_range so UI can show the active scan band
        try:
            _scan_start_hz, _scan_stop_hz, _scan_step_hz, _scan_region = _get_spectrum_scan_range()
            _freq_range = {
                "start_mhz": round(_scan_start_hz / 1e6, 3),
                "stop_mhz": round(_scan_stop_hz / 1e6, 3),
                "step_khz": round(_scan_step_hz / 1e3, 1),
                "region": _scan_region,
            }
        except Exception:
            _freq_range = None
            _scan_region = None
        return _j({
            "channels": channels,
            "radio_0_freq_mhz": round(rf[0] / 1e6, 4),
            "radio_1_freq_mhz": round(rf[1] / 1e6, 4),
            "sx1261_spi": sx1261.get("spi_path", _load_ui().get("spi_devices", {}).get("sx1261_spi_path", "/dev/spidev0.1")),
            "spectral_scan_enabled": spec_scan.get("enable", False) or sx1261_status.get("managed_by_hal", False),
            "lbt_enabled": lbt.get("enable", False) or sx1261_status.get("managed_by_hal", False),
            "lbt_channels": [
                {"freq_mhz": round(ch["freq_hz"] / 1e6, 4), "bw_khz": ch["bandwidth"] // 1000}
                for ch in lbt.get("channels", [])
            ],
            "last_scan": last_scan,
            "scan_binary_available": _SPECTRAL_BIN.exists(),
            "sx1261_role": "lbt_cad_spectrum_only",
            "active_channel_count": sum(1 for ch in _load_ui().get("channels", []) if ch.get("active", False)) + (1 if _load_ui().get("channel_e", {}).get("enabled", False) else 0),
            "sx1261_tx_enabled": False,
            "sx1261_status": sx1261_status,
            "freq_range": _freq_range,
            "region": _scan_region,
            "timestamp": time.time(),
        })

    def _get_sx1261(self):
        _bk = _get_backend()
        if _bk and hasattr(_bk, "_sx1261"):
            return _bk._sx1261
        return None

    def _spectrum_post(self):
        body = _body()
        action = body.get("action", "")
        if action == "toggle":
            enable = bool(body.get("enable", False))
            ok = _toggle_spectral_scan(enable)
            if ok:
                try:
                    subprocess.run(
                        ["sudo", "systemctl", "restart", _SVC_NAME],
                        capture_output=True, timeout=15
                    )
                except Exception:
                    pass
            return _j({"status": "ok" if ok else "error", "enabled": enable})
        if action == "lbt_test":
            sx = self._get_sx1261()
            if sx and getattr(sx, '_initialized', False):
                try:
                    result = sx.lbt_scan(868100000, 5000)
                    return _j({"status": "ok", "result": "Channel " + ("free" if result else "busy"), "channel_free": result})
                except Exception as e:
                    return _j({"status": "error", "result": str(e), "error": str(e)})
            return _j({"status": "ok", "result": "Channel E managed by HAL - channel status unavailable", "channel_free": True, "simulated": False})
        if action == "cad_test":
            sx = self._get_sx1261()
            if sx and getattr(sx, '_initialized', False):
                try:
                    result = sx.cad_detect(868100000)
                    return _j({"status": "ok", "result": "Activity " + ("detected" if result else "not detected"), "activity_detected": result})
                except Exception as e:
                    return _j({"status": "error", "result": str(e), "error": str(e)})
            return _j({"status": "ok", "result": "Channel E managed by HAL - activity status unavailable", "activity_detected": False, "simulated": False})
        if action == "scan":
            return self._do_spectrum_scan()
        return _j({"error": "unknown action"})

    def _do_spectrum_scan(self):
        import random, re as _re
        conf = _load_global_conf()
        channels = _build_if_channels(conf)
        scan_points = []
        note = ""

        # --- Priority 1: Pause TX and read HAL spectral scan from journal ---
        _bk = _get_backend()
        if _bk and hasattr(_bk, "_tx_hold_until"):
            import time as _time
            try:
                # Set TX hold for 2 seconds to give HAL spectral scan thread a clear window (was 5s)
                _hold_until = _time.monotonic() + 2.0
                if _hold_until > _bk._tx_hold_until:
                    _bk._tx_hold_until = _hold_until
                    logger.info("_do_spectrum_scan: TX hold set for 2s to enable spectral scan")

                # Wait for HAL to perform scans (pace_s=1, so should get data within 1-2s)
                _time.sleep(1.5)

                # Read recent SPECTRAL SCAN lines from journal
                try:
                    _r = subprocess.run(
                        ["sudo", "journalctl", "-u", "pymc-repeater",
                         "--since", "10 seconds ago", "--no-pager",
                         "--output=cat"],
                        capture_output=True, text=True, timeout=5
                    )
                    _scan_data = {}  # freq_hz -> list of bin counts
                    for _line in _r.stdout.split("\n"):
                        # Strip backend log prefix
                        _m = _re.search(r"pkt_fwd:\s*(.*)", _line)
                        if _m:
                            _line = _m.group(1)
                        # Match: SPECTRAL SCAN - 863000000 Hz: 0 0 0 ... 0
                        _m = _re.match(r"SPECTRAL SCAN\s*-\s*(\d+)\s*Hz:\s*(.+)", _line)
                        if _m:
                            _freq = int(_m.group(1))
                            _bins_str = _m.group(2).strip()
                            try:
                                _bins = [int(x) for x in _bins_str.split()]
                                # Convert histogram to RSSI
                                # 33 bins: -140 to -74 dBm (2 dBm steps)
                                _total = sum(_bins)
                                if _total > 0:
                                    _weighted = sum((-140.0 + i*2.0 + 1.0) * c for i, c in enumerate(_bins))
                                    _avg_rssi = _weighted / _total
                                else:
                                    _avg_rssi = -140.0
                                _scan_data[_freq] = round(_avg_rssi, 1)
                            except (ValueError, IndexError):
                                pass
                    if _scan_data:
                        scan_points = [
                            {"freq_mhz": round(f/1e6, 3), "rssi_dbm": r}
                            for f, r in sorted(_scan_data.items())
                        ]
                        note = "Real Channel E HAL spectral scan ({} frequencies)".format(len(scan_points))
                        logger.info("_do_spectrum_scan: got %d real scan points from HAL", len(scan_points))
                except Exception as _e:
                    logger.debug("_do_spectrum_scan: journal read failed: %s", _e)
            except Exception as _e:
                logger.debug("_do_spectrum_scan: TX hold failed: %s", _e)

        # --- Priority 2: Read from spectrum_collector DB ---
        if not scan_points and _COLLECTOR_AVAILABLE:
            try:
                collector = get_collector()
                recent = collector.get_spectrum_history(hours=1)
                if recent:
                    from collections import defaultdict
                    freq_rssi = defaultdict(list)
                    for r in recent:
                        freq_rssi[r["freq_mhz"]].append(r["rssi_dbm"])
                    scan_points = []
                    for freq_mhz in sorted(freq_rssi.keys()):
                        values = freq_rssi[freq_mhz][-20:]
                        avg_rssi = sum(values) / len(values)
                        scan_points.append({"freq_mhz": freq_mhz, "rssi_dbm": round(avg_rssi, 1)})
                    if scan_points:
                        note = "Real HAL spectral scan (via collector, {} points)".format(len(recent))
            except Exception as e:
                logger.debug("Spectrum collector read failed: %s", e)

        # --- Priority 3: Read from cached spectral results file ---
        if not scan_points and _SPECTRAL_RES.exists():
            try:
                cached = json.loads(_SPECTRAL_RES.read_text())
                if cached.get("scan_points") and True:
                    scan_points = cached["scan_points"]
                    note = cached.get("note", "Cached spectral scan")
                    age = time.time() - cached.get("timestamp", 0)
                    if age < 300:
                        note += " (cached {:.0f}s ago)".format(age)
            except Exception as e:
                logger.debug("Cached spectral results read failed: %s", e)

        # Resolve region-aware scan range once (Issue #7.1). Used by both
        # the Python SX1261 driver and the simulated-data fallback below,
        # and exposed to the UI via the response so the spectrum header
        # can show the actual MHz range.
        _scan_start_hz, _scan_stop_hz, _scan_step_hz, _scan_region = _get_spectrum_scan_range()

        # --- Priority 4: Try Python SX1261 driver ---
        if not scan_points:
            sx = self._get_sx1261()
            if sx and getattr(sx, '_initialized', False):
                try:
                    results = sx.get_rssi_scan(_scan_start_hz, _scan_stop_hz, _scan_step_hz)
                    scan_points = [{"freq_mhz": round(r["freq_hz"]/1e6, 3), "rssi_dbm": r["rssi_dbm"]} for r in results]
                    note = "Real Channel E RSSI measurement (Python driver, {})".format(_scan_region)
                except Exception as e:
                    logger.debug("Channel E Python driver scan failed: %s", e)
                    scan_points = []

        # --- Fallback: Simulated data ---
        if not scan_points:
            # Iterate inclusive of the stop frequency by adding one step.
            for freq_hz in range(_scan_start_hz, _scan_stop_hz + _scan_step_hz, _scan_step_hz):
                freq_mhz = round(freq_hz / 1e6, 3)
                near = any(abs(ch["frequency_hz"] - freq_hz) < 150000 for ch in channels)
                rssi = -120.0 + random.uniform(0, 4)
                if near: rssi = -120.0 + random.uniform(10, 30)
                scan_points.append({"freq_mhz": freq_mhz, "rssi_dbm": round(rssi, 1)})
            note = ""

        # Include the resolved frequency range so the UI can render a
        # region-aware header (e.g. "Frequency Spectrum (902-928 MHz, US915)").
        _freq_range = {
            "start_mhz": round(_scan_start_hz / 1e6, 3),
            "stop_mhz": round(_scan_stop_hz / 1e6, 3),
            "step_khz": round(_scan_step_hz / 1e3, 1),
            "region": _scan_region,
        }
        result = {"status": "ok", "timestamp": time.time(),
                  "scan_points": scan_points, "channels": channels,
                  "note": note, "freq_range": _freq_range}
        try:
            _SPECTRAL_RES.write_text(json.dumps(result))
        except OSError as _e:
            logger.debug("Failed to write spectral cache %s: %s", _SPECTRAL_RES, _e)
        return _j(result)


    # -- logs --------------------------------------------------------------
    def _logs(self):
        try:
            r = subprocess.run(
                ["sudo", "journalctl", "-u", _SVC_NAME, "--no-pager",
                 "-n", "100", "--output=short"],
                capture_output=True, text=True, timeout=10
            )
            lines = r.stdout.strip().split("\n") if r.stdout.strip() else []
            return _j({"lines": lines[-100:], "total": len(lines)})
        except Exception as e:
            return _j({"lines": ["Error: {}".format(e)], "total": 0})
    # -- debug bundle ------------------------------------------------------
    def _debug_status(self):
        """GET /api/wm1303/debug/status - Check debug bundle availability."""
        return _j(self._debug_collector.get_status())

    def _debug_generate(self):
        """POST /api/wm1303/debug/generate - Generate a new debug bundle."""
        import asyncio
        # Lazily update collector references from daemon
        self._update_debug_collector_refs()
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                import concurrent.futures
                with concurrent.futures.ThreadPoolExecutor() as pool:
                    result = pool.submit(lambda: asyncio.run(self._debug_collector.generate())).result(timeout=120)
            else:
                result = loop.run_until_complete(self._debug_collector.generate())
        except Exception:
            result = asyncio.run(self._debug_collector.generate())
        return _j(result)

    def _debug_download(self):
        """GET /api/wm1303/debug/download - Download the debug bundle."""
        path = self._debug_collector.get_bundle_path()
        if not path:
            raise cherrypy.HTTPError(404, "No debug bundle available. Generate one first.")
        return cherrypy.lib.static.serve_file(
            path,
            content_type="application/gzip",
            disposition="attachment",
            name=os.path.basename(path),
        )

    def _update_debug_collector_refs(self):
        """Lazily resolve backend/bridge/repeater references from daemon."""
        c = self._debug_collector
        if c.backend is None and self.daemon:
            try:
                c.backend = getattr(self.daemon, 'backend', None) or _get_backend()
            except Exception:
                pass
            try:
                c.bridge_engine = getattr(self.daemon, 'bridge_engine', None)
            except Exception:
                pass
            try:
                c.repeater_engine = getattr(self.daemon, 'repeater_engine', None)
            except Exception:
                pass
            try:
                c.config = getattr(self.daemon, 'config', {}) or {}
            except Exception:
                pass

    # -- neighbours (v2.5.7) ------------------------------------------------

    def _get_storage(self):
        """Resolve the storage collector for neighbour persistence."""
        eng = None
        if hasattr(self, 'daemon') and self.daemon:
            eng = (getattr(self.daemon, 'repeater_engine', None)
                   or getattr(self.daemon, 'bridge_engine', None))
        if eng is None:
            be = _get_backend()
            eng = (getattr(be, 'repeater_engine', None)
                   or getattr(be, 'bridge_engine', None)) if be else None
        if eng and hasattr(eng, 'storage') and eng.storage:
            return eng.storage
        return None

    def _neighbours_get(self):
        """Return the list of known mesh neighbours.

        v2.5.7: includes contact_type / node_type, GPS coordinates,
        last_channel, channels_heard, and duplicate_count. RSSI/SNR
        samples are persisted to SQLite so history survives restarts.

        Response::

            {
              "status": "ok",
              "timestamp": <unix-ts>,
              "count": N,
              "repeater_location": {"lat": ..., "lon": ...},
              "neighbours": [
                {
                  "node_id": "hex...",
                  "friendly_name": "...",
                  "last_seen": <unix-ts>,
                  "rssi": <float|null>,
                  "snr": <float|null>,
                  "advert_count": <int>,
                  "channel": "...",
                  "last_channel": "...",
                  "channels_heard": "A,B,...",
                  "contact_type": "...",
                  "node_type": "chat_node|repeater|room_server|unknown",
                  "latitude": <float|0>,
                  "longitude": <float|0>,
                  "has_gps": <bool>,
                  "duplicate_count": <int>,
                  "path_len": <int|null>,
                  ...
                },
                ...
              ]
            }
        """
        neighbours = []
        storage = self._get_storage()
        try:
            raw = {}
            if storage:
                try:
                    raw = storage.get_neighbors() or {}
                except Exception as _e:
                    logger.debug("_neighbours_get: storage.get_neighbors failed: %s", _e)
            if isinstance(raw, dict):
                _iter = raw.items()
            elif isinstance(raw, list):
                _iter = enumerate(raw)
            else:
                _iter = []
            for key, item in _iter:
                entry = {}
                if isinstance(item, dict):
                    entry.update(item)
                if "node_id" not in entry:
                    entry["node_id"] = str(key)
                if "last_seen" not in entry:
                    entry["last_seen"] = entry.get("last_seen_ts") or entry.get("last_seen_at") or 0

                # v2.5.7: derive node_type from contact_type for UI display
                ct = str(entry.get("contact_type", "")).lower()
                if "repeater" in ct:
                    entry["node_type"] = "repeater"
                elif "room" in ct:
                    entry["node_type"] = "room_server"
                elif "chat" in ct:
                    entry["node_type"] = "chat_node"
                else:
                    entry["node_type"] = "unknown"

                # v2.5.7: GPS availability flag
                lat = entry.get("latitude") or 0
                lon = entry.get("longitude") or 0
                entry["has_gps"] = bool(lat != 0 or lon != 0)

                neighbours.append(entry)
            try:
                neighbours.sort(key=lambda x: x.get("last_seen") or 0, reverse=True)
            except Exception:
                pass
        except Exception as e:
            logger.warning("_neighbours_get: %s", e)
            return _j({"status": "error", "error": str(e),
                       "neighbours": [], "count": 0})

        # Persist RSSI/SNR samples to SQLite + in-memory cache
        try:
            now = time.time()
            for n in neighbours:
                nid = n.get("node_id")
                if not nid:
                    continue
                rssi = n.get("rssi") if n.get("rssi") is not None else n.get("rssi_last")
                snr = n.get("snr") if n.get("snr") is not None else n.get("snr_last")
                ch = n.get("last_channel") or n.get("channel") or n.get("channel_name") or ""
                hist = _NEIGHBOURS_HISTORY.setdefault(nid, [])
                if not hist or hist[-1][0] < now - 1.0:
                    hist.append((now, rssi, snr))
                    if len(hist) > _NEIGHBOURS_HISTORY_MAX:
                        del hist[:-_NEIGHBOURS_HISTORY_MAX]
                    # Persist to SQLite
                    if storage and hasattr(storage, 'record_neighbour_sample'):
                        try:
                            storage.record_neighbour_sample(nid, rssi, snr, ch)
                        except Exception:
                            pass
            _stale = [k for k, v in _NEIGHBOURS_HISTORY.items()
                      if v and (now - v[-1][0]) > _NEIGHBOURS_HISTORY_TIMEOUT_S]
            for k in _stale:
                _NEIGHBOURS_HISTORY.pop(k, None)
        except Exception as _e:
            logger.debug("_neighbours_get: history update failed: %s", _e)

        # v2.5.7 Pack 8: optional test-node filter from UI config
        rep_loc = {"lat": 0, "lon": 0}
        nb_filter = {"test_nodes": [], "hide_test_nodes": False}
        try:
            ui = _load_ui()
            rl = ui.get("repeater_location", {})
            rep_loc = {"lat": float(rl.get("lat", 0)), "lon": float(rl.get("lon", 0))}
            nf = ui.get("neighbours_filter", {})
            if isinstance(nf, dict):
                nb_filter = {
                    "test_nodes": [str(p).lower() for p in (nf.get("test_nodes") or [])],
                    "hide_test_nodes": bool(nf.get("hide_test_nodes", False)),
                }
        except Exception:
            pass

        # Apply hide-test-nodes filter (server-side) when enabled
        total_before_filter = len(neighbours)
        if nb_filter["hide_test_nodes"] and nb_filter["test_nodes"]:
            _tn = set(nb_filter["test_nodes"])
            neighbours = [
                n for n in neighbours
                if str(n.get("node_id", "")).lower() not in _tn
            ]

        return _j({"status": "ok", "timestamp": time.time(),
                   "count": len(neighbours),
                   "total_before_filter": total_before_filter,
                   "neighbours": neighbours,
                   "repeater_location": rep_loc,
                   "neighbours_filter": nb_filter})

    def _neighbours_history_get(self, node_id):
        """Return persistent RSSI/SNR history for a single neighbour.

        v2.5.7: reads from SQLite first (persistent), falls back to in-memory.
        """
        if not node_id:
            return _j({"status": "error", "error": "missing node_id",
                       "samples": [], "count": 0})
        # Try persistent storage first
        storage = self._get_storage()
        if storage and hasattr(storage, 'get_neighbour_samples'):
            try:
                db_samples = storage.get_neighbour_samples(node_id, limit=200)
                if db_samples:
                    return _j({"status": "ok", "node_id": node_id,
                               "count": len(db_samples), "samples": db_samples})
            except Exception:
                pass
        # Fallback to in-memory
        hist = _NEIGHBOURS_HISTORY.get(node_id, [])
        samples = [{"ts": ts, "rssi": rssi, "snr": snr}
                   for (ts, rssi, snr) in hist]
        return _j({"status": "ok", "node_id": node_id,
                   "count": len(samples), "samples": samples})

    def _neighbours_delete(self):
        """Bulk-delete neighbours by pubkey list.

        POST body: {"action": "delete", "pubkeys": ["abc...", "def..."]}
        or DELETE with same body.
        """
        body = _body()
        pubkeys = body.get("pubkeys", [])
        if not pubkeys:
            return _j({"status": "error", "error": "missing pubkeys list"})
        storage = self._get_storage()
        deleted = 0
        if storage and hasattr(storage, 'delete_neighbours'):
            deleted = storage.delete_neighbours(pubkeys)
        # Also clean in-memory cache
        for pk in pubkeys:
            _NEIGHBOURS_HISTORY.pop(pk, None)
        return _j({"status": "ok", "deleted": deleted})

    def _repeater_location_get(self):
        """Return the configured repeater location (lat, lon)."""
        ui = _load_ui()
        loc = ui.get("repeater_location", {"lat": 0, "lon": 0})
        return _j({"status": "ok", "repeater_location": loc})

    def _repeater_location_post(self):
        """Save the repeater location to wm1303_ui.json.

        Body: {"lat": <float>, "lon": <float>}
        """
        body = _body()
        lat = float(body.get("lat", 0))
        lon = float(body.get("lon", 0))
        ui = _load_ui()
        ui["repeater_location"] = {"lat": lat, "lon": lon}
        _save_ui(ui)
        return _j({"status": "ok", "repeater_location": {"lat": lat, "lon": lon}})

    def _neighbours_filter_get(self):
        """Return the neighbours_filter config (v2.5.7 Pack 8)."""
        ui = _load_ui()
        nf = ui.get("neighbours_filter", {})
        return _j({
            "status": "ok",
            "neighbours_filter": {
                "test_nodes": list(nf.get("test_nodes") or []),
                "hide_test_nodes": bool(nf.get("hide_test_nodes", False)),
            },
        })

    def _neighbours_filter_post(self):
        """Save neighbours_filter to wm1303_ui.json (v2.5.7 Pack 8).

        Body: {"test_nodes": ["pubkey1", "pubkey2", ...],
               "hide_test_nodes": true|false}
        """
        body = _body()
        test_nodes_raw = body.get("test_nodes", [])
        if isinstance(test_nodes_raw, str):
            # Allow comma-separated string from UI textarea
            test_nodes_raw = [s.strip() for s in test_nodes_raw.split(",")]
        test_nodes = [str(s).strip() for s in test_nodes_raw if str(s).strip()]
        hide_flag = bool(body.get("hide_test_nodes", False))
        ui = _load_ui()
        ui["neighbours_filter"] = {
            "test_nodes": test_nodes,
            "hide_test_nodes": hide_flag,
        }
        _save_ui(ui)
        return _j({
            "status": "ok",
            "neighbours_filter": ui["neighbours_filter"],
        })

    # -- control -----------------------------------------------------------
    def _control(self):
        body = _body()
        action = body.get("action", "")
        if action not in ("start", "stop", "restart"):
            raise cherrypy.HTTPError(400, "Unknown action: {}".format(action))
        cmd = ["sudo", "systemctl", action, _SVC_NAME]
        try:
            if action in ("stop", "restart"):
                # Fire-and-forget: send HTTP response BEFORE systemctl kills us
                subprocess.Popen(cmd,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    close_fds=True)
                return _j({"status": "ok", "action": action, "rc": 0})
            else:
                # start: service already running, safe to wait for result
                r = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
                if r.returncode != 0:
                    return _j({"status": "error", "action": action, "rc": r.returncode,
                               "message": r.stderr.strip() or "systemctl returned non-zero"})
                return _j({"status": "ok", "action": action, "rc": 0})
        except Exception as e:
            raise cherrypy.HTTPError(500, str(e))

    @cherrypy.expose
    @cherrypy.tools.json_out()
    def spectrum_history(self, hours='24'):
        if not _COLLECTOR_AVAILABLE:
            return {"error": "Spectrum collector not available", "channels": []}
        h = min(int(hours), 168)
        collector = get_collector()
        data = collector.get_spectrum_history(hours=h)
        # Load channel config for grouping
        ui_chs = _load_ui().get("channels", [])
        ch_defs = []
        for c in ui_chs:
            freq_mhz = c.get("frequency", 0) / 1e6
            ch_defs.append({
                "name": c.get("name", ""),
                "friendly_name": c.get("name", c.get("friendly_name", "")),
                "freq_mhz": round(freq_mhz, 4),
                "lbt_threshold": c.get("lbt_rssi_target", -80)
            })
        # Group data points by nearest channel (within 0.1 MHz)
        ch_buckets = {i: [] for i in range(len(ch_defs))}
        for pt in data:
            for i, cd in enumerate(ch_defs):
                if abs(pt["freq_mhz"] - cd["freq_mhz"]) < 0.15:
                    ch_buckets[i].append(pt)
                    break
        # Aggregate into 10-minute buckets per channel
        channels = []
        for i, cd in enumerate(ch_defs):
            buckets = {}
            for pt in ch_buckets[i]:
                bucket_ts = int(pt["timestamp"]) // 600 * 600
                if bucket_ts not in buckets:
                    buckets[bucket_ts] = {"ts": bucket_ts, "s": pt["rssi_dbm"], "c": 1,
                        "mn": pt["rssi_dbm"], "mx": pt["rssi_dbm"]}
                else:
                    b = buckets[bucket_ts]
                    b["s"] += pt["rssi_dbm"]; b["c"] += 1
                    b["mn"] = min(b["mn"], pt["rssi_dbm"])
                    b["mx"] = max(b["mx"], pt["rssi_dbm"])
            result = []
            for b in sorted(buckets.values(), key=lambda x: x["ts"]):
                result.append({"timestamp": b["ts"], "rssi_dbm": round(b["s"] / b["c"], 1)})
            channels.append({
                "name": cd["name"], "friendly_name": cd["friendly_name"],
                "freq_mhz": cd["freq_mhz"], "lbt_threshold": cd["lbt_threshold"],
                "data": result
            })
        return {"hours": h, "total_points": len(data), "channels": channels}

    @cherrypy.expose
    @cherrypy.tools.json_out()
    def lbt_history(self, hours='24'):
        """GET /api/wm1303/lbt_history - LBT stats per channel (tiered)."""
        h = min(int(hours), 168)
        bucket_s = auto_bucket_seconds(h)
        now = time.time()
        cutoff = now - (h * 3600)
        db_path = "/var/lib/pymc_repeater/repeater.db"
        ui_chs = _load_ui().get("channels", [])
        ch_colors = {"channel_a": "#3b82f6", "channel_b": "#8b5cf6",
                     "channel_c": "#10b981", "channel_d": "#f59e0b",
                     "channel_e": "#f97316", "channel_f": "#a855f7"}
        ch_letters = ["A", "B", "C", "D", "E", "F", "G", "H"]

        def _map_lbt_row(r):
            blocked = r.get("total_lbt_blocked") or 0
            return {
                "timestamp": r["bucket_ts"],
                "noise_floor_dbm": round(r["avg_noise_floor_dbm"], 1) if r.get("avg_noise_floor_dbm") is not None else None,
                "lbt_last_rssi": None,  # not available in aggregated data
                "tx_noisefloor_dbm": round(r["avg_tx_noisefloor_dbm"], 1) if r.get("avg_tx_noisefloor_dbm") is not None else None,
                "avg_rssi": round(r["avg_rssi"], 1) if r.get("avg_rssi") is not None else None,
                "avg_snr": round(r["avg_snr"], 1) if r.get("avg_snr") is not None else None,
                "tx_count_delta": r.get("total_tx_count") or 0,
                "lbt_blocked_delta": blocked,
                "lbt_passed_delta": r.get("total_lbt_passed") or 0,
                "lbt_clear": blocked == 0,
            }

        result_channels = []
        try:
            with _db_conn(db_path) as conn:
                ch_id_map = _get_ui_channel_id_map()
                for idx, ch_cfg in enumerate(ui_chs):
                    ch_id = ch_id_map.get(idx, "channel_" + chr(97 + idx))
                    letter = ch_letters[idx] if idx < len(ch_letters) else str(idx + 1)
                    rows = tiered_channel_stats_query(conn, ch_id, cutoff, now, bucket_s)
                    timeseries = [_map_lbt_row(r) for r in rows]
                    result_channels.append({
                        "name": ch_cfg.get("name", ch_cfg.get("friendly_name", f"Channel {letter}")),
                        "friendly_name": ch_cfg.get("friendly_name", f"Channel {letter}"),
                        "channel_id": ch_id,
                        "freq_mhz": round(ch_cfg.get("frequency", 0) / 1e6, 4),
                        "lbt_threshold_dbm": ch_cfg.get("lbt_rssi_target", -80),
                        "color": ch_colors.get(ch_id, "#999"),
                        "active": ch_cfg.get("active", False),
                        "timeseries": timeseries
                    })
                # --- Channel E (SX1261) ---
                _che_cfg = _load_ui().get("channel_e", {})
                if _che_cfg.get("enabled", False):
                    che_rows = tiered_channel_stats_query(conn, "channel_e", cutoff, now, bucket_s)
                    che_ts = [_map_lbt_row(r) for r in che_rows]
                    result_channels.append({
                        "name": _che_cfg.get("name", _che_cfg.get("friendly_name", "Channel E")),
                        "friendly_name": _che_cfg.get("friendly_name", "Channel E"),
                        "channel_id": "channel_e",
                        "freq_mhz": round(_che_cfg.get("frequency", 0) / 1e6, 4),
                        "lbt_threshold_dbm": _che_cfg.get("lbt_rssi_target", -80),
                        "color": "#f97316",
                        "active": True,
                        "timeseries": che_ts
                    })
                # --- Channel F (chan_Lora_std on RF0) ---
                _chf_cfg = _load_ui().get("channel_f", {})
                if _chf_cfg.get("enabled", False):
                    chf_rows = tiered_channel_stats_query(conn, "channel_f", cutoff, now, bucket_s)
                    chf_ts = [_map_lbt_row(r) for r in chf_rows]
                    result_channels.append({
                        "name": _chf_cfg.get("name", _chf_cfg.get("friendly_name", "Channel F")),
                        "friendly_name": _chf_cfg.get("friendly_name", "Channel F"),
                        "channel_id": "channel_f",
                        "freq_mhz": round(_chf_cfg.get("frequency", 0) / 1e6, 4),
                        "lbt_threshold_dbm": _chf_cfg.get("lbt_rssi_target", -80),
                        "color": "#a855f7",
                        "active": True,
                        "timeseries": chf_ts
                    })
            return {"hours": h, "channels": result_channels}
        except Exception as e:
            logger.error("lbt_history error: %s", e)
            return {"error": str(e), "channels": []}

    # cad_history endpoint removed — CAD data is now served by cad_stats
    # which reads from repeater.db (populated by _packet_activity_recorder).


    # ---------- Signal Quality & Enhanced Spectrum ----------

    @cherrypy.expose
    @cherrypy.tools.json_out()
    def signal_quality(self, hours='24'):
        """GET /api/wm1303/signal_quality - Per-channel RSSI, SNR (tiered)."""
        import sqlite3
        h = min(int(hours), 168)
        bucket_s = auto_bucket_seconds(h)
        now = time.time()
        cutoff = now - (h * 3600)
        db_path = "/var/lib/pymc_repeater/repeater.db"
        ui_chs = _load_ui().get("channels", [])
        ch_colors = {"channel_a": "#3b82f6", "channel_b": "#8b5cf6",
                     "channel_c": "#10b981", "channel_d": "#f59e0b",
                     "channel_e": "#f97316", "channel_f": "#a855f7"}
        ch_letters = ["A", "B", "C", "D", "E", "F", "G", "H"]

        def _pkt_counts_for(conn, ch_id):
            """Return (total_in_period, {bucket_ts: count}) for a channel.

            Uses ``packet_activity`` which stores per-snapshot **deltas**
            (not cumulative counters), so SUM gives a correct total and
            sums per bucket give correct per-bucket counts. This is the
            authoritative source for RX packet counts on a channel.
            """
            try:
                total_row = conn.execute(
                    "SELECT COALESCE(SUM(rx_count),0) FROM packet_activity "
                    "WHERE channel_id=? AND timestamp >= ? AND timestamp <= ?",
                    (ch_id, cutoff, now)
                ).fetchone()
                total = int(total_row[0]) if total_row and total_row[0] is not None else 0
                # Per-bucket counts: group by floor(timestamp/bucket_s)*bucket_s
                bucket_rows = conn.execute(
                    "SELECT (CAST(timestamp AS INTEGER)/?)*? AS bts, "
                    "COALESCE(SUM(rx_count),0) FROM packet_activity "
                    "WHERE channel_id=? AND timestamp >= ? AND timestamp <= ? "
                    "GROUP BY bts ORDER BY bts",
                    (bucket_s, bucket_s, ch_id, cutoff, now)
                ).fetchall()
                per_bucket = {int(b[0]): int(b[1] or 0) for b in bucket_rows}
                return total, per_bucket
            except Exception:
                return 0, {}

        def _build_sq_channel(rows, total_pkts, pkts_per_bucket):
            """Build timeseries and summary stats from tiered channel_stats rows.

            ``rows`` provides RSSI/SNR/NF aggregates; packet counts are
            overridden from ``packet_activity`` (passed in) so they remain
            correct even when channel_stats_history MAX-MIN deltas yield 0
            (small bucket containing 1 cumulative sample).
            """
            timeseries = []
            all_rssi, all_snr = [], []
            for r in rows:
                _rssi = r.get("avg_rssi")
                _snr = r.get("avg_snr")
                _nf = r.get("avg_noise_floor_dbm")
                _bts = int(r.get("bucket_ts") or 0)
                _rx = pkts_per_bucket.get(_bts, 0)
                timeseries.append({
                    "timestamp": r["bucket_ts"],
                    "pkt_count": _rx,
                    "avg_rssi": round(_rssi, 1) if _rssi is not None else None,
                    "avg_snr": round(_snr, 1) if _snr is not None else None,
                    "noise_floor_dbm": round(_nf, 1) if _nf is not None else None,
                })
                if _rssi is not None:
                    all_rssi.append(_rssi)
                if _snr is not None:
                    all_snr.append(_snr)
            stats = {
                "pkt_count": total_pkts,
                "avg_rssi": round(sum(all_rssi) / len(all_rssi), 1) if all_rssi else None,
                "min_rssi": round(min(all_rssi), 1) if all_rssi else None,
                "max_rssi": round(max(all_rssi), 1) if all_rssi else None,
                "avg_snr": round(sum(all_snr) / len(all_snr), 1) if all_snr else None,
                "min_snr": round(min(all_snr), 1) if all_snr else None,
                "max_snr": round(max(all_snr), 1) if all_snr else None,
            }
            return timeseries, stats

        result_channels = []
        try:
            with _db_conn(db_path) as conn:
                ch_id_map = _get_ui_channel_id_map()
                for idx, ch_cfg in enumerate(ui_chs):
                    if not ch_cfg.get('active', False):
                        continue
                    ch_id = ch_id_map.get(idx, "channel_" + chr(97 + idx))
                    letter = ch_letters[idx] if idx < len(ch_letters) else str(idx + 1)
                    rows = tiered_channel_stats_query(conn, ch_id, cutoff, now, bucket_s)
                    _tot, _per = _pkt_counts_for(conn, ch_id)
                    timeseries, stats = _build_sq_channel(rows, _tot, _per)
                    result_channels.append({
                        "name": ch_cfg.get("name", ch_cfg.get("friendly_name", f"Channel {letter}")),
                        "friendly_name": ch_cfg.get("friendly_name", f"Channel {letter}"),
                        "channel_id": ch_id,
                        "freq_mhz": round(ch_cfg.get("frequency", 0) / 1e6, 4),
                        "spreading_factor": ch_cfg.get("spreading_factor", 0),
                        "active": ch_cfg.get("active", False),
                        "color": ch_colors.get(ch_id, "#999"),
                        "stats": stats,
                        "timeseries": timeseries
                    })
                # --- Channel E (SX1261) ---
                _che_cfg = _load_ui().get("channel_e", {})
                if _che_cfg.get("enabled", False):
                    che_rows = tiered_channel_stats_query(conn, "channel_e", cutoff, now, bucket_s)
                    _tot_e, _per_e = _pkt_counts_for(conn, "channel_e")
                    che_ts, che_stats = _build_sq_channel(che_rows, _tot_e, _per_e)
                    result_channels.append({
                        "name": _che_cfg.get("name", _che_cfg.get("friendly_name", "Channel E")),
                        "friendly_name": _che_cfg.get("friendly_name", "Channel E"),
                        "channel_id": "channel_e",
                        "freq_mhz": round(_che_cfg.get("frequency", 0) / 1e6, 4),
                        "spreading_factor": _che_cfg.get("spreading_factor", 0),
                        "active": True,
                        "color": "#f97316",
                        "stats": che_stats,
                        "timeseries": che_ts
                    })
                # --- Channel F (chan_Lora_std on RF0) ---
                _chf_cfg = _load_ui().get("channel_f", {})
                if _chf_cfg.get("enabled", False):
                    chf_rows = tiered_channel_stats_query(conn, "channel_f", cutoff, now, bucket_s)
                    _tot_f, _per_f = _pkt_counts_for(conn, "channel_f")
                    chf_ts, chf_stats = _build_sq_channel(chf_rows, _tot_f, _per_f)
                    result_channels.append({
                        "name": _chf_cfg.get("name", _chf_cfg.get("friendly_name", "Channel F")),
                        "friendly_name": _chf_cfg.get("friendly_name", "Channel F"),
                        "channel_id": "channel_f",
                        "freq_mhz": round(_chf_cfg.get("frequency", 0) / 1e6, 4),
                        "spreading_factor": _chf_cfg.get("spreading_factor", 0),
                        "active": True,
                        "color": "#a855f7",
                        "stats": chf_stats,
                        "timeseries": chf_ts
                    })
                # Noise floor from noise_floor_history table — kept as direct query
                # for per-channel individual data points overlay.
                conn.row_factory = sqlite3.Row
                nf_rows = conn.execute("""
                    SELECT timestamp, channel_id, noise_floor_dbm
                    FROM noise_floor_history
                    WHERE timestamp > ?
                    ORDER BY timestamp ASC
                """, (cutoff,)).fetchall()
                noise_floor_ts = []
                for row in nf_rows:
                    noise_floor_ts.append({
                        "timestamp": row["timestamp"],
                        "channel_id": row["channel_id"],
                        "noise_floor_dbm": round(row["noise_floor_dbm"], 1) if row["noise_floor_dbm"] else None
                    })
                current_nf = conn.execute("""
                    SELECT AVG(nfh.noise_floor_dbm) as avg_nf
                    FROM noise_floor_history nfh
                    INNER JOIN (
                        SELECT channel_id, MAX(timestamp) as max_ts
                        FROM noise_floor_history
                        GROUP BY channel_id
                    ) latest ON nfh.channel_id = latest.channel_id
                               AND nfh.timestamp = latest.max_ts
                """).fetchone()
                conn.row_factory = None
                return {
                    "hours": h,
                    "channels": result_channels,
                    "noise_floor": {
                        "current": round(current_nf["avg_nf"], 1) if current_nf and current_nf["avg_nf"] else None,
                        "timeseries": noise_floor_ts
                    }
                }
        except Exception as e:
            logger.error("signal_quality error: %s", e)
            return {"error": str(e), "channels": [], "noise_floor": {"current": None, "timeseries": []}}

    @cherrypy.expose
    @cherrypy.tools.json_out()

    # ---- Per-channel Noise Floor ----

    def _noise_floor_get(self, **params):
        """GET /api/wm1303/noise_floor - Per-channel noise floor with history (tiered).

        Query params:
          range  - '1h','6h','24h','3d','7d' (default '1h')
          channel - filter to single channel_id (optional)
        """
        import time as _time
        import sqlite3 as _sqlite3

        now = _time.time()
        range_str = params.get('range', '1h')
        channel_filter = params.get('channel', None)

        RANGE_SECS = {
            '1h':  3600,
            '6h':  6*3600,
            '24h': 24*3600,
            '3d':  3*24*3600,
            '7d':  7*24*3600,
        }
        span_secs = RANGE_SECS.get(range_str, 3600)
        since_ts = now - span_secs
        h_equiv = max(1, span_secs // 3600)
        bucket_s = auto_bucket_seconds(h_equiv)

        db_path = "/var/lib/pymc_repeater/repeater.db"
        result_channels = {}

        try:
            import os as _os
            if not _os.path.exists(db_path):
                return _j({"channels": {}, "range": range_str,
                           "error": "database not found"})

            with _db_conn(db_path, timeout=5) as conn:
                # Get current noise floor per channel (direct query — always recent)
                conn.row_factory = _sqlite3.Row
                current_rows = conn.execute("""
                    SELECT nfh.*
                    FROM noise_floor_history nfh
                    INNER JOIN (
                        SELECT channel_id, MAX(timestamp) as max_ts
                        FROM noise_floor_history
                        GROUP BY channel_id
                    ) latest ON nfh.channel_id = latest.channel_id
                               AND nfh.timestamp = latest.max_ts
                """).fetchall()
                conn.row_factory = None

                for row in current_rows:
                    ch_id = row["channel_id"]
                    if channel_filter and ch_id != channel_filter:
                        continue
                    result_channels[ch_id] = {
                        "current": round(row["noise_floor_dbm"], 1),
                        "last_update": row["timestamp"],
                        "history": [],
                        "stats": {}
                    }

                # Build list of channels to query
                ch_ids_to_query = list(result_channels.keys())
                if channel_filter and channel_filter not in ch_ids_to_query:
                    ch_ids_to_query = [channel_filter]
                if not ch_ids_to_query:
                    # Discover channels from the DB
                    conn.row_factory = _sqlite3.Row
                    disc_rows = conn.execute(
                        "SELECT DISTINCT channel_id FROM noise_floor_history WHERE timestamp >= ?",
                        (since_ts,)
                    ).fetchall()
                    conn.row_factory = None
                    ch_ids_to_query = [r["channel_id"] for r in disc_rows
                                       if not channel_filter or r["channel_id"] == channel_filter]

                # Tiered history per channel
                for ch_id in ch_ids_to_query:
                    rows = tiered_noise_floor_query(conn, ch_id, since_ts, now, bucket_s)
                    if ch_id not in result_channels:
                        result_channels[ch_id] = {
                            "current": None, "last_update": None,
                            "history": [], "stats": {}
                        }
                    hist = []
                    all_nf = []
                    for r in rows:
                        _nf = r.get("avg_noise_floor_dbm")
                        _min = r.get("min_noise_floor_dbm")
                        _max = r.get("max_noise_floor_dbm")
                        hist.append({
                            "ts": r["bucket_ts"],
                            "avg_nf": round(_nf, 1) if _nf is not None else None,
                            "min_nf": round(_min, 1) if _min is not None else None,
                            "max_nf": round(_max, 1) if _max is not None else None,
                            "samples": r.get("total_samples_collected") or 0,
                        })
                        if _nf is not None:
                            all_nf.append(_nf)
                    result_channels[ch_id]["history"] = hist
                    result_channels[ch_id]["stats"] = {
                        "count": len(all_nf),
                        "avg": round(sum(all_nf) / len(all_nf), 1) if all_nf else None,
                        "min": round(min(all_nf), 1) if all_nf else None,
                        "max": round(max(all_nf), 1) if all_nf else None,
                    }

            # Also get in-memory noise floors from backend
            # Map live keys to DB-style keys to avoid duplicates
            try:
                _bk = _get_backend()
                if _bk and hasattr(_bk, 'get_channel_noise_floors'):
                    live_nf = _bk.get_channel_noise_floors()
                    # Build reverse map: friendly_name -> 'channel_a', etc.
                    _CHID = ['channel_a', 'channel_b', 'channel_c', 'channel_d']
                    _label_to_dbid = {}
                    try:
                        ui_chs = _load_ui().get("channels", [])
                        _aidx = 0
                        for _idx, _ch in enumerate(ui_chs):
                            _abc = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
                            _label = _ch.get("name", _ch.get("friendly_name", "Channel " + (_abc[_idx] if _idx < len(_abc) else str(_idx + 1))))
                            _label_to_dbid[_label] = _CHID[_aidx] if _ch.get("active", False) and _aidx < len(_CHID) else None
                            _label_to_dbid[_ch.get("name", "")] = _label_to_dbid[_label]
                            if _ch.get("active", False) and _aidx < len(_CHID):
                                _aidx += 1
                    except Exception:
                        pass
                    for ch_id, nf_val in live_nf.items():
                        # Try to map live key to DB key
                        db_key = _label_to_dbid.get(ch_id, ch_id)
                        if db_key and db_key in result_channels:
                            result_channels[db_key]["current_live"] = nf_val
                        elif ch_id in result_channels:
                            result_channels[ch_id]["current_live"] = nf_val
                        # Don't create new entries - DB data is the source of truth
            except Exception:
                pass

        except Exception as e:
            import logging
            logging.getLogger("WM1303API").warning("noise_floor endpoint error: %s", e)
            return _j({"channels": {}, "error": str(e)})

        # Filter to only active channels using position-based channel IDs
        try:
            ui_chs = _load_ui().get("channels", [])
            _CHID = ['channel_a', 'channel_b', 'channel_c', 'channel_d']
            active_ch_ids = set()
            aidx = 0
            for ch_cfg in ui_chs:
                if ch_cfg.get("active", False) and aidx < len(_CHID):
                    active_ch_ids.add(_CHID[aidx])
                    aidx += 1
            # Include Channel E (SX1261) if enabled
            _che_cfg = _load_ui().get("channel_e", {})
            if _che_cfg.get("enabled", False):
                active_ch_ids.add("channel_e")
            # Include Channel F (chan_Lora_std on RF0) if enabled
            _chf_cfg = _load_ui().get("channel_f", {})
            if _chf_cfg.get("enabled", False):
                active_ch_ids.add("channel_f")
            # Also accept old-style friendly names for backward compatibility with existing DB rows
            active_ch_names = set()
            for ch_cfg in ui_chs:
                if ch_cfg.get("active", False):
                    active_ch_names.add(ch_cfg.get("name", ""))
            if active_ch_ids:
                result_channels = {k: v for k, v in result_channels.items()
                                   if k in active_ch_ids or k in active_ch_names}
        except Exception:
            pass

        # Convert channel IDs to friendly names
        try:
            ui_cfg = _load_ui()
            _CHID = ['channel_a', 'channel_b', 'channel_c', 'channel_d']
            _abc = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
            id_to_label = {}
            name_to_label = {}
            aidx = 0
            for idx, ch_cfg in enumerate(ui_cfg.get("channels", [])):
                ch_name = ch_cfg.get("name", "")
                label = ch_cfg.get("name", ch_cfg.get("friendly_name", "Channel " + (_abc[idx] if idx < len(_abc) else str(idx + 1))))
                name_to_label[ch_name] = label
                if ch_cfg.get("active", False) and aidx < len(_CHID):
                    id_to_label[_CHID[aidx]] = label
                    aidx += 1
            # Map Channel E to its friendly name or 'Channel E'
            _che_cfg = ui_cfg.get("channel_e", {})
            if _che_cfg.get("enabled", False):
                id_to_label["channel_e"] = _che_cfg.get("name", _che_cfg.get("friendly_name", "Channel E"))
            # Map Channel F to its friendly name or 'Channel F'
            _chf_cfg = ui_cfg.get("channel_f", {})
            if _chf_cfg.get("enabled", False):
                id_to_label["channel_f"] = _chf_cfg.get("name", _chf_cfg.get("friendly_name", "Channel F"))
            converted = {}
            for k, v in result_channels.items():
                new_key = id_to_label.get(k, name_to_label.get(k, k))
                converted[new_key] = v
            result_channels = converted
        except Exception:
            pass

        return _j({
            "channels": result_channels,
            "range": range_str,
        })

    # ---- CAD Stats ----

    def _cad_stats_get(self, **params):
        """GET /api/wm1303/cad_stats - CAD event timeline (tiered).

        Query params:
          range  - '1h','6h','24h','3d','7d' (default '1h')
          channel - filter to single channel_id (optional)
        """
        import time as _time
        import sqlite3 as _sqlite3

        now = _time.time()
        range_str = params.get('range', '1h')
        channel_filter = params.get('channel', None)

        RANGE_MAP = {
            '1h':  (3600,       1),
            '6h':  (6*3600,     5),
            '24h': (24*3600,   15),
            '3d':  (3*24*3600, 60),
            '7d':  (7*24*3600, 120),
        }
        span_secs, bucket_min = RANGE_MAP.get(range_str, (3600, 1))
        since_ts = now - span_secs
        bucket_secs = bucket_min * 60

        db_path = "/var/lib/pymc_repeater/repeater.db"

        channels = {}
        buckets = {}
        recent = []

        try:
            import os as _os
            if not _os.path.exists(db_path):
                return _j({"channels": {}, "buckets": {}, "recent": [], "tx_queue_cad": {},
                           "range": range_str, "bucket_minutes": bucket_min, "error": "database not found"})

            with _db_conn(db_path) as conn:
                # Check if cad_events table exists
                conn.row_factory = _sqlite3.Row
                tbl = conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name='cad_events'"
                ).fetchone()
                conn.row_factory = None
                if not tbl:
                    pass  # Table not yet created
                else:
                    # Discover channel IDs present in the range
                    conn.row_factory = _sqlite3.Row
                    if channel_filter:
                        ch_ids = [channel_filter]
                    else:
                        disc = conn.execute(
                            "SELECT DISTINCT channel_id FROM cad_events WHERE timestamp >= ?",
                            (since_ts,)
                        ).fetchall()
                        ch_ids = [r["channel_id"] for r in disc]
                    conn.row_factory = None

                    # Tiered aggregation per channel
                    for ch_id in ch_ids:
                        rows = tiered_cad_events_query(conn, ch_id, since_ts, now, bucket_secs)
                        ch_total_clear = 0
                        ch_total_det = 0
                        ch_buckets = []
                        for r in rows:
                            _clear = r.get("total_cad_clear") or 0
                            _det = r.get("total_cad_detected") or 0
                            ch_total_clear += _clear
                            ch_total_det += _det
                            ch_buckets.append({
                                "ts": r["bucket_ts"],
                                "clear": _clear,
                                "detected": _det,
                            })
                        channels[ch_id] = {
                            "clear": ch_total_clear,
                            "detected": ch_total_det,
                            "total": ch_total_clear + ch_total_det,
                        }
                        buckets[ch_id] = ch_buckets

                    # Recent rows (last 100) — direct query on raw table
                    conn.row_factory = _sqlite3.Row
                    where = ["timestamp >= ?"]
                    qparams = [since_ts]
                    if channel_filter:
                        where.append("channel_id = ?")
                        qparams.append(channel_filter)
                    where_str = ' AND '.join(where)
                    recent_rows = conn.execute(f"""
                        SELECT timestamp, channel_id, cad_clear, cad_detected
                        FROM cad_events
                        WHERE {where_str}
                        ORDER BY timestamp DESC
                        LIMIT 100
                    """, qparams).fetchall()
                    conn.row_factory = None

                    recent = [{
                        "ts": row["timestamp"],
                        "channel": row["channel_id"],
                        "clear": row["cad_clear"] or 0,
                        "detected": row["cad_detected"] or 0,
                    } for row in recent_rows]

        except Exception as e:
            import logging
            logging.getLogger("WM1303API").warning("cad_stats endpoint error: %s", e)
            return _j({"channels": {}, "buckets": {}, "recent": [], "tx_queue_cad": {},
                       "range": range_str, "bucket_minutes": bucket_min, "error": str(e)})

        # Also get live CAD stats from TX queue stats
        tx_cad_stats = {}
        try:
            _bk = _get_backend()
            if _bk and _bk._tx_queue_manager:
                for ch_id, q in _bk._tx_queue_manager.queues.items():
                    tx_cad_stats[ch_id] = {
                        "cad_clear": q.stats.get("cad_clear", 0),
                        "cad_detected": q.stats.get("cad_detected", 0),
                        "cad_last_result": q.stats.get("cad_last_result"),
                    }
        except Exception:
            pass

        return _j({
            "channels": channels,
            "recent": recent,
            "buckets": buckets,
            "tx_queue_cad": tx_cad_stats,
            "range": range_str,
            "bucket_minutes": bucket_min,
        })

    def _cache_stats_get(self, **params):
        """GET /api/wm1303/cache_stats - Snapshot of in-memory caches.

        Returns per-cache size, max_size, TTL, oldest/newest entry age, and
        engine counters for diagnosing state-accumulation drift on long-running
        deployments (GitHub issues #24/#25). Read-only; no side effects.

        ts_mode field: 'wall' = `time.time()` based, 'monotonic' =
        `time.monotonic()` based. Both reference times are computed at the
        start of this handler so all reported ages are consistent.
        """
        import time as _time

        now_wall = _time.time()
        now_mono = _time.monotonic()

        def _summarize(d, ts_mode, ttl=None, max_size=None, description=""):
            """Build a stats dict for a {key: ts} mapping."""
            size = len(d) if d is not None else 0
            ref_now = now_mono if ts_mode == "monotonic" else now_wall
            oldest_age = None
            newest_age = None
            if d and size > 0:
                try:
                    values = [v for v in d.values()
                              if isinstance(v, (int, float))]
                    if values:
                        oldest_age = round(ref_now - min(values), 2)
                        newest_age = round(ref_now - max(values), 2)
                except Exception:
                    pass
            return {
                "size": size,
                "max_size": max_size,
                "ttl_seconds": ttl,
                "oldest_age_seconds": oldest_age,
                "newest_age_seconds": newest_age,
                "ts_mode": ts_mode,
                "description": description,
            }

        def _age_from(monotonic_ts):
            """Return age in seconds for a monotonic timestamp, or None."""
            try:
                if monotonic_ts and monotonic_ts > 0:
                    return round(now_mono - float(monotonic_ts), 2)
            except Exception:
                pass
            return None

        # ── BridgeEngine caches ──────────────────────────────────────────
        bridge_stats = {}
        bridge_counters = {}
        try:
            from repeater.bridge_engine import _active_bridge
            bridge = _active_bridge
        except Exception:
            bridge = None

        if bridge is not None:
            try:
                bridge_stats["seen_dedup"] = _summarize(
                    getattr(bridge, "_seen", {}),
                    ts_mode="monotonic",
                    ttl=getattr(bridge, "dedup_ttl", None),
                    description="Packet-hash dedup cache (BridgeEngine._seen; populated via time.monotonic() in _is_duplicate)",
                )
                bridge_stats["tx_echo_hashes"] = _summarize(
                    getattr(bridge, "_tx_echo_hashes", {}),
                    ts_mode="monotonic",
                    ttl=getattr(bridge, "_tx_echo_ttl", None),
                    description="Recent TX hashes for RF-echo detection",
                )
                _ev = getattr(bridge, "_dedup_events", None)
                bridge_stats["dedup_events_ring"] = {
                    "size": len(_ev) if _ev is not None else 0,
                    "max_size": getattr(_ev, "maxlen", None) if _ev is not None else None,
                    "ttl_seconds": None,
                    "oldest_age_seconds": None,
                    "newest_age_seconds": None,
                    "ts_mode": "n/a",
                    "description": "Recent dedup-event ring buffer (deque, FIFO)",
                }
                try:
                    st = bridge.get_stats() or {}
                    bridge_counters = {
                        "forwarded_packets": st.get("forwarded_packets", 0),
                        "dropped_duplicate": st.get("dropped_duplicate", 0),
                        "dropped_filtered": st.get("dropped_filtered", 0),
                        "fwd_echo_detected": st.get("fwd_echo_detected", 0),
                        "tx_echo_detected": getattr(bridge, "_tx_echo_detected", 0),
                    }
                except Exception:
                    pass
            except Exception as _be:
                bridge_stats["error"] = str(_be)

        # ── WM1303Backend caches ─────────────────────────────────────────
        backend_stats = {}
        backend_counters = {}
        _bk = None
        try:
            _bk = _get_backend()
        except Exception:
            pass

        if _bk is not None:
            try:
                backend_stats["tx_echo_hashes"] = _summarize(
                    getattr(_bk, "_tx_echo_hashes", {}),
                    ts_mode="monotonic",
                    ttl=getattr(_bk, "_tx_echo_ttl", None),
                    description="Recent TX hashes for backend self-echo detection",
                )
                backend_stats["rx_dedup_cache"] = _summarize(
                    getattr(_bk, "_rx_dedup_cache", {}),
                    ts_mode="monotonic",
                    description="Multi-demod RX dedup cache (per-frame hash)",
                )
                _tx_ack = getattr(_bk, "_tx_ack_cache", None)
                backend_stats["tx_ack_cache"] = {
                    "size": len(_tx_ack) if _tx_ack is not None else 0,
                    "max_size": getattr(_bk, "_tx_ack_cache_max", None),
                    "ttl_seconds": getattr(_bk, "_tx_ack_cache_ttl", None),
                    "oldest_age_seconds": None,
                    "newest_age_seconds": None,
                    "ts_mode": "n/a",
                    "description": "OrderedDict of recent TX ACK results from HAL post_tx phase",
                }
                backend_stats["lbt_config_cache"] = {
                    "size": len(getattr(_bk, "_lbt_config_cache", {}) or {}),
                    "max_size": None,
                    "ttl_seconds": getattr(_bk, "_lbt_config_cache_ttl", None),
                    "oldest_age_seconds": _age_from(getattr(_bk, "_lbt_config_cache_time", 0)),
                    "newest_age_seconds": _age_from(getattr(_bk, "_lbt_config_cache_time", 0)),
                    "ts_mode": "monotonic",
                    "description": "Per-channel LBT config cache (refreshed periodically from wm1303_ui.json)",
                }
                backend_stats["cad_config_cache"] = {
                    "size": len(getattr(_bk, "_cad_config_cache", {}) or {}),
                    "max_size": None,
                    "ttl_seconds": None,
                    "oldest_age_seconds": _age_from(getattr(_bk, "_cad_config_cache_time", 0)),
                    "newest_age_seconds": _age_from(getattr(_bk, "_cad_config_cache_time", 0)),
                    "ts_mode": "monotonic",
                    "description": "Per-channel CAD enabled cache (refreshed periodically from wm1303_ui.json)",
                }
                backend_stats["channel_e_config_cache"] = {
                    "size": len(getattr(_bk, "_channel_e_config_cache", {}) or {}),
                    "max_size": None,
                    "ttl_seconds": getattr(_bk, "_channel_e_cache_ttl", None),
                    "oldest_age_seconds": _age_from(getattr(_bk, "_channel_e_cache_time", 0)),
                    "newest_age_seconds": _age_from(getattr(_bk, "_channel_e_cache_time", 0)),
                    "ts_mode": "monotonic",
                    "description": "Channel E (SX1261) config snapshot cache",
                }
                backend_stats["channel_f_config_cache"] = {
                    "size": len(getattr(_bk, "_channel_f_config_cache", {}) or {}),
                    "max_size": None,
                    "ttl_seconds": None,
                    "oldest_age_seconds": None,
                    "newest_age_seconds": None,
                    "ts_mode": "n/a",
                    "description": "Channel F (SX1302 service modem) config snapshot cache",
                }
                backend_counters = {
                    "tx_unknown_echo_detected": getattr(_bk, "_tx_unknown_echo_detected", 0),
                }
            except Exception as _bke:
                backend_stats["error"] = str(_bke)

        return _j({
            "timestamp": now_wall,
            "now_monotonic": now_mono,
            "bridge_engine": {
                "available": bridge is not None,
                "caches": bridge_stats,
                "counters": bridge_counters,
            },
            "wm1303_backend": {
                "available": _bk is not None,
                "caches": backend_stats,
                "counters": backend_counters,
            },
        })

    def noise_floor_history(self, hours='24'):
        """GET /api/wm1303/noise_floor_history - Noise floor measurements over time (tiered)."""
        import sqlite3
        h = min(int(hours), 168)
        bucket_s = auto_bucket_seconds(h)
        now = time.time()
        cutoff = now - (h * 3600)
        db_path = "/var/lib/pymc_repeater/repeater.db"
        try:
            with _db_conn(db_path) as conn:
                # Discover channels present in the range
                conn.row_factory = sqlite3.Row
                disc = conn.execute(
                    "SELECT DISTINCT channel_id FROM noise_floor_history WHERE timestamp > ?",
                    (cutoff,)
                ).fetchall()
                ch_ids = [r["channel_id"] for r in disc]
                conn.row_factory = None

                # Tiered query per channel, merge into flat list
                data = []
                all_nf = []
                for ch_id in ch_ids:
                    rows = tiered_noise_floor_query(conn, ch_id, cutoff, now, bucket_s)
                    for r in rows:
                        _nf = r.get("avg_noise_floor_dbm")
                        _min = r.get("min_noise_floor_dbm")
                        _max = r.get("max_noise_floor_dbm")
                        data.append({
                            "timestamp": r["bucket_ts"],
                            "channel_id": ch_id,
                            "noise_floor_dbm": round(_nf, 1) if _nf is not None else None,
                            "min_rssi": round(_min, 1) if _min is not None else None,
                            "max_rssi": round(_max, 1) if _max is not None else None,
                            "samples": r.get("total_samples_collected") or 0,
                        })
                        if _nf is not None:
                            all_nf.append(_nf)
                # Sort by timestamp across all channels
                data.sort(key=lambda x: x["timestamp"])
                return _j({
                    "hours": h,
                    "total_measurements": len(data),
                    "stats": {
                        "avg": round(sum(all_nf) / len(all_nf), 1) if all_nf else None,
                        "min": round(min(all_nf), 1) if all_nf else None,
                        "max": round(max(all_nf), 1) if all_nf else None,
                    },
                    "data": data
                })
        except Exception as e:
            logger.error(f"noise_floor_history error: {e}")
            return _j({"error": str(e), "data": []})


    # ---------- HAL Advanced Radio Settings ----------

    @cherrypy.expose
    @cherrypy.tools.json_out()
    def _packet_activity(self, **params):
        """GET /api/wm1303/packet_activity - RX/TX activity per channel (tiered)."""
        hours_str = params.get("hours", "24")
        try:
            h = min(int(hours_str), 168)
        except (ValueError, TypeError):
            h = 24
        bucket_s = auto_bucket_seconds(h)
        now = time.time()
        cutoff = now - (h * 3600)
        db_path = "/var/lib/pymc_repeater/repeater.db"
        ui_chs = _load_ui().get("channels", [])
        ch_letters = ["A", "B", "C", "D", "E", "F", "G", "H"]
        ch_colors = ["#3b82f6", "#8b5cf6", "#10b981", "#f59e0b",
                     "#f472b6", "#fb923c", "#06b6d4", "#a3e635"]
        result_channels = []
        try:
            ch_id_map = _get_ui_channel_id_map()
            with _db_conn(db_path) as conn:
                for idx, ch_cfg in enumerate(ui_chs):
                    ch_id = ch_id_map.get(idx, "channel_" + chr(97 + idx))
                    letter = ch_letters[idx] if idx < len(ch_letters) else str(idx + 1)
                    rows = tiered_packet_activity_query(conn, ch_id, cutoff, now, bucket_s)
                    timeseries = [{"t": r["bucket_ts"], "rx": r["total_rx_count"] or 0,
                                   "tx": r["total_tx_count"] or 0} for r in rows]
                    result_channels.append({
                        "id": ch_id,
                        "label": ch_cfg.get("name", ch_cfg.get("friendly_name", f"Channel {letter}")),
                        "color": ch_colors[idx % len(ch_colors)],
                        "data": timeseries
                    })
                # --- Channel E (SX1261) ---
                _che_cfg = _load_ui().get("channel_e", {})
                if _che_cfg.get("enabled", False):
                    che_rows = tiered_packet_activity_query(conn, "channel_e", cutoff, now, bucket_s)
                    che_ts = [{"t": r["bucket_ts"], "rx": r["total_rx_count"] or 0,
                               "tx": r["total_tx_count"] or 0} for r in che_rows]
                    result_channels.append({
                        "id": "channel_e",
                        "label": _che_cfg.get("name", _che_cfg.get("friendly_name", "Channel E")),
                        "color": "#f97316",
                        "data": che_ts
                    })
                # --- Channel F (chan_Lora_std on RF0) ---
                _chf_cfg = _load_ui().get("channel_f", {})
                if _chf_cfg.get("enabled", False):
                    chf_rows = tiered_packet_activity_query(conn, "channel_f", cutoff, now, bucket_s)
                    chf_ts = [{"t": r["bucket_ts"], "rx": r["total_rx_count"] or 0,
                               "tx": r["total_tx_count"] or 0} for r in chf_rows]
                    result_channels.append({
                        "id": "channel_f",
                        "label": _chf_cfg.get("name", _chf_cfg.get("friendly_name", "Channel F")),
                        "color": "#a855f7",
                        "data": chf_ts
                    })
            return _j({"hours": h, "bucket_seconds": bucket_s, "channels": result_channels})
        except Exception as e:
            logger.error("packet_activity error: %s", e)
            return _j({"error": str(e), "hours": h, "channels": []})

    def _crc_error_rate(self, **params):
        """GET /api/wm1303/crc_error_rate - Per-channel CRC error rate (tiered)."""
        hours_str = params.get("hours", "1")
        try:
            h = min(int(hours_str), 168)
        except (ValueError, TypeError):
            h = 1
        channel_id = params.get("channel_id", None)
        bucket_s = auto_bucket_seconds(h)
        now = time.time()
        cutoff = now - (h * 3600)
        db_path = "/var/lib/pymc_repeater/repeater.db"
        ui_chs = _load_ui().get("channels", [])
        ch_letters = ["A", "B", "C", "D", "E", "F", "G", "H"]
        ch_colors = ["#ef4444", "#f97316", "#eab308", "#a855f7",
                     "#ec4899", "#f43f5e", "#fb7185", "#fbbf24"]
        result_channels = []
        try:
            ch_id_map = _get_ui_channel_id_map()
            with _db_conn(db_path) as conn:
                for idx, ch_cfg in enumerate(ui_chs):
                    ch_id = ch_id_map.get(idx, "channel_" + chr(97 + idx))
                    if channel_id and ch_id != channel_id:
                        continue
                    letter = ch_letters[idx] if idx < len(ch_letters) else str(idx + 1)
                    rows = tiered_crc_error_rate_query(conn, ch_id, cutoff, now, bucket_s)
                    timeseries = [{"t": r["bucket_ts"],
                                   "crc_error": r["total_crc_errors"] or 0,
                                   "crc_disabled": r["total_crc_disabled"] or 0} for r in rows]
                    result_channels.append({
                        "id": ch_id,
                        "label": ch_cfg.get("name", ch_cfg.get("friendly_name", f"Channel {letter}")),
                        "color": ch_colors[idx % len(ch_colors)],
                        "data": timeseries
                    })
                # Also check for 'unknown' channel
                unk_rows = tiered_crc_error_rate_query(conn, "unknown", cutoff, now, bucket_s)
                if unk_rows:
                    unk_ts = [{"t": r["bucket_ts"],
                               "crc_error": r["total_crc_errors"] or 0,
                               "crc_disabled": r["total_crc_disabled"] or 0} for r in unk_rows]
                    result_channels.append({
                        "id": "unknown",
                        "label": "Unknown",
                        "color": "#6b7280",
                        "data": unk_ts
                    })
            return _j({"hours": h, "bucket_seconds": bucket_s, "channels": result_channels})
        except Exception as e:
            logger.error("crc_error_rate error: %s", e)
            return _j({"error": str(e), "hours": h, "channels": []})


    def _packet_metrics(self, **params):
        """GET /api/wm1303/packet_metrics - Per-packet RX/TX metrics per channel (tiered).

        Returns per-channel per-bucket arrays:
          rx_bytes (sum), tx_bytes (sum), tx_airtime_ms (sum), tx_wait_ms (sum),
          rx_hops (avg), rx_crc_ok (count), rx_crc_err (count).
        """
        hours_str = params.get("hours", "24")
        try:
            h = min(int(hours_str), 168)
        except (ValueError, TypeError):
            h = 24
        bucket_s = auto_bucket_seconds(h)
        now = time.time()
        cutoff = now - (h * 3600)
        db_path = "/var/lib/pymc_repeater/repeater.db"
        ui_chs = _load_ui().get("channels", [])
        ch_letters = ["A", "B", "C", "D", "E", "F", "G", "H"]
        ch_colors = ["#3b82f6", "#8b5cf6", "#10b981", "#f59e0b",
                     "#f472b6", "#fb923c", "#06b6d4", "#a3e635"]

        def _agg_channel_tiered(conn, ch_id):
            rows = tiered_packet_metrics_query(conn, ch_id, cutoff, now, bucket_s)
            # Pivot by bucket_ts, combining rx and tx direction rows
            buckets = {}
            for r in rows:
                bk = r["bucket_ts"]
                b = buckets.setdefault(bk, {
                    "rx_bytes": 0, "tx_bytes": 0,
                    "tx_airtime_ms": 0.0, "tx_wait_ms": 0.0,
                    "rx_hops_avg": None, "rx_crc_ok": 0, "rx_crc_err": 0,
                })
                _dir = r.get("direction", "")
                _sc = r.get("sample_count") or 0
                _crc_err = r.get("crc_error_count") or 0
                if _dir == "rx":
                    b["rx_bytes"] += r.get("total_bytes") or 0
                    b["rx_crc_err"] += _crc_err
                    b["rx_crc_ok"] += max(0, _sc - _crc_err)
                    _hop = r.get("avg_hop_count")
                    if _hop is not None:
                        # avg_hop_count from tiered is already the average hop_count
                        # The original adds +1 (path_len=0 → 1 transmission)
                        b["rx_hops_avg"] = float(_hop) + 1.0
                elif _dir == "tx":
                    b["tx_bytes"] += r.get("total_bytes") or 0
                    b["tx_airtime_ms"] += float(r.get("total_airtime_ms") or 0)
            series = []
            for bk_ts in sorted(buckets.keys()):
                b = buckets[bk_ts]
                _total_rx = b["rx_crc_ok"] + b["rx_crc_err"]
                if b["rx_crc_ok"] > 0:
                    crc_err_ratio = b["rx_crc_err"] / _total_rx
                elif b["rx_bytes"] > 0 or b["tx_bytes"] > 0:
                    crc_err_ratio = 0.0
                else:
                    crc_err_ratio = None
                series.append({
                    "t": bk_ts,
                    "rx_bytes": b["rx_bytes"],
                    "tx_bytes": b["tx_bytes"],
                    "tx_airtime_ms": round(b["tx_airtime_ms"], 1),
                    "tx_wait_ms": round(b["tx_wait_ms"], 1),
                    "rx_hops": round(b["rx_hops_avg"], 2) if b["rx_hops_avg"] is not None else None,
                    "rx_crc_ok": b["rx_crc_ok"],
                    "rx_crc_err": b["rx_crc_err"],
                    "rx_crc_err_ratio": round(crc_err_ratio, 3) if crc_err_ratio is not None else None,
                })
            return series

        result_channels = []
        try:
            ch_id_map = _get_ui_channel_id_map()
            with _db_conn(db_path) as conn:
                for idx, ch_cfg in enumerate(ui_chs):
                    ch_id = ch_id_map.get(idx, "channel_" + chr(97 + idx))
                    letter = ch_letters[idx] if idx < len(ch_letters) else str(idx + 1)
                    series = _agg_channel_tiered(conn, ch_id)
                    result_channels.append({
                        "id": ch_id,
                        "label": ch_cfg.get("name", ch_cfg.get("friendly_name", f"Channel {letter}")),
                        "color": ch_colors[idx % len(ch_colors)],
                        "data": series,
                    })
                # Channel E (SX1261)
                _che_cfg = _load_ui().get("channel_e", {})
                if _che_cfg.get("enabled", False):
                    series_e = _agg_channel_tiered(conn, "channel_e")
                    result_channels.append({
                        "id": "channel_e",
                        "label": _che_cfg.get("name", _che_cfg.get("friendly_name", "Channel E")),
                        "color": "#f97316",
                        "data": series_e,
                    })
                # Channel F (chan_Lora_std on RF0)
                _chf_cfg = _load_ui().get("channel_f", {})
                if _chf_cfg.get("enabled", False):
                    series_f = _agg_channel_tiered(conn, "channel_f")
                    result_channels.append({
                        "id": "channel_f",
                        "label": _chf_cfg.get("name", _chf_cfg.get("friendly_name", "Channel F")),
                        "color": "#a855f7",
                        "data": series_f,
                    })
            return _j({"hours": h, "bucket_seconds": bucket_s, "channels": result_channels})
        except Exception as e:
            logger.error("packet_metrics error: %s", e)
            return _j({"error": str(e), "hours": h, "channels": []})


    def tx_activity(self, hours='24'):
        """GET /api/wm1303/tx_activity - TX activity per channel from channel_stats_history."""
        import sqlite3
        h = min(int(hours), 168)
        db_path = "/var/lib/pymc_repeater/repeater.db"
        cutoff = time.time() - (h * 3600)
        bucket_s = 60  # 1-minute buckets
        ui_chs = _load_ui().get("channels", [])
        ch_colors = {"channel_a": "#3b82f6", "channel_b": "#8b5cf6",
                     "channel_c": "#10b981", "channel_d": "#f59e0b",
                     "channel_e": "#f97316", "channel_f": "#a855f7"}
        ch_letters = ["A", "B", "C", "D", "E", "F", "G", "H"]
        result_channels = []
        try:
            with _db_conn(db_path) as conn:
                conn.row_factory = sqlite3.Row
                ch_id_map = _get_ui_channel_id_map()
                for idx, ch_cfg in enumerate(ui_chs):
                    ch_id = ch_id_map.get(idx, "channel_" + chr(97 + idx))
                    letter = ch_letters[idx] if idx < len(ch_letters) else str(idx + 1)
                    rows = conn.execute(
                        "SELECT timestamp, tx_count, tx_failed, lbt_blocked, "
                        "tx_airtime_ms, tx_bytes FROM channel_stats_history "
                        "WHERE channel_id = ? AND timestamp > ? ORDER BY timestamp",
                        (ch_id, cutoff)
                    ).fetchall()
                    timeseries = []
                    if len(rows) >= 2:
                        # Compute deltas between consecutive rows, assign to minute buckets
                        buckets = {}
                        for i in range(1, len(rows)):
                            prev, cur = rows[i-1], rows[i]
                            bk = int(cur["timestamp"] / bucket_s) * bucket_s
                            tx_d = max(0, (cur["tx_count"] or 0) - (prev["tx_count"] or 0))
                            fail_d = max(0, (cur["tx_failed"] or 0) - (prev["tx_failed"] or 0))
                            lbt_d = max(0, (cur["lbt_blocked"] or 0) - (prev["lbt_blocked"] or 0))
                            air_d = max(0, (cur["tx_airtime_ms"] or 0) - (prev["tx_airtime_ms"] or 0))
                            bytes_d = max(0, (cur["tx_bytes"] or 0) - (prev["tx_bytes"] or 0))
                            if bk not in buckets:
                                buckets[bk] = {"tx_sent": 0, "tx_failed": 0, "lbt_blocked": 0,
                                               "airtime_ms": 0.0, "tx_bytes": 0}
                            buckets[bk]["tx_sent"] += tx_d
                            buckets[bk]["tx_failed"] += fail_d
                            buckets[bk]["lbt_blocked"] += lbt_d
                            buckets[bk]["airtime_ms"] += air_d
                            buckets[bk]["tx_bytes"] += bytes_d
                        for bk_ts in sorted(buckets.keys()):
                            b = buckets[bk_ts]
                            if b["tx_sent"] > 0 or b["tx_failed"] > 0 or b["lbt_blocked"] > 0:
                                timeseries.append({
                                    "timestamp": bk_ts,
                                    "tx_sent": b["tx_sent"],
                                    "tx_failed": b["tx_failed"],
                                    "lbt_blocked": b["lbt_blocked"],
                                    "airtime_ms": round(b["airtime_ms"], 1),
                                    "tx_bytes": b["tx_bytes"]
                                })
                    result_channels.append({
                        "name": ch_cfg.get("name", ch_cfg.get("friendly_name", f"Channel {letter}")),
                        "channel_id": ch_id,
                        "color": ch_colors.get(ch_id, "#999"),
                        "timeseries": timeseries
                    })
                # --- Channel E (SX1261) ---
                _che_cfg = _load_ui().get("channel_e", {})
                if _che_cfg.get("enabled", False):
                    che_rows = conn.execute(
                        "SELECT timestamp, tx_count, tx_failed, lbt_blocked, "
                        "tx_airtime_ms, tx_bytes FROM channel_stats_history "
                        "WHERE channel_id = ? AND timestamp > ? ORDER BY timestamp",
                        ("channel_e", cutoff)
                    ).fetchall()
                    che_ts = []
                    if len(che_rows) >= 2:
                        che_buckets = {}
                        for i in range(1, len(che_rows)):
                            prev, cur = che_rows[i-1], che_rows[i]
                            bk = int(cur["timestamp"] / bucket_s) * bucket_s
                            tx_d = max(0, (cur["tx_count"] or 0) - (prev["tx_count"] or 0))
                            fail_d = max(0, (cur["tx_failed"] or 0) - (prev["tx_failed"] or 0))
                            lbt_d = max(0, (cur["lbt_blocked"] or 0) - (prev["lbt_blocked"] or 0))
                            air_d = max(0, (cur["tx_airtime_ms"] or 0) - (prev["tx_airtime_ms"] or 0))
                            bytes_d = max(0, (cur["tx_bytes"] or 0) - (prev["tx_bytes"] or 0))
                            if bk not in che_buckets:
                                che_buckets[bk] = {"tx_sent": 0, "tx_failed": 0, "lbt_blocked": 0,
                                                   "airtime_ms": 0.0, "tx_bytes": 0}
                            che_buckets[bk]["tx_sent"] += tx_d
                            che_buckets[bk]["tx_failed"] += fail_d
                            che_buckets[bk]["lbt_blocked"] += lbt_d
                            che_buckets[bk]["airtime_ms"] += air_d
                            che_buckets[bk]["tx_bytes"] += bytes_d
                        for bk_ts in sorted(che_buckets.keys()):
                            b = che_buckets[bk_ts]
                            if b["tx_sent"] > 0 or b["tx_failed"] > 0 or b["lbt_blocked"] > 0:
                                che_ts.append({
                                    "timestamp": bk_ts,
                                    "tx_sent": b["tx_sent"],
                                    "tx_failed": b["tx_failed"],
                                    "lbt_blocked": b["lbt_blocked"],
                                    "airtime_ms": round(b["airtime_ms"], 1),
                                    "tx_bytes": b["tx_bytes"]
                                })
                    result_channels.append({
                        "name": _che_cfg.get("name", _che_cfg.get("friendly_name", "Channel E")),
                        "channel_id": "channel_e",
                        "color": "#f97316",
                        "timeseries": che_ts
                    })
                # --- Channel F (chan_Lora_std on RF0) ---
                _chf_cfg = _load_ui().get("channel_f", {})
                if _chf_cfg.get("enabled", False):
                    chf_rows = conn.execute(
                        "SELECT timestamp, tx_count, tx_failed, lbt_blocked, "
                        "tx_airtime_ms, tx_bytes FROM channel_stats_history "
                        "WHERE channel_id = ? AND timestamp > ? ORDER BY timestamp",
                        ("channel_f", cutoff)
                    ).fetchall()
                    chf_ts = []
                    if len(chf_rows) >= 2:
                        chf_buckets = {}
                        for i in range(1, len(chf_rows)):
                            prev, cur = chf_rows[i-1], chf_rows[i]
                            bk = int(cur["timestamp"] / bucket_s) * bucket_s
                            tx_d = max(0, (cur["tx_count"] or 0) - (prev["tx_count"] or 0))
                            fail_d = max(0, (cur["tx_failed"] or 0) - (prev["tx_failed"] or 0))
                            lbt_d = max(0, (cur["lbt_blocked"] or 0) - (prev["lbt_blocked"] or 0))
                            air_d = max(0, (cur["tx_airtime_ms"] or 0) - (prev["tx_airtime_ms"] or 0))
                            bytes_d = max(0, (cur["tx_bytes"] or 0) - (prev["tx_bytes"] or 0))
                            if bk not in chf_buckets:
                                chf_buckets[bk] = {"tx_sent": 0, "tx_failed": 0, "lbt_blocked": 0,
                                                   "airtime_ms": 0.0, "tx_bytes": 0}
                            chf_buckets[bk]["tx_sent"] += tx_d
                            chf_buckets[bk]["tx_failed"] += fail_d
                            chf_buckets[bk]["lbt_blocked"] += lbt_d
                            chf_buckets[bk]["airtime_ms"] += air_d
                            chf_buckets[bk]["tx_bytes"] += bytes_d
                        for bk_ts in sorted(chf_buckets.keys()):
                            b = chf_buckets[bk_ts]
                            if b["tx_sent"] > 0 or b["tx_failed"] > 0 or b["lbt_blocked"] > 0:
                                chf_ts.append({
                                    "timestamp": bk_ts,
                                    "tx_sent": b["tx_sent"],
                                    "tx_failed": b["tx_failed"],
                                    "lbt_blocked": b["lbt_blocked"],
                                    "airtime_ms": round(b["airtime_ms"], 1),
                                    "tx_bytes": b["tx_bytes"]
                                })
                    result_channels.append({
                        "name": _chf_cfg.get("name", _chf_cfg.get("friendly_name", "Channel F")),
                        "channel_id": "channel_f",
                        "color": "#a855f7",
                        "timeseries": chf_ts
                    })
            return _j({"hours": h, "bucket_minutes": 1, "channels": result_channels})
        except Exception as e:
            logger.error("tx_activity error: %s", e)
            return _j({"error": str(e), "hours": h, "bucket_minutes": 1, "channels": []})


    @cherrypy.expose
    def origin_stats(self, hours='192'):
        """GET /api/wm1303/origin_stats - Origin channel activity from origin_channel_stats table."""
        import sqlite3
        h = min(int(hours), 192)
        db_path = "/var/lib/pymc_repeater/repeater.db"
        cutoff = time.time() - (h * 3600)
        bucket_s = 60  # 1-minute buckets (match tx_activity + other Spectrum charts)
        ui_chs = _load_ui().get("channels", [])
        ch_colors = {"channel_a": "#3b82f6", "channel_b": "#8b5cf6",
                     "channel_c": "#10b981", "channel_d": "#f59e0b",
                     "channel_e": "#f97316", "channel_f": "#a855f7"}
        ch_letters = ["A", "B", "C", "D", "E", "F", "G", "H"]
        result_channels = []
        summary = {}
        try:
            with _db_conn(db_path) as conn:
                conn.row_factory = sqlite3.Row
                ch_id_map = _get_ui_channel_id_map()
                for idx, ch_cfg in enumerate(ui_chs):
                    ch_id = ch_id_map.get(idx, "channel_" + chr(97 + idx))
                    letter = ch_letters[idx] if idx < len(ch_letters) else str(idx + 1)
                    friendly = ch_cfg.get("name", ch_cfg.get("friendly_name", f"Channel {letter}"))
                    rows = conn.execute(
                        "SELECT timestamp, count FROM origin_channel_stats "
                        "WHERE channel_id = ? AND timestamp > ? ORDER BY timestamp",
                        (ch_id, cutoff)
                    ).fetchall()
                    # Aggregate into buckets
                    timeseries = []
                    total_count = 0
                    if rows:
                        buckets = {}
                        for row in rows:
                            bk = int(row["timestamp"] / bucket_s) * bucket_s
                            if bk not in buckets:
                                buckets[bk] = 0
                            buckets[bk] += row["count"]
                        for bk_ts in sorted(buckets.keys()):
                            cnt = buckets[bk_ts]
                            total_count += cnt
                            timeseries.append({"timestamp": bk_ts, "count": cnt})
                    summary[ch_id] = total_count
                    result_channels.append({
                        "name": friendly,
                        "channel_id": ch_id,
                        "color": ch_colors.get(ch_id, "#999"),
                        "active": ch_cfg.get("active", False),
                        "timeseries": timeseries,
                        "total": total_count
                    })
                # Also add channel_e if enabled
                _che_cfg = _load_ui().get("channel_e", {})
                if _che_cfg.get("enabled", False):
                    che_rows = conn.execute(
                        "SELECT timestamp, count FROM origin_channel_stats "
                        "WHERE channel_id = ? AND timestamp > ? ORDER BY timestamp",
                        ("channel_e", cutoff)
                    ).fetchall()
                    che_ts = []
                    che_total = 0
                    if che_rows:
                        che_buckets = {}
                        for row in che_rows:
                            bk = int(row["timestamp"] / bucket_s) * bucket_s
                            if bk not in che_buckets:
                                che_buckets[bk] = 0
                            che_buckets[bk] += row["count"]
                        for bk_ts in sorted(che_buckets.keys()):
                            cnt = che_buckets[bk_ts]
                            che_total += cnt
                            che_ts.append({"timestamp": bk_ts, "count": cnt})
                    summary["channel_e"] = che_total
                    result_channels.append({
                        "name": _che_cfg.get("name", _che_cfg.get("friendly_name", "Channel E")),
                        "channel_id": "channel_e",
                        "color": "#f97316",
                        "active": True,
                        "timeseries": che_ts,
                        "total": che_total
                    })
                # Also add channel_f if enabled (chan_Lora_std on RF0)
                _chf_cfg = _load_ui().get("channel_f", {})
                if _chf_cfg.get("enabled", False):
                    chf_rows = conn.execute(
                        "SELECT timestamp, count FROM origin_channel_stats "
                        "WHERE channel_id = ? AND timestamp > ? ORDER BY timestamp",
                        ("channel_f", cutoff)
                    ).fetchall()
                    chf_ts = []
                    chf_total = 0
                    if chf_rows:
                        chf_buckets = {}
                        for row in chf_rows:
                            bk = int(row["timestamp"] / bucket_s) * bucket_s
                            if bk not in chf_buckets:
                                chf_buckets[bk] = 0
                            chf_buckets[bk] += row["count"]
                        for bk_ts in sorted(chf_buckets.keys()):
                            cnt = chf_buckets[bk_ts]
                            chf_total += cnt
                            chf_ts.append({"timestamp": bk_ts, "count": cnt})
                    summary["channel_f"] = chf_total
                    result_channels.append({
                        "name": _chf_cfg.get("name", _chf_cfg.get("friendly_name", "Channel F")),
                        "channel_id": "channel_f",
                        "color": "#a855f7",
                        "active": True,
                        "timeseries": chf_ts,
                        "total": chf_total
                    })
            # Add live (unflushed) counts from bridge engine
            try:
                from repeater.bridge_engine import _active_bridge
                if _active_bridge:
                    live_counts = _active_bridge.get_origin_counts()
                    for ch_id, cnt in live_counts.items():
                        if cnt > 0:
                            summary[ch_id] = summary.get(ch_id, 0) + cnt
                            # Find matching channel entry and add live count
                            for ch_entry in result_channels:
                                if ch_entry["channel_id"] == ch_id:
                                    ch_entry["total"] += cnt
                                    ch_entry["live_count"] = cnt
                                    break
            except Exception:
                pass
            return _j({"hours": h, "bucket_minutes": 1, "channels": result_channels, "summary": summary})
        except Exception as e:
            logger.error("origin_stats error: %s", e)
            return _j({"error": str(e), "hours": h, "bucket_minutes": 1, "channels": [], "summary": {}})





    # ═══════════════════ ADV. CONFIG ENDPOINTS ═══════════════════

    def _adv_config_get(self):
        """Return all advanced config parameters from config.yaml and wm1303_ui.json."""
        try:
            import yaml
            cfg = {}
            try:
                with open("/etc/pymc_repeater/config.yaml") as f:
                    cfg = yaml.safe_load(f) or {}
            except Exception as e:
                logger.warning("adv_config_get: could not read config.yaml: %s", e)

            ui = _load_ui()
            adv = ui.get("adv_config", {})
            hal = ui.get("hal_advanced", {})
            gpio = ui.get("gpio_pins", {})
            spi = ui.get("spi_devices", {})
            result = {
                "dedup_ttl_seconds":    cfg.get("bridge", {}).get("dedup_ttl_seconds", cfg.get("bridge", {}).get("dedup_ttl", 300)),
                "cache_ttl":            cfg.get("repeater", {}).get("cache_ttl", 60),
                "max_cache_size":       cfg.get("repeater", {}).get("max_cache_size", adv.get("max_cache_size", 1000)),
                "queue_size":           cfg.get("wm1303", {}).get("tx_queue", {}).get("queue_size", 15),
                "inter_packet_delay":   cfg.get("wm1303", {}).get("tx_queue", {}).get("tx_delay_ms", 0),
                "packet_ttl":           adv.get("tx_packet_ttl_seconds", 5),
                "overflow_policy":      adv.get("tx_overflow_policy", "drop_oldest"),
                "nf_interval":          adv.get("noise_floor_interval_seconds", 30),
                "nf_tx_hold":           adv.get("noise_floor_tx_hold_seconds", 2),
                "nf_buffer_size":       adv.get("noise_floor_buffer_size", 20),
                "force_host_fe_ctrl":   hal.get("force_host_fe_ctrl", False),
                "lna_lut":              hal.get("lna_lut", "0x03"),
                "pa_lut":               hal.get("pa_lut", "0x04"),
                "agc_ana_gain":         hal.get("agc_ana_gain", "auto"),
                "agc_dec_gain":         hal.get("agc_dec_gain", "auto"),
                "channelizer_fixed_gain": hal.get("channelizer_fixed_gain", False),
                "gpio_base_offset":     gpio.get("gpio_base_offset", 512),
                "sx1302_reset_pin":     gpio.get("sx1302_reset", 17),
                "sx1302_power_en_pin":  gpio.get("sx1302_power_en", 18),
                "sx1261_reset_pin":     gpio.get("sx1261_reset", 5),
                "ad5338r_reset_pin":    gpio.get("ad5338r_reset", 13),
                "sx1302_spi_path":      spi.get("sx1302_spi_path", "/dev/spidev0.0"),
                "sx1261_spi_path":      spi.get("sx1261_spi_path", "/dev/spidev0.1"),
                "tx_delay_factor":      cfg.get("delays", {}).get("tx_delay_factor", 0.5),
                "agc_reload_interval_s": hal.get("agc_reload_interval_s", 300),
            }
            return _j(result)
        except Exception as e:
            logger.error("adv_config_get error: %s", e)
            return _j({"error": str(e)})

    def _adv_config_post(self):
        """Save advanced config parameters and restart service."""
        import subprocess as _sp
        import yaml
        try:
            body = _body()
            group = body.get("group", "")
            params = body.get("params", {})

            if not group or not params:
                return _j({"status": "error", "error": "Missing group or params"})

            logger.info("adv_config_post: group=%s params=%s", group, params)

            cfg = {}
            cfg_path = "/etc/pymc_repeater/config.yaml"
            try:
                with open(cfg_path) as f:
                    cfg = yaml.safe_load(f) or {}
            except Exception:
                pass

            ui = _load_ui()
            adv = ui.setdefault("adv_config", {})
            hal = ui.setdefault("hal_advanced", {})
            cfg_changed = False

            if group == "dedup_cache":
                if "dedup_ttl_seconds" in params:
                    cfg.setdefault("bridge", {})["dedup_ttl_seconds"] = int(params["dedup_ttl_seconds"])
                    cfg_changed = True
                if "cache_ttl" in params:
                    cfg.setdefault("repeater", {})["cache_ttl"] = int(params["cache_ttl"])
                    cfg_changed = True
                if "max_cache_size" in params:
                    cfg.setdefault("repeater", {})["max_cache_size"] = int(params["max_cache_size"])
                    cfg_changed = True
                    # Clear any legacy value so UI slider is the single source of truth.
                    if "max_cache_size" in adv:
                        adv.pop("max_cache_size", None)

            elif group == "tx_queue":
                tq = cfg.setdefault("wm1303", {}).setdefault("tx_queue", {})
                if "queue_size" in params:
                    tq["queue_size"] = int(params["queue_size"])
                    cfg_changed = True
                if "inter_packet_delay" in params:
                    tq["tx_delay_ms"] = int(params["inter_packet_delay"])
                    cfg_changed = True
                if "packet_ttl" in params:
                    adv["tx_packet_ttl_seconds"] = int(params["packet_ttl"])
                if "overflow_policy" in params:
                    adv["tx_overflow_policy"] = str(params["overflow_policy"])

            elif group == "config":
                delays = cfg.setdefault("delays", {})
                if "tx_delay_factor" in params:
                    delays["tx_delay_factor"] = float(params["tx_delay_factor"])
                    cfg_changed = True

            elif group == "noise_floor":
                if "nf_interval" in params:
                    adv["noise_floor_interval_seconds"] = int(params["nf_interval"])
                if "nf_tx_hold" in params:
                    adv["noise_floor_tx_hold_seconds"] = int(params["nf_tx_hold"])
                if "nf_buffer_size" in params:
                    adv["noise_floor_buffer_size"] = int(params["nf_buffer_size"])

            elif group == "hal_advanced":
                if "force_host_fe_ctrl" in params:
                    hal["force_host_fe_ctrl"] = bool(params["force_host_fe_ctrl"])
                if "lna_lut" in params:
                    hal["lna_lut"] = str(params["lna_lut"])
                if "pa_lut" in params:
                    hal["pa_lut"] = str(params["pa_lut"])
                if "agc_ana_gain" in params:
                    hal["agc_ana_gain"] = str(params["agc_ana_gain"])
                if "agc_dec_gain" in params:
                    hal["agc_dec_gain"] = str(params["agc_dec_gain"])
                if "channelizer_fixed_gain" in params:
                    hal["channelizer_fixed_gain"] = bool(params["channelizer_fixed_gain"])
                if "agc_reload_interval_s" in params:
                    hal["agc_reload_interval_s"] = int(params["agc_reload_interval_s"])

            elif group == "gpio_pins":
                gpio = ui.setdefault("gpio_pins", {})
                if "gpio_base_offset" in params:
                    gpio["gpio_base_offset"] = int(params["gpio_base_offset"])
                if "sx1302_reset_pin" in params:
                    gpio["sx1302_reset"] = int(params["sx1302_reset_pin"])
                if "sx1302_power_en_pin" in params:
                    gpio["sx1302_power_en"] = int(params["sx1302_power_en_pin"])
                if "sx1261_reset_pin" in params:
                    gpio["sx1261_reset"] = int(params["sx1261_reset_pin"])
                if "ad5338r_reset_pin" in params:
                    gpio["ad5338r_reset"] = int(params["ad5338r_reset_pin"])
                ui["gpio_pins"] = gpio
                # Regenerate GPIO shell scripts with new pin assignments
                try:
                    _regenerate_gpio_scripts(gpio)
                    logger.info("adv_config: regenerated GPIO scripts")
                except Exception as e:
                    logger.error("adv_config: failed to regenerate GPIO scripts: %s", e)

            elif group == "spi_devices":
                spi = ui.setdefault("spi_devices", {})
                if "sx1302_spi_path" in params:
                    spi["sx1302_spi_path"] = str(params["sx1302_spi_path"]).strip()
                if "sx1261_spi_path" in params:
                    spi["sx1261_spi_path"] = str(params["sx1261_spi_path"]).strip()
                ui["spi_devices"] = spi
                # Update global_conf.json with new SPI paths
                try:
                    gc_path = Path(_PKTFWD_DIR) / "global_conf.json"
                    if gc_path.exists():
                        gc = json.loads(gc_path.read_text())
                        if "sx1302_spi_path" in params:
                            gc.setdefault("SX130x_conf", {})["com_path"] = spi["sx1302_spi_path"]
                        if "sx1261_spi_path" in params:
                            gc.setdefault("SX130x_conf", {}).setdefault("sx1261_conf", {})["spi_path"] = spi["sx1261_spi_path"]
                        _safe_write(gc_path, json.dumps(gc, indent=2))
                        cfg_changed = False  # already written directly
                        logger.info("adv_config: updated SPI paths in global_conf.json")
                except Exception as e:
                    logger.error("adv_config: failed to update global_conf.json SPI paths: %s", e)

            else:
                return _j({"status": "error", "error": "Unknown group: " + group})

            ui["adv_config"] = adv
            ui["hal_advanced"] = hal
            _save_ui(ui)
            logger.info("adv_config: saved UI JSON")

            if cfg_changed:
                try:
                    with open(cfg_path, "w") as f:
                        yaml.dump(cfg, f, default_flow_style=False)
                    logger.info("adv_config: saved config.yaml")
                except OSError as e:
                    logger.warning("adv_config: could not write config.yaml: %s", e)

            restarted = False
            try:
                _sp.Popen(["sudo", "systemctl", "restart", _SVC_NAME])
                restarted = True
                logger.info("adv_config: service restart triggered")
            except Exception as e:
                logger.error("adv_config: service restart failed: %s", e)

            return _j({"status": "ok", "group": group, "service_restarted": restarted})

        except Exception as e:
            logger.error("adv_config_post error: %s", e)
            return _j({"status": "error", "error": str(e)})



    # ------------------------------------------------------------------ #
    #  Channel E  (LoRa RX)                                        #
    # ------------------------------------------------------------------ #
    def _channel_e_get(self):
        """Return Channel E LoRa RX channel configuration (SSOT: wm1303_ui.json)."""
        try:
            ui = _load_ui()
            che = ui.get("channel_e", {})
            cr_raw = che.get("coding_rate", "4/5")
            if isinstance(cr_raw, int):
                cr_str = {1:"4/5",2:"4/6",3:"4/7",4:"4/8"}.get(cr_raw, "4/5")
            else:
                cr_str = str(cr_raw) if cr_raw else "4/5"
            result = {
                "status": "ok",
                "enabled": che.get("enabled", False),
                "enable": che.get("enabled", False),
                "active": che.get("enabled", False),
                "frequency": che.get("frequency", 869618000),
                "bandwidth": che.get("bandwidth", 62500),
                "spreading_factor": che.get("spreading_factor", 8),
                "coding_rate": cr_str,
                "boosted_rx": che.get("boosted_rx", False),
                "name": che.get("name", che.get("friendly_name", "Channel E")),
                "friendly_name": che.get("friendly_name", "Channel E"),
                "preamble_length": che.get("preamble_length", 17),
                "lbt_enabled": che.get("lbt_enabled", False),
                "lbt_threshold": che.get("lbt_threshold", -80),
                "lbt_rssi_target": che.get("lbt_threshold", -80),
                "cad_enabled": che.get("cad_enabled", False),
                "tx_power": che.get("tx_power", 27),
            }
            return _j(result)
        except Exception as ex:
            logger.error("_channel_e_get: %s", ex)
            return _j({"status": "error", "reason": str(ex)})

    def _channel_e_post(self):
        """Save Channel E LoRa RX channel configuration."""
        try:
            body = json.loads(cherrypy.request.body.read())
            restart = body.pop("restart", False)
            gc = _load_global_conf()
            sx_conf = gc.setdefault("SX130x_conf", {}).setdefault("sx1261_conf", {})
            lora_rx = sx_conf.setdefault("lora_rx", {})
            lora_rx["enable"] = bool(body.get("enable", body.get("enabled", lora_rx.get("enable", False))))
            if "frequency" in body:
                lora_rx["freq_hz"] = int(body["frequency"])
            if "bandwidth" in body:
                lora_rx["bandwidth"] = int(body["bandwidth"])
            if "spreading_factor" in body:
                lora_rx["spreading_factor"] = int(body["spreading_factor"])
            if "coding_rate" in body:
                _cr_val = body["coding_rate"]
                _cr_s2i = {"4/5": 1, "4/6": 2, "4/7": 3, "4/8": 4}
                if isinstance(_cr_val, str) and _cr_val in _cr_s2i:
                    lora_rx["coding_rate"] = _cr_s2i[_cr_val]
                else:
                    try:
                        lora_rx["coding_rate"] = int(_cr_val)
                    except (ValueError, TypeError):
                        lora_rx["coding_rate"] = 1
            lbt = sx_conf.setdefault("lbt", {})
            if "lbt_enabled" in body:
                lbt["enable"] = bool(body["lbt_enabled"])
            if "lbt_threshold" in body or "lbt_rssi_target" in body:
                lbt["rssi_target"] = int(body.get("lbt_rssi_target", body.get("lbt_threshold", -80)))
            (_PKTFWD_DIR / "global_conf.json").write_text(json.dumps(gc, indent=2))
            (_PKTFWD_DIR / "bridge_conf.json").write_text(json.dumps(gc, indent=2))
            ui = _load_ui()
            che_ui = ui.setdefault("channel_e", {})
            # Note: sync_word is intentionally NOT accepted here. It is a
            # device-wide setting stored at the top-level of wm1303_ui.json
            # and managed via /api/wm1303/sync_word. Any sync_word value in
            # the request body is silently ignored.
            for key in ["name", "friendly_name", "boosted_rx", "preamble_length", "cad_enabled", "tx_power", "lbt_enabled", "lbt_threshold"]:
                if key in body:
                    che_ui[key] = body[key]
            # Defensive cleanup: drop any leftover per-channel sync_word
            che_ui.pop("sync_word", None)
            che_ui["enabled"] = lora_rx["enable"]
            che_ui["lbt_enabled"] = lbt.get("enable", False)
            che_ui["lbt_threshold"] = lbt.get("rssi_target", -80)
            che_ui["frequency"] = lora_rx.get("freq_hz", 869618000)
            che_ui["bandwidth"] = lora_rx.get("bandwidth", 62500)
            che_ui["spreading_factor"] = lora_rx.get("spreading_factor", 8)
            che_ui["coding_rate"] = {1:"4/5",2:"4/6",3:"4/7",4:"4/8"}.get(lora_rx.get("coding_rate",1), "4/5")
            _save_ui(ui)
            logger.info("channel_e config saved: freq=%s bw=%s sf=%s cr=%s enabled=%s",
                        lora_rx.get("freq_hz"), lora_rx.get("bandwidth"),
                        lora_rx.get("spreading_factor"), lora_rx.get("coding_rate"),
                        lora_rx["enable"])
            if restart:
                import subprocess as _sp_r, threading as _thr_r
                def _do_restart():
                    import time as _t; _t.sleep(1)
                    _sp_r.run(["sudo", "systemctl", "restart", _SVC_NAME], capture_output=True, timeout=30)
                _thr_r.Thread(target=_do_restart, daemon=True).start()
            return _j({"status": "ok", "restart": restart})
        except Exception as ex:
            logger.error("_channel_e_post: %s", ex)
            return _j({"status": "error", "reason": str(ex)})

    # ---------------------------------------------------------------
    # Channel F endpoints (Issue #1 multi-region BW250/500 support)
    # ---------------------------------------------------------------
    # Channel F = chan_Lora_std on RF0, runs in PARALLEL with channels A-D
    # (chan_multiSF_0-3). Supports BW125/250/500 single-SF reception.
    # UI-only persistence: wm1303_backend.py reads channel_f from
    # wm1303_ui.json and generates chan_Lora_std dynamically. We do NOT
    # write to global_conf.json from here (unlike channel_e which also
    # writes the SX1261 lora_rx block).
    # ---------------------------------------------------------------
    def _channel_f_get(self):
        """Return Channel F (chan_Lora_std) configuration (SSOT: wm1303_ui.json)."""
        try:
            ui = _load_ui()
            chf = ui.get("channel_f", {})
            cr_raw = chf.get("coding_rate", "4/5")
            if isinstance(cr_raw, int):
                cr_str = {1: "4/5", 2: "4/6", 3: "4/7", 4: "4/8"}.get(cr_raw, "4/5")
            else:
                cr_str = str(cr_raw) if cr_raw else "4/5"
            result = {
                "status": "ok",
                "enabled": chf.get("enabled", False),
                "enable": chf.get("enabled", False),
                "active": chf.get("enabled", False),
                "frequency": chf.get("frequency", 869525000),
                "bandwidth": chf.get("bandwidth", 250000),
                "spreading_factor": chf.get("spreading_factor", 9),
                "coding_rate": cr_str,
                "name": chf.get("name", chf.get("friendly_name", "Channel F")),
                "friendly_name": chf.get("friendly_name", "Channel F"),
                "preamble_length": chf.get("preamble_length", 16),
                "tx_power": chf.get("tx_power", 22),
                "lbt_enabled": chf.get("lbt_enabled", False),
                "lbt_threshold": chf.get("lbt_threshold", -80),
                "lbt_rssi_target": chf.get("lbt_rssi_target", chf.get("lbt_threshold", -80)),
                "cad_enabled": chf.get("cad_enabled", False),
                "boosted_rx": chf.get("boosted_rx", False),
            }
            return _j(result)
        except Exception as ex:
            logger.error("_channel_f_get: %s", ex)
            return _j({"status": "error", "reason": str(ex)})

    def _channel_f_post(self):
        """Save Channel F (chan_Lora_std) configuration to wm1303_ui.json only.

        The backend (wm1303_backend.py _generate_bridge_conf) reads channel_f
        from wm1303_ui.json on each (re)start and constructs chan_Lora_std
        dynamically with the correct if-offset, bandwidth and SF. Therefore
        no global_conf.json write is required here. Pass restart=true in the
        body to apply changes via systemctl restart pymc-repeater.
        """
        try:
            body = json.loads(cherrypy.request.body.read())
            restart = body.pop("restart", False)

            # Validation: bandwidth must be one of the chan_Lora_std-supported values
            bw_in = body.get("bandwidth")
            if bw_in is not None:
                try:
                    bw_val = int(bw_in)
                except (TypeError, ValueError):
                    return _j({"status": "error", "reason": "bandwidth must be integer Hz"})
                if bw_val not in (125000, 250000, 500000):
                    return _j({
                        "status": "error",
                        "reason": "bandwidth must be 125000, 250000 or 500000 Hz",
                    })

            # Validation: spreading_factor 5..12
            sf_in = body.get("spreading_factor")
            if sf_in is not None:
                try:
                    sf_val = int(sf_in)
                except (TypeError, ValueError):
                    return _j({"status": "error", "reason": "spreading_factor must be integer"})
                if sf_val < 5 or sf_val > 12:
                    return _j({
                        "status": "error",
                        "reason": "spreading_factor must be in range 5..12",
                    })

            ui = _load_ui()
            chf_ui = ui.setdefault("channel_f", {})

            # enable/enabled aliases
            if "enabled" in body or "enable" in body:
                chf_ui["enabled"] = bool(body.get("enabled", body.get("enable", False)))

            if "frequency" in body:
                chf_ui["frequency"] = int(body["frequency"])
            if "bandwidth" in body:
                chf_ui["bandwidth"] = int(body["bandwidth"])
            if "spreading_factor" in body:
                chf_ui["spreading_factor"] = int(body["spreading_factor"])
            if "coding_rate" in body:
                _cr_val = body["coding_rate"]
                _cr_i2s = {1: "4/5", 2: "4/6", 3: "4/7", 4: "4/8"}
                if isinstance(_cr_val, int) and _cr_val in _cr_i2s:
                    chf_ui["coding_rate"] = _cr_i2s[_cr_val]
                else:
                    chf_ui["coding_rate"] = str(_cr_val) if _cr_val else "4/5"

            # Note: sync_word is intentionally NOT accepted here. It is a
            # device-wide setting stored at the top-level of wm1303_ui.json
            # and managed via /api/wm1303/sync_word. Any sync_word value in
            # the request body is silently ignored.
            for key in (
                "name",
                "friendly_name",
                "preamble_length",
                "tx_power",
                "lbt_enabled",
                "lbt_threshold",
                "lbt_rssi_target",
                "cad_enabled",
                "boosted_rx",
            ):
                if key in body:
                    chf_ui[key] = body[key]

            # Defensive cleanup: drop any leftover per-channel sync_word
            chf_ui.pop("sync_word", None)

            _save_ui(ui)
            logger.info(
                "channel_f config saved: enabled=%s freq=%s bw=%s sf=%s",
                chf_ui.get("enabled"),
                chf_ui.get("frequency"),
                chf_ui.get("bandwidth"),
                chf_ui.get("spreading_factor"),
            )

            if restart:
                import subprocess as _sp_r, threading as _thr_r
                def _do_restart():
                    import time as _t
                    _t.sleep(1)
                    _sp_r.run(["sudo", "systemctl", "restart", _SVC_NAME],
                              capture_output=True, timeout=30)
                _thr_r.Thread(target=_do_restart, daemon=True).start()

            return _j({"status": "ok", "restart": restart})
        except Exception as ex:
            logger.error("_channel_f_post: %s", ex)
            return _j({"status": "error", "reason": str(ex)})

    # ---------------------------------------------------------------
    # Region & Preset endpoints (Issue #4 multi-region support)
    # ---------------------------------------------------------------
    def _regions_get(self):
        """GET /api/wm1303/regions - list all available regulatory regions.

        Returns a JSON array of region summaries (code, label, tx_freq_min/max, etc.).
        """
        try:
            from pymc_core.hardware.region_config import (
                REGIONS as _REGIONS,
                get_region_summary as _summary,
            )
        except Exception as ex:
            logger.warning("_regions_get: region_config not available: %s", ex)
            return _j({"regions": [], "error": "region_config module not available"})
        try:
            out = []
            for code in _REGIONS.keys():
                out.append(_summary(code))
            return _j({"regions": out, "count": len(out)})
        except Exception as ex:
            logger.error("_regions_get: %s", ex)
            return _j({"regions": [], "error": str(ex)})

    def _region_get(self):
        """GET /api/wm1303/region - return the currently selected region from UI config."""
        try:
            ui = _load_ui()
            r = ui.get("region", "EU868")
            if isinstance(r, str):
                r = {"code": r.upper(), "tx_freq_min": None, "tx_freq_max": None}
            elif isinstance(r, dict):
                r = {
                    "code": str(r.get("code", "EU868")).upper(),
                    "tx_freq_min": r.get("tx_freq_min"),
                    "tx_freq_max": r.get("tx_freq_max"),
                }
            # Add summary info for the active region
            try:
                from pymc_core.hardware.region_config import (
                    get_region_summary as _summary,
                    get_tx_bounds as _bounds,
                )
                r["summary"] = _summary(r["code"])
                _mn, _mx = _bounds(
                    r["code"],
                    custom_min=r.get("tx_freq_min"),
                    custom_max=r.get("tx_freq_max"),
                )
                r["resolved_tx_freq_min"] = _mn
                r["resolved_tx_freq_max"] = _mx
            except Exception as ex:
                logger.debug("_region_get: summary unavailable: %s", ex)
            return _j(r)
        except Exception as ex:
            logger.error("_region_get: %s", ex)
            return _j({"code": "EU868", "error": str(ex)})

    def _region_post(self):
        """POST/PUT /api/wm1303/region - update the regulatory region in UI config.

        Body JSON: {"code": "AU915", optional: "tx_freq_min": 915000000, "tx_freq_max": 928000000}
        For CUSTOM region, tx_freq_min and tx_freq_max must be provided.
        """
        try:
            body = _body()
            code = str(body.get("code", "")).strip().upper()
            if not code:
                cherrypy.response.status = 400
                return _j({"status": "error", "reason": "missing 'code' field"})
            # Validate region code against known regions
            try:
                from pymc_core.hardware.region_config import REGIONS as _REGIONS
                if code not in _REGIONS:
                    cherrypy.response.status = 400
                    return _j({
                        "status": "error",
                        "reason": "unknown region code",
                        "valid_codes": list(_REGIONS.keys()),
                    })
            except Exception:
                pass  # region_config not available, accept anyway
            # For CUSTOM, require frequency bounds
            _custom_min = body.get("tx_freq_min")
            _custom_max = body.get("tx_freq_max")
            if code == "CUSTOM":
                if not _custom_min or not _custom_max:
                    cherrypy.response.status = 400
                    return _j({
                        "status": "error",
                        "reason": "CUSTOM region requires tx_freq_min and tx_freq_max",
                    })
            # Persist to UI config
            ui = _load_ui()
            ui["region"] = {
                "code": code,
                "tx_freq_min": int(_custom_min) if _custom_min else None,
                "tx_freq_max": int(_custom_max) if _custom_max else None,
            }
            _save_ui(ui)
            logger.info("_region_post: region updated to %s", code)
            return _j({"status": "ok", "region": ui["region"]})
        except Exception as ex:
            logger.error("_region_post: %s", ex)
            cherrypy.response.status = 500
            return _j({"status": "error", "reason": str(ex)})

    # ---------------------------------------------------------------
    # Sync word endpoints (device-wide LoRa network sync word)
    # ---------------------------------------------------------------
    # sync_word is a DEVICE-WIDE setting stored at the top level of
    # wm1303_ui.json (NOT per-channel). HAL v2.10 exposes lorawan_public
    # as a board-level flag (lgw_conf_board_t).
    #
    # SX1302 hardware supports ONLY two sync word values:
    #   - Private (0x1424 / 5156)  -> lorawan_public = false
    #   - Public  (0x3444 / 13380) -> lorawan_public = true
    # The HAL function sx1302_lora_syncword() hard-codes the peak positions
    # for these two values only; any other value is undefined behavior and
    # breaks interoperability with MeshCore and standard LoRa hardware.
    # Custom sync words are therefore NOT accepted by this API.
    # ---------------------------------------------------------------
    _SYNC_WORD_PRIVATE = 5156   # 0x1424
    _SYNC_WORD_PUBLIC = 13380   # 0x3444

    def _sync_word_get(self):
        """GET /api/wm1303/sync_word - return the device-wide LoRa sync word.

        Returns: {"value": int, "mode": "private|public", "hex": "0xNNNN"}

        Legacy configs with mode=="custom" are normalized to Private for
        backward compatibility (SX1302 hardware cannot honor Custom).
        """
        try:
            ui = _load_ui()
            sw = ui.get("sync_word")
            if isinstance(sw, dict):
                value = int(sw.get("value", self._SYNC_WORD_PRIVATE))
                mode = str(sw.get("mode", "private")).lower()
            elif isinstance(sw, int):
                # Legacy/compat: bare integer at top level
                value = int(sw)
                mode = "private"
            else:
                value = self._SYNC_WORD_PRIVATE
                mode = "private"
            # Normalize: only Private/Public are valid. Any other value
            # (including legacy "custom") falls back to Private.
            if value == self._SYNC_WORD_PUBLIC:
                mode = "public"
            else:
                # Anything else -> Private (covers legacy custom configs)
                value = self._SYNC_WORD_PRIVATE
                mode = "private"
            return _j({
                "value": value,
                "mode": mode,
                "hex": "0x{:04X}".format(value & 0xFFFF),
            })
        except Exception as ex:
            logger.error("_sync_word_get: %s", ex)
            return _j({
                "value": self._SYNC_WORD_PRIVATE,
                "mode": "private",
                "hex": "0x1424",
                "error": str(ex),
            })

    def _sync_word_post(self):
        """POST/PUT /api/wm1303/sync_word - update the device-wide LoRa sync word.

        Body JSON: {"mode": "private|public", optional "restart": bool}

        - Private: value forced to 0x1424 (5156)
        - Public:  value forced to 0x3444 (13380)

        Custom sync words are NOT supported: SX1302 hardware can only set
        the board-level lorawan_public flag, which selects between these
        two predefined values. Requests with mode=="custom" return HTTP 400.
        """
        try:
            body = _body()
            restart = bool(body.pop("restart", False))
            mode = str(body.get("mode", "")).strip().lower()
            if mode == "custom":
                cherrypy.response.status = 400
                return _j({
                    "status": "error",
                    "reason": "custom mode not supported, hardware limitation",
                })
            if mode not in ("private", "public"):
                cherrypy.response.status = 400
                return _j({
                    "status": "error",
                    "reason": "mode must be one of: private, public",
                })
            if mode == "private":
                value = self._SYNC_WORD_PRIVATE
            else:  # public
                value = self._SYNC_WORD_PUBLIC
            ui = _load_ui()
            ui["sync_word"] = {"value": int(value), "mode": mode}
            _save_ui(ui)
            logger.info("_sync_word_post: device sync_word updated to 0x%04X (%s)", value, mode)
            if restart:
                import subprocess as _sp_r, threading as _thr_r
                def _do_restart():
                    import time as _t
                    _t.sleep(1)
                    _sp_r.run(["sudo", "systemctl", "restart", _SVC_NAME],
                              capture_output=True, timeout=30)
                _thr_r.Thread(target=_do_restart, daemon=True).start()
            return _j({
                "status": "ok",
                "sync_word": {
                    "value": int(value),
                    "mode": mode,
                    "hex": "0x{:04X}".format(value & 0xFFFF),
                },
                "restart": restart,
            })
        except Exception as ex:
            logger.error("_sync_word_post: %s", ex)
            cherrypy.response.status = 500
            return _j({"status": "error", "reason": str(ex)})

    def _presets_get(self):
        """GET /api/wm1303/presets - return community channel presets.

        Reads /etc/pymc_repeater/presets.json (deployed by installer) or falls back
        to /opt/pymc_repeater/presets.json. Returns the parsed JSON content.
        """
        from pathlib import Path as _Path
        _candidates = [
            _Path("/etc/pymc_repeater/presets.json"),
            _Path("/opt/pymc_repeater/presets.json"),
        ]
        for p in _candidates:
            if p.exists():
                try:
                    import json as _json
                    data = _json.loads(p.read_text())
                    return _j(data)
                except Exception as ex:
                    logger.error("_presets_get: error reading %s: %s", p, ex)
                    return _j({"presets": [], "error": str(ex)})
        return _j({"presets": [], "note": "presets.json not deployed"})

# --- Background unified 60s recorder (packet_activity + cad + origin + crc) ---
# Memory optimization (v2.4.6): Previously two separate daemon threads ran
# identical 60s loops for packet_activity and crc_error_rate recording. They
# have been merged into a single unified thread to reduce thread count and
# SQLite connection overhead. All DB access now uses _db_conn() which
# properly closes connections (preventing the connection leak that was
# present in the old raw sqlite3.connect() pattern).
_pkt_act_last_counts = {}  # {channel_id: {"rx": N, "tx": N}} cumulative from previous interval
_cad_last_counts = {}  # {channel_id: {"cad_clear": N, ...}}
import threading as _thr_pkt


def _init_unified_recorder_tables():
    """Create all tables used by the unified 60s recorder (idempotent)."""
    _db = "/var/lib/pymc_repeater/repeater.db"
    try:
        with _db_conn(_db) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=5000")
            # packet_activity table
            conn.execute("""CREATE TABLE IF NOT EXISTS packet_activity (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp REAL NOT NULL,
                channel_id TEXT NOT NULL,
                rx_count INTEGER DEFAULT 0,
                tx_count INTEGER DEFAULT 0)""")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_pktact_ts ON packet_activity(timestamp)")
            # cad_events table (HW/SW split)
            conn.execute("""CREATE TABLE IF NOT EXISTS cad_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp REAL NOT NULL,
                channel_id TEXT NOT NULL,
                cad_clear INTEGER DEFAULT 0,
                cad_detected INTEGER DEFAULT 0,
                cad_skipped INTEGER DEFAULT 0,
                cad_hw_clear INTEGER DEFAULT 0,
                cad_hw_detected INTEGER DEFAULT 0,
                cad_sw_clear INTEGER DEFAULT 0,
                cad_sw_detected INTEGER DEFAULT 0)""")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_cadevt_ts ON cad_events(timestamp)")
            for _col in ('cad_hw_clear', 'cad_hw_detected', 'cad_sw_clear', 'cad_sw_detected'):
                try:
                    conn.execute(f"ALTER TABLE cad_events ADD COLUMN {_col} INTEGER DEFAULT 0")
                except Exception:
                    pass  # column already exists
            # origin_channel_stats table
            conn.execute("""CREATE TABLE IF NOT EXISTS origin_channel_stats (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp REAL NOT NULL,
                channel_id TEXT NOT NULL,
                count INTEGER DEFAULT 0)""")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_origin_ch_ts ON origin_channel_stats(timestamp)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_origin_ch_id ON origin_channel_stats(channel_id)")
            # crc_error_rate table
            conn.execute("""CREATE TABLE IF NOT EXISTS crc_error_rate (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp REAL NOT NULL,
                channel_id TEXT NOT NULL,
                crc_error_count INTEGER NOT NULL DEFAULT 0,
                crc_disabled_count INTEGER NOT NULL DEFAULT 0)""")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_crcrate_ts ON crc_error_rate(timestamp)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_crcrate_ch_ts ON crc_error_rate(channel_id, timestamp)")
    except Exception as _init_e:
        logger.debug("unified_recorder_tables init: %s", _init_e)


def _record_packet_activity_once(now):
    """Record one 60s sample of per-channel RX/TX + CAD + origin stats."""
    global _pkt_act_last_counts, _cad_last_counts
    _db = "/var/lib/pymc_repeater/repeater.db"
    _bk = _get_backend()
    if not _bk:
        return
    try:
        ch_stats = _bk.get_channel_stats()
    except Exception:
        return
    if not ch_stats:
        return
    # Packet activity deltas
    inserts = []
    for ch_id, stats in ch_stats.items():
        cur_rx = stats.get("rx_count", 0) or 0
        cur_tx = stats.get("tx_count", 0) or 0
        prev = _pkt_act_last_counts.get(ch_id)
        if prev is not None:
            delta_rx = max(0, cur_rx - prev["rx"])
            delta_tx = max(0, cur_tx - prev["tx"])
            if delta_rx > 0 or delta_tx > 0:
                inserts.append((now, ch_id, delta_rx, delta_tx))
            else:
                inserts.append((now, ch_id, 0, 0))  # keep timeline continuous
        else:
            inserts.append((now, ch_id, 0, 0))  # baseline
        _pkt_act_last_counts[ch_id] = {"rx": cur_rx, "tx": cur_tx}
    # CAD deltas
    cad_inserts = []
    try:
        if hasattr(_bk, '_tx_queue_manager') and _bk._tx_queue_manager:
            for ch_id, q in _bk._tx_queue_manager.queues.items():
                cur_clear = q.stats.get("cad_clear", 0) or 0
                cur_det = q.stats.get("cad_detected", 0) or 0
                cur_hw_clear = q.stats.get("cad_hw_clear", 0) or 0
                cur_hw_det = q.stats.get("cad_hw_detected", 0) or 0
                cur_sw_clear = q.stats.get("cad_sw_clear", 0) or 0
                cur_sw_det = q.stats.get("cad_sw_detected", 0) or 0
                prev_cad = _cad_last_counts.get(ch_id)
                if prev_cad is not None:
                    d_clear = max(0, cur_clear - prev_cad.get("cad_clear", 0))
                    d_det = max(0, cur_det - prev_cad.get("cad_detected", 0))
                    d_hw_clear = max(0, cur_hw_clear - prev_cad.get("cad_hw_clear", 0))
                    d_hw_det = max(0, cur_hw_det - prev_cad.get("cad_hw_detected", 0))
                    d_sw_clear = max(0, cur_sw_clear - prev_cad.get("cad_sw_clear", 0))
                    d_sw_det = max(0, cur_sw_det - prev_cad.get("cad_sw_detected", 0))
                    cad_inserts.append((now, ch_id, d_clear, d_det, 0,
                                        d_hw_clear, d_hw_det,
                                        d_sw_clear, d_sw_det))
                else:
                    cad_inserts.append((now, ch_id, 0, 0, 0, 0, 0, 0, 0))
                _cad_last_counts[ch_id] = {
                    "cad_clear": cur_clear,
                    "cad_detected": cur_det,
                    "cad_hw_clear": cur_hw_clear,
                    "cad_hw_detected": cur_hw_det,
                    "cad_sw_clear": cur_sw_clear,
                    "cad_sw_detected": cur_sw_det,
                }
    except Exception as _cad_e:
        logger.debug("unified_recorder CAD: %s", _cad_e)
    # Origin channel stats
    origin_inserts = []
    try:
        from repeater.bridge_engine import _active_bridge
        if _active_bridge:
            origin_counts = _active_bridge.get_and_reset_origin_counts()
            if origin_counts:
                origin_inserts = [(now, ch_id, cnt) for ch_id, cnt in origin_counts.items() if cnt > 0]
    except Exception as _origin_e:
        logger.debug("unified_recorder origin: %s", _origin_e)
    # Single DB write for all packet_activity tables
    try:
        with _db_conn(_db) as conn:
            if inserts:
                conn.executemany(
                    "INSERT INTO packet_activity (timestamp, channel_id, rx_count, tx_count) VALUES (?,?,?,?)",
                    inserts)
            if cad_inserts:
                conn.executemany(
                    "INSERT INTO cad_events (timestamp, channel_id, cad_clear, cad_detected, cad_skipped, cad_hw_clear, cad_hw_detected, cad_sw_clear, cad_sw_detected) VALUES (?,?,?,?,?,?,?,?,?)",
                    cad_inserts)
            if origin_inserts:
                conn.executemany(
                    "INSERT INTO origin_channel_stats (timestamp, channel_id, count) VALUES (?,?,?)",
                    origin_inserts)
    except Exception as _wr_e:
        logger.debug("unified_recorder write packet_activity: %s", _wr_e)


def _record_crc_error_rate_once(now):
    """Record one 60s sample of per-channel CRC error/disabled counts."""
    _db = "/var/lib/pymc_repeater/repeater.db"
    _bk = _get_backend()
    if not _bk:
        return
    try:
        counters = _bk.get_and_reset_crc_rate_counters()
    except Exception:
        return
    if not counters:
        return
    inserts = []
    for ch_id, counts in counters.items():
        crc_err = counts.get("crc_error", 0)
        crc_dis = counts.get("crc_disabled", 0)
        inserts.append((now, ch_id, crc_err, crc_dis))
    if not inserts:
        return
    try:
        with _db_conn(_db) as conn:
            conn.executemany(
                "INSERT INTO crc_error_rate (timestamp, channel_id, crc_error_count, crc_disabled_count) VALUES (?,?,?,?)",
                inserts)
    except Exception as _wr_e:
        logger.debug("unified_recorder write crc: %s", _wr_e)


def _unified_60s_recorder():
    """Single background thread that runs all 60s periodic recorders.
    Replaces _packet_activity_recorder and _crc_error_rate_recorder threads."""
    # Init tables once at startup
    _init_unified_recorder_tables()
    while True:
        try:
            time.sleep(60)
            now = time.time()
            # Each sub-recorder has its own try/except so a failure in one
            # does not affect the others.
            try:
                _record_packet_activity_once(now)
            except Exception as _e1:
                logger.debug("unified_recorder: packet_activity failed: %s", _e1)
            try:
                _record_crc_error_rate_once(now)
            except Exception as _e2:
                logger.debug("unified_recorder: crc_error_rate failed: %s", _e2)
        except Exception as _e:
            logger.debug("unified_recorder loop: %s", _e)
            try:
                time.sleep(60)
            except Exception:
                break


_unified_rec_thread = _thr_pkt.Thread(target=_unified_60s_recorder,
                                      daemon=True, name="unified-60s-recorder")
_unified_rec_thread.start()


# Auto-start spectrum collector
if _COLLECTOR_AVAILABLE:
    try:
        _sc = get_collector()
    except Exception as _e:
        import logging
        logging.getLogger("wm1303_api").warning(f"Failed to start spectrum collector: {_e}")
