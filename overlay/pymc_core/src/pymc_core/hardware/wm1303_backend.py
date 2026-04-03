"""WM1303/SX1303 concentrator backend for pyMC_Repeater.

RF0-TX Architecture (proper hardware TX path):
  - SX1250_0 (RF0): RX + TX via SKY66420 FEM (PA + LNA + RF Switch)
  - SX1250_1 (RF1): RX only (no PA, SAW filter path)
  - IF chains 0-2 on RF0 for RX
  - TX via PULL_RESP to lora_pkt_fwd -> HAL routes to RF0 (tx_enable=true)
  - Direct TX: no stop/start cycle needed (RF0 has proper FEM for TX/RX switching)
  - Self-echo detection discards own TX heard back on RX
  - Per-channel TX queues serialize transmissions
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import random
import re
import socket
import struct
import subprocess
import threading
import time
import hashlib
import sqlite3
import math
from pathlib import Path
from typing import Any

from pymc_core.hardware.tx_queue import ChannelTXQueue, TXQueueManager, GlobalTXScheduler, MAX_CHANNELS

logger = logging.getLogger('WM1303Backend')

# Semtech UDP packet forwarder protocol identifiers
PKT_PUSH_DATA = 0x00
PKT_PUSH_ACK  = 0x01
PKT_PULL_DATA = 0x02
PKT_PULL_RESP = 0x03
PKT_PULL_ACK  = 0x04
PKT_TX_ACK    = 0x05
PROTOCOL_VER  = 0x02

# Default paths for SenseCAP M1 WM1303 Pi HAT
PKTFWD_DIR     = Path('/home/pi/wm1303_pf')
PKTFWD_BIN     = PKTFWD_DIR / 'lora_pkt_fwd'
PKTFWD_RESET   = PKTFWD_DIR / 'reset_lgw.sh'
BRIDGE_CONF    = PKTFWD_DIR / 'bridge_conf.json'
UDP_PORT_UP    = 1730
UDP_PORT_DOWN  = 1730

# Database path for channel stats history
_DB_PATH = '/var/lib/pymc_repeater/repeater.db'

# Path to the UI config (SSOT for channel definitions)
UI_JSON_PATH = Path('/etc/pymc_repeater/wm1303_ui.json')

# Fixed channel-name to IF-chain mapping.
# These NEVER change regardless of channel order or active/inactive status.
CHANNEL_IF_MAP = {
    'ch-1':    0,   # Channel A -> chan_multiSF_0
    'ch-2':    1,   # Channel B -> chan_multiSF_1
    'ch-new':  2,   # Channel D -> chan_multiSF_2
    'ch-notu': 3,   # Channel C -> chan_multiSF_3
}




def _bw_hz_to_str(bw_hz: int) -> str:
    mapping = {125000: '125', 250000: '250', 500000: '500'}
    return mapping.get(int(bw_hz), '125')


def _datr_str(sf: int, bw_hz: int) -> str:
    return f'SF{sf}BW{_bw_hz_to_str(bw_hz)}'


def _parse_datr(datr: str):
    """Parse "SF8BW125" -> (8, 125000)"""
    m = re.match(r'SF(\d+)BW(\d+)', datr)
    if m:
        return int(m.group(1)), int(m.group(2)) * 1000
    return None, None


# Semtech reference TX gain LUT for SX1250 + SKY66420 FEM (EU868)
# 16 entries from 12 dBm to 27 dBm via RF0
DEFAULT_TX_GAIN_LUT = [
    {"rf_power": 12, "pa_gain": 0, "pwr_idx": 15},
    {"rf_power": 13, "pa_gain": 0, "pwr_idx": 16},
    {"rf_power": 14, "pa_gain": 0, "pwr_idx": 17},
    {"rf_power": 15, "pa_gain": 0, "pwr_idx": 19},
    {"rf_power": 16, "pa_gain": 0, "pwr_idx": 20},
    {"rf_power": 17, "pa_gain": 0, "pwr_idx": 22},
    {"rf_power": 18, "pa_gain": 1, "pwr_idx": 1},
    {"rf_power": 19, "pa_gain": 1, "pwr_idx": 2},
    {"rf_power": 20, "pa_gain": 1, "pwr_idx": 3},
    {"rf_power": 21, "pa_gain": 1, "pwr_idx": 4},
    {"rf_power": 22, "pa_gain": 1, "pwr_idx": 5},
    {"rf_power": 23, "pa_gain": 1, "pwr_idx": 6},
    {"rf_power": 24, "pa_gain": 1, "pwr_idx": 7},
    {"rf_power": 25, "pa_gain": 1, "pwr_idx": 9},
    {"rf_power": 26, "pa_gain": 1, "pwr_idx": 11},
    {"rf_power": 27, "pa_gain": 1, "pwr_idx": 14},
]


def _generate_bridge_conf(channels: dict[str, dict]) -> dict:
    """Generate a lora_pkt_fwd global_conf.json with FIXED IF chain assignments.

    Architecture:
      - RF0 (SX1250_0): RX + TX via SKY66420 FEM, fixed IF chains 0-3
      - RF1 (SX1250_1): RX only (no PA), tx_enable=false
      - TX via PULL_RESP routed to RF0 (rfch=0)

    IF chain mapping is FIXED by channel name (CHANNEL_IF_MAP).
    Center frequency is computed as the average of ALL defined channel
    frequencies (not just active ones), ensuring IF offsets remain stable
    when channels are activated/deactivated.
    """
    # Read ALL channel definitions from wm1303_ui.json (the SSOT)
    all_ui_channels = []
    try:
        if UI_JSON_PATH.exists():
            ui_data = json.loads(UI_JSON_PATH.read_text())
            all_ui_channels = ui_data.get('channels', [])
    except Exception as ex:
        logger.warning('_generate_bridge_conf: could not read %s: %s', UI_JSON_PATH, ex)

    if not all_ui_channels:
        logger.warning('_generate_bridge_conf: no channels in UI config, '
                      'falling back to channels arg')
        # Fallback: use passed-in channels (legacy behavior)
        freqs = [int(cfg['frequency']) for cfg in channels.values()]
        if not freqs:
            raise ValueError('No channels configured')
        center = sum(freqs) // len(freqs)
    else:
        # Compute center from ALL defined channel frequencies (active + inactive)
        all_freqs = [int(ch.get('frequency', 0))
                     for ch in all_ui_channels if ch.get('frequency', 0)]
        if not all_freqs:
            raise ValueError('No channel frequencies found in UI config')
        center = sum(all_freqs) // len(all_freqs)

    logger.info('_generate_bridge_conf: center=%d Hz (from %d defined channels)',
                center, len(all_ui_channels) if all_ui_channels else len(channels))

    # Validate all frequencies are within +/-500kHz of center
    for ch in (all_ui_channels or []):
        f = int(ch.get('frequency', 0))
        if f and abs(f - center) > 490000:
            raise ValueError(
                f'Channel {ch.get("name", "?")} freq {f} too far from center {center}')

    # --- HAL-level LBT is ALWAYS DISABLED ---
    # All LBT logic is handled in Python software (GlobalTXScheduler)
    _lbt_enabled = False
    _lbt_rssi_target = -80
    _lbt_channels = []

    # --- SX1261: compute spectral_scan dynamically from channel config ---
    _src_channels = all_ui_channels if all_ui_channels else list(channels.values())
    _max_bw_hz = max(int(cfg.get('bandwidth', 125000)) for cfg in _src_channels)
    _half_bw = _max_bw_hz // 2
    _nb_chan = 36  # cover 863-870 MHz (36 x ~200kHz steps)
    _spectral_scan_conf = {
        'enable': True,
        'freq_start': 863000000,  # full EU868 band start for wide spectral view
        'nb_chan': _nb_chan,
        'nb_scan': 100,  # reduced from 2000 for faster scans (less TX abort)
        'pace_s': 1,  # try every 1s to catch TX gaps
    }
    logger.info('_generate_bridge_conf: spectral_scan enabled '
                '(freq_start=%d, nb_chan=%d, bw=%dHz, center=%d)',
                center - _half_bw, _nb_chan, _max_bw_hz, center)

    # Build chan_multiSF_0 through chan_multiSF_7 with FIXED IF chain mapping
    chan_configs = {}

    if all_ui_channels:
        # INDEX-BASED mapping: channel position in UI list -> IF chain index
        # This is independent of channel names (which can be renamed in UI)
        for idx, ch in enumerate(all_ui_channels):
            if idx > 7:  # max 8 IF chains (chan_multiSF_0 to chan_multiSF_7)
                logger.warning('_generate_bridge_conf: more than 8 channels, '
                              'ignoring channel %d+', idx)
                break
            if_idx = idx  # Position-based: first UI channel -> IF chain 0, etc.
            ch_name = ch.get('name', '')
            ch_freq = int(ch.get('frequency', 0))
            if_offset = ch_freq - center
            is_active = ch.get('active', False)
            chan_configs[f'chan_multiSF_{if_idx}'] = {
                'enable': is_active, 'radio': 0, 'if': if_offset
            }
            logger.info('_generate_bridge_conf: %s (idx=%d) -> chan_multiSF_%d: '
                       'enable=%s, if=%+d',
                       ch_name, idx, if_idx, is_active, if_offset)
    else:
        # Fallback: position-based (legacy)
        chan_list = list(channels.items())
        for i in range(min(len(chan_list), 4)):
            _, cfg = chan_list[i]
            freq = int(cfg['frequency'])
            if_offset = freq - center
            chan_configs[f'chan_multiSF_{i}'] = {
                'enable': True, 'radio': 0, 'if': if_offset
            }

    # Ensure slots 0-3 exist (disabled if not set), and 4-7 always disabled
    for i in range(8):
        key = f'chan_multiSF_{i}'
        if key not in chan_configs:
            chan_configs[key] = {'enable': False, 'radio': 0, 'if': 0}

    conf = {
        'SX130x_conf': {
            'com_type': 'SPI',
            'com_path': '/dev/spidev0.0',
            'lorawan_public': False,
            'clksrc': 0,
            'antenna_gain': 0,
            'full_duplex': False,
            'precision_timestamp': {
                'enable': False,
                'max_ts_metrics': 255,
                'nb_symbols': 1,
            },
            # RF0 = RX + TX via SKY66420 FEM (PA + LNA + RF Switch)
            'radio_0': {
                'enable': True,
                'type': 'SX1250',
                'freq': center,
                'rssi_offset': -215.4,
                'rssi_tcomp': {'coeff_a': 0, 'coeff_b': 0, 'coeff_c': 20.41,
                               'coeff_d': 2162.56, 'coeff_e': 0},
                'tx_enable': True,
                'tx_freq_min': 863000000,
                'tx_freq_max': 870000000,
                'tx_gain_lut': DEFAULT_TX_GAIN_LUT,
            },
            # RF1 = RX only (no PA, SAW filter path)
            'radio_1': {
                'enable': True,
                'type': 'SX1250',
                'freq': center,
                'rssi_offset': -215.4,
                'rssi_tcomp': {'coeff_a': 0, 'coeff_b': 0, 'coeff_c': 20.41,
                               'coeff_d': 2162.56, 'coeff_e': 0},
                'tx_enable': False,
            },
            **chan_configs,
            'chan_Lora_std':  {'enable': False, 'radio': 0, 'if': 0,
                              'bandwidth': 250000, 'spread_factor': 7},
            'chan_FSK':       {'enable': False, 'radio': 0, 'if': 0,
                              'bandwidth': 125000, 'datarate': 50000},
            # SX1261 companion chip for LBT (Listen Before Talk)
            'sx1261_conf': {
                'spi_path': '/dev/spidev0.1',
                'rssi_offset': 0,
                'spectral_scan': _spectral_scan_conf,
                'lbt': {
                    'enable': _lbt_enabled,
                    'rssi_target': _lbt_rssi_target,
                    'nb_channel': len(_lbt_channels),
                    'channels': _lbt_channels,
                },
            },
        },
        'gateway_conf': {
            'gateway_ID': 'AA555A0000000000',
            'server_address': '127.0.0.1',
            'serv_port_up':   UDP_PORT_UP,
            'serv_port_down': UDP_PORT_DOWN,
            'keepalive_interval': 10,
            'stat_interval': 30,
            'push_timeout_ms': 100,
            'forward_crc_valid': True,
            'forward_crc_error': True,
            'forward_crc_disabled': True,
        },
    }
    return conf


# Module-level reference for direct API access (replaces gc.get_referrers)
_active_backend = None  # type: WM1303Backend | None

class WM1303Backend:
    """WM1303/SX1303 concentrator backend using lora_pkt_fwd.

    RF0-TX Architecture:
    - RF0 (SX1250_0): RX + TX via SKY66420 FEM (PA + LNA + RF Switch)
    - RF1 (SX1250_1): RX only (no PA)
    - IF chains 0-2 on RF0
    - TX via direct PULL_RESP to running pkt_fwd (no stop/start needed)
    - Self-echo detection discards own TX heard back on RX
    """

    def __init__(self, config: dict[str, Any]):
        self.config = config
        self.virtual_radios: dict[str, Any] = {}
        self.channels: dict[str, dict] = {}
        self._proc: subprocess.Popen | None = None
        self._sock: socket.socket | None = None
        self._pull_addr: tuple | None = None
        self._running = False
        self._thread: threading.Thread | None = None
        self._stdout_thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._tx_lock = threading.Lock()
        self._last_tx_end: float = 0.0  # monotonic time when last TX completes

        # TX stats
        self._tx_packets_sent_total = 0

        # Self-echo detection: store hashes of recently transmitted packets
        self._tx_echo_hashes: dict[str, float] = {}  # md5_hash -> monotonic_time
        self._tx_echo_ttl = 30.0  # seconds to keep TX hashes
        self._tx_echo_detected = 0
        self._rx_dedup_cache: dict = {}  # multi-demod dedup  # counter for stats

        # Dispatcher RX callback (set by Dispatcher via set_rx_callback)
        self.rx_callback = None
        self._last_pkt_rssi: int = -120
        self._last_pkt_snr: float = 0.0

        # AGC Recovery: reset concentrator after TX to restore RX sensitivity
        self._agc_reset_timer = None
        self._pktfwd_ready_event = threading.Event()  # threading.Timer or None
        self._agc_reset_lock = threading.Lock()
        self._agc_reset_delay = 0.3  # seconds after last TX before reset
        self._agc_resets = 0  # counter for stats
        self._agc_resetting = False  # flag to prevent concurrent resets
        self._current_burst_tx_time = 0.0  # cumulative TX time for current burst

        # TX batch hold: after RX, delay TX by this many seconds to collect more floods
        self._tx_hold_until = 0.0  # monotonic timestamp; TX held until this time
        self._tx_hold_seconds = 2.0  # hold window in seconds

        # Per-channel TX queues (managed internally)
        self._tx_queue_manager: TXQueueManager | None = None
        self._global_tx_scheduler: GlobalTXScheduler | None = None

        # Per-channel RX statistics (updated in _dispatch_rx)
        self._channel_rx_stats: dict[str, dict] = {}

        # Per-channel TX timing statistics (updated after each TX)
        self._channel_tx_stats: dict[str, dict] = {}
        self._start_time = time.time()  # for duty cycle calculation

        # Store module-level reference for API access
        global _active_backend
        _active_backend = self

        # Software LBT: config cache with TTL
        self._lbt_config_cache = {}
        self._lbt_config_cache_time = 0
        self._lbt_config_cache_ttl = 5.0  # seconds
        
        # Per-channel noise floor monitor
        self._nf_monitor_thread: threading.Thread | None = None
        self._nf_monitor_running = False
        self._channel_noise_floors: dict[str, float] = {}  # ui_config_name -> noise_floor_dbm
        self._nf_lock = threading.Lock()  # protects _channel_noise_floors and _freq_to_ui_name
        self._freq_to_ui_name: dict[int, str] = {}  # freq_hz -> ui_config_name (e.g. 869461000 -> 'SF8')
        self._nf_interval = 30  # seconds between noise floor updates

        # CAD config cache
        self._cad_config_cache: dict[str, bool] = {}  # channel_id -> cad_enabled
        self._cad_config_cache_time: float = 0


        # SX1261 for LBT/CAD only (not TX)
        self._sx1261 = None
        self._sx1261_available = False

        # RX Watchdog: auto-restart pkt_fwd when concentrator stops receiving
        self._last_rx_timestamp = time.monotonic()
        self._watchdog_timeout = 180  # 3 minutes without RX triggers restart
        self._watchdog_thread: threading.Thread | None = None
        self._watchdog_running = False

        # Early detection: PUSH_DATA stat monitoring (Detection Method 1)
        self._zero_rx_stat_count = 0   # consecutive stat windows with rxnb=0
        self._last_stat_rxnb = -1      # last rxnb value from PUSH_DATA stats
        self._last_stat_txnb = 0       # last txnb value from PUSH_DATA stats

        # Early detection: SX1261 RSSI vs SX1302 RX comparison (Detection Method 2)
        self._rssi_spike_count = 0             # strong RSSI detections by SX1261
        self._rssi_spike_window_start = time.monotonic()  # window start for spike counting


    def register_virtual_radio(self, radio) -> None:
        self.virtual_radios[radio.channel_id] = radio
        self.channels[radio.channel_id] = radio.channel_config

    def get_radios(self) -> list:
        """Create and register VirtualLoRaRadio instances from SSOT + config.

        PRIMARY source: wm1303_ui.json (SSOT) — the user-facing channel config.
        FALLBACK: config.yaml wm1303.channels — only used when SSOT has no channels.

        Each active SSOT channel becomes a VirtualLoRaRadio that self-registers
        via register_virtual_radio() in __init__.

        Returns:
            List of VirtualLoRaRadio instances (one per active channel).
        """
        from .virtual_radio import VirtualLoRaRadio
        import json as _json
        from pathlib import Path as _Path

        # If radios already registered, return them
        if self.virtual_radios:
            logger.info('WM1303Backend.get_radios(): returning %d existing radios',
                       len(self.virtual_radios))
            return list(self.virtual_radios.values())

        _CHANNEL_ID_BY_INDEX = ['channel_a', 'channel_b', 'channel_c', 'channel_d']

        # ---- PRIMARY: read channels from SSOT (wm1303_ui.json) ----
        ui_path = _Path('/etc/pymc_repeater/wm1303_ui.json')
        ssot_channels = []
        try:
            if ui_path.exists():
                ui = _json.loads(ui_path.read_text())
                ssot_channels = ui.get('channels', [])
        except Exception as e:
            logger.warning('WM1303Backend.get_radios(): failed to read SSOT: %s', e)

        # Filter to active-only SSOT channels
        active_ssot = [ch for ch in ssot_channels if ch.get('active', True)]

        if active_ssot:
            logger.info('WM1303Backend.get_radios(): SSOT has %d active channels '
                       '(of %d total)', len(active_ssot), len(ssot_channels))
            radios = []
            for idx, ssot_ch in enumerate(active_ssot):
                if idx >= len(_CHANNEL_ID_BY_INDEX):
                    logger.warning('WM1303Backend.get_radios(): max %d channels '
                                  'supported, ignoring extra', len(_CHANNEL_ID_BY_INDEX))
                    break
                channel_id = _CHANNEL_ID_BY_INDEX[idx]
                channel_config = {
                    'frequency': int(ssot_ch.get('frequency', 0)),
                    'spreading_factor': int(ssot_ch.get('spreading_factor', 7)),
                    'bandwidth': int(ssot_ch.get('bandwidth', 125000)),
                    'coding_rate': ssot_ch.get('coding_rate', '4/5'),
                    'preamble_length': int(ssot_ch.get('preamble_length', 17)),
                    'tx_power': int(ssot_ch.get('tx_power', 14)),
                    'tx_enable': ssot_ch.get('tx_enabled', True),
                    'active': True,
                    'name': ssot_ch.get('name', f'ch_{idx}'),
                    'friendly_name': ssot_ch.get('friendly_name', f'Channel {idx}'),
                }
                logger.info('WM1303Backend.get_radios(): SSOT -> %s freq=%d SF%d (%s)',
                           channel_id, channel_config['frequency'],
                           channel_config['spreading_factor'],
                           channel_config['friendly_name'])
                radio = VirtualLoRaRadio(self, channel_id, channel_config)
                radios.append(radio)

            logger.info('WM1303Backend.get_radios(): created %d radios from SSOT: %s',
                       len(radios), [r.channel_id for r in radios])
            return radios

        # ---- FALLBACK: read from config.yaml ----
        logger.warning('WM1303Backend.get_radios(): no active SSOT channels, '
                      'falling back to config.yaml')
        wm1303_cfg = self.config.get('wm1303', {})
        channels_cfg = wm1303_cfg.get('channels', {})
        if not channels_cfg:
            logger.warning('WM1303Backend.get_radios(): no channels in config.yaml either')
            return []

        radios = []
        for channel_id, cfg in channels_cfg.items():
            channel_config = dict(cfg)
            ch_freq = int(cfg.get('frequency', 0))
            logger.info('WM1303Backend.get_radios(): config.yaml -> %s freq=%d SF%s',
                       channel_id, ch_freq, channel_config.get('spreading_factor', '?'))
            radio = VirtualLoRaRadio(self, channel_id, channel_config)
            radios.append(radio)

        logger.info('WM1303Backend.get_radios(): created %d radios from config.yaml: %s',
                   len(radios), [r.channel_id for r in radios])
        return radios

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    # ----------------------------------------------------------------
    # LoRaRadio interface methods required by dev-branch Dispatcher
    # ----------------------------------------------------------------

    def set_rx_callback(self, callback):
        """Set a callback to be called with each received packet (bytes, rssi, snr)."""
        self.rx_callback = callback
        logger.info("WM1303Backend: RX callback registered: %s", callback)

    def get_last_rssi(self) -> int:
        """Return last received RSSI in dBm."""
        return self._last_pkt_rssi

    def get_last_snr(self) -> float:
        """Return last received SNR in dB."""
        return self._last_pkt_snr

    def sleep(self):
        """Put the radio into low-power mode (no-op for WM1303 concentrator)."""
        pass

    async def wait_for_rx(self) -> bytes:
        """Wait for a packet (not used - WM1303 uses callback-based RX via UDP)."""
        raise NotImplementedError(
            "WM1303Backend uses callback-based RX via set_rx_callback(), "
            "not polling via wait_for_rx()"
        )

    def begin(self) -> bool:
        if self._running:
            return True

        # Auto-create virtual radios if none registered yet
        if not self.virtual_radios:
            logger.info('WM1303Backend.begin(): no virtual radios registered, calling get_radios()')
            self.get_radios()

        try:
            self._loop = asyncio.get_event_loop()
        except RuntimeError:
            self._loop = None

        # Write bridge_conf.json with SSOT overlay
        self._write_pktfwd_config()

        # Start UDP server
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(('0.0.0.0', UDP_PORT_UP))
        self._sock.settimeout(2.0)
        logger.info('WM1303Backend: UDP server listening on port %d', UDP_PORT_UP)

        # Reset and start lora_pkt_fwd
        self._start_pktfwd()

        # Initialize per-channel TX queues
        self._init_tx_queues()

        # Try to initialize SX1261 for LBT/CAD (optional, not for TX)
        self._init_sx1261_lbt()

        # Start UDP listener thread
        self._running = True
        self._thread = threading.Thread(target=self._udp_loop, daemon=True)
        self._thread.start()
        logger.info('WM1303Backend: backend started with %d virtual channels '
                    '(RF0-TX direct PULL_RESP architecture)', len(self.channels))

        # Start channel stats snapshot thread (periodic DB snapshots)
        self._snapshot_running = True
        self._snapshot_thread = threading.Thread(
            target=self._channel_stats_snapshot_loop, daemon=True)
        self._snapshot_thread.start()

        # Start RX watchdog thread
        self._watchdog_running = True
        self._last_rx_timestamp = time.monotonic()  # reset before watchdog starts
        self._watchdog_thread = threading.Thread(
            target=self._watchdog_loop, daemon=True, name='rx-watchdog')
        self._watchdog_thread.start()


        # Start periodic heartbeat reset (full concentrator restart every 3 min)
        # self._start_heartbeat_reset()  # DISABLED: breaks UDP pipeline
        return True


    def _write_pktfwd_config(self) -> None:
        """Write bridge_conf.json from _generate_bridge_conf (SSOT).

        bridge_conf.json is AUTHORITATIVE. global_conf.json is always
        a copy of bridge_conf.json after generation.
        """
        conf = _generate_bridge_conf(self.channels)
        BRIDGE_CONF.write_text(json.dumps(conf, indent=2))
        Path('/tmp/pymc_wm1303_bridge_conf.json').write_text(json.dumps(conf, indent=2))
        logger.info('WM1303Backend: wrote bridge_conf.json for %d channels '
                    '(fixed IF chain mapping, RF0-TX architecture)', len(self.channels))

        # Copy bridge_conf.json -> global_conf.json (bridge is authoritative)
        _gc_path = Path('/home/pi/wm1303_pf/global_conf.json')
        try:
            _bc = json.loads(BRIDGE_CONF.read_text())
            _gc_path.write_text(json.dumps(_bc, indent=2))
            logger.info('WM1303Backend: global_conf.json updated from bridge_conf.json')
        except Exception as _ex:
            logger.warning('WM1303Backend: global_conf.json update failed: %s', _ex)

    def _init_tx_queues(self) -> None:
        """Initialize per-channel TX queues.

        SSOT: Frequencies are corrected from wm1303_ui.json before creating
        TX queues. Inactive channels are excluded.
        """
        # SSOT: Apply frequency corrections from wm1303_ui.json
        self._apply_ssot_channel_freqs()

        # FIX Bug3: Build set of inactive channel frequencies from UI config
        _inactive_freqs = set()
        try:
            _ui_path = Path('/etc/pymc_repeater/wm1303_ui.json')
            if _ui_path.exists():
                _ui = json.loads(_ui_path.read_text())
                for _uc in _ui.get('channels', []):
                    if not _uc.get('active', True):
                        _inactive_freqs.add(int(_uc.get('frequency', 0)))
                if _inactive_freqs:
                    logger.info('WM1303Backend: inactive channel freqs: %s', _inactive_freqs)
        except Exception as _e:
            logger.warning('WM1303Backend: could not read UI config for inactive check: %s', _e)

        self._tx_queue_manager = TXQueueManager()
        for channel_id, cfg in self.channels.items():
            # FIX Bug3: Skip inactive channels
            ch_freq = int(cfg.get('frequency', 0))
            if ch_freq in _inactive_freqs:
                logger.info('WM1303Backend: skipping TX queue for %s '
                           '(freq=%d is INACTIVE in UI config)', channel_id, ch_freq)
                continue
            freq_hz = int(cfg.get('frequency', 869462500))
            bw_hz = int(cfg.get('bandwidth', 125000))
            bw_khz = bw_hz / 1000.0
            sf = int(cfg.get('spreading_factor', 8))
            cr_raw = cfg.get('coding_rate', '4/5')
            if isinstance(cr_raw, str) and '/' in cr_raw:
                cr = int(cr_raw.split('/')[1])
            else:
                cr = int(cr_raw)
            preamble = int(cfg.get('preamble_length', 17))
            tx_power = int(cfg.get('tx_power', 14))
            self._tx_queue_manager.add_channel(
                channel_id=channel_id,
                freq_hz=freq_hz,
                bw_khz=bw_khz,
                sf=sf,
                cr=cr,
                preamble=preamble,
                tx_power=tx_power,
            )
            logger.info('WM1303Backend: TX queue created for %s '
                       '(freq=%d, SF%d, BW%.0fkHz)',
                       channel_id, freq_hz, sf, bw_khz)

    def _apply_ssot_channel_freqs(self) -> None:
        """Apply SSOT channel params from wm1303_ui.json to self.channels.

        Matches by POSITION (index) not by SF to avoid mismatches when SF is
        changed in the UI but not yet updated in config.yaml.
        Syncs: frequency, spreading_factor, bandwidth, coding_rate,
               preamble_length, tx_power.
        """
        ui_path = Path('/etc/pymc_repeater/wm1303_ui.json')
        if not ui_path.exists():
            logger.warning('WM1303Backend: SSOT file %s not found, '
                          'using config.yaml frequencies as-is', ui_path)
            return
        try:
            ui = json.loads(ui_path.read_text())
            ui_channels = ui.get("channels", [])  # ALL channels for position-based SSOT sync
            if not ui_channels:
                logger.warning('WM1303Backend: SSOT has no active channels')
                return

            # Match by position: config.yaml channels order ↔ UI channels order
            channel_items = list(self.channels.items())
            corrections = 0
            for idx, (channel_id, cfg) in enumerate(channel_items):
                if idx >= len(ui_channels):
                    logger.debug('WM1303Backend: SSOT has only %d channels, '
                                'no match for %s (index %d)',
                                len(ui_channels), channel_id, idx)
                    break
                ssot_ch = ui_channels[idx]

                # Sync frequency
                old_freq = int(cfg.get('frequency', 0))
                new_freq = int(ssot_ch.get('frequency', old_freq))
                if old_freq != new_freq:
                    cfg['frequency'] = new_freq
                    corrections += 1
                    logger.info('WM1303Backend: SSOT freq correction '
                               '%s [idx=%d]: %d -> %d Hz',
                               channel_id, idx, old_freq, new_freq)

                # Sync spreading_factor and other params from SSOT
                for key in ('spreading_factor', 'bandwidth', 'coding_rate',
                            'preamble_length', 'tx_power'):
                    if key in ssot_ch:
                        ssot_val = ssot_ch[key]
                        old_val = cfg.get(key)
                        if str(old_val) != str(ssot_val):
                            cfg[key] = ssot_val
                            corrections += 1
                            logger.info('WM1303Backend: SSOT sync '
                                       '%s.%s [idx=%d]: %s -> %s',
                                       channel_id, key, idx, old_val, ssot_val)

                logger.debug('WM1303Backend: SSOT matched %s [idx=%d] <- '
                            'UI channel %r', channel_id, idx,
                            ssot_ch.get('name', f'idx{idx}'))

            if corrections > 0:
                logger.info('WM1303Backend: SSOT applied %d corrections '
                           'to self.channels (position-based matching)',
                           corrections)
            else:
                logger.info('WM1303Backend: SSOT check passed — all '
                           'channel params match')
        except Exception as e:
            logger.warning('WM1303Backend: SSOT channel freq overlay '
                          'failed: %s', e)

    def _init_sx1261_lbt(self) -> None:
        """Initialize SX1261 status reporting (managed by HAL)."""
        self._sx1261 = None
        self._sx1261_available = False
        self._sx1261_managed_by_hal = True
        self._sx1261_hal_config = {}

        try:
            import yaml
            cfg_path = Path("/etc/pymc_repeater/config.yaml")
            if cfg_path.exists():
                with open(cfg_path) as f:
                    cfg = yaml.safe_load(f) or {}
                self._sx1261_hal_config = cfg.get('sx1261_lbt', {})
                logger.info('WM1303Backend: SX1261 is managed by lora_pkt_fwd HAL '
                           '(SPI conflict prevents independent access). '
                           'Config: role=%s', self._sx1261_hal_config.get('role', 'unknown'))
            else:
                logger.info('WM1303Backend: config.yaml not found, SX1261 status unknown')
        except Exception as e:
            logger.warning('WM1303Backend: Could not read SX1261 config: %s', e)



    # ------------------------------------------------------------------
    # Software LBT (Listen Before Talk) via spectral scan
    # ------------------------------------------------------------------

    def _read_spectral_scan(self, max_age: float = 30.0) -> dict:
        """Read latest spectral scan results. Returns {freq_mhz: rssi_dbm}.

        Args:
            max_age: Maximum age in seconds for scan data (default 30 for LBT).
                     Use larger values for noise floor estimation.
        """
        try:
            scan_path = Path('/tmp/pymc_spectral_results.json')
            if not scan_path.exists():
                return {}
            data = json.loads(scan_path.read_text())
            ts = data.get('timestamp', 0)
            age = time.time() - ts
            if age > max_age:
                logger.debug('WM1303Backend: spectral scan data stale (%.0fs old, max=%.0fs)',
                            age, max_age)
                return {}
            result = {}
            for pt in data.get('scan_points', []):
                result[pt['freq_mhz']] = pt['rssi_dbm']
            return result
        except Exception as e:
            logger.debug('WM1303Backend: spectral scan read error: %s', e)
            return {}

    def _read_spectrum_from_db(self, max_age: float = 3600.0) -> dict:
        """Fallback: read recent spectrum data from spectrum_history.db.

        Returns {freq_mhz: rssi_dbm} from most recent scan entries.
        """
        try:
            import sqlite3
            db_path = '/var/lib/pymc_repeater/spectrum_history.db'
            if not os.path.exists(db_path):
                return {}
            since_ts = time.time() - max_age
            with sqlite3.connect(db_path) as conn:
                rows = conn.execute(
                    'SELECT freq_mhz, AVG(rssi_dbm) '
                    'FROM spectrum_scans WHERE timestamp > ? '
                    'GROUP BY freq_mhz ORDER BY freq_mhz',
                    (since_ts,)
                ).fetchall()
            if not rows:
                return {}
            result = {r[0]: round(r[1], 1) for r in rows}
            logger.debug('NoiseFloorMonitor: got %d freq points from spectrum_history.db', len(result))
            return result
        except Exception as e:
            logger.debug('NoiseFloorMonitor: spectrum DB read error: %s', e)
            return {}


    def _get_channel_lbt_config(self, channel_id: str) -> dict:
        """Get LBT config for a channel from UI config (cached)."""
        now = time.time()
        if now - self._lbt_config_cache_time < self._lbt_config_cache_ttl:
            cached = self._lbt_config_cache.get(channel_id)
            if cached is not None:
                return cached
        # Refresh cache
        try:
            ui_path = Path('/etc/pymc_repeater/wm1303_ui.json')
            if not ui_path.exists():
                self._lbt_config_cache = {}
                self._lbt_config_cache_time = now
                return {'lbt_enabled': False}
            ui = json.loads(ui_path.read_text())
            new_cache = {}
            for ch in ui.get('channels', []):
                ch_name = ch.get('name', '')
                new_cache[ch_name] = {
                    'lbt_enabled': ch.get('lbt_enabled', False),
                    'lbt_rssi_target': ch.get('lbt_rssi_target', -80),
                }
                # Also cache by friendly_name for matching
                fn = ch.get('friendly_name', '')
                if fn:
                    fn_key = fn.lower().replace(' ', '_')
                    new_cache[fn_key] = new_cache[ch_name]
            self._lbt_config_cache = new_cache
            self._lbt_config_cache_time = now
            return new_cache.get(channel_id, {'lbt_enabled': False})
        except Exception:
            return {'lbt_enabled': False}


    # ------------------------------------------------------------------
    # Per-Channel Noise Floor Monitor
    # ------------------------------------------------------------------

    def _start_noise_floor_monitor(self) -> None:
        """Start the background noise floor monitoring thread."""
        if self._nf_monitor_running:
            return
        self._nf_monitor_running = True
        self._nf_monitor_thread = threading.Thread(
            target=self._noise_floor_monitor_loop, daemon=True, name='NoiseFloorMonitor')
        self._nf_monitor_thread.start()
        logger.info('WM1303Backend: NoiseFloorMonitor started (interval=%ds)', self._nf_interval)

    def _stop_noise_floor_monitor(self) -> None:
        """Stop the noise floor monitoring thread."""
        self._nf_monitor_running = False
        if self._nf_monitor_thread:
            self._nf_monitor_thread.join(timeout=5)
            self._nf_monitor_thread = None
        logger.info('WM1303Backend: NoiseFloorMonitor stopped')

    def _noise_floor_monitor_loop(self) -> None:
        """Background loop: measure per-channel noise floor every N seconds.

        Before each measurement cycle, sets a TX hold window to let the HAL's
        spectral scan thread run uninterrupted.  The spectral scan on the
        SX1261 is continuously aborted by TX operations; pausing TX for a few
        seconds allows it to complete and produce real RSSI data.
        """
        # Wait 10s for system to stabilize before first measurement
        for _ in range(20):
            if not self._nf_monitor_running:
                return
            time.sleep(0.5)

        while self._nf_monitor_running:
            try:
                # --- TX Hold: pause TX so the HAL spectral scan can complete ---
                hold_seconds = 4.0
                self._tx_hold_until = time.monotonic() + hold_seconds
                logger.info('NoiseFloorMonitor: TX hold set for %.0fs '
                           '(spectral scan window)', hold_seconds)
                # Wait for the hold window to elapse so the scan can run
                _hold_start = time.monotonic()
                while time.monotonic() - _hold_start < hold_seconds:
                    if not self._nf_monitor_running:
                        return
                    time.sleep(0.5)
                # Small extra pause for the collector to parse log output
                time.sleep(0.5)

                # Now read spectral data and compute noise floors
                self._update_channel_noise_floors()

                # Feed noise floor values into TX queue LBT RSSI buffers
                self._feed_noise_floor_to_tx_queues()
            except Exception as e:
                logger.error('NoiseFloorMonitor error: %s', e)
            # Sleep in small increments for responsive shutdown
            for _ in range(self._nf_interval * 2):
                if not self._nf_monitor_running:
                    return
                time.sleep(0.5)

    def _update_channel_noise_floors(self) -> None:
        """Compute per-channel noise floor from spectral scan data and store in DB."""
        # Load active channels from UI config
        try:
            ui_path = Path('/etc/pymc_repeater/wm1303_ui.json')
            if not ui_path.exists():
                return
            ui = json.loads(ui_path.read_text())
            channels = [ch for ch in ui.get('channels', []) if ch.get('active', False)]
            # Build/refresh freq_hz -> ui_config_name mapping
            new_map = {}
            for ch in ui.get('channels', []):
                f = ch.get('frequency', 0)
                n = ch.get('name', '')
                if f and n:
                    new_map[int(f)] = n
            with self._nf_lock:
                self._freq_to_ui_name = new_map
        except Exception as e:
            logger.debug('NoiseFloorMonitor: cannot read UI config: %s', e)
            return

        if not channels:
            return

        # Read spectral scan data
        # Read spectral scan data (use long max_age for noise floor, fallback to DB)
        scan_data = self._read_spectral_scan(max_age=86400.0)
        if not scan_data:
            scan_data = self._read_spectrum_from_db(max_age=86400.0)
        if not scan_data:
            logger.debug('NoiseFloorMonitor: no spectral scan data available (JSON or DB)')
            return

        now = time.time()
        _DB_PATH = '/var/lib/pymc_repeater/repeater.db'

        for ch in channels:
            ch_name = ch.get('name', '')
            freq_hz = ch.get('frequency', 0)
            bw_hz = ch.get('bandwidth', 125000)
            if not freq_hz:
                continue

            freq_mhz = freq_hz / 1e6
            bw_mhz = bw_hz / 1e6
            half_bw = bw_mhz / 2

            # Collect scan points within ±BW/2 of channel center
            nearby_rssi = []
            for scan_freq, rssi_dbm in scan_data.items():
                if abs(scan_freq - freq_mhz) <= half_bw:
                    nearby_rssi.append(rssi_dbm)

            if not nearby_rssi:
                # Try wider tolerance (±1 MHz) if no points within BW
                for scan_freq, rssi_dbm in scan_data.items():
                    if abs(scan_freq - freq_mhz) <= 1.0:
                        nearby_rssi.append(rssi_dbm)

            if not nearby_rssi:
                continue

            samples_collected = len(nearby_rssi)

            # Get current noise floor for this channel (for filtering)
            with self._nf_lock:
                current_nf = self._channel_noise_floors.get(ch_name, -120.0)

            # Filter: only accept samples where sample < current_nf + 14 dB
            # This removes strong signals, keeping only noise-level samples
            filter_threshold = current_nf + 14.0
            accepted = [r for r in nearby_rssi if r < filter_threshold]

            # Detection Method 2: Count strong RSSI spikes (signal > noise_floor + 20 dB)
            # These indicate LoRa packets in the air that the SX1302 should be demodulating
            spike_threshold = current_nf + 20.0
            spikes_this_scan = sum(1 for r in nearby_rssi if r > spike_threshold)
            if spikes_this_scan > 0:
                self._rssi_spike_count += spikes_this_scan
                logger.debug('NoiseFloorMonitor: %s: %d RSSI spikes above %.1f dBm '
                            '(nf=%.1f, total_spikes=%d)',
                            ch_name, spikes_this_scan, spike_threshold,
                            current_nf, self._rssi_spike_count)


            if not accepted:
                # If all samples above threshold, use the minimum as a rough estimate
                accepted = [min(nearby_rssi)]

            samples_accepted = len(accepted)
            new_nf = sum(accepted) / len(accepted)

            # Clamp to valid range
            new_nf = max(-130.0, min(-50.0, new_nf))

            min_rssi = min(nearby_rssi)
            max_rssi = max(nearby_rssi)

            # Update in-memory cache
            with self._nf_lock:
                self._channel_noise_floors[ch_name] = round(new_nf, 1)

            # Store in SQLite
            try:
                with sqlite3.connect(_DB_PATH) as conn:
                    conn.execute(
                        """INSERT INTO noise_floor_history
                        (timestamp, channel_id, noise_floor_dbm,
                         samples_collected, samples_accepted, min_rssi, max_rssi)
                        VALUES (?, ?, ?, ?, ?, ?, ?)""",
                        (now, ch_name, round(new_nf, 1),
                         samples_collected, samples_accepted,
                         round(min_rssi, 1), round(max_rssi, 1)))
                    conn.commit()
            except Exception as e:
                logger.debug('NoiseFloorMonitor: DB store error for %s: %s', ch_name, e)

        logger.debug('NoiseFloorMonitor: updated noise floors for %d channels: %s',
                    len(channels),
                    {ch['name']: self._channel_noise_floors.get(ch['name'])
                     for ch in channels})

    def get_channel_noise_floors(self) -> dict:
        """Get current per-channel noise floor values (thread-safe)."""
        with self._nf_lock:
            return dict(self._channel_noise_floors)

    def _feed_noise_floor_to_tx_queues(self) -> None:
        """Push latest noise-floor values into each TX queue's LBT RSSI buffer.

        Called after every _update_channel_noise_floors() cycle so the
        per-channel rolling buffers (and consequently noise_floor_lbt_avg/
        min/max exposed via the channels/live API) reflect real measurements
        from the SX1261 spectral scan rather than staying at the default
        None/-120 fallback.

        Note: TX queues use internal IDs (channel_a, channel_b, ...) while
        _channel_noise_floors uses UI config names (SF8, SF-test, SF7, ...).
        We bridge the gap via self._freq_to_ui_name (built by _update_channel_noise_floors).
        """
        if not self._tx_queue_manager:
            return
        with self._nf_lock:
            nf_copy = dict(self._channel_noise_floors)
            freq_map = dict(self._freq_to_ui_name)
        if not nf_copy or not freq_map:
            return

        fed = 0
        mapping_info = {}
        for ch_id, queue in self._tx_queue_manager.queues.items():
            ui_name = freq_map.get(int(queue.freq_hz))
            if ui_name is None:
                continue
            nf_val = nf_copy.get(ui_name)
            if nf_val is not None:
                queue.record_lbt_rssi(nf_val)
                fed += 1
                mapping_info[ch_id] = (ui_name, nf_val)
        if fed:
            logger.debug('NoiseFloorMonitor: fed noise-floor RSSI to %d TX queues: %s',
                        fed, mapping_info)

    # ------------------------------------------------------------------
    # CAD-like detection from spectral scan data
    # ------------------------------------------------------------------

    def _get_channel_cad_config(self, channel_id: str) -> bool:
        """Check if CAD is enabled for a channel (cached)."""
        now = time.time()
        if now - self._cad_config_cache_time < self._lbt_config_cache_ttl:
            cached = self._cad_config_cache.get(channel_id)
            if cached is not None:
                return cached
        # Refresh from UI config
        try:
            ui_path = Path('/etc/pymc_repeater/wm1303_ui.json')
            if not ui_path.exists():
                self._cad_config_cache = {}
                self._cad_config_cache_time = now
                return False
            ui = json.loads(ui_path.read_text())
            new_cache = {}
            for ch in ui.get('channels', []):
                ch_name = ch.get('name', '')
                # CAD defaults to True when LBT is enabled
                cad_enabled = ch.get('cad_enabled', ch.get('lbt_enabled', False))
                new_cache[ch_name] = cad_enabled
                fn = ch.get('friendly_name', '')
                if fn:
                    fn_key = fn.lower().replace(' ', '_')
                    new_cache[fn_key] = cad_enabled
            self._cad_config_cache = new_cache
            self._cad_config_cache_time = now
            return new_cache.get(channel_id, False)
        except Exception:
            return False

    def _cad_check(self, channel_id: str, freq_hz: int, rssi: float) -> dict:
        """CAD-like channel activity detection using spectral scan RSSI pattern.

        Analyzes the RSSI distribution around the channel frequency.
        A LoRa signal concentrates energy in a narrow band, while
        broadband noise is spread evenly across frequencies.

        Returns dict with: detected (bool), confidence (float 0-1), reason (str)
        """
        scan_data = self._read_spectral_scan()
        if not scan_data:
            return {'detected': False, 'confidence': 0, 'reason': 'no_scan_data'}

        freq_mhz = freq_hz / 1e6
        # Collect RSSI values within ±0.5 MHz of channel
        nearby = []
        for scan_freq, scan_rssi in scan_data.items():
            if abs(scan_freq - freq_mhz) <= 0.5:
                nearby.append((scan_freq, scan_rssi))

        if len(nearby) < 2:
            return {'detected': False, 'confidence': 0, 'reason': 'insufficient_scan_points'}

        rssi_values = [r for _, r in nearby]
        max_rssi = max(rssi_values)
        min_rssi = min(rssi_values)
        mean_rssi = sum(rssi_values) / len(rssi_values)
        spread = max_rssi - min_rssi

        # Get noise floor for reference (map freq_hz -> UI name -> noise floor)
        with self._nf_lock:
            _ui_name = self._freq_to_ui_name.get(int(freq_hz), '')
            nf = self._channel_noise_floors.get(_ui_name, -120.0)

        # Detection logic:
        # 1. If max RSSI is well above noise floor AND energy is concentrated
        #    (large spread between max and mean) -> likely LoRa signal
        # 2. If RSSI is roughly uniform -> broadband noise
        above_noise = max_rssi - nf
        concentration = max_rssi - mean_rssi  # High = energy in few points

        # LoRa signal indicators:
        # - Max RSSI significantly above noise floor (>10 dB)
        # - Energy concentrated in 1-2 scan points (concentration > 3 dB)
        detected = False
        confidence = 0.0

        if above_noise > 10 and concentration > 3:
            detected = True
            confidence = min(1.0, (above_noise - 10) / 20.0 * 0.5 + (concentration - 3) / 10.0 * 0.5)
        elif above_noise > 6 and concentration > 5:
            detected = True
            confidence = min(0.7, concentration / 15.0)

        return {
            'detected': detected,
            'confidence': round(confidence, 2),
            'reason': 'lora_signal_detected' if detected else 'clear',
            'max_rssi': round(max_rssi, 1),
            'mean_rssi': round(mean_rssi, 1),
            'spread': round(spread, 1),
            'above_noise': round(above_noise, 1),
            'concentration': round(concentration, 1),
            'scan_points': len(nearby),
        }

    def _lbt_check(self, channel_id: str, freq_hz: int) -> dict:
        """Enhanced LBT check with spectral-scan harvest and noise-floor fallback.

        Data sources (tried in order):
          1. Spectral scan JSON file (max_age 120 s – covers several monitor cycles)
          2. Spectrum history DB      (max_age 300 s)
          3. Per-channel noise floor cache (always available after first monitor cycle)

        When real scan data is available the full flow runs:
          ① RSSI check (fast) – broadband interference detection
          ② CAD check (spectral pattern analysis) – LoRa signal detection
          ③ Allow TX if both pass

        When only the noise-floor estimate is available the check still returns
        a valid RSSI value so the rolling buffer in ChannelTXQueue gets populated.
        """
        lbt_cfg = self._get_channel_lbt_config(channel_id)
        if not lbt_cfg.get('lbt_enabled', False):
            return {'allow': True, 'lbt_enabled': False, 'reason': 'lbt_disabled'}

        freq_mhz = freq_hz / 1e6
        threshold = lbt_cfg.get('lbt_rssi_target', -80)

        # Use per-channel noise floor for adaptive threshold if available
        # Map freq_hz -> UI config name -> noise floor value
        with self._nf_lock:
            _ui_name = self._freq_to_ui_name.get(int(freq_hz), '')
            ch_nf = self._channel_noise_floors.get(_ui_name)
        if ch_nf is not None:
            adaptive_threshold = min(threshold, ch_nf + 10.0)
        else:
            adaptive_threshold = threshold

        # --- Try to get spectral scan data (multiple sources) ---
        scan_data = self._read_spectral_scan(max_age=120.0)
        if not scan_data:
            scan_data = self._read_spectrum_from_db(max_age=300.0)

        # --- If we have scan data, find closest point to our frequency ---
        rssi = None
        best_freq = None
        if scan_data:
            best_dist = float('inf')
            for scan_freq in scan_data:
                dist = abs(scan_freq - freq_mhz)
                if dist < best_dist:
                    best_dist = dist
                    best_freq = scan_freq
            if best_freq is not None and best_dist <= 0.5:
                rssi = scan_data[best_freq]

        # --- Fallback: use noise floor cache as RSSI estimate ---
        if rssi is None and ch_nf is not None:
            rssi = ch_nf
            return {
                'allow': True,
                'lbt_enabled': True,
                'rssi': rssi,
                'threshold': adaptive_threshold,
                'noise_floor': ch_nf,
                'freq_mhz': freq_mhz,
                'reason': 'noise_floor_estimate',
            }

        # --- No data at all: allow TX but report no measurement ---
        if rssi is None:
            return {
                'allow': True,
                'lbt_enabled': True,
                'rssi': None,
                'threshold': adaptive_threshold,
                'noise_floor': ch_nf,
                'reason': 'no_scan_data',
            }

        # ① RSSI above threshold → block (broadband interference)
        if rssi > adaptive_threshold:
            self._store_cad_event(channel_id, 'detected', rssi, 'rssi_block')
            return {
                'allow': False,
                'lbt_enabled': True,
                'rssi': rssi,
                'threshold': adaptive_threshold,
                'noise_floor': ch_nf,
                'freq_mhz': best_freq,
                'reason': 'channel_busy',
                'cad_result': 'skipped_rssi_blocked',
            }

        # ② CAD check (if enabled for this channel)
        cad_enabled = self._get_channel_cad_config(channel_id)
        cad_result = None
        if cad_enabled:
            cad_result = self._cad_check(channel_id, freq_hz, rssi)
            if cad_result.get('detected', False):
                self._store_cad_event(channel_id, 'detected', rssi, 'lbt_check')
                return {
                    'allow': False,
                    'lbt_enabled': True,
                    'rssi': rssi,
                    'threshold': adaptive_threshold,
                    'noise_floor': ch_nf,
                    'freq_mhz': best_freq,
                    'reason': 'cad_signal_detected',
                    'cad_result': cad_result,
                }
            else:
                self._store_cad_event(channel_id, 'clear', rssi, 'lbt_check')

        # ③ Both checks passed – allow TX
        return {
            'allow': True,
            'lbt_enabled': True,
            'rssi': rssi,
            'threshold': adaptive_threshold,
            'noise_floor': ch_nf,
            'freq_mhz': best_freq,
            'reason': 'clear',
            'cad_result': cad_result,
        }

    def _store_cad_event(self, channel_id: str, result: str,
                         rssi: float = None, context: str = 'lbt_check') -> None:
        """Store a CAD event in the database (fire-and-forget)."""
        try:
            _DB_PATH = '/var/lib/pymc_repeater/repeater.db'
            with sqlite3.connect(_DB_PATH) as conn:
                conn.execute(
                    "INSERT INTO cad_events (timestamp, channel_id, result, rssi_at_time, context) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (time.time(), channel_id, result, rssi, context))
                conn.commit()
        except Exception:
            pass  # Non-critical, don't block TX

    async def ensure_tx_queues_started(self) -> None:
        """Start the GlobalTXScheduler for round-robin TX across all queues."""
        if self._tx_queue_manager and self._tx_queue_manager.queues:
            self._global_tx_scheduler = GlobalTXScheduler(
                send_func=self._send_for_scheduler,
                queues=self._tx_queue_manager.queues,
                post_tx_callback=self._update_tx_stats,
                lbt_check=self._lbt_check,
                tx_hold_getter=lambda: self._tx_hold_until,
            )
            await self._global_tx_scheduler.start()
            logger.info('WM1303Backend: GlobalTXScheduler started with %d queues',
                        len(self._tx_queue_manager.queues))
            # Start per-channel noise floor monitor
            self._start_noise_floor_monitor()
        else:
            logger.warning('WM1303Backend: No TX queues to schedule')

    def stop(self) -> None:
        self._running = False

        # Stop RX watchdog
        self._watchdog_running = False


        # Cancel any pending AGC reset timer
        _agc_timer = getattr(self, "_agc_reset_timer", None)
        if _agc_timer:
            _agc_timer.cancel()
            self._agc_reset_timer = None

        # Cancel any pending heartbeat reset timer
        _hb_timer = getattr(self, "_heartbeat_timer", None)
        if _hb_timer:
            _hb_timer.cancel()
            self._heartbeat_timer = None
        self._snapshot_running = False

        # Stop noise floor monitor
        self._stop_noise_floor_monitor()


        if self._global_tx_scheduler:
            try:
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    loop.create_task(self._global_tx_scheduler.stop())
            except Exception:
                pass

        if self._tx_queue_manager:
            self._tx_queue_manager.stop_all()
        if self._sx1261:
            try:
                self._sx1261.close()
            except Exception:
                pass
        self._stop_pktfwd_process()

        if self._sock:
            self._sock.close()
            self._sock = None


    def _stop_pktfwd_process(self) -> None:
        """Stop the lora_pkt_fwd process cleanly."""
        if self._proc:
            try:
                self._proc.terminate()
                self._proc.wait(timeout=5)
            except Exception:
                try:
                    self._proc.kill()
                    self._proc.wait(timeout=3)
                except Exception:
                    pass
            self._proc = None
        # Also kill any stray instances
        try:
            subprocess.run(['sudo', 'killall', '-9', 'lora_pkt_fwd'],
                          capture_output=True, timeout=5)
        except Exception:
            pass
        if self._stdout_thread:
            self._stdout_thread.join(timeout=3)
            self._stdout_thread = None
        self._pull_addr = None
        logger.info('WM1303Backend: lora_pkt_fwd stopped')

    def _start_pktfwd(self) -> None:
        # Kill any existing lora_pkt_fwd
        try:
            subprocess.run(['sudo', 'killall', '-9', 'lora_pkt_fwd'],
                          capture_output=True, timeout=5)
            time.sleep(1.0)
        except Exception:
            pass

        # Run GPIO reset (standard reset_lgw.sh only)
        try:
            logger.info('WM1303Backend: running reset_lgw.sh for initial reset')
            r = subprocess.run(
                ['sudo', str(PKTFWD_RESET), 'start'],
                cwd=str(PKTFWD_DIR), capture_output=True, timeout=15
            )
            logger.info('WM1303Backend: reset stdout: %s',
                       r.stdout.decode(errors='replace')[:200])
            if r.returncode != 0:
                logger.warning('WM1303Backend: reset returned %d', r.returncode)
            time.sleep(0.5)  # TCXO warmup (optimized for AGC recovery)
        except Exception as e:
            logger.error('WM1303Backend: GPIO reset failed: %s', e)

        # Start lora_pkt_fwd with bridge config
        try:
            cmd = ['sudo', str(PKTFWD_BIN), '-c', str(BRIDGE_CONF)]
            logger.info('WM1303Backend: starting lora_pkt_fwd '
                       '(RF0-TX, direct PULL_RESP): %s', ' '.join(cmd))
            self._proc = subprocess.Popen(
                cmd,
                cwd=str(PKTFWD_DIR),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
            # Wait for pkt_fwd to signal ready via PULL_DATA (max 5s)
            self._pktfwd_ready_event.clear()
            _t = time.monotonic()
            _ready = self._pktfwd_ready_event.wait(timeout=5.0)
            _wait_ms = round((time.monotonic() - _t) * 1000)
            logger.info("WM1303Backend: pktfwd ready in %dms (timeout=%s)", _wait_ms, not _ready)
            if self._proc.poll() is not None:
                out = self._proc.stdout.read()
                logger.error('WM1303Backend: lora_pkt_fwd exited: %s', out[:500])
                raise RuntimeError('lora_pkt_fwd exited prematurely')
            logger.info('WM1303Backend: lora_pkt_fwd started (pid %d) '
                       '[RF0-TX, direct PULL_RESP]', self._proc.pid)
            # Start stdout reader thread to drain pipe buffer
            self._stdout_thread = threading.Thread(
                target=self._pktfwd_stdout_reader, daemon=True,
                name='pktfwd-stdout')
            self._stdout_thread.start()

        except Exception as e:
            logger.error('WM1303Backend: failed to start lora_pkt_fwd: %s', e)
            raise

    def _restart_pkt_fwd(self) -> None:
        """Restart lora_pkt_fwd subprocess (called by watchdog or manually)."""
        logger.warning('WM1303Backend: _restart_pkt_fwd - stopping current process')
        try:
            self._stop_pktfwd_process()
        except Exception as e:
            logger.error('WM1303Backend: _restart_pkt_fwd stop error: %s', e)
        # Wait for hardware to settle after stopping
        time.sleep(2.0)
        logger.info('WM1303Backend: _restart_pkt_fwd - starting new process')
        try:
            self._start_pktfwd()
            logger.info('WM1303Backend: _restart_pkt_fwd - pkt_fwd restarted successfully')
        except Exception as e:
            logger.error('WM1303Backend: _restart_pkt_fwd start error: %s', e)
            raise

    def _watchdog_loop(self) -> None:
        """Monitor RX activity with 3 detection methods and restart pkt_fwd if stuck.

        Detection 1 (STAT): PUSH_DATA stats show rxnb=0 for 2+ consecutive windows (~60s)
        Detection 2 (RSSI): SX1261 sees strong RF but SX1302 not receiving (60s)
        Detection 3 (TIMEOUT): Original fallback - no RX for full timeout (180s)
        """
        logger.info('WM1303Backend: RX watchdog started (timeout=%ds, '
                    'stat_detect=2 windows, rssi_detect=5 spikes/60s)',
                    self._watchdog_timeout)
        while self._watchdog_running:
            time.sleep(30)  # check every 30 seconds
            if not self._watchdog_running:
                break

            elapsed = time.monotonic() - self._last_rx_timestamp

            # Detection 1: PUSH_DATA stats show no RX for 2+ windows (~60s)
            if self._zero_rx_stat_count >= 2:
                logger.warning(
                    'WM1303Backend: WATCHDOG [STAT] - rxnb=0 for %d stat windows '
                    'with TX active, restarting pkt_fwd',
                    self._zero_rx_stat_count
                )
                self._do_watchdog_restart('stat_monitor')
                continue

            # Detection 2: SX1261 sees RF but SX1302 not receiving (60s)
            if elapsed > 60 and self._rssi_spike_count >= 5:
                logger.warning(
                    'WM1303Backend: WATCHDOG [RSSI] - %d RSSI spikes detected by SX1261 '
                    'but no RX for %.0fs, restarting pkt_fwd',
                    self._rssi_spike_count, elapsed
                )
                self._do_watchdog_restart('rssi_monitor')
                continue

            # Detection 3: Original fallback - no RX for full timeout (180s)
            if elapsed > self._watchdog_timeout:
                logger.warning(
                    'WM1303Backend: WATCHDOG [TIMEOUT] - No RX for %.0fs '
                    '(timeout=%ds), restarting pkt_fwd',
                    elapsed, self._watchdog_timeout
                )
                self._do_watchdog_restart('timeout')
                continue

            # Periodic RSSI spike window reset (every 120s) to avoid stale counts
            spike_window_age = time.monotonic() - self._rssi_spike_window_start
            if spike_window_age > 120:
                if self._rssi_spike_count > 0:
                    logger.debug('WM1303Backend: WATCHDOG - resetting RSSI spike count '
                                '(%d spikes over %.0fs window)',
                                self._rssi_spike_count, spike_window_age)
                self._rssi_spike_count = 0
                self._rssi_spike_window_start = time.monotonic()

            logger.debug('WM1303Backend: WATCHDOG OK - last_rx=%.0fs ago, '
                        'stat_zero=%d, rssi_spikes=%d',
                        elapsed, self._zero_rx_stat_count, self._rssi_spike_count)
        logger.info('WM1303Backend: RX watchdog stopped')

    def _do_watchdog_restart(self, trigger_reason: str) -> None:
        """Common restart logic for all watchdog triggers."""
        logger.warning('WM1303Backend: WATCHDOG restart triggered by: %s', trigger_reason)
        try:
            self._restart_pkt_fwd()
            self._last_rx_timestamp = time.monotonic()
            self._zero_rx_stat_count = 0
            self._rssi_spike_count = 0
            self._rssi_spike_window_start = time.monotonic()
            logger.info('WM1303Backend: WATCHDOG - pkt_fwd restart complete (trigger=%s)',
                       trigger_reason)
        except Exception as e:
            logger.error('WM1303Backend: WATCHDOG restart failed (trigger=%s): %s',
                        trigger_reason, e)


    def _pktfwd_stdout_reader(self) -> None:
        """Drain lora_pkt_fwd stdout to prevent pipe buffer blocking."""
        logger.info('WM1303Backend: stdout reader thread started')
        try:
            while self._proc and self._proc.poll() is None:
                line = self._proc.stdout.readline()
                if line:
                    line_str = line.strip() if isinstance(line, str) else line.decode(errors='replace').strip()
                    if line_str:
                        if any(kw in line_str for kw in ('TX ', 'ERROR', 'WARNING', 'PULL', 'PUSH', 'INFO', 'rejec', 'too late', 'collision', 'BEACON')):
                            logger.info('pkt_fwd: %s', line_str)
                        else:
                            logger.debug('pkt_fwd: %s', line_str)
        except Exception as e:
            logger.debug('WM1303Backend: stdout reader ended: %s', e)
        logger.info('WM1303Backend: stdout reader thread stopped')


    # ------------------------------------------------------------------
    # UDP listener loop (runs in thread)
    # ------------------------------------------------------------------

    def _udp_loop(self) -> None:
        logger.info('WM1303Backend: UDP listener thread started')
        while self._running:
            try:
                data, addr = self._sock.recvfrom(65536)
            except socket.timeout:
                continue
            except OSError:
                break
            try:
                self._handle_udp(data, addr)
            except Exception as e:
                logger.exception('WM1303Backend: UDP handler error: %s', e)
        logger.info('WM1303Backend: UDP listener thread exiting')

    def _handle_udp(self, data: bytes, addr: tuple) -> None:
        if len(data) < 4:
            return
        ver, tok_h, tok_l, pkt_type = data[0], data[1], data[2], data[3]
        token = (tok_h << 8) | tok_l

        if pkt_type == PKT_PUSH_DATA:    # RX packet(s)
            self._send_ack(addr, token, PKT_PUSH_ACK)
            if len(data) > 12:
                try:
                    payload_json = json.loads(data[12:].decode())
                    rxpk_list = payload_json.get('rxpk', [])
                    if rxpk_list:
                        logger.info('WM1303Backend: PUSH_DATA with %d rxpk packets', len(rxpk_list))
                    for rxpk in rxpk_list:
                        self._dispatch_rx(rxpk)

                    # Detection Method 1: Parse stat object for rxnb/txnb monitoring
                    stat_obj = payload_json.get('stat')
                    if stat_obj and isinstance(stat_obj, dict):
                        rxnb = stat_obj.get('rxnb', 0)
                        txnb = stat_obj.get('txnb', 0)
                        self._last_stat_rxnb = rxnb
                        self._last_stat_txnb = txnb
                        # Track consecutive zero-RX stat windows
                        # Only count when TX is active (txnb > 0 or recent TX activity)
                        has_tx_activity = txnb > 0 or (time.monotonic() - self._last_tx_end < 60)
                        if rxnb == 0 and has_tx_activity:
                            self._zero_rx_stat_count += 1
                            logger.info('WM1303Backend: PUSH_DATA stat rxnb=0, txnb=%d '
                                       '(zero_count=%d, tx_active=%s)',
                                       txnb, self._zero_rx_stat_count, has_tx_activity)
                        elif rxnb > 0:
                            if self._zero_rx_stat_count > 0:
                                logger.info('WM1303Backend: PUSH_DATA stat rxnb=%d - '
                                           'resetting zero_rx_stat_count from %d',
                                           rxnb, self._zero_rx_stat_count)
                            self._zero_rx_stat_count = 0
                        else:
                            logger.debug('WM1303Backend: PUSH_DATA stat rxnb=%d txnb=%d '
                                        '(no TX activity, not counting)', rxnb, txnb)
                except Exception as e:
                    logger.warning('WM1303Backend: failed to parse PUSH_DATA: %s', e)

        elif pkt_type == PKT_PULL_DATA:  # TX poll
            self._pull_addr = addr
            self._pktfwd_ready_event.set()
            logger.info('WM1303Backend: PULL_DATA received from pkt_fwd at %s', addr)
            self._send_ack(addr, token, PKT_PULL_ACK)

        elif pkt_type == PKT_TX_ACK:     # TX result
            if len(data) > 4:
                raw_payload = data[4:]
                try:
                    ack = json.loads(raw_payload.decode())
                    err = ack.get('txpk_ack', {}).get('error', 'NONE')
                    if err != 'NONE':
                        logger.warning('WM1303Backend: TX_ACK error: %s (full: %s)', err, ack)
                    else:
                        logger.info('WM1303Backend: TX_ACK OK (json)')
                except (UnicodeDecodeError, json.JSONDecodeError):
                    raw_hex = raw_payload.hex()
                    if len(raw_payload) >= 2 and raw_payload[-1] in (0x00, 0x01):
                        logger.info('WM1303Backend: TX_ACK OK (binary: %s)', raw_hex)
                    else:
                        logger.warning('WM1303Backend: TX_ACK unknown binary: %s', raw_hex)
            else:
                logger.info('WM1303Backend: TX_ACK (minimal, %d bytes)', len(data))


    def _send_ack(self, addr: tuple, token: int, ack_type: int) -> None:
        pkt = bytes([PROTOCOL_VER, (token >> 8) & 0xFF, token & 0xFF, ack_type])
        try:
            self._sock.sendto(pkt, addr)
        except Exception as e:
            logger.debug('WM1303Backend: sendto failed: %s', e)

    def _dispatch_rx(self, rxpk: dict) -> None:
        """Route a received packet to the matching VirtualLoRaRadio."""
        _rx_freq = rxpk.get('freq', 0)
        _rx_datr = rxpk.get('datr', '?')
        _rx_rssi = rxpk.get('rssi', '?')
        _rx_stat = rxpk.get('stat', -1)
        # CRC error = -1, CRC disabled = 0, CRC OK = 1
        _crc_label = {1: 'CRC_OK', 0: 'CRC_DISABLED', -1: 'CRC_ERROR'}.get(_rx_stat, f'CRC_{_rx_stat}')
        _rx_lsnr = rxpk.get('lsnr', '?')
        _raw_data = rxpk.get('data', '')
        _raw_size = len(_raw_data)
        if _rx_stat != 1:  # Not CRC OK - log as WARNING with hex for analysis
            try:
                import base64 as _b64
                _raw_bytes = _b64.b64decode(_raw_data) if _raw_data else b''
                _hex_preview = _raw_bytes[:16].hex() if _raw_bytes else ''
            except Exception:
                _hex_preview = '?'
            logger.warning('WM1303Backend: RX %s freq=%.3f datr=%s rssi=%s snr=%s size=%d hex=%s',
                          _crc_label, _rx_freq, _rx_datr, _rx_rssi, _rx_lsnr, _raw_size, _hex_preview)
        else:
            logger.info('WM1303Backend: RX %s freq=%.3f datr=%s rssi=%s snr=%s size=%d',
                        _crc_label, _rx_freq, _rx_datr, _rx_rssi, _rx_lsnr, _raw_size)
        # AGC settling noise filter: CRC_DISABLED with SNR < -5 dB = settling artifact
        if _rx_stat == 0:  # CRC_DISABLED
            try:
                _snr_check = float(_rx_lsnr) if _rx_lsnr != '?' else -99.0
            except (ValueError, TypeError):
                _snr_check = -99.0
            if _snr_check < -5.0:
                logger.debug('WM1303Backend: CRC_DISABLED noise filtered snr=%.1f (AGC settling)', _snr_check)
                return
        try:
            freq_hz = int(float(rxpk.get('freq', 0)) * 1e6)
            datr = rxpk.get('datr', 'SF7BW125')
            rx_sf, rx_bw = _parse_datr(datr)
            payload_b64 = rxpk.get('data', '')
            if not payload_b64:
                return
            payload = base64.b64decode(payload_b64)

            # Self-echo detection: check if this RX matches a recent TX
            _rx_echo_hash = hashlib.md5(payload).hexdigest()[:12]
            _now_mono = time.monotonic()
            if _rx_echo_hash in self._tx_echo_hashes:
                _tx_time = self._tx_echo_hashes[_rx_echo_hash]
                _age = _now_mono - _tx_time
                if _age < self._tx_echo_ttl:
                    self._tx_echo_detected += 1
                    logger.warning('WM1303Backend: Self-echo detected! hash=%s age=%.1fs rssi=%s '
                                   'freq=%.3f (total_echoes=%d) - DISCARDING',
                                   _rx_echo_hash, _age, _rx_rssi, _rx_freq, self._tx_echo_detected)
                    return
                else:
                    del self._tx_echo_hashes[_rx_echo_hash]
            # Multi-demod dedup: prevent 8x TX for same packet
            _dd_hash = hashlib.md5(payload).hexdigest()[:12]
            _dd_now = time.monotonic()
            if _dd_hash in self._rx_dedup_cache:
                if _dd_now - self._rx_dedup_cache[_dd_hash] < 2.0:
                    logger.debug('WM1303Backend: multi-demod dup %s', _dd_hash)
                    return
            self._rx_dedup_cache[_dd_hash] = _dd_now
            if len(self._rx_dedup_cache) > 100:
                self._rx_dedup_cache = {k:v for k,v in self._rx_dedup_cache.items() if _dd_now-v < 5.0}
            rssi = float(rxpk.get('rssi', -99))
            snr = float(rxpk.get('lsnr', 0))
        except Exception as e:
            logger.warning('WM1303Backend: rxpk parse error: %s', e)
            return

        # --- Dispatcher RX callback (dev-branch interface) ---
        self._last_pkt_rssi = int(rssi)
        self._last_pkt_snr = float(snr)
        if self.rx_callback is not None and self._loop is not None:
            try:
                self._loop.call_soon_threadsafe(self.rx_callback, payload, int(rssi), float(snr))
            except Exception as _cb_err:
                logger.warning("WM1303Backend: rx_callback error: %s", _cb_err)

        matched = False
        freq_only_match = None
        for cid, radio in self.virtual_radios.items():
            ccfg = radio.channel_config
            ch_freq = int(ccfg.get('frequency', 0))
            ch_sf   = int(ccfg.get('spreading_factor', 0))
            freq_delta = abs(freq_hz - ch_freq)
            if freq_delta <= 50000 and (ch_sf == 0 or rx_sf == ch_sf):
                logger.info('WM1303Backend: RX->%s freq=%d SF%d %d bytes rssi=%.1f snr=%.1f',
                             cid, freq_hz, rx_sf, len(payload), rssi, snr)
                radio.enqueue_rx(payload, rssi=int(rssi), snr=snr)
                self._last_rx_timestamp = time.monotonic()  # watchdog: RX activity
                self._zero_rx_stat_count = 0  # reset stat monitor on valid RX
                self._rssi_spike_count = 0    # reset RSSI spike monitor on valid RX
                self._update_rx_stats(cid, freq_hz, rssi, snr)
                # TX batch hold: delay TX to collect more floods in this window
                _hold_until = time.monotonic() + self._tx_hold_seconds
                if _hold_until > self._tx_hold_until:
                    self._tx_hold_until = _hold_until
                    logger.info('WM1303Backend: TX hold set for %.1fs (batch window)',
                               self._tx_hold_seconds)
                matched = True
                break
            elif freq_delta <= 50000 and freq_only_match is None:
                freq_only_match = (cid, radio, ch_sf)
            else:
                logger.debug('WM1303Backend: no match %s: ch_freq=%d(delta=%d) ch_sf=%d rx_sf=%s',
                            cid, ch_freq, freq_delta, ch_sf, rx_sf)

        if not matched and freq_only_match:
            cid, radio, ch_sf = freq_only_match
            logger.warning('WM1303Backend: RX->%s FREQ-ONLY match (ch_sf=%d != rx_sf=%s) '
                          'freq=%d %d bytes rssi=%.1f - routing anyway',
                          cid, ch_sf, rx_sf, freq_hz, len(payload), rssi)
            radio.enqueue_rx(payload, rssi=int(rssi), snr=snr)
            self._last_rx_timestamp = time.monotonic()  # watchdog: RX activity
            self._zero_rx_stat_count = 0  # reset stat monitor on valid RX
            self._rssi_spike_count = 0    # reset RSSI spike monitor on valid RX
            matched = True

        if not matched:
            logger.info('WM1303Backend: no channel match for freq=%d SF%s '
                        '(channels: %s)',
                        freq_hz, rx_sf,
                        {c: (int(r.channel_config.get('frequency',0)),
                             int(r.channel_config.get('spreading_factor',0)))
                         for c, r in self.virtual_radios.items()})

    # ------------------------------------------------------------------
    # TX via direct PULL_RESP (through RF0)
    # ------------------------------------------------------------------

    async def send(self, channel_id_or_data, data: bytes = None, tx_power: int = None) -> dict:
        """Send a packet on the specified channel via GlobalTXScheduler.

        Supports two calling conventions:
        - send(channel_id: str, data: bytes, tx_power=None)  # channel-based (bridge engine)
        - send(data: bytes)  # base LoRaRadio interface (Dispatcher)

        Enqueues to the per-channel TXQueue. The GlobalTXScheduler
        handles actual transmission in round-robin order.
        """
        # Detect calling convention: if first arg is bytes, it's the Dispatcher interface
        if isinstance(channel_id_or_data, (bytes, bytearray)):
            data = channel_id_or_data
            # Pick the first active channel as default
            channel_id = None
            for cid, cfg in self.channels.items():
                if cfg.get('active', True):
                    channel_id = cid
                    break
            if channel_id is None:
                # No active channels - use first channel
                channel_id = next(iter(self.channels), None)
            if channel_id is None:
                logger.warning('WM1303Backend: send() called but no channels configured')
                return {'ok': False, 'error': 'No channels configured'}
            logger.debug('WM1303Backend: Dispatcher send() -> channel %s', channel_id)
        else:
            channel_id = channel_id_or_data

        if channel_id not in self.channels:
            raise ValueError(f'Unknown channel: {channel_id}')
        cfg = self.channels[channel_id]
        if tx_power is None:
            tx_power = int(cfg.get('tx_power', 14))

        # Track hash for self-echo detection at enqueue time
        _tx_hash = hashlib.md5(data).hexdigest()[:12]
        self._tx_echo_hashes[_tx_hash] = time.monotonic()
        logger.info('WM1303Backend: TX echo hash pre-stored: %s (ch=%s)', _tx_hash, channel_id)

        # Enqueue to the per-channel TXQueue (GlobalTXScheduler handles sending)
        if self._tx_queue_manager:
            result = await self._tx_queue_manager.enqueue(channel_id, data, tx_power)
        else:
            logger.warning('WM1303Backend: No TX queue manager, sending directly')
            txpk = self._build_txpk(cfg, data, tx_power)
            result = await asyncio.get_event_loop().run_in_executor(
                None, self._send_pull_resp_sync, txpk)

        if result.get('ok'):
            self._tx_packets_sent_total += 1
            logger.info('WM1303Backend: TX on %s (%d bytes) via GlobalTXScheduler',
                       channel_id, len(data))
        else:
            logger.warning('WM1303Backend: TX failed on %s: %s', channel_id, result)

        return result

    async def _send_for_scheduler(self, txpk: dict, channel_id: str) -> dict:
        """Send a single txpk via PULL_RESP. Called by GlobalTXScheduler."""
        result = await asyncio.get_event_loop().run_in_executor(
            None, self._send_pull_resp_sync, txpk)
        return result

    def _update_tx_stats(self, channel_id: str, send_ms: float,
                         airtime_ms: float, wait_ms: float,
                         payload_len: int) -> None:
        """Update per-channel TX timing statistics."""
        if channel_id not in self._channel_tx_stats:
            self._channel_tx_stats[channel_id] = {
                "tx_packets": 0,
                "tx_bytes": 0,
                "total_airtime_ms": 0.0,
                "total_send_ms": 0.0,
                "total_wait_ms": 0.0,
                "last_airtime_ms": 0.0,
                "last_send_ms": 0.0,
                "last_wait_ms": 0.0,
                "avg_airtime_ms": 0.0,
                "avg_send_ms": 0.0,
                "avg_wait_ms": 0.0,
                "last_tx": None,
                "_airtime_history": [],
                "_send_history": [],
                "_wait_history": [],
            }
        s = self._channel_tx_stats[channel_id]
        s["tx_packets"] += 1
        s["tx_bytes"] += payload_len
        s["total_airtime_ms"] = round(s["total_airtime_ms"] + airtime_ms, 1)
        s["total_send_ms"] = round(s["total_send_ms"] + send_ms, 1)
        s["total_wait_ms"] = round(s["total_wait_ms"] + wait_ms, 1)
        s["last_airtime_ms"] = round(airtime_ms, 1)
        s["last_send_ms"] = round(send_ms, 1)
        s["last_wait_ms"] = round(wait_ms, 1)
        s["last_tx"] = time.time()
        # Rolling averages (last 50)
        for key, val, hist_key in [
            ("avg_airtime_ms", airtime_ms, "_airtime_history"),
            ("avg_send_ms", send_ms, "_send_history"),
            ("avg_wait_ms", wait_ms, "_wait_history"),
        ]:
            s[hist_key].append(val)
            if len(s[hist_key]) > 50:
                s[hist_key] = s[hist_key][-50:]
            s[key] = round(sum(s[hist_key]) / len(s[hist_key]), 1)

    def _build_txpk(self, cfg: dict, data: bytes,
                    tx_power: int = 14) -> dict:
        """Build a txpk JSON object for PULL_RESP."""
        freq_mhz  = int(cfg.get('frequency', 869462500)) / 1e6
        sf        = int(cfg.get('spreading_factor', 8))
        bw_hz     = int(cfg.get('bandwidth', 125000))
        cr_raw    = cfg.get('coding_rate', '4/5')
        cr_str    = cr_raw if isinstance(cr_raw, str) else f'4/{cr_raw}'
        preamble  = int(cfg.get('preamble_length', 17))
        datr      = _datr_str(sf, bw_hz)
        payload_b64 = base64.b64encode(data).decode()

        return {
            'imme': True,
            'freq': round(freq_mhz, 6),
            'rfch': 0,  # RF0 = TX chain (SKY66420 FEM with PA)
            'powe': tx_power,
            'modu': 'LORA',
            'datr': datr,
            'codr': cr_str,
            'ipol': False,
            'size': len(data),
            'data': payload_b64,
            'prea': preamble,
            'ncrc': False,
        }

    @staticmethod
    def _lora_airtime_s(sf: int, bw_hz: int, payload_len: int,
                        preamble: int = 17, cr: int = 5,
                        explicit_header: bool = True, crc: bool = True) -> float:
        """Calculate LoRa time-on-air in seconds."""
        bw = bw_hz
        n_preamble = preamble + 4.25
        t_sym = (2 ** sf) / bw
        t_preamble = n_preamble * t_sym
        de = 1 if (sf >= 11 and bw <= 125000) else 0
        ih = 0 if explicit_header else 1
        crc_bits = 16 if crc else 0
        numerator = 8 * payload_len - 4 * sf + 28 + crc_bits - 20 * ih
        denominator = 4 * (sf - 2 * de)
        n_payload = 8 + max(0, math.ceil(numerator / denominator)) * (cr)
        t_payload = n_payload * t_sym
        return t_preamble + t_payload


    def _send_pull_resp_sync(self, txpk: dict) -> dict:
        """Send a PULL_RESP packet to lora_pkt_fwd (blocking).

        Returns dict with:
          ok: bool
          send_ms: wall-clock time of UDP sendto only (NOT including airtime wait)
          airtime_ms: calculated LoRa time-on-air
        """
        if self._pull_addr is None:
            logger.warning('WM1303Backend: PULL_RESP BLOCKED: _pull_addr is None — '
                          'lora_pkt_fwd has not sent PULL_DATA yet!')
            return {'error': 'no_pull_addr', 'ok': False, 'send_ms': 0}
        with self._tx_lock:
            # Wait for previous TX to finish (airtime-based delay)
            now = time.monotonic()
            _airtime_wait_ms = 0.0
            if self._last_tx_end > now:
                wait_s = self._last_tx_end - now + 0.05
                _airtime_wait_ms = round(wait_s * 1000, 1)
                logger.info('WM1303Backend: waiting %.1fms for previous TX airtime to clear',
                           _airtime_wait_ms)
                time.sleep(wait_s)

            token = random.randint(0, 0xFFFF)
            body  = json.dumps({'txpk': txpk}).encode()
            pkt   = bytes([PROTOCOL_VER, (token >> 8) & 0xFF,
                          token & 0xFF, PKT_PULL_RESP]) + body
            try:
                # FIX Bug1: Measure ONLY the UDP sendto time, AFTER airtime wait
                _send_start = time.monotonic()
                self._sock.sendto(pkt, self._pull_addr)
                _send_ms = round((time.monotonic() - _send_start) * 1000, 2)

                logger.info('WM1303Backend: PULL_RESP sent to %s '
                            '(freq=%.6f, datr=%s, rfch=0, udp_send=%.1fms, airwait=%.1fms)',
                            self._pull_addr, txpk['freq'], txpk['datr'],
                            _send_ms, _airtime_wait_ms)

                # Calculate LoRa airtime and track TX completion time
                _airtime_ms_val = 0.0
                _datr = txpk.get("datr", "SF8BW125")
                _sf_m = re.match(r"SF(\d+)BW(\d+)", _datr)
                if _sf_m:
                    _sf = int(_sf_m.group(1))
                    _bw = int(_sf_m.group(2)) * 1000
                    _airtime = self._lora_airtime_s(_sf, _bw, txpk.get("size", 0), txpk.get("prea", 17))
                    _airtime_ms_val = round(_airtime * 1000, 1)
                    self._last_tx_end = time.monotonic() + _airtime
                    logger.info('WM1303Backend: TX airtime %.1fms (SF%d BW%d %d bytes)',
                               _airtime_ms_val, _sf, _bw, txpk.get('size', 0))

                # Store TX payload hash for self-echo detection
                try:
                    _tx_data_b64 = txpk.get('data', '')
                    if _tx_data_b64:
                        _tx_payload = base64.b64decode(_tx_data_b64)
                        _tx_hash = hashlib.md5(_tx_payload).hexdigest()[:12]
                        self._tx_echo_hashes[_tx_hash] = time.monotonic()
                        logger.info('WM1303Backend: TX echo hash stored: %s (total=%d)',
                                   _tx_hash, len(self._tx_echo_hashes))
                        # Cleanup expired entries
                        _now_m = time.monotonic()
                        self._tx_echo_hashes = {
                            k: v for k, v in self._tx_echo_hashes.items()
                            if _now_m - v < self._tx_echo_ttl
                        }
                except Exception as _e:
                    logger.debug('WM1303Backend: TX hash storage error: %s', _e)


                # Schedule AGC recovery reset after TX burst
                self._current_burst_tx_time += _airtime_ms_val / 1000.0  # track burst TX time (seconds)
                # AGC recovery DISABLED: causes ~96s deaf (6s restart + 90s self-recovery)
                # SX1302 self-recovers in ~90s without restart - no benefit to restarting
                # self._schedule_agc_reset()

                return {'ok': True, 'freq': txpk['freq'], 'datr': txpk['datr'],
                        'airtime_ms': _airtime_ms_val, 'send_ms': _send_ms}
            except Exception as e:
                logger.error('WM1303Backend: PULL_RESP send failed: %s', e)
                return {'error': str(e), 'ok': False, 'send_ms': 0}

    # ------------------------------------------------------------------
    # Status & Stats
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # AGC Recovery (post-TX concentrator reset)
    # ------------------------------------------------------------------

    def _schedule_agc_reset(self) -> None:
        """Schedule an AGC recovery reset after TX.

        Batches multiple TX events: cancels any pending timer and sets
        a new one. The reset fires `_agc_reset_delay` seconds after the
        LAST TX in a burst.
        """
        with self._agc_reset_lock:
            if self._agc_reset_timer:
                self._agc_reset_timer.cancel()
            self._agc_reset_timer = threading.Timer(
                self._agc_reset_delay, self._do_agc_reset)
            self._agc_reset_timer.daemon = True
            self._agc_reset_timer.start()
            logger.info("WM1303Backend: AGC reset scheduled in %.1fs",
                       self._agc_reset_delay)

    def _do_agc_reset(self) -> None:
        """Lightweight AGC recovery - restarts pkt_fwd without GPIO reset.

        After TX the SX1302 AGC firmware takes ~70s to self-recover.
        Restarting pkt_fwd restores RX in ~2.5s.
        """
        if not self._running:
            return
        if getattr(self, "_agc_resetting", False):
            logger.warning("WM1303Backend: [AGC RECOVERY] already in progress, skipping")
            return
        self._agc_resetting = True
        self._current_burst_tx_time = 0.0
        _start = time.monotonic()
        logger.info("WM1303Backend: [AGC RECOVERY] starting lightweight pkt_fwd restart")
        try:
            self._stop_pktfwd_process()
            time.sleep(0.3)
            self._start_pktfwd()  # includes PULL_DATA wait
            _elapsed = time.monotonic() - _start
            self._agc_resets += 1
            logger.info("WM1303Backend: [AGC RECOVERY] complete in %.1fs (total=%d)",
                        _elapsed, self._agc_resets)
        except Exception as _e:
            logger.error("WM1303Backend: [AGC RECOVERY] failed: %s", _e)
        finally:
            self._agc_resetting = False
            with self._agc_reset_lock:
                self._agc_reset_timer = None


    # === ORIGINAL FULL AGC RESET CODE (commented out for lightweight test) ===
    # AGC_FULL_RESET:     def _do_agc_reset(self) -> None:
    # AGC_FULL_RESET:         """Perform AGC recovery by restarting lora_pkt_fwd.
    # AGC_FULL_RESET:
    # AGC_FULL_RESET:         After TX on RF0 (same chain as RX), the SX1302 internal AGC
    # AGC_FULL_RESET:         firmware can get stuck, making the concentrator deaf. This
    # AGC_FULL_RESET:         method kills and restarts lora_pkt_fwd with a GPIO reset to
    # AGC_FULL_RESET:         restore RX sensitivity.
    # AGC_FULL_RESET:         """
    # AGC_FULL_RESET:         if not self._running:
    # AGC_FULL_RESET:             return
    # AGC_FULL_RESET:         if getattr(self, "_agc_resetting", False):
    # AGC_FULL_RESET:             logger.warning("WM1303Backend: [AGC RECOVERY] already in progress, skipping")
    # AGC_FULL_RESET:             return
    # AGC_FULL_RESET:
    # AGC_FULL_RESET:         self._agc_resetting = True
    # AGC_FULL_RESET:         _start = time.monotonic()
    # AGC_FULL_RESET:         logger.info("WM1303Backend: [AGC RECOVERY] starting concentrator reset")
    # AGC_FULL_RESET:         try:
    # AGC_FULL_RESET:             # Step 1: Stop current pkt_fwd
    # AGC_FULL_RESET:             self._stop_pktfwd_process()
    # AGC_FULL_RESET:             time.sleep(0.5)
    # AGC_FULL_RESET:
    # AGC_FULL_RESET:             # Step 2: GPIO reset to restore AGC
    # AGC_FULL_RESET:             try:
    # AGC_FULL_RESET:                 logger.info("WM1303Backend: [AGC RECOVERY] running GPIO reset")
    # AGC_FULL_RESET:                 r = subprocess.run(
    # AGC_FULL_RESET:                     ["sudo", str(PKTFWD_RESET), "start"],
    # AGC_FULL_RESET:                     cwd=str(PKTFWD_DIR), capture_output=True, timeout=15
    # AGC_FULL_RESET:                 )
    # AGC_FULL_RESET:                 if r.returncode != 0:
    # AGC_FULL_RESET:                     logger.warning("WM1303Backend: [AGC RECOVERY] GPIO reset rc=%d",
    # AGC_FULL_RESET:                                   r.returncode)
    # AGC_FULL_RESET:                 time.sleep(2.0)  # TCXO warmup
    # AGC_FULL_RESET:             except Exception as e:
    # AGC_FULL_RESET:                 logger.error("WM1303Backend: [AGC RECOVERY] GPIO reset failed: %s", e)
    # AGC_FULL_RESET:
    # AGC_FULL_RESET:             # Step 3: Restart pkt_fwd
    # AGC_FULL_RESET:             try:
    # AGC_FULL_RESET:                 cmd = ["sudo", str(PKTFWD_BIN), "-c", str(BRIDGE_CONF)]
    # AGC_FULL_RESET:                 logger.info("WM1303Backend: [AGC RECOVERY] restarting pkt_fwd: %s",
    # AGC_FULL_RESET:                            " ".join(cmd))
    # AGC_FULL_RESET:                 self._proc = subprocess.Popen(
    # AGC_FULL_RESET:                     cmd,
    # AGC_FULL_RESET:                     cwd=str(PKTFWD_DIR),
    # AGC_FULL_RESET:                     stdout=subprocess.PIPE,
    # AGC_FULL_RESET:                     stderr=subprocess.STDOUT,
    # AGC_FULL_RESET:                     text=True,
    # AGC_FULL_RESET:                 )
    # AGC_FULL_RESET:                 time.sleep(2.0)  # Wait for concentrator init (optimized)
    # AGC_FULL_RESET:                 if self._proc.poll() is not None:
    # AGC_FULL_RESET:                     out = self._proc.stdout.read()
    # AGC_FULL_RESET:                     logger.error("WM1303Backend: [AGC RECOVERY] pkt_fwd exited: %s",
    # AGC_FULL_RESET:                                 out[:500])
    # AGC_FULL_RESET:                 else:
    # AGC_FULL_RESET:                     logger.info("WM1303Backend: [AGC RECOVERY] pkt_fwd restarted (pid %d)",
    # AGC_FULL_RESET:                                self._proc.pid)
    # AGC_FULL_RESET:                     # Restart stdout reader thread
    # AGC_FULL_RESET:                     self._stdout_thread = threading.Thread(
    # AGC_FULL_RESET:                         target=self._pktfwd_stdout_reader, daemon=True,
    # AGC_FULL_RESET:                         name="pktfwd-stdout")
    # AGC_FULL_RESET:                     self._stdout_thread.start()
    # AGC_FULL_RESET:             except Exception as e:
    # AGC_FULL_RESET:                 logger.error("WM1303Backend: [AGC RECOVERY] restart failed: %s", e)
    # AGC_FULL_RESET:
    # AGC_FULL_RESET:             self._agc_resets += 1
    # AGC_FULL_RESET:             _elapsed_ms = round((time.monotonic() - _start) * 1000)
    # AGC_FULL_RESET:             logger.info("WM1303Backend: [AGC RECOVERY] complete (%dms, total=%d)",
    # AGC_FULL_RESET:                        _elapsed_ms, self._agc_resets)
    # AGC_FULL_RESET:         except Exception as e:
    # AGC_FULL_RESET:             logger.error("WM1303Backend: [AGC RECOVERY] failed: %s", e)
    # AGC_FULL_RESET:         finally:
    # AGC_FULL_RESET:             self._agc_resetting = False
    # AGC_FULL_RESET:             with self._agc_reset_lock:
    # AGC_FULL_RESET:                 self._agc_reset_timer = None
    # AGC_FULL_RESET:
    # === END ORIGINAL FULL AGC RESET CODE ===

    # ------------------------------------------------------------------
    # Full Concentrator Reset (used by heartbeat)
    # ------------------------------------------------------------------
    def _do_full_reset(self) -> None:
        """Perform full concentrator reset - kill pkt_fwd, GPIO reset, restart.

        Used by the periodic heartbeat to prevent IF chain drift.
        Based on the original AGC_FULL_RESET code.
        """
        if not self._running:
            return
        if getattr(self, "_agc_resetting", False):
            logger.warning("WM1303Backend: [FULL RESET] already in progress, skipping")
            return

        self._agc_resetting = True
        _start = time.monotonic()
        logger.info("WM1303Backend: [FULL RESET] starting concentrator reset")
        try:
            # Step 1: Stop current pkt_fwd
            self._stop_pktfwd_process()
            time.sleep(0.5)

            # Step 2: GPIO reset to restore AGC
            try:
                logger.info("WM1303Backend: [FULL RESET] running GPIO reset")
                r = subprocess.run(
                    ["sudo", str(PKTFWD_RESET), "start"],
                    cwd=str(PKTFWD_DIR), capture_output=True, timeout=15
                )
                if r.returncode != 0:
                    logger.warning("WM1303Backend: [FULL RESET] GPIO reset rc=%d",
                                  r.returncode)
                time.sleep(2.0)  # TCXO warmup
            except Exception as e:
                logger.error("WM1303Backend: [FULL RESET] GPIO reset failed: %s", e)

            # Step 3: Restart pkt_fwd
            try:
                cmd = ["sudo", str(PKTFWD_BIN), "-c", str(BRIDGE_CONF)]
                logger.info("WM1303Backend: [FULL RESET] restarting pkt_fwd: %s",
                           " ".join(cmd))
                self._proc = subprocess.Popen(
                    cmd,
                    cwd=str(PKTFWD_DIR),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                )
                time.sleep(2.0)  # Wait for concentrator init
                if self._proc.poll() is not None:
                    out = self._proc.stdout.read()
                    logger.error("WM1303Backend: [FULL RESET] pkt_fwd exited: %s",
                                out[:500])
                else:
                    logger.info("WM1303Backend: [FULL RESET] pkt_fwd restarted (pid %d)",
                               self._proc.pid)
                    # Restart stdout reader thread
                    self._stdout_thread = threading.Thread(
                        target=self._pktfwd_stdout_reader, daemon=True,
                        name="pktfwd-stdout")
                    self._stdout_thread.start()
            except Exception as e:
                logger.error("WM1303Backend: [FULL RESET] restart failed: %s", e)

            self._agc_resets += 1
            _elapsed_ms = round((time.monotonic() - _start) * 1000)
            logger.info("WM1303Backend: [FULL RESET] complete (%dms, total=%d)",
                       _elapsed_ms, self._agc_resets)
        except Exception as e:
            logger.error("WM1303Backend: [FULL RESET] failed: %s", e)
        finally:
            self._agc_resetting = False
            # Reset burst tracking
            self._current_burst_tx_time = 0.0

    # ------------------------------------------------------------------
    # Periodic Heartbeat Reset
    # ------------------------------------------------------------------
    def _start_heartbeat_reset(self) -> None:
        """Start periodic full concentrator reset to prevent IF chain drift."""
        self._heartbeat_interval = 180  # 3 minutes
        self._heartbeat_timer = None
        self._schedule_heartbeat()
        logger.info("WM1303Backend: heartbeat reset scheduled every %ds", self._heartbeat_interval)

    def _schedule_heartbeat(self) -> None:
        """Schedule the next heartbeat full reset."""
        if not self._running:
            return
        self._heartbeat_timer = threading.Timer(
            self._heartbeat_interval, self._heartbeat_reset)
        self._heartbeat_timer.daemon = True
        self._heartbeat_timer.start()

    def _heartbeat_reset(self) -> None:
        """Execute periodic full concentrator reset."""
        if not self._running:
            return
        logger.info("WM1303Backend: HEARTBEAT - periodic full concentrator reset")
        self._do_full_reset()
        logger.info("WM1303Backend: HEARTBEAT complete")
        self._schedule_heartbeat()  # schedule next heartbeat


    def _update_rx_stats(self, channel_id: str, freq_hz: int,
                         rssi: float, snr: float) -> None:
        """Update per-channel RX statistics."""
        if channel_id not in self._channel_rx_stats:
            self._channel_rx_stats[channel_id] = {
                "rx_count": 0, "last_rssi": -120.0, "last_snr": 0.0,
                "rssi_sum": 0.0, "snr_sum": 0.0,
                "last_rx_time": None, "freq_hz": freq_hz,
            }
        s = self._channel_rx_stats[channel_id]
        s["rx_count"] += 1
        s["last_rssi"] = round(rssi, 1)
        s["last_snr"] = round(snr, 1)
        s["rssi_sum"] += rssi
        s["snr_sum"] += snr
        s["last_rx_time"] = time.time()
        s["freq_hz"] = freq_hz

    def _init_channel_stats_db(self) -> None:
        """Create channel_stats_history table if it doesn't exist."""
        try:
            with sqlite3.connect(_DB_PATH) as conn:
                conn.execute("""CREATE TABLE IF NOT EXISTS channel_stats_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp REAL NOT NULL,
                    channel_id TEXT NOT NULL,
                    rx_count INTEGER DEFAULT 0,
                    avg_rssi REAL,
                    avg_snr REAL,
                    tx_count INTEGER DEFAULT 0,
                    tx_failed INTEGER DEFAULT 0,
                    tx_airtime_ms REAL DEFAULT 0,
                    tx_bytes INTEGER DEFAULT 0,
                    lbt_blocked INTEGER DEFAULT 0,
                    lbt_passed INTEGER DEFAULT 0,
                    lbt_last_rssi REAL,
                    lbt_threshold REAL,
                    noise_floor_dbm REAL
                )""")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_csh_ts ON channel_stats_history(timestamp)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_csh_ch ON channel_stats_history(channel_id)")
                conn.commit()
            logger.info("channel_stats_history table ready")
        except Exception as e:
            logger.error("Failed to init channel_stats_history table: %s", e)

        # Create noise_floor_history table for per-channel noise floor tracking
        try:
            with sqlite3.connect(_DB_PATH) as conn:
                conn.execute("""CREATE TABLE IF NOT EXISTS noise_floor_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp REAL NOT NULL,
                    channel_id TEXT NOT NULL,
                    noise_floor_dbm REAL NOT NULL,
                    samples_collected INTEGER DEFAULT 0,
                    samples_accepted INTEGER DEFAULT 0,
                    min_rssi REAL,
                    max_rssi REAL
                )""")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_nfh_ts ON noise_floor_history(timestamp)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_nfh_ch ON noise_floor_history(channel_id)")
                conn.commit()
            logger.info("noise_floor_history table ready")
        except Exception as e:
            logger.error("Failed to init noise_floor_history table: %s", e)

    def _snapshot_channel_stats(self) -> None:
        """Take a snapshot of current per-channel stats and write to DB."""
        try:
            now = time.time()
            ch_stats = self.get_channel_stats()
            if not ch_stats:
                return

            # Get latest noise floor
            nf_dbm = None
            try:
                with sqlite3.connect(_DB_PATH) as conn:
                    row = conn.execute(
                        "SELECT noise_floor_dbm FROM noise_floor ORDER BY timestamp DESC LIMIT 1"
                    ).fetchone()
                    if row:
                        nf_dbm = row[0]
            except Exception:
                pass

            with sqlite3.connect(_DB_PATH) as conn:
                # Load LBT thresholds from UI config as fallback
                _lbt_thresholds = {}
                try:
                    import json as _json, yaml as _yaml
                    with open("/etc/pymc_repeater/wm1303_ui.json") as _uf:
                        _ui = _json.load(_uf)
                    with open("/etc/pymc_repeater/config.yaml") as _cf:
                        _cfg = _yaml.safe_load(_cf)
                    _config_keys = list(_cfg.get("wm1303", {}).get("channels", {}).keys())
                    _active_pos = 0
                    for _ui_ch in _ui.get("channels", []):
                        if _ui_ch.get("active", False) and _active_pos < len(_config_keys):
                            if _ui_ch.get("lbt_enabled", False):
                                _lbt_thresholds[_config_keys[_active_pos]] = _ui_ch.get("lbt_rssi_target", -80)
                            _active_pos += 1
                except Exception:
                    pass

                for ch_id, data in ch_stats.items():
                    rx_count = data.get("rx_count", 0)
                    # Compute avg RSSI/SNR from raw sums
                    rx_raw = self._channel_rx_stats.get(ch_id, {})
                    rc = rx_raw.get("rx_count", 0)
                    avg_rssi = round(rx_raw["rssi_sum"] / rc, 1) if rc > 0 else None
                    avg_snr = round(rx_raw["snr_sum"] / rc, 1) if rc > 0 else None

                    tx_count = data.get("tx_count", 0)
                    tx_failed = data.get("tx_failed", 0)
                    tx_airtime = data.get("total_tx_airtime_ms", 0)
                    tx_bytes = data.get("tx_bytes", 0)
                    lbt_blocked = data.get("lbt_blocked", 0)
                    lbt_passed = data.get("lbt_passed", 0)
                    lbt_last_rssi = data.get("lbt_last_rssi")
                    lbt_threshold = data.get("lbt_last_threshold")
                    if lbt_threshold is None:
                        lbt_threshold = _lbt_thresholds.get(ch_id)

                    conn.execute(
                        """INSERT INTO channel_stats_history
                        (timestamp, channel_id, rx_count, avg_rssi, avg_snr,
                         tx_count, tx_failed, tx_airtime_ms, tx_bytes,
                         lbt_blocked, lbt_passed, lbt_last_rssi, lbt_threshold,
                         noise_floor_dbm)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (now, ch_id, rx_count, avg_rssi, avg_snr,
                         tx_count, tx_failed, tx_airtime, tx_bytes,
                         lbt_blocked, lbt_passed, lbt_last_rssi, lbt_threshold,
                         nf_dbm))
                conn.commit()
            logger.debug("Channel stats snapshot: %d channels recorded", len(ch_stats))
        except Exception as e:
            logger.error("Error in _snapshot_channel_stats: %s", e)

    def _channel_stats_snapshot_loop(self) -> None:
        """Background thread: periodically snapshot channel stats to DB."""
        logger.info("Starting channel stats snapshot loop (interval=300s)")
        self._init_channel_stats_db()
        # Wait 60s before first snapshot to let the system stabilize
        for _ in range(12):
            if not self._snapshot_running:
                return
            time.sleep(5)
        while self._snapshot_running:
            try:
                self._snapshot_channel_stats()
                # Cleanup old data (older than 7 days)
                cutoff = time.time() - (7 * 86400)
                try:
                    with sqlite3.connect(_DB_PATH) as conn:
                        conn.execute(
                            "DELETE FROM channel_stats_history WHERE timestamp < ?",
                            (cutoff,))
                        conn.commit()
                except Exception as ce:
                    logger.error("Error cleaning old channel stats: %s", ce)
            except Exception as e:
                logger.error("Error in snapshot loop: %s", e)
            # Sleep 300 seconds (5 minutes) in small intervals
            for _ in range(60):
                if not self._snapshot_running:
                    return
                time.sleep(5)
        logger.info("Channel stats snapshot loop stopped")

    def get_channel_stats(self) -> dict:
        """Return combined per-channel RX and TX statistics."""
        result = {}
        tx_stats = {}
        if self._tx_queue_manager:
            tx_stats = self._tx_queue_manager.get_status()
        all_chs = set(list(self._channel_rx_stats.keys()) + list(tx_stats.keys()))
        for ch_id in all_chs:
            rx = self._channel_rx_stats.get(ch_id, {})
            tx = tx_stats.get(ch_id, {})
            tx_timing = self._channel_tx_stats.get(ch_id, {})
            rc = rx.get("rx_count", 0)
            # Calculate uptime-based duty cycle
            uptime_s = time.time() - self._start_time if self._start_time else 1
            total_airtime_s = tx_timing.get("total_airtime_ms", 0) / 1000.0
            duty_pct = round((total_airtime_s / uptime_s) * 100, 3) if uptime_s > 0 else 0
            result[ch_id] = {
                "rx_count": rc,
                "last_rssi": rx.get("last_rssi", -120.0),
                "rssi_avg": round(rx["rssi_sum"] / rc, 1) if rc > 0 else -120.0,
                "last_snr": rx.get("last_snr", 0.0),
                "snr_avg": round(rx["snr_sum"] / rc, 1) if rc > 0 else 0.0,
                "last_rx_time": rx.get("last_rx_time"),
                "tx_count": tx.get("total_sent", 0),
                "tx_failed": tx.get("total_failed", 0),
                "tx_pending": tx.get("pending", 0),
                "last_tx_time": tx.get("last_tx_time"),
                "avg_tx_time_ms": tx.get("avg_tx_time_ms", 0),
                "freq_hz": rx.get("freq_hz", tx.get("freq_hz", 0)),
                # New TX timing fields
                "avg_tx_airtime_ms": tx_timing.get("avg_airtime_ms", 0),
                "avg_tx_send_ms": tx_timing.get("avg_send_ms", 0),
                "avg_tx_wait_ms": tx_timing.get("avg_wait_ms", 0),
                "last_tx_airtime_ms": tx_timing.get("last_airtime_ms", 0),
                "last_tx_send_ms": tx_timing.get("last_send_ms", 0),
                "last_tx_wait_ms": tx_timing.get("last_wait_ms", 0),
                "total_tx_airtime_ms": tx_timing.get("total_airtime_ms", 0),
                "total_tx_send_ms": tx_timing.get("total_send_ms", 0),
                "total_tx_wait_ms": tx_timing.get("total_wait_ms", 0),
                "tx_bytes": tx_timing.get("tx_bytes", 0),
                "tx_duty_pct": duty_pct,
                # Software LBT stats
                "lbt_blocked": tx.get("lbt_blocked", 0),
                "lbt_passed": tx.get("lbt_passed", 0),
                "lbt_skipped": tx.get("lbt_skipped", 0),
                "lbt_last_blocked_at": tx.get("lbt_last_blocked_at"),
                "lbt_last_rssi": tx.get("lbt_last_rssi"),
                "lbt_last_threshold": tx.get("lbt_last_threshold"),
            }
        return result

    def get_tx_stats(self) -> dict:
        """Return TX subsystem stats."""
        stats = {
            'architecture': 'RF0_TX_DIRECT',
            'tx_packets_sent_total': self._tx_packets_sent_total,
            'tx_echo_detected': self._tx_echo_detected,
            'tx_echo_hashes_active': len(self._tx_echo_hashes),
            'watchdog_last_rx_ago': round(time.monotonic() - self._last_rx_timestamp, 1),
            'watchdog_zero_rx_stat_count': self._zero_rx_stat_count,
            'watchdog_rssi_spike_count': self._rssi_spike_count,
            'watchdog_last_stat_rxnb': self._last_stat_rxnb,
            'watchdog_last_stat_txnb': self._last_stat_txnb,
            'sx1261_available': self._sx1261_available,
            'sx1261_managed_by_hal': getattr(self, '_sx1261_managed_by_hal', False),
            'sx1261_role': 'managed_by_hal' if getattr(self, '_sx1261_managed_by_hal', False) else ('LBT/CAD only' if self._sx1261_available else 'not_available'),
        }
        if self._tx_queue_manager:
            stats['tx_queues'] = self._tx_queue_manager.get_status()
        if self._sx1261:
            stats['sx1261_status'] = self._sx1261.get_status()
        elif getattr(self, '_sx1261_managed_by_hal', False):
            stats['sx1261_status'] = {
                'managed_by_hal': True,
                'reason': 'SPI bus shared with lora_pkt_fwd',
                'config': getattr(self, '_sx1261_hal_config', {}),
            }
        return stats