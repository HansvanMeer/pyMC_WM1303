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
        Path("/home/pi/wm1303_pf/bridge_conf.json"),
        Path("/home/pi/wm1303_pf/global_conf.json"),
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
import subprocess
import logging
import time
from pathlib import Path

_STATUS_CACHE={}
_STATUS_CACHE_TTL=8


logger = logging.getLogger(__name__)

# Paths
_SVC_NAME    = "pymc-repeater"
_UI_JSON     = Path("/etc/pymc_repeater/wm1303_ui.json")
_GLOBAL_CONF = Path("/home/pi/wm1303_pf/global_conf.json")
_SPECTRAL_BIN = Path("/home/pi/wm1303_pf/spectral_scan")
_SPECTRAL_RES = Path("/tmp/pymc_spectral_results.json")

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

def _load_ui() -> dict:
    if _UI_JSON.exists():
        try:
            return json.loads(_UI_JSON.read_text())
        except Exception:
            pass
    return {"channels": [], "bridge": {"rules": []}}

def _save_ui(data: dict):
    _UI_JSON.write_text(json.dumps(data, indent=2))



def _get_ui_channel_id_map():
    """Map each UI channel index to its actual config.yaml channel key.
    Active UI channels matched by position to config.yaml wm1303.channels keys.
    Inactive channels get a non-colliding 'inactive_N' key."""
    import yaml
    ui_chs = _load_ui().get("channels", [])
    config_keys = []
    try:
        with open("/etc/pymc_repeater/config.yaml") as _f:
            cfg = yaml.safe_load(_f)
        config_keys = list(cfg.get("wm1303", {}).get("channels", {}).keys())
    except Exception:
        pass
    id_map = {}
    used_keys = set()
    active_pos = 0
    for ui_idx, ch in enumerate(ui_chs):
        if ch.get("active", False):
            if active_pos < len(config_keys):
                key = config_keys[active_pos]
            else:
                key = "channel_" + chr(97 + ui_idx)
            id_map[ui_idx] = key
            used_keys.add(key)
            active_pos += 1
        else:
            id_map[ui_idx] = "inactive_" + str(ui_idx)
    return id_map


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
    _BRIDGE_CONF_PATH = Path('/home/pi/wm1303_pf/bridge_conf.json')
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
    _GLOBAL_CONF.write_text('\n'.join(result))
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

    script_dir = '/home/pi/wm1303_pf'
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



class WM1303API:
    exposed = True

    def __init__(self, daemon=None):
        self.daemon = daemon

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
                friendly = ch.get("friendly_name", ch.get("name", "Unknown"))
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

        if resource == "tx_activity":
            return self.tx_activity(**params)

        # -- dedup events (bridge engine dedup visualization) --
        if resource == "dedup":
            sub = args[0] if args else ""
            if method == "GET":
                return self._dedup_events_get(**params)


        # -- per-channel noise floor (enhanced) --
        if resource == "noise_floor":
            if method == "GET":
                return self._noise_floor_get(**params)

        # -- CAD stats --
        if resource == "cad_stats":
            if method == "GET":
                return self._cad_stats_get(**params)

                # -- adv_config (advanced configuration) --
        if resource == "adv_config":
            if method == "GET":
                return self._adv_config_get()
            if method == "POST":
                return self._adv_config_post()

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
        # Read version from VERSION file
        _version = "0.9.315"
        try:
            _vf = Path("/opt/pymc_repeater/repos/pyMC_Repeater/repeater/web/html/VERSION")
            if _vf.exists():
                _version = _vf.read_text().strip()
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
            "spi_path": "/dev/spidev0.0",
            "sx1261_spi": "/dev/spidev0.1",
            "uptime": uptime_str,
            "eui": eui_str,
            "temperature": temperature,
            "packets_received": _pkt_rx,
            "packets_sent": _pkt_tx,
            "packets_forwarded": _pkt_fwd,
            "active_channels": sum(1 for ch in _load_ui().get("channels", []) if ch.get("active", False)),
            "total_channels": 4,
            "inactive_channels": 4 - sum(1 for ch in _load_ui().get("channels", []) if ch.get("active", False)),
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
            ch.setdefault("lbt_rssi_target", -80)
            ch.setdefault("tx_power", 14)
            ch.setdefault("lbt_enabled", False)
            ch.setdefault("cad_enabled", True)
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
                    ch_name = ch.get('friendly_name', ch.get('name', '?'))
                    delta_khz = abs(f - center) / 1000
                    max_khz = max_if / 1000
                    warnings.append(
                        f"Channel '{ch_name}' at {f/1e6:.3f} MHz is {delta_khz:.1f} kHz "
                        f"from center {center/1e6:.3f} MHz (max {max_khz:.1f} kHz for "
                        f"BW {bw/1000:.0f} kHz). It will be force-disabled at startup.")

        ui = _load_ui()
        ui["channels"] = channels
        _save_ui(ui)
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
        """Return aggregated live operational data per channel."""
        import time as _t
        import yaml
        import urllib.request, json as _json2

        # Load channel config from yaml
        channels_config = {}
        try:
            cfg_path = Path("/etc/pymc_repeater/config.yaml")
            with open(cfg_path) as f:
                cfg = yaml.safe_load(f) or {}
            channels_config = cfg.get("wm1303", {}).get("channels", {})
        except Exception:
            pass

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
                _resp = urllib.request.urlopen("http://127.0.0.1:8000/api/wm1303/tx_queues", timeout=2)
                _tq = _json2.loads(_resp.read())
                tx_stats = _tq.get("queues", {})
            except Exception:
                pass

        # Sum packet counts from per-channel backend stats
        total_rx = 0
        total_tx = 0
        for _cn_t, _cs_t in channel_stats.items():
            total_rx += _cs_t.get("rx_count", 0)
            total_tx += _cs_t.get("tx_count", 0)

        # Get service uptime in seconds
        uptime_seconds = 0
        try:
            import subprocess as _sp
            ru = _sp.run(
                ["sudo", "systemctl", "show", _SVC_NAME, "--property=ActiveEnterTimestamp"],
                capture_output=True, text=True, timeout=5
            )
            for line in ru.stdout.splitlines():
                if line.startswith("ActiveEnterTimestamp="):
                    ts_str = line.split("=", 1)[1].strip()
                    if ts_str:
                        from datetime import datetime as _dtc
                        try:
                            dt = _dtc.strptime(ts_str, "%a %Y-%m-%d %H:%M:%S %Z")
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
                scan_points = scan.get("scan_points", [])
                for pt in scan_points:
                    freq_hz = int(pt.get("freq_hz", 0))
                    rssi = pt.get("rssi_dbm", -120)
                    noise_data[freq_hz] = rssi
        except Exception:
            pass

        # Also try spectrum collector DB
        if not noise_data and _COLLECTOR_AVAILABLE:
            try:
                collector = get_collector()
                recent = collector.get_spectrum_history(hours=1)
                for pt in recent:
                    freq_mhz = pt.get("freq_mhz", 0)
                    freq_hz = int(freq_mhz * 1e6)
                    rssi = pt.get("rssi_dbm", -120)
                    if freq_hz not in noise_data or rssi < noise_data[freq_hz]:
                        noise_data[freq_hz] = rssi
            except Exception:
                pass

        def _find_noise(freq_hz, tolerance=150000):
            best = -120.0
            best_dist = float("inf")
            for nf_hz, nf_rssi in noise_data.items():
                dist = abs(nf_hz - freq_hz)
                if dist < tolerance and dist < best_dist:
                    best = nf_rssi
                    best_dist = dist
            result = round(best, 1)
            return result if result != 0 else -120.0

        # Build per-channel live data
        channel_names = ["channel_a", "channel_b", "channel_c", "channel_d"]
        ch_labels = {"channel_a": "ch-a", "channel_b": "ch-b", "channel_c": "ch-c", "channel_d": "ch-d"}
        # Load friendly names from UI config
        _ui_chs = _load_ui().get("channels", [])
        _abc = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
        _friendly_map = {}
        for _idx, _uch in enumerate(_ui_chs):
            _fn = _uch.get("friendly_name", "Channel " + (_abc[_idx] if _idx < len(_abc) else str(_idx + 1)))
            _friendly_map[_idx] = _fn
        channels_live = []
        active_count = sum(1 for cn in channel_names if channels_config.get(cn, {}).get("frequency", 0))
        active_idx = -1
        for ch_name in channel_names:
            ch_cfg = channels_config.get(ch_name, {})
            if not ch_cfg:
                continue
            freq = ch_cfg.get("frequency", 0)
            if not freq:
                continue
            active_idx += 1
            # Get real per-channel stats from backend
            ch_st = channel_stats.get(ch_name, {})
            ch_tx = tx_stats.get(ch_name, {})
            rx_count = ch_st.get("rx_count", 0)
            tx_sent = ch_st.get("tx_count", 0) or ch_tx.get("total_sent", 0)
            tx_failed = ch_st.get("tx_failed", 0) or ch_tx.get("total_failed", 0)
            last_tx = ch_st.get("last_tx_time") or ch_tx.get("last_tx_time")
            last_rx = ch_st.get("last_rx_time")
            rssi_last = ch_st.get("last_rssi", -120.0)
            rssi_avg = ch_st.get("rssi_avg", -120.0)
            snr_last = ch_st.get("last_snr", 0.0)
            # Noise floor: prefer LBT RSSI rolling average from TX queue, fallback to spectral scan
            nf_lbt_avg = ch_tx.get("noise_floor_lbt_avg")
            noise_floor = _find_noise(freq)
            if noise_floor == 0 or noise_floor is None: noise_floor = -120.0
            # If LBT rolling RSSI average available and spectral gave fallback, use LBT
            if nf_lbt_avg is not None and nf_lbt_avg > -119.0:
                noise_floor = nf_lbt_avg
            channels_live.append({
                "name": ch_labels.get(ch_name, ch_name),
                "friendly_name": _friendly_map.get(active_idx, "Channel " + (_abc[active_idx] if active_idx < len(_abc) else str(active_idx + 1))),
                "frequency": freq,
                "bandwidth": ch_cfg.get("bandwidth", 125000),
                "spreading_factor": ch_cfg.get("spreading_factor", 7),
                "coding_rate": ch_cfg.get("coding_rate", "4/5"),
                "rx_packets": rx_count,
                "tx_packets": tx_sent,
                "tx_failed": tx_failed,
                "last_rx": last_rx,
                "last_tx": last_tx,
                "rssi_last": rssi_last,
                "rssi_avg": rssi_avg,
                "snr_last": snr_last,
                "noise_floor": noise_floor,
                # TX timing stats
                "avg_tx_airtime_ms": ch_st.get("avg_tx_airtime_ms", 0),
                "avg_tx_send_ms": ch_st.get("avg_tx_send_ms", 0),
                "avg_tx_wait_ms": ch_st.get("avg_tx_wait_ms", 0),
                "last_tx_airtime_ms": ch_st.get("last_tx_airtime_ms", 0),
                "total_tx_airtime_ms": ch_st.get("total_tx_airtime_ms", 0),
                "total_tx_send_ms": ch_st.get("total_tx_send_ms", 0),
                "tx_bytes": ch_st.get("tx_bytes", 0),
                "tx_duty_pct": ch_st.get("tx_duty_pct", 0),
                # Software LBT stats (prefer tx_queue stats, fallback to backend stats)
                "lbt_blocked": ch_tx.get("lbt_blocked", 0) or ch_st.get("lbt_blocked", 0),
                "lbt_passed": ch_tx.get("lbt_passed", 0) or ch_st.get("lbt_passed", 0),
                "lbt_skipped": ch_tx.get("lbt_skipped", 0) or ch_st.get("lbt_skipped", 0),
                "lbt_last_blocked_at": ch_tx.get("lbt_last_blocked_at") or ch_st.get("lbt_last_blocked_at"),
                "lbt_last_rssi": ch_tx.get("lbt_last_rssi") or ch_st.get("lbt_last_rssi"),
                # LBT RSSI noise floor estimates (rolling buffer of last 20 measurements)
                "noise_floor_lbt_avg": ch_tx.get("noise_floor_lbt_avg"),
                "noise_floor_lbt_min": ch_tx.get("noise_floor_lbt_min"),
                "noise_floor_lbt_max": ch_tx.get("noise_floor_lbt_max"),
                "noise_floor_lbt_samples": ch_tx.get("noise_floor_lbt_samples", 0),
                # Queue health stats
                "queue_pending": ch_tx.get("pending", 0),
                "queue_size": ch_tx.get("queue_size", 15),
                "dropped_overflow": ch_tx.get("dropped_overflow", 0),
                "dropped_ttl": ch_tx.get("dropped_ttl", 0),
            })

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
                "total_echo": st.get('fwd_echo_detected', 0),
                "total_filtered": st.get('dropped_filtered', 0),
                "dedup_seen_active": st.get('dedup_seen_active', 0),
                "dedup_events_buffered": st.get('dedup_events_buffered', 0),
            }

        # --- Try SQLite for historical data ---
        db_path = "/var/lib/pymc_repeater/repeater.db"
        buckets = []
        period_stats = {"total_forwarded": 0, "total_duplicate": 0,
                        "total_echo": 0, "total_filtered": 0,
                        "period_start": since_ts, "period_end": until_ts,
                        "dedup_ratio": 0.0}

        try:
            import os as _os
            if _os.path.exists(db_path):
                with _sqlite3.connect(db_path) as conn:
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
                            period_stats["dedup_ratio"] = round(
                                period_stats["total_duplicate"] / total, 4)
                        period_stats.update(live_stats)
                        return _j({"events": events, "stats": period_stats, "mode": "raw"})

                    # Aggregated buckets
                    bucket_secs = bucket_min * 60
                    rows = conn.execute(f"""
                        SELECT
                            CAST((ts / {bucket_secs}) AS INTEGER) * {bucket_secs} AS bucket_ts,
                            SUM(CASE WHEN event_type = 'forwarded' THEN 1 ELSE 0 END) AS forwarded,
                            SUM(CASE WHEN event_type = 'duplicate' THEN 1 ELSE 0 END) AS duplicate,
                            SUM(CASE WHEN event_type = 'echo' THEN 1 ELSE 0 END) AS echo,
                            SUM(CASE WHEN event_type = 'filtered' THEN 1 ELSE 0 END) AS filtered
                        FROM dedup_events
                        WHERE ts >= ? AND ts <= ?
                        GROUP BY bucket_ts
                        ORDER BY bucket_ts ASC
                    """, (since_ts, until_ts)).fetchall()

                    buckets = [{"ts": int(r["bucket_ts"]), "forwarded": r["forwarded"],
                                "duplicate": r["duplicate"], "echo": r["echo"],
                                "filtered": r["filtered"]} for r in rows]

                    # Totals from query
                    totals = conn.execute("""
                        SELECT
                            SUM(CASE WHEN event_type = 'forwarded' THEN 1 ELSE 0 END) AS tf,
                            SUM(CASE WHEN event_type = 'duplicate' THEN 1 ELSE 0 END) AS td,
                            SUM(CASE WHEN event_type = 'echo' THEN 1 ELSE 0 END) AS te,
                            SUM(CASE WHEN event_type = 'filtered' THEN 1 ELSE 0 END) AS tfi,
                            COUNT(*) AS total
                        FROM dedup_events WHERE ts >= ? AND ts <= ?
                    """, (since_ts, until_ts)).fetchone()

                    period_stats["total_forwarded"] = totals["tf"] or 0
                    period_stats["total_duplicate"] = totals["td"] or 0
                    period_stats["total_echo"] = totals["te"] or 0
                    period_stats["total_filtered"] = totals["tfi"] or 0
                    total = totals["total"] or 0
                    if total > 0:
                        period_stats["dedup_ratio"] = round(
                            period_stats["total_duplicate"] / total, 4)

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
                        bkt_map[bts] = {"ts": bts, "forwarded": 0, "duplicate": 0, "echo": 0, "filtered": 0}
                    et = ev.get('type', '')
                    if et in bkt_map[bts]:
                        bkt_map[bts][et] += 1
                buckets = sorted(bkt_map.values(), key=lambda x: x['ts'])
                for ev in events:
                    k = "total_" + ev.get('type', '')
                    if k in period_stats:
                        period_stats[k] += 1
                total = len(events)
                if total > 0:
                    period_stats["dedup_ratio"] = round(
                        period_stats["total_duplicate"] / total, 4)

        period_stats.update(live_stats)
        return _j({
            "buckets": buckets,
            "stats": period_stats,
            "range": range_str,
            "bucket_minutes": bucket_min,
        })

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

        # Build response with channel info from config
        import yaml
        channels_config = {}
        try:
            cfg_path = Path("/etc/pymc_repeater/config.yaml")
            with open(cfg_path) as f:
                cfg = yaml.safe_load(f) or {}
            channels_config = cfg.get("wm1303", {}).get("channels", {})
        except Exception:
            pass

        queues = {}
        channel_names = ["channel_a", "channel_b", "channel_c", "channel_d"]
        for ch_name in channel_names:
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
        return _j({
            "channels": channels,
            "radio_0_freq_mhz": round(rf[0] / 1e6, 4),
            "radio_1_freq_mhz": round(rf[1] / 1e6, 4),
            "sx1261_spi": sx1261.get("spi_path", "/dev/spidev0.1"),
            "spectral_scan_enabled": spec_scan.get("enable", False) or sx1261_status.get("managed_by_hal", False),
            "lbt_enabled": lbt.get("enable", False) or sx1261_status.get("managed_by_hal", False),
            "lbt_channels": [
                {"freq_mhz": round(ch["freq_hz"] / 1e6, 4), "bw_khz": ch["bandwidth"] // 1000}
                for ch in lbt.get("channels", [])
            ],
            "last_scan": last_scan,
            "scan_binary_available": _SPECTRAL_BIN.exists(),
            "sx1261_role": "lbt_cad_spectrum_only",
            "sx1261_tx_enabled": False,
            "sx1261_status": sx1261_status,
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
            return _j({"status": "ok", "result": "SX1261 managed by HAL - channel status unavailable", "channel_free": True, "simulated": False})
        if action == "cad_test":
            sx = self._get_sx1261()
            if sx and getattr(sx, '_initialized', False):
                try:
                    result = sx.cad_detect(868100000)
                    return _j({"status": "ok", "result": "Activity " + ("detected" if result else "not detected"), "activity_detected": result})
                except Exception as e:
                    return _j({"status": "error", "result": str(e), "error": str(e)})
            return _j({"status": "ok", "result": "SX1261 managed by HAL - activity status unavailable", "activity_detected": False, "simulated": False})
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
                # Set TX hold for 5 seconds to give HAL spectral scan thread a clear window
                _hold_until = _time.monotonic() + 5.0
                if _hold_until > _bk._tx_hold_until:
                    _bk._tx_hold_until = _hold_until
                    logger.info("_do_spectrum_scan: TX hold set for 5s to enable spectral scan")

                # Wait for HAL to perform scans (pace_s=1, so should get data within 2-3s)
                _time.sleep(3.0)

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
                        note = "Real SX1261 HAL spectral scan ({} frequencies)".format(len(scan_points))
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

        # --- Priority 4: Try Python SX1261 driver ---
        if not scan_points:
            sx = self._get_sx1261()
            if sx and getattr(sx, '_initialized', False):
                try:
                    results = sx.get_rssi_scan(863000000, 870000000, 200000)
                    scan_points = [{"freq_mhz": round(r["freq_hz"]/1e6, 3), "rssi_dbm": r["rssi_dbm"]} for r in results]
                    note = "Real SX1261 RSSI measurement (Python driver)"
                except Exception as e:
                    logger.debug("SX1261 Python driver scan failed: %s", e)
                    scan_points = []

        # --- Fallback: Simulated data ---
        if not scan_points:
            for freq_hz in range(863000000, 870200000, 200000):
                freq_mhz = round(freq_hz / 1e6, 3)
                near = any(abs(ch["frequency_hz"] - freq_hz) < 150000 for ch in channels)
                rssi = -120.0 + random.uniform(0, 4)
                if near: rssi = -120.0 + random.uniform(10, 30)
                scan_points.append({"freq_mhz": freq_mhz, "rssi_dbm": round(rssi, 1)})
            note = ""

        result = {"status": "ok", "timestamp": time.time(), "scan_points": scan_points, "channels": channels, "note": note}
        _SPECTRAL_RES.write_text(json.dumps(result))
        return _j(result)

        # --- Priority 1: Read from spectrum_collector DB (HAL-driven scans) ---
        if _COLLECTOR_AVAILABLE:
            try:
                collector = get_collector()
                recent = collector.get_spectrum_history(hours=1)
                if recent:
                    # Group by frequency, take most recent RSSI per freq
                    from collections import defaultdict
                    freq_rssi = defaultdict(list)
                    for r in recent:
                        freq_rssi[r["freq_mhz"]].append(r["rssi_dbm"])
                    # Build scan points from most recent data (average last N samples)
                    scan_points = []
                    for freq_mhz in sorted(freq_rssi.keys()):
                        values = freq_rssi[freq_mhz][-20:]  # last 20 samples
                        avg_rssi = sum(values) / len(values)
                        scan_points.append({"freq_mhz": freq_mhz, "rssi_dbm": round(avg_rssi, 1)})
                    if scan_points:
                        note = "Real HAL spectral scan (via spectrum_collector, {} points)".format(len(recent))
                        logger.debug("Spectrum scan: %d points from collector DB", len(scan_points))
            except Exception as e:
                logger.debug("Spectrum collector read failed: %s", e)

        # --- Priority 2: Read from cached spectral results file ---
        if not scan_points and _SPECTRAL_RES.exists():
            try:
                cached = json.loads(_SPECTRAL_RES.read_text())
                if cached.get("scan_points") and True:
                    scan_points = cached["scan_points"]
                    note = cached.get("note", "Cached spectral scan")
                    age = time.time() - cached.get("timestamp", 0)
                    if age < 300:  # less than 5 min old
                        note += " (cached {:.0f}s ago)".format(age)
            except Exception as e:
                logger.debug("Cached spectral results read failed: %s", e)

        # --- Priority 3: Try Python SX1261 driver (when HAL not managing) ---
        if not scan_points:
            sx = self._get_sx1261()
            if sx and getattr(sx, '_initialized', False):
                try:
                    results = sx.get_rssi_scan(863000000, 870000000, 200000)
                    scan_points = [{"freq_mhz": round(r["freq_hz"]/1e6, 3), "rssi_dbm": r["rssi_dbm"]} for r in results]
                    note = "Real SX1261 RSSI measurement (Python driver)"
                except Exception as e:
                    logger.debug("SX1261 Python driver scan failed: %s", e)
                    scan_points = []

        # --- Fallback: Simulated data ---
        if not scan_points:
            for freq_hz in range(863000000, 870200000, 200000):
                freq_mhz = round(freq_hz / 1e6, 3)
                near = any(abs(ch["frequency_hz"] - freq_hz) < 150000 for ch in channels)
                rssi = -120.0 + random.uniform(0, 4)
                if near: rssi = -120.0 + random.uniform(10, 30)
                scan_points.append({"freq_mhz": freq_mhz, "rssi_dbm": round(rssi, 1)})
            note = ""

        result = {"status": "ok", "timestamp": time.time(), "scan_points": scan_points, "channels": channels, "note": note}
        _SPECTRAL_RES.write_text(json.dumps(result))
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
                "friendly_name": c.get("friendly_name", c.get("name", "")),
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
        """GET /api/wm1303/lbt_history - LBT stats per channel from channel_stats_history."""
        import sqlite3
        h = min(int(hours), 168)
        db_path = "/var/lib/pymc_repeater/repeater.db"
        cutoff = time.time() - (h * 3600)
        bucket_s = 600
        ui_chs = _load_ui().get("channels", [])
        ch_colors = {"channel_a": "#3b82f6", "channel_b": "#8b5cf6",
                     "channel_c": "#10b981", "channel_d": "#f59e0b"}
        ch_letters = ["A", "B", "C", "D", "E", "F", "G", "H"]
        result_channels = []
        try:
            with sqlite3.connect(db_path) as conn:
                conn.row_factory = sqlite3.Row
                ch_id_map = _get_ui_channel_id_map()
                for idx, ch_cfg in enumerate(ui_chs):
                    ch_id = ch_id_map.get(idx, "channel_" + chr(97 + idx))
                    letter = ch_letters[idx] if idx < len(ch_letters) else str(idx + 1)
                    rows = conn.execute(
                        "SELECT timestamp, tx_count, lbt_blocked, lbt_passed, "
                        "lbt_last_rssi, lbt_threshold, noise_floor_dbm "
                        "FROM channel_stats_history "
                        "WHERE channel_id = ? AND timestamp > ? ORDER BY timestamp",
                        (ch_id, cutoff)
                    ).fetchall()
                    # NOTE: noise_floor_history uses friendly names not channel IDs,
                    # and noise_floor_dbm in channel_stats_history is a global value.
                    # Use lbt_last_rssi which has actual per-channel values.
                    timeseries = []
                    if len(rows) >= 2:
                        buckets = {}
                        for row in rows:
                            bk = int(row["timestamp"] / bucket_s) * bucket_s
                            if bk not in buckets:
                                buckets[bk] = []
                            buckets[bk].append(row)
                        prev_last = None
                        for bk_ts in sorted(buckets.keys()):
                            bk_rows = buckets[bk_ts]
                            first = prev_last if prev_last else bk_rows[0]
                            last = bk_rows[-1]
                            tx_delta = max(0, (last["tx_count"] or 0) - (first["tx_count"] or 0))
                            blocked_delta = max(0, (last["lbt_blocked"] or 0) - (first["lbt_blocked"] or 0))
                            passed_delta = max(0, (last["lbt_passed"] or 0) - (first["lbt_passed"] or 0))
                            # Use lbt_last_rssi for per-channel noise floor
                            lbt_rssi_vals = [r["lbt_last_rssi"] for r in bk_rows if r["lbt_last_rssi"] is not None]
                            avg_nf = round(sum(lbt_rssi_vals)/len(lbt_rssi_vals), 1) if lbt_rssi_vals else None
                            timeseries.append({
                                "timestamp": bk_ts,
                                "noise_floor_dbm": avg_nf,
                                "tx_count_delta": tx_delta,
                                "lbt_blocked_delta": blocked_delta,
                                "lbt_passed_delta": passed_delta,
                                "lbt_clear": blocked_delta == 0
                            })
                            prev_last = last
                    result_channels.append({
                        "name": ch_cfg.get("friendly_name", f"Channel {letter}"),
                        "friendly_name": ch_cfg.get("friendly_name", f"Channel {letter}"),
                        "channel_id": ch_id,
                        "freq_mhz": round(ch_cfg.get("frequency", 0) / 1e6, 4),
                        "lbt_threshold_dbm": ch_cfg.get("lbt_rssi_target", -80),
                        "color": ch_colors.get(ch_id, "#999"),
                        "active": ch_cfg.get("active", False),
                        "timeseries": timeseries
                    })
            return {"hours": h, "channels": result_channels}
        except Exception as e:
            logger.error("lbt_history error: %s", e)
            return {"error": str(e), "channels": []}

    @cherrypy.expose
    @cherrypy.tools.json_out()
    def cad_history(self, hours='24'):
        if not _COLLECTOR_AVAILABLE:
            return {"error": "Spectrum collector not available", "data": []}
        h = min(int(hours), 168)
        collector = get_collector()
        data = collector.get_cad_history(hours=h)
        return {"hours": h, "total_events": len(data), "data": data}


    # ---------- Signal Quality & Enhanced Spectrum ----------

    @cherrypy.expose
    @cherrypy.tools.json_out()
    def signal_quality(self, hours='24'):
        """GET /api/wm1303/signal_quality - Per-channel RSSI, SNR from channel_stats_history."""
        import sqlite3
        h = min(int(hours), 168)
        db_path = "/var/lib/pymc_repeater/repeater.db"
        cutoff = time.time() - (h * 3600)
        bucket_s = 600  # 10-minute buckets
        ui_chs = _load_ui().get("channels", [])
        ch_colors = {"channel_a": "#3b82f6", "channel_b": "#8b5cf6",
                     "channel_c": "#10b981", "channel_d": "#f59e0b"}
        ch_letters = ["A", "B", "C", "D", "E", "F", "G", "H"]
        result_channels = []
        try:
            with sqlite3.connect(db_path) as conn:
                conn.row_factory = sqlite3.Row
                ch_id_map = _get_ui_channel_id_map()
                for idx, ch_cfg in enumerate(ui_chs):
                    ch_id = ch_id_map.get(idx, "channel_" + chr(97 + idx))
                    letter = ch_letters[idx] if idx < len(ch_letters) else str(idx + 1)
                    rows = conn.execute(
                        "SELECT timestamp, rx_count, avg_rssi, avg_snr, noise_floor_dbm "
                        "FROM channel_stats_history "
                        "WHERE channel_id = ? AND timestamp > ? ORDER BY timestamp",
                        (ch_id, cutoff)
                    ).fetchall()
                    timeseries = []
                    if len(rows) >= 2:
                        buckets = {}
                        for row in rows:
                            bk = int(row["timestamp"] / bucket_s) * bucket_s
                            if bk not in buckets:
                                buckets[bk] = []
                            buckets[bk].append(row)
                        sorted_bks = sorted(buckets.keys())
                        prev_last = None
                        for bk_ts in sorted_bks:
                            bk_rows = buckets[bk_ts]
                            first = prev_last if prev_last else bk_rows[0]
                            last = bk_rows[-1]
                            rx_delta = max(0, (last["rx_count"] or 0) - (first["rx_count"] or 0))
                            rssi_vals = [r["avg_rssi"] for r in bk_rows if r["avg_rssi"] is not None]
                            snr_vals = [r["avg_snr"] for r in bk_rows if r["avg_snr"] is not None]
                            try:
                                nf_vals = [r["noise_floor_dbm"] for r in bk_rows if r["noise_floor_dbm"] is not None]
                            except (IndexError, KeyError):
                                nf_vals = []
                            avg_rssi = round(sum(rssi_vals) / len(rssi_vals), 1) if rssi_vals else None
                            avg_snr = round(sum(snr_vals) / len(snr_vals), 1) if snr_vals else None
                            avg_nf = round(sum(nf_vals) / len(nf_vals), 1) if nf_vals else None
                            timeseries.append({
                                "timestamp": bk_ts,
                                "pkt_count": rx_delta,
                                "avg_rssi": avg_rssi,
                                "avg_snr": avg_snr,
                                "noise_floor_dbm": avg_nf
                            })
                            prev_last = last
                    # Summary stats
                    all_rssi = [r["avg_rssi"] for r in rows if r["avg_rssi"] is not None]
                    all_snr = [r["avg_snr"] for r in rows if r["avg_snr"] is not None]
                    total_rx = 0
                    if len(rows) >= 2:
                        total_rx = max(0, (rows[-1]["rx_count"] or 0) - (rows[0]["rx_count"] or 0))
                    result_channels.append({
                        "name": ch_cfg.get("friendly_name", f"Channel {letter}"),
                        "friendly_name": ch_cfg.get("friendly_name", f"Channel {letter}"),
                        "channel_id": ch_id,
                        "freq_mhz": round(ch_cfg.get("frequency", 0) / 1e6, 4),
                        "spreading_factor": ch_cfg.get("spreading_factor", 0),
                        "active": ch_cfg.get("active", False),
                        "color": ch_colors.get(ch_id, "#999"),
                        "stats": {
                            "pkt_count": total_rx,
                            "avg_rssi": round(sum(all_rssi)/len(all_rssi), 1) if all_rssi else None,
                            "min_rssi": round(min(all_rssi), 1) if all_rssi else None,
                            "max_rssi": round(max(all_rssi), 1) if all_rssi else None,
                            "avg_snr": round(sum(all_snr)/len(all_snr), 1) if all_snr else None,
                            "min_snr": round(min(all_snr), 1) if all_snr else None,
                            "max_snr": round(max(all_snr), 1) if all_snr else None,
                        },
                        "timeseries": timeseries
                    })
                # Noise floor from noise_floor table (this part works fine)
                nf_rows = conn.execute("""
                    SELECT
                        CAST((timestamp / 600) AS INTEGER) * 600 as bucket_ts,
                        AVG(noise_floor_dbm) as avg_nf,
                        MIN(noise_floor_dbm) as min_nf,
                        MAX(noise_floor_dbm) as max_nf,
                        COUNT(*) as cnt
                    FROM noise_floor
                    WHERE timestamp > ?
                    GROUP BY bucket_ts
                    ORDER BY bucket_ts
                """, (cutoff,)).fetchall()
                noise_floor_ts = []
                for row in nf_rows:
                    noise_floor_ts.append({
                        "timestamp": row["bucket_ts"],
                        "avg_nf": round(row["avg_nf"], 1) if row["avg_nf"] else None,
                        "min_nf": round(row["min_nf"], 1) if row["min_nf"] else None,
                        "max_nf": round(row["max_nf"], 1) if row["max_nf"] else None
                    })
                current_nf = conn.execute(
                    "SELECT noise_floor_dbm FROM noise_floor ORDER BY timestamp DESC LIMIT 1"
                ).fetchone()
                return {
                    "hours": h,
                    "bucket_minutes": 10,
                    "channels": result_channels,
                    "noise_floor": {
                        "current": round(current_nf["noise_floor_dbm"], 1) if current_nf else None,
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
        """GET /api/wm1303/noise_floor - Per-channel noise floor with history.

        Query params:
          range  - '1h','6h','24h','3d','7d' (default '1h')
          channel - filter to single channel_id (optional)
        """
        import time as _time
        import sqlite3 as _sqlite3

        now = _time.time()
        range_str = params.get('range', '1h')
        channel_filter = params.get('channel', None)

        AUTO_BUCKETS = {
            '1h':  (3600,       1),
            '6h':  (6*3600,     5),
            '24h': (24*3600,   15),
            '3d':  (3*24*3600, 60),
            '7d':  (7*24*3600, 120),
        }
        span_secs, bucket_min = AUTO_BUCKETS.get(range_str, (3600, 1))
        since_ts = now - span_secs

        db_path = "/var/lib/pymc_repeater/repeater.db"
        result_channels = {}

        try:
            import os as _os
            if not _os.path.exists(db_path):
                return _j({"channels": {}, "error": "database not found"})

            bucket_secs = bucket_min * 60

            with _sqlite3.connect(db_path) as conn:
                conn.row_factory = _sqlite3.Row

                # Get current noise floor per channel
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

                # Get history buckets
                where = ["timestamp >= ?"]
                qparams = [since_ts]
                if channel_filter:
                    where.append("channel_id = ?")
                    qparams.append(channel_filter)
                where_str = ' AND '.join(where)

                hist_rows = conn.execute(f"""
                    SELECT
                        channel_id,
                        CAST((timestamp / {bucket_secs}) AS INTEGER) * {bucket_secs} AS bucket_ts,
                        AVG(noise_floor_dbm) as avg_nf,
                        MIN(noise_floor_dbm) as min_nf,
                        MAX(noise_floor_dbm) as max_nf,
                        SUM(samples_collected) as total_samples,
                        SUM(samples_accepted) as total_accepted
                    FROM noise_floor_history
                    WHERE {where_str}
                    GROUP BY channel_id, bucket_ts
                    ORDER BY channel_id, bucket_ts ASC
                """, qparams).fetchall()

                for row in hist_rows:
                    ch_id = row["channel_id"]
                    if ch_id not in result_channels:
                        result_channels[ch_id] = {
                            "current": None, "last_update": None,
                            "history": [], "stats": {}
                        }
                    result_channels[ch_id]["history"].append({
                        "ts": int(row["bucket_ts"]),
                        "avg_nf": round(row["avg_nf"], 1) if row["avg_nf"] else None,
                        "min_nf": round(row["min_nf"], 1) if row["min_nf"] else None,
                        "max_nf": round(row["max_nf"], 1) if row["max_nf"] else None,
                        "samples": row["total_samples"] or 0,
                    })

                # Get per-channel stats
                stat_rows = conn.execute(f"""
                    SELECT channel_id,
                        COUNT(*) as cnt,
                        AVG(noise_floor_dbm) as avg_nf,
                        MIN(noise_floor_dbm) as min_nf,
                        MAX(noise_floor_dbm) as max_nf
                    FROM noise_floor_history
                    WHERE {where_str}
                    GROUP BY channel_id
                """, qparams).fetchall()

                for row in stat_rows:
                    ch_id = row["channel_id"]
                    if ch_id in result_channels:
                        result_channels[ch_id]["stats"] = {
                            "count": row["cnt"],
                            "avg": round(row["avg_nf"], 1) if row["avg_nf"] else None,
                            "min": round(row["min_nf"], 1) if row["min_nf"] else None,
                            "max": round(row["max_nf"], 1) if row["max_nf"] else None,
                        }

            # Also get in-memory noise floors from backend
            try:
                _bk = _get_backend()
                if _bk and hasattr(_bk, 'get_channel_noise_floors'):
                    live_nf = _bk.get_channel_noise_floors()
                    for ch_id, nf_val in live_nf.items():
                        if ch_id not in result_channels:
                            result_channels[ch_id] = {
                                "current": nf_val, "last_update": now,
                                "history": [], "stats": {}
                            }
                        else:
                            result_channels[ch_id]["current_live"] = nf_val
            except Exception:
                pass

        except Exception as e:
            import logging
            logging.getLogger("WM1303API").warning("noise_floor endpoint error: %s", e)
            return _j({"channels": {}, "error": str(e)})

        # Filter to only active channels
        # NOTE: noise_floor_history stores friendly names (e.g. 'SF8') as channel_id,
        # not internal IDs (e.g. 'channel_a'), so we must match using ch_cfg['name'].
        try:
            ui_chs = _load_ui().get("channels", [])
            active_ch_names = set()
            for ch_cfg in ui_chs:
                if ch_cfg.get("active", False):
                    active_ch_names.add(ch_cfg.get("name", ""))
            if active_ch_names:
                result_channels = {k: v for k, v in result_channels.items() if k in active_ch_names}
        except Exception:
            pass

        # Convert friendly channel names to 'Channel A', 'Channel B', etc.
        try:
            ui_cfg = _load_ui()
            name_to_label = {}
            for idx, ch_cfg in enumerate(ui_cfg.get("channels", [])):
                ch_name = ch_cfg.get("name", "")
                label = "Channel " + chr(65 + idx)  # A, B, C, D
                name_to_label[ch_name] = label
            converted = {}
            for k, v in result_channels.items():
                new_key = name_to_label.get(k, k)
                converted[new_key] = v
            result_channels = converted
        except Exception:
            pass

        return _j({
            "channels": result_channels,
            "range": range_str,
            "bucket_minutes": bucket_min,
        })

    # ---- CAD Stats ----

    def _cad_stats_get(self, **params):
        """GET /api/wm1303/cad_stats - CAD event timeline from cad_events table.

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

            with _sqlite3.connect(db_path) as conn:
                conn.row_factory = _sqlite3.Row

                # Check if cad_events table exists
                tbl = conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name='cad_events'"
                ).fetchone()
                if not tbl:
                    pass  # Table not yet created
                else:
                    where = ["timestamp >= ?"]
                    qparams = [since_ts]
                    if channel_filter:
                        where.append("channel_id = ?")
                        qparams.append(channel_filter)
                    where_str = ' AND '.join(where)

                    # Summary per channel
                    summary_rows = conn.execute(f"""
                        SELECT channel_id,
                            SUM(cad_clear) as total_clear,
                            SUM(cad_detected) as total_detected,
                            SUM(cad_skipped) as total_skipped,
                            COUNT(*) as sample_count
                        FROM cad_events
                        WHERE {where_str}
                        GROUP BY channel_id
                    """, qparams).fetchall()

                    for row in summary_rows:
                        ch_id = row["channel_id"]
                        t_clear = row["total_clear"] or 0
                        t_det = row["total_detected"] or 0
                        t_skip = row["total_skipped"] or 0
                        channels[ch_id] = {
                            "clear": t_clear,
                            "detected": t_det,
                            "skipped": t_skip,
                            "total": t_clear + t_det + t_skip,
                        }

                    # Time-bucketed aggregation
                    bucket_rows = conn.execute(f"""
                        SELECT
                            channel_id,
                            CAST((timestamp / {bucket_secs}) AS INTEGER) * {bucket_secs} AS bucket_ts,
                            SUM(cad_clear) as sum_clear,
                            SUM(cad_detected) as sum_detected,
                            SUM(cad_skipped) as sum_skipped
                        FROM cad_events
                        WHERE {where_str}
                        GROUP BY channel_id, bucket_ts
                        ORDER BY channel_id, bucket_ts ASC
                    """, qparams).fetchall()

                    for row in bucket_rows:
                        ch_id = row["channel_id"]
                        if ch_id not in buckets:
                            buckets[ch_id] = []
                        buckets[ch_id].append({
                            "ts": int(row["bucket_ts"]),
                            "clear": row["sum_clear"] or 0,
                            "detected": row["sum_detected"] or 0,
                            "skipped": row["sum_skipped"] or 0,
                        })

                    # Recent rows (last 100)
                    recent_rows = conn.execute(f"""
                        SELECT timestamp, channel_id, cad_clear, cad_detected, cad_skipped
                        FROM cad_events
                        WHERE {where_str}
                        ORDER BY timestamp DESC
                        LIMIT 100
                    """, qparams).fetchall()

                    recent = [{
                        "ts": row["timestamp"],
                        "channel": row["channel_id"],
                        "clear": row["cad_clear"] or 0,
                        "detected": row["cad_detected"] or 0,
                        "skipped": row["cad_skipped"] or 0,
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
                        "cad_skipped": q.stats.get("cad_skipped", 0),
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

    def noise_floor_history(self, hours='24'):
        """GET /api/wm1303/noise_floor_history - Noise floor measurements over time."""
        h = min(int(hours), 168)
        import sqlite3
        db_path = "/var/lib/pymc_repeater/repeater.db"
        cutoff = time.time() - (h * 3600)
        try:
            with sqlite3.connect(db_path) as conn:
                conn.row_factory = sqlite3.Row
                rows = conn.execute("""
                    SELECT
                        CAST((timestamp / 300) AS INTEGER) * 300 as bucket_ts,
                        AVG(noise_floor_dbm) as avg_nf,
                        MIN(noise_floor_dbm) as min_nf,
                        MAX(noise_floor_dbm) as max_nf,
                        COUNT(*) as sample_count
                    FROM noise_floor
                    WHERE timestamp > ?
                    GROUP BY bucket_ts
                    ORDER BY bucket_ts
                """, (cutoff,)).fetchall()
                data = []
                for row in rows:
                    data.append({
                        "timestamp": row["bucket_ts"],
                        "avg_nf": round(row["avg_nf"], 1) if row["avg_nf"] else None,
                        "min_nf": round(row["min_nf"], 1) if row["min_nf"] else None,
                        "max_nf": round(row["max_nf"], 1) if row["max_nf"] else None,
                        "samples": row["sample_count"]
                    })
                stats = conn.execute("""
                    SELECT
                        COUNT(*) as total,
                        AVG(noise_floor_dbm) as avg_nf,
                        MIN(noise_floor_dbm) as min_nf,
                        MAX(noise_floor_dbm) as max_nf
                    FROM noise_floor
                    WHERE timestamp > ?
                """, (cutoff,)).fetchone()
                return _j({
                    "hours": h,
                    "total_measurements": stats["total"] if stats else 0,
                    "stats": {
                        "avg": round(stats["avg_nf"], 1) if stats and stats["avg_nf"] else None,
                        "min": round(stats["min_nf"], 1) if stats and stats["min_nf"] else None,
                        "max": round(stats["max_nf"], 1) if stats and stats["max_nf"] else None,
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
        """GET /api/wm1303/packet_activity - RX/TX activity per channel from packet_activity table."""
        import sqlite3 as _sq3
        hours_str = params.get("hours", "24")
        try:
            h = min(int(hours_str), 168)
        except (ValueError, TypeError):
            h = 24
        # Bucket sizes: 1h->1min, 6h->5min, 24h->15min, 72h->1h, 168h->4h
        if h <= 1:
            bucket_s = 60
        elif h <= 6:
            bucket_s = 300
        elif h <= 24:
            bucket_s = 900
        elif h <= 72:
            bucket_s = 3600
        else:
            bucket_s = 14400
        cutoff = time.time() - (h * 3600)
        db_path = "/var/lib/pymc_repeater/repeater.db"
        ui_chs = _load_ui().get("channels", [])
        ch_letters = ["A", "B", "C", "D", "E", "F", "G", "H"]
        ch_colors = ["#3b82f6", "#8b5cf6", "#10b981", "#f59e0b",
                     "#f472b6", "#fb923c", "#06b6d4", "#a3e635"]
        result_channels = []
        try:
            ch_id_map = _get_ui_channel_id_map()
            with _sq3.connect(db_path) as conn:
                conn.row_factory = _sq3.Row
                for idx, ch_cfg in enumerate(ui_chs):
                    ch_id = ch_id_map.get(idx, "channel_" + chr(97 + idx))
                    letter = ch_letters[idx] if idx < len(ch_letters) else str(idx + 1)
                    rows = conn.execute(
                        "SELECT timestamp, rx_count, tx_count FROM packet_activity "
                        "WHERE channel_id = ? AND timestamp > ? ORDER BY timestamp",
                        (ch_id, cutoff)
                    ).fetchall()
                    # Bucket the data
                    buckets = {}
                    for row in rows:
                        bk = int(row["timestamp"] / bucket_s) * bucket_s
                        if bk not in buckets:
                            buckets[bk] = {"rx": [], "tx": []}
                        buckets[bk]["rx"].append(row["rx_count"] or 0)
                        buckets[bk]["tx"].append(row["tx_count"] or 0)
                    timeseries = []
                    for bk_ts in sorted(buckets.keys()):
                        bk = buckets[bk_ts]
                        # Delta within bucket (last - first)
                        rx_delta = sum(bk["rx"])  # Sum of deltas in bucket
                        tx_delta = sum(bk["tx"])  # Sum of deltas in bucket
                        timeseries.append({"t": bk_ts, "rx": rx_delta, "tx": tx_delta})
                    result_channels.append({
                        "id": ch_id,
                        "label": ch_cfg.get("friendly_name", f"Channel {letter}"),
                        "color": ch_colors[idx % len(ch_colors)],
                        "data": timeseries
                    })
            return _j({"hours": h, "bucket_seconds": bucket_s, "channels": result_channels})
        except Exception as e:
            logger.error("packet_activity error: %s", e)
            return _j({"error": str(e), "hours": h, "channels": []})

    def tx_activity(self, hours='24'):
        """GET /api/wm1303/tx_activity - TX activity per channel from channel_stats_history."""
        import sqlite3
        h = min(int(hours), 168)
        db_path = "/var/lib/pymc_repeater/repeater.db"
        cutoff = time.time() - (h * 3600)
        bucket_s = 600  # 10-minute buckets
        ui_chs = _load_ui().get("channels", [])
        ch_colors = {"channel_a": "#3b82f6", "channel_b": "#8b5cf6",
                     "channel_c": "#10b981", "channel_d": "#f59e0b"}
        ch_letters = ["A", "B", "C", "D", "E", "F", "G", "H"]
        result_channels = []
        try:
            with sqlite3.connect(db_path) as conn:
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
                        # Group into buckets and compute deltas
                        buckets = {}
                        for row in rows:
                            bk = int(row["timestamp"] / bucket_s) * bucket_s
                            if bk not in buckets:
                                buckets[bk] = []
                            buckets[bk].append(row)
                        for bk_ts in sorted(buckets.keys()):
                            bk_rows = buckets[bk_ts]
                            if len(bk_rows) >= 2:
                                first, last = bk_rows[0], bk_rows[-1]
                            elif bk_ts in buckets and len(buckets) > 1:
                                # Single row in bucket - try delta from previous bucket
                                first = last = bk_rows[0]
                            else:
                                continue
                            tx_delta = max(0, (last["tx_count"] or 0) - (first["tx_count"] or 0))
                            fail_delta = max(0, (last["tx_failed"] or 0) - (first["tx_failed"] or 0))
                            lbt_delta = max(0, (last["lbt_blocked"] or 0) - (first["lbt_blocked"] or 0))
                            air_delta = max(0, (last["tx_airtime_ms"] or 0) - (first["tx_airtime_ms"] or 0))
                            bytes_delta = max(0, (last["tx_bytes"] or 0) - (first["tx_bytes"] or 0))
                            if tx_delta > 0 or fail_delta > 0 or lbt_delta > 0:
                                timeseries.append({
                                    "timestamp": bk_ts,
                                    "tx_sent": tx_delta,
                                    "tx_failed": fail_delta,
                                    "lbt_blocked": lbt_delta,
                                    "airtime_ms": round(air_delta, 1),
                                    "tx_bytes": bytes_delta
                                })
                    result_channels.append({
                        "name": ch_cfg.get("friendly_name", f"Channel {letter}"),
                        "channel_id": ch_id,
                        "color": ch_colors.get(ch_id, "#999"),
                        "timeseries": timeseries
                    })
            return _j({"hours": h, "bucket_minutes": 10, "channels": result_channels})
        except Exception as e:
            logger.error("tx_activity error: %s", e)
            return _j({"error": str(e), "hours": h, "bucket_minutes": 10, "channels": []})





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
            result = {
                "dedup_ttl_seconds":    cfg.get("bridge", {}).get("dedup_ttl_seconds", 300),
                "cache_ttl":            cfg.get("repeater", {}).get("cache_ttl", 60),
                "max_cache_size":       adv.get("max_cache_size", 1000),
                "queue_size":           cfg.get("wm1303", {}).get("tx_queue", {}).get("queue_size", 15),
                "inter_packet_delay":   cfg.get("wm1303", {}).get("tx_queue", {}).get("tx_delay_ms", 20),
                "packet_ttl":           adv.get("tx_packet_ttl_seconds", 5),
                "overflow_policy":      adv.get("tx_overflow_policy", "drop_oldest"),
                "nf_interval":          adv.get("noise_floor_interval_seconds", 30),
                "nf_tx_hold":           adv.get("noise_floor_tx_hold_seconds", 4),
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
                    adv["max_cache_size"] = int(params["max_cache_size"])

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

            else:
                return _j({"status": "error", "error": "Unknown group: " + group})

            ui["adv_config"] = adv
            ui["hal_advanced"] = hal
            _save_ui(ui)
            logger.info("adv_config: saved UI JSON")

            if cfg_changed:
                with open(cfg_path, "w") as f:
                    yaml.dump(cfg, f, default_flow_style=False)
                logger.info("adv_config: saved config.yaml")

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


# --- Background packet activity recorder (every 60s) ---
_pkt_act_last_counts = {}  # {channel_id: {"rx": N, "tx": N}} cumulative from previous interval
_cad_last_counts = {}  # {channel_id: {"cad_clear": N, ...}}

def _packet_activity_recorder():
    """Periodically record per-channel RX/TX deltas to packet_activity table.
    Reads live stats from the WM1303 backend via _get_backend().get_channel_stats()."""
    import sqlite3 as _sq3r
    global _pkt_act_last_counts, _cad_last_counts
    _db = "/var/lib/pymc_repeater/repeater.db"
    # Ensure table exists on first run
    try:
        with _sq3r.connect(_db) as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS packet_activity (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp REAL NOT NULL,
                channel_id TEXT NOT NULL,
                rx_count INTEGER DEFAULT 0,
                tx_count INTEGER DEFAULT 0)""")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_pktact_ts ON packet_activity(timestamp)")
            conn.execute("""CREATE TABLE IF NOT EXISTS cad_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp REAL NOT NULL,
                channel_id TEXT NOT NULL,
                cad_clear INTEGER DEFAULT 0,
                cad_detected INTEGER DEFAULT 0,
                cad_skipped INTEGER DEFAULT 0)""")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_cadevt_ts ON cad_events(timestamp)")
            conn.commit()
    except Exception as _init_e:
        logger.debug("packet_activity_recorder init: %s", _init_e)
    while True:
        try:
            time.sleep(60)
            # Get live per-channel stats from the backend
            _bk = _get_backend()
            if not _bk:
                continue
            try:
                ch_stats = _bk.get_channel_stats()
            except Exception:
                continue
            if not ch_stats:
                continue
            now = time.time()
            inserts = []
            for ch_id, stats in ch_stats.items():
                cur_rx = stats.get("rx_count", 0) or 0
                cur_tx = stats.get("tx_count", 0) or 0
                prev = _pkt_act_last_counts.get(ch_id)
                if prev is not None:
                    # Compute delta since last interval
                    delta_rx = max(0, cur_rx - prev["rx"])
                    delta_tx = max(0, cur_tx - prev["tx"])
                    # Only insert if there was any activity
                    if delta_rx > 0 or delta_tx > 0:
                        inserts.append((now, ch_id, delta_rx, delta_tx))
                    else:
                        # Insert zero row to keep timeline continuous
                        inserts.append((now, ch_id, 0, 0))
                else:
                    # First reading for this channel - insert zeros, establish baseline
                    inserts.append((now, ch_id, 0, 0))
                # Update last counts
                _pkt_act_last_counts[ch_id] = {"rx": cur_rx, "tx": cur_tx}
            # Write to DB
            if inserts:
                with _sq3r.connect(_db) as conn:
                    conn.executemany(
                        "INSERT INTO packet_activity (timestamp, channel_id, rx_count, tx_count) VALUES (?,?,?,?)",
                        inserts
                    )
                    conn.commit()
            # --- CAD delta tracking ---
            try:
                cad_inserts = []
                if _bk and hasattr(_bk, '_tx_queue_manager') and _bk._tx_queue_manager:
                    for ch_id, q in _bk._tx_queue_manager.queues.items():
                        cur_clear = q.stats.get("cad_clear", 0) or 0
                        cur_det = q.stats.get("cad_detected", 0) or 0
                        cur_skip = q.stats.get("cad_skipped", 0) or 0
                        prev_cad = _cad_last_counts.get(ch_id)
                        if prev_cad is not None:
                            d_clear = max(0, cur_clear - prev_cad["cad_clear"])
                            d_det = max(0, cur_det - prev_cad["cad_detected"])
                            d_skip = max(0, cur_skip - prev_cad["cad_skipped"])
                            if d_clear > 0 or d_det > 0 or d_skip > 0:
                                cad_inserts.append((now, ch_id, d_clear, d_det, d_skip))
                            else:
                                cad_inserts.append((now, ch_id, 0, 0, 0))
                        else:
                            cad_inserts.append((now, ch_id, 0, 0, 0))
                        _cad_last_counts[ch_id] = {
                            "cad_clear": cur_clear,
                            "cad_detected": cur_det,
                            "cad_skipped": cur_skip,
                        }
                if cad_inserts:
                    with _sq3r.connect(_db) as conn:
                        conn.executemany(
                            "INSERT INTO cad_events (timestamp, channel_id, cad_clear, cad_detected, cad_skipped) VALUES (?,?,?,?,?)",
                            cad_inserts
                        )
                        conn.commit()
            except Exception as _cad_e:
                logger.debug("cad_events recorder: %s", _cad_e)
            # Cleanup old data (older than 8 days) from all chart tables
            cutoff = now - 8 * 86400
            with _sq3r.connect(_db) as conn:
                conn.execute("DELETE FROM packet_activity WHERE timestamp < ?", (cutoff,))
                conn.execute("DELETE FROM dedup_events WHERE ts < ?", (cutoff,))
                conn.execute("DELETE FROM noise_floor WHERE timestamp < ?", (cutoff,))
                conn.execute("DELETE FROM noise_floor_history WHERE timestamp < ?", (cutoff,))
                conn.execute("DELETE FROM cad_events WHERE timestamp < ?", (cutoff,))
                conn.commit()
        except Exception as _e:
            logger.debug("packet_activity_recorder: %s", _e)
            try:
                time.sleep(60)
            except Exception:
                break


import threading as _thr_pkt
_pkt_rec_thread = _thr_pkt.Thread(target=_packet_activity_recorder, daemon=True)
_pkt_rec_thread.start()


# Auto-start spectrum collector
if _COLLECTOR_AVAILABLE:
    try:
        _sc = get_collector()
    except Exception as _e:
        import logging
        logging.getLogger("wm1303_api").warning(f"Failed to start spectrum collector: {_e}")
