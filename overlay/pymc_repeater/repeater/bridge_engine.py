"""Bridge engine for WM1303 multi-channel operation.

Forwards packets between channels with deduplication and rule-based
packet-type filtering.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from collections import deque
import threading
import queue

from repeater.web.packet_trace import trace_event as _trace

logger = logging.getLogger('BridgeEngine')

_active_bridge: "BridgeEngine | None" = None  # module-level reference for API access


def _hexdump(data: bytes, mx: int = 80) -> str:
    """Format bytes as hex string for logging."""
    h = ' '.join(f'{b:02x}' for b in data[:mx])
    return h + (f' ...({len(data)}B)' if len(data) > mx else '')

def _mc_hdr(data: bytes) -> str:
    """Parse MeshCore header for hex dump logging."""
    if not data or len(data) < 2:
        return 'TOO_SHORT'
    hdr = data[0]
    rt = hdr & 0x03
    pt = (hdr >> 2) & 0x0F
    ver = (hdr >> 6) & 0x03
    has_tc = rt in (0x00, 0x03)
    idx = 5 if has_tc else 1
    if idx >= len(data):
        return f'rt={rt} pt={pt} v={ver} TRUNC'
    pl_raw = data[idx]
    hops = pl_raw & 0x3F
    hsz = ((pl_raw >> 6) & 0x03) + 1
    pbytes = hops * hsz
    rn = {0:'TFLOOD',1:'FLOOD',2:'DIRECT',3:'TDIRECT'}
    tn = {0:'REQ',1:'RESP',2:'TXT',3:'ACK',4:'ADVERT',5:'GRP_TXT',6:'GRP_DATA',7:'ANON',8:'PATH',9:'TRACE',10:'MULTI',11:'CTRL',15:'RAW'}
    return f'{rn.get(rt,"?")}/{tn.get(pt,"?")} v{ver} path_raw=0x{pl_raw:02x}(hops={hops},hsz={hsz},pbytes={pbytes}) tc={has_tc}'




def _extract_mc_payload(data: bytes) -> bytes:
    """Extract payload bytes after MeshCore header + path data.

    The payload stays constant across hop iterations, making it
    suitable for detecting self-echo feedback loops.
    """
    if not data or len(data) < 2:
        return data
    hdr = data[0]
    rt = hdr & 0x03
    has_tc = rt in (0x00, 0x03)  # TFLOOD or TDIRECT have timestamp
    idx = 5 if has_tc else 1  # path_raw byte offset
    if idx >= len(data):
        return data
    pl_raw = data[idx]
    hops = pl_raw & 0x3F
    hsz = ((pl_raw >> 6) & 0x03) + 1  # hash size in bytes
    pbytes = hops * hsz
    payload_start = idx + 1 + pbytes
    if payload_start >= len(data):
        return data  # malformed, return all for safe hashing
    return data[payload_start:]


def _stable_hash(data: bytes, length: int = 12) -> str:
    """Compute a stable hash from header byte + payload (skipping path data).

    The MeshCore path data (hop hashes, hop count) changes at each hop,
    so hashing the full packet produces different results for the same
    message at different points in the mesh.  By hashing only byte-0
    (header: route type + payload type + version) plus the payload after
    the path data, we get a hash that stays constant across hops.
    """
    payload = _extract_mc_payload(data)
    return hashlib.sha256(data[0:1] + payload).hexdigest()[:length]


def emit_lbt_cad_trace_steps(pkt_hash8: str, channel_id: str,
                              tx_result: dict,
                              pkt_type_name: str = None) -> None:
    """Emit `lbt_check` and `cad_check` trace steps based on a backend TX result.

    Shared helper used by both BridgeEngine (_forward_by_rules) and
    ChannelEBridge (_tx_handler) so every TX that goes through the HAL
    CAD+LBT path gets consistent chronological trace visibility.

    The `tx_result` dict is the return value of `backend.send()` / queue.enqueue().
    If the post-TX ack did not arrive (older HAL or TX-blocked early path),
    the corresponding keys will be absent and no steps are emitted.

    Steps use a unified multi-line detail layout matching tx_send style:
        "LBT PASS\\n  RSSI: -87 dBm\\n  Threshold: -80 dBm\\n  Retries: 0"
        "CAD CLEAR\\n  TX Noisefloor: -128 dBm\\n  Reason: clear\\n  Retries: 0"
    Missing values are omitted (no 'None dBm' lines).
    """
    if not isinstance(tx_result, dict):
        return
    try:
        # ---- LBT ----
        if tx_result.get('lbt_enabled'):
            _lbt_pass = bool(tx_result.get('lbt_pass', True))
            _lbt_rssi = tx_result.get('lbt_rssi_dbm')
            _lbt_thr = tx_result.get('lbt_threshold_dbm')
            _lbt_retries = int(tx_result.get('lbt_retries', 0) or 0)
            _lbt_header = 'LBT PASS' if _lbt_pass else 'LBT BLOCKED'
            _parts = [_lbt_header]
            if _lbt_rssi is not None:
                _parts.append('  RSSI: %s dBm' % _lbt_rssi)
            if _lbt_thr is not None:
                _parts.append('  Threshold: %s dBm' % _lbt_thr)
            _parts.append('  Retries: %d' % _lbt_retries)
            if _lbt_pass:
                _lbt_status = 'ok' if _lbt_retries == 0 else 'partial'
            else:
                _lbt_status = 'filtered'
            _trace(pkt_hash8, 'lbt_check', channel=channel_id,
                   pkt_type=pkt_type_name, detail='\n'.join(_parts),
                   status=_lbt_status)
        # ---- CAD ----
        if tx_result.get('cad_enabled'):
            _cad_detected = bool(tx_result.get('cad_detected', False))
            _cad_retries = int(tx_result.get('cad_retries', 0) or 0)
            _cad_reason = (tx_result.get('cad_reason') or '').strip()
            _tx_nf = tx_result.get('tx_noisefloor_dbm')
            _cad_header = 'CAD DETECTED' if _cad_detected else 'CAD CLEAR'
            _parts = [_cad_header]
            if _tx_nf is not None:
                _parts.append('  TX Noisefloor: %s dBm' % _tx_nf)
            if _cad_reason:
                _parts.append('  Reason: %s' % _cad_reason)
            _parts.append('  Retries: %d' % _cad_retries)
            if _cad_detected:
                _cad_status = 'filtered'
            else:
                _cad_status = 'ok' if _cad_retries == 0 else 'partial'

            # Get CAD duration from nested 'cad' dict or flat key
            _cad_obj = tx_result.get('cad', {})
            _cad_duration_ms = 0
            if isinstance(_cad_obj, dict):
                _cad_duration_ms = int(_cad_obj.get('duration_ms', 0) or 0)

            # Emit cad_start backdated by duration, then cad_check at current time
            if _cad_duration_ms > 0:
                _trace(pkt_hash8, 'cad_start', channel=channel_id,
                       pkt_type=pkt_type_name,
                       detail='CAD scan started\n  Duration: %d ms' % _cad_duration_ms,
                       status='ok',
                       ts_offset_ms=float(_cad_duration_ms))
                _parts.append('  Duration: %d ms' % _cad_duration_ms)

            _trace(pkt_hash8, 'cad_check', channel=channel_id,
                   pkt_type=pkt_type_name, detail='\n'.join(_parts),
                   status=_cad_status)
    except Exception as _trace_e:
        logger.debug('emit_lbt_cad_trace_steps: failed: %s', _trace_e)




class BridgeEngine:
    """Cross-channel packet bridge for WM1303 concentrator.

    Supports rule-based forwarding with MeshCore packet-type filtering.
    """

    # ------------------------------------------------------------------ #
    # MeshCore header bit-field layout (from protocol/constants.py)
    # ------------------------------------------------------------------ #
    # Byte 0: [VER(2) | TYPE(4) | ROUTE(2)]
    #   bits 0-1  = route type
    #   bits 2-5  = payload type (shifted by 2)
    #   bits 6-7  = version
    PH_TYPE_SHIFT = 2
    PH_TYPE_MASK  = 0x0F

    # Payload-type values (4 bits, from protocol/constants.py)
    PAYLOAD_TYPE_REQ        = 0x00
    PAYLOAD_TYPE_RESPONSE   = 0x01
    PAYLOAD_TYPE_TXT_MSG    = 0x02
    PAYLOAD_TYPE_ACK        = 0x03
    PAYLOAD_TYPE_ADVERT     = 0x04
    PAYLOAD_TYPE_GRP_TXT    = 0x05
    PAYLOAD_TYPE_GRP_DATA   = 0x06
    PAYLOAD_TYPE_ANON_REQ   = 0x07
    PAYLOAD_TYPE_PATH       = 0x08
    PAYLOAD_TYPE_TRACE      = 0x09
    PAYLOAD_TYPE_MULTIPART  = 0x0A
    PAYLOAD_TYPE_CONTROL    = 0x0B
    PAYLOAD_TYPE_RAW_CUSTOM = 0x0F

    # Human-readable name -> numeric type mapping for rule filters
    PKT_TYPE_MAP = {
        'REQ':        0x00,
        'REQUEST':    0x00,
        'RESPONSE':   0x01,
        'TXT_MSG':    0x02,
        'TEXT':       0x02,
        'MESSAGE':    0x02,
        'ACK':        0x03,
        'ADVERT':     0x04,
        'GRP_TXT':    0x05,
        'GROUP_TEXT': 0x05,
        'GRP_DATA':   0x06,
        'GROUP_DATA': 0x06,
        'ANON_REQ':   0x07,
        'PATH':       0x08,
        'TRACE':      0x09,
        'MULTIPART':  0x0A,
        'CONTROL':    0x0B,
        'RAW_CUSTOM': 0x0F,
        'RAW':        0x0F,
        'ALL':        None,  # None = match everything
    }

    # Reverse map: numeric -> canonical name (for logging)
    PKT_TYPE_NAMES = {
        0x00: 'REQ',
        0x01: 'RESPONSE',
        0x02: 'TXT_MSG',
        0x03: 'ACK',
        0x04: 'ADVERT',
        0x05: 'GRP_TXT',
        0x06: 'GRP_DATA',
        0x07: 'ANON_REQ',
        0x08: 'PATH',
        0x09: 'TRACE',
        0x0A: 'MULTIPART',
        0x0B: 'CONTROL',
        0x0F: 'RAW_CUSTOM',
    }

    # Non-radio endpoints (handled by external callbacks, not in _radio_map)
    # NOTE: channel_e (SX1261) and channel_f (SX1302 chan_Lora_std) are NOT in
    # _radio_map (they are not VirtualLoRaRadio instances) but they ARE RF
    # endpoints — they perform real over-the-air TX/RX via dedicated bridge
    # plugins (channel_e_bridge.py / channel_f_bridge.py).
    NON_RADIO_ENDPOINTS = {'mqtt', 'repeater', 'channel_e', 'channel_f'}

    # RF-transmitting endpoints: endpoints that perform actual over-the-air TX
    # and should participate in TX ordering / origin-channel-first priority,
    # alongside WM1303 radio channels. Non-RF endpoints (repeater, mqtt) are
    # excluded because they do internal processing, not RF TX.
    # Extend this set as more RF endpoints (e.g. extra SX126x channels) are added.
    RF_ENDPOINTS = {'channel_e', 'channel_f'}

    # Internal sources that bypass dedup (non-RF origins that re-inject processed packets)
    # channel_e and channel_f are excluded because they ARE RF sources
    # (channel_e = SX1261 radio, channel_f = SX1302 chan_Lora_std)
    DEDUP_BYPASS_SOURCES = {'mqtt', 'repeater'}

    # Static fallback aliases (overridden by dynamic aliases built in __init__)
    _STATIC_ALIASES = {
        'n1': 'channel_a', 'channel_a': 'channel_a', 'ch-1': 'channel_a',
        'n2': 'channel_b', 'channel_b': 'channel_b', 'ch-2': 'channel_b',
        'n3': 'channel_c', 'channel_c': 'channel_c', 'ch-3': 'channel_c',
        'n4': 'channel_d', 'channel_d': 'channel_d', 'ch-4': 'channel_d',
    }

    def __init__(self, radios: list, rules: list | None = None,
                 dedup_ttl: float = 300.0, tx_delay_ms: float = 0.0):
        self.radios = radios
        # Build radio map: channel_id -> radio instance
        self._radio_map = {
            getattr(r, 'channel_id', f'ch{i}'): r
            for i, r in enumerate(radios)
        }

        # Build CHANNEL_ALIASES dynamically from actual radio objects
        self.CHANNEL_ALIASES = dict(self._STATIC_ALIASES)  # start with defaults
        self._build_dynamic_aliases(radios)

        # Build reverse alias map: channel_id -> set of all names that resolve to it
        self._reverse_aliases: dict[str, set[str]] = {}
        self._build_reverse_aliases()

        self.rules = rules or []
        self.dedup_ttl = dedup_ttl
        self.tx_delay = tx_delay_ms / 1000.0
        self._seen: dict[str, float] = {}
        self._running = False
        self.forwarded_packets = 0
        self.dropped_duplicate = 0
        self.dropped_filtered = 0

        # External endpoint handlers (for non-radio targets like mqtt, repeater)
        self._mqtt_handler = None      # callback(data: bytes) -> None
        self._repeater_handler = None   # callback(data: bytes) -> None
        self._repeater_engine = None     # RepeaterHandler instance for counter updates
        self._endpoint_handlers: dict[str, callable] = {}  # name -> callback

        self._endpoint_handlers: dict[str, callable] = {}  # name -> callback

        # Origin channel counters: track how many packets each channel sources for the repeater
        self._origin_channel_counts: dict[str, int] = {}
        self._origin_channel_lock = threading.Lock()

        # TX echo detection: buffer of stable hashes from recently transmitted
        # packets.  When a packet arrives whose stable hash matches a recent TX,
        # it is almost certainly our own transmission bouncing back via the mesh.
        #
        # NOTE: The primary echo classification now lives in WM1303Backend
        # (_classify_echo) which drops true RF self-echoes before they reach
        # the bridge.  This bridge-level check is a secondary safety net and
        # uses a short TTL (0.5 s) so that only genuine RF feedback (<100 ms)
        # is caught here.  Mesh-echoes (neighbour retransmissions, typically
        # >1 s) deliberately pass through so the bridge engine can forward
        # them to companion servers and protocol handlers.
        self._tx_echo_hashes: dict[str, float] = {}   # stable_hash -> monotonic ts
        self._tx_echo_ttl = 0.5                        # seconds to keep TX hashes
        self._tx_echo_detected = 0                     # counter for stats
        self._tx_echo_cleanup_ts = 0.0                 # last cleanup timestamp

        # Raw RX callbacks: invoked for EVERY RF-received packet BEFORE echo/dedup
        # filtering.  This allows companion frame servers to receive all packets
        # (including TX echoes and duplicates) for "Heard Repeats" and node
        # discovery, without interfering with the bridge forwarding logic.
        # Callback signature: callback(data: bytes, source_name: str, rssi, snr)
        self._on_raw_rx_callbacks: list = []



        # Dedup event logging for visualization
        self._dedup_events: deque = deque(maxlen=500)  # ring buffer

        # SQLite dedup event persistence (non-blocking writer thread)
        self._sqlite_handler = None
        self._dedup_queue: queue.Queue = queue.Queue()
        self._sqlite_writer_thread: threading.Thread | None = None
        self._sqlite_writer_running = False
        self._last_cleanup_ts = time.time()
        self._cleanup_interval = 3600  # cleanup every hour

        # Build display name map for human-readable trace output
        self._display_names: dict[str, str] = {}
        self._build_display_names(radios)


        # Module-level reference for API access
        global _active_bridge
        _active_bridge = self

        logger.info('BridgeEngine: initialized with %d radios, %d rules, dedup_ttl=%.1fs',
                    len(radios), len(self.rules), self.dedup_ttl)
        logger.info('BridgeEngine: radio_map keys: %s', list(self._radio_map.keys()))
        logger.info('BridgeEngine: reverse_aliases: %s',
                    {k: sorted(v) for k, v in self._reverse_aliases.items()})
        for rule in self.rules:
            rsrc = rule.get('source', '?')
            rtgt = rule.get('target', '?')
            rsrc_resolved = self._resolve_channel(rsrc)
            rtgt_resolved = self._resolve_channel(rtgt)
            logger.info('BridgeEngine: rule %s: %s(%s) -> %s(%s) '
                       '(filter=%s, packet_types=%s, enabled=%s)',
                       rule.get('name', rule.get('id', '?')), rsrc, rsrc_resolved,
                       rtgt, rtgt_resolved,
                       rule.get('filter', 'all'), rule.get('packet_types', []),
                       rule.get('enabled', True))

    def _build_reverse_aliases(self) -> None:
        """Build reverse alias map: canonical channel_id -> set of all alias names.

        This enables bidirectional matching: given a channel_id like 'channel_a',
        we can find all names that resolve to it (e.g., {'ch-1', 'n1', 'channel_a',
        'Channel A', 'channel a'}).
        """
        self._reverse_aliases = {}
        for alias_name, canonical_id in self.CHANNEL_ALIASES.items():
            if canonical_id not in self._reverse_aliases:
                self._reverse_aliases[canonical_id] = set()
            self._reverse_aliases[canonical_id].add(alias_name)
        # Also add non-radio endpoints as identity mappings
        for ep in self.NON_RADIO_ENDPOINTS:
            if ep not in self._reverse_aliases:
                self._reverse_aliases[ep] = {ep}
        logger.info('BridgeEngine: reverse aliases built for %d canonical IDs',
                    len(self._reverse_aliases))

    def _source_matches(self, rule_source_raw: str, source_cid: str) -> bool:
        """Check if a rule's source matches the packet's source channel.

        Bidirectional matching:
        1. Forward: resolve rule_source_raw to canonical ID, compare with source_cid
        2. Reverse: check if rule_source_raw is in the set of aliases for source_cid
        3. Direct: exact string match as fallback

        This ensures 'ch-1' matches 'channel_a' regardless of alias direction.
        """
        # Direct match (fastest path)
        if rule_source_raw == source_cid:
            return True

        # Forward resolution: resolve rule source name to canonical channel_id
        resolved = self.CHANNEL_ALIASES.get(rule_source_raw, rule_source_raw)
        if resolved == source_cid:
            return True

        # Reverse lookup: check if rule_source_raw is any alias of source_cid
        aliases_for_source = self._reverse_aliases.get(source_cid, set())
        if rule_source_raw in aliases_for_source:
            return True

        # Also try resolving source_cid (in case it's an alias too)
        source_resolved = self.CHANNEL_ALIASES.get(source_cid, source_cid)
        if resolved == source_resolved:
            return True

        return False

    def _build_dynamic_aliases(self, radios: list) -> None:
        """Build channel aliases dynamically from actual radio objects AND wm1303_ui.json.

        Each VirtualLoRaRadio has:
          - channel_id: e.g., 'channel_a', 'channel_b', 'channel_d'
          - channel_config: dict from config.yaml (may lack 'name' / 'friendly_name')

        The wm1303_ui.json (SSOT) has the actual UI channel names ('ch-1', 'ch-new')
        and friendly names ('Channel A', 'Channel D'). We match UI channels to radios
        by spreading_factor since that's unique per channel.

        Builds aliases from all sources so any name resolves correctly.
        """
        import json
        from pathlib import Path

        # Phase 1: Build aliases from radio objects (config.yaml data)
        sf_to_cid = {}  # spreading_factor -> channel_id mapping
        for r in radios:
            cid = getattr(r, 'channel_id', None)
            if not cid:
                continue
            # Identity alias: channel_id -> channel_id
            self.CHANNEL_ALIASES[cid] = cid
            cfg = getattr(r, 'channel_config', {})
            # Track SF -> channel_id for UI matching
            sf = int(cfg.get('spreading_factor', 0))
            if sf:
                sf_to_cid[sf] = cid
            # Config-based aliases (if present)
            name = cfg.get('name', '')
            if name:
                self.CHANNEL_ALIASES[name] = cid
            friendly = cfg.get('friendly_name', '')
            if friendly:
                self.CHANNEL_ALIASES[friendly] = cid
                self.CHANNEL_ALIASES[friendly.lower()] = cid

        # Phase 2: Read wm1303_ui.json for UI channel names and friendly names
        # Match by POSITION (index) not by SF to avoid mismatches when SF is
        # changed in the UI but not yet in config.yaml.
        ui_path = Path('/etc/pymc_repeater/wm1303_ui.json')
        try:
            if ui_path.exists():
                ui = json.loads(ui_path.read_text())
                # Build ordered list of active radio channel_ids
                radio_cids = [getattr(r, 'channel_id', None) for r in radios]
                radio_cids = [c for c in radio_cids if c]  # filter None
                ui_channels = ui.get("channels", [])  # ALL channels for position-based alias mapping
                for idx, uc in enumerate(ui_channels):
                    if idx >= len(radio_cids):
                        logger.debug('BridgeEngine: UI channel index %d has no radio (only %d radios)',
                                    idx, len(radio_cids))
                        break
                    cid = radio_cids[idx]
                    # UI name alias: e.g., 'ch-1' -> 'channel_a'
                    ui_name = uc.get('name', '')
                    if ui_name:
                        self.CHANNEL_ALIASES[ui_name] = cid
                    # Friendly name alias: e.g., 'Channel A' -> 'channel_a'
                    ui_friendly = uc.get('friendly_name', '')
                    if ui_friendly:
                        self.CHANNEL_ALIASES[ui_friendly] = cid
                        self.CHANNEL_ALIASES[ui_friendly.lower()] = cid
                    logger.debug('BridgeEngine: UI channel[%d] %r -> %s (position-based)',
                                idx, ui_name, cid)
        except Exception as e:
            logger.warning('BridgeEngine: failed to read UI config for aliases: %s', e)

        logger.info('BridgeEngine: dynamic aliases built: %s', dict(self.CHANNEL_ALIASES))

    def _build_display_names(self, radios: list) -> None:
        """Build display name map: channel_id -> human-readable name for traces.

        Naming rules:
          - channel_e   -> friendly_name from UI config (e.g. 'EU-Narrow')
          - channel_a-d  -> internal name from UI config (e.g. 'local-test')
          - repeater     -> 'Repeater'
          - mqtt         -> 'MQTT'
        """
        import json
        from pathlib import Path

        # Static mappings for non-radio endpoints
        self._display_names = {
            'repeater': 'Repeater',
            'mqtt': 'MQTT',
        }

        # Read channel_e friendly_name from UI config
        ui_path = Path('/etc/pymc_repeater/wm1303_ui.json')
        try:
            if ui_path.exists():
                ui = json.loads(ui_path.read_text())
                # Channel E display name from its friendly_name
                che_cfg = ui.get('channel_e', {})
                che_friendly = che_cfg.get('friendly_name', '') or che_cfg.get('name', '')
                if che_friendly:
                    self._display_names['channel_e'] = che_friendly
                # Channel F display name from its friendly_name
                chf_cfg = ui.get('channel_f', {})
                chf_friendly = chf_cfg.get('friendly_name', '') or chf_cfg.get('name', '')
                if chf_friendly:
                    self._display_names['channel_f'] = chf_friendly
                # Channels a-d: use internal name from channels list
                radio_cids = [getattr(r, 'channel_id', None) for r in radios]
                radio_cids = [c for c in radio_cids if c]
                ui_channels = ui.get('channels', [])
                for idx, uc in enumerate(ui_channels):
                    if idx >= len(radio_cids):
                        break
                    cid = radio_cids[idx]
                    # Use the UI name (user-configurable internal name)
                    ui_name = uc.get('name', '')
                    if ui_name and ui_name != cid:
                        self._display_names[cid] = ui_name
                    else:
                        # Fallback: use friendly_name if different from generic
                        fn = uc.get('friendly_name', '')
                        if fn and fn not in ('Channel A', 'Channel B', 'Channel C', 'Channel D'):
                            self._display_names[cid] = fn
        except Exception as e:
            logger.warning('BridgeEngine: failed to build display names: %s', e)

        logger.info('BridgeEngine: display names: %s', self._display_names)

    def _dn(self, channel_id: str) -> str:
        """Get display name for a channel ID (short alias for trace messages)."""
        return self._display_names.get(channel_id, channel_id)

    def _format_rule_name(self, rule: dict) -> str:
        """Format a rule as 'SourceName → TargetName' using display names."""
        src_raw = rule.get('source', rule.get('from', '?'))
        tgt_raw = rule.get('target', rule.get('to', '?'))
        src_cid = self._resolve_channel(src_raw)
        tgt_cid = self._resolve_channel(tgt_raw)
        return '%s → %s' % (self._dn(src_cid), self._dn(tgt_cid))
    def _format_received_detail(self, channel_id: str, size: int,
                                 pkt_type_name: str,
                                 rssi: float | int | None = None,
                                 snr: float | None = None,
                                 noise_floor: float | None = None,
                                 freq_mhz: float | None = None,
                                 datarate: str | None = None) -> str:
        """Format a rich multi-line detail string for a `received` trace step.

        Always starts with `RX on <channel> | <size> bytes | <type>` on the
        first line. Additional lines with RF metadata are appended only when
        values are provided (no placeholders for missing data).
        """
        lines = ['RX on %s' % self._dn(channel_id)]
        lines.append('  Size: %d bytes' % size)
        if pkt_type_name:
            lines.append('  Type: %s' % pkt_type_name)
        if freq_mhz is not None:
            try:
                lines.append('  Frequency: %.3f MHz' % float(freq_mhz))
            except Exception:
                pass
        if datarate:
            lines.append('  Data rate: %s' % datarate)
        if rssi is not None:
            try:
                lines.append('  RSSI: %d dBm' % int(rssi))
            except Exception:
                pass
        if snr is not None:
            try:
                lines.append('  SNR: %.1f dB' % float(snr))
            except Exception:
                pass
        if noise_floor is not None:
            try:
                lines.append('  Noise floor: %d dBm' % int(noise_floor))
            except Exception:
                pass
        return '\n'.join(lines)

    def emit_received_trace(self, pkt_hash8: str, channel_id: str,
                            pkt_type_name: str, size: int,
                            rssi: float | int | None = None,
                            snr: float | None = None,
                            noise_floor: float | None = None,
                            freq_mhz: float | None = None,
                            datarate: str | None = None) -> None:
        """Public helper: emit a `received` trace step with rich RF metadata.

        Safe to call from any thread or RX path (e.g. `channel_e_bridge`).
        All RF fields are optional — only provided values are rendered.
        """
        try:
            detail = self._format_received_detail(
                channel_id, size, pkt_type_name,
                rssi=rssi, snr=snr, noise_floor=noise_floor,
                freq_mhz=freq_mhz, datarate=datarate)
            _trace(pkt_hash8, 'received',
                   channel=channel_id, pkt_type=pkt_type_name, detail=detail)
        except Exception as _e:  # pragma: no cover - defensive only
            logger.debug('emit_received_trace: failed (%s)', _e)


    @staticmethod
    def _format_tx_result(tx_result: dict, channel_name: str, rule_display: str) -> str:
        """Format a TX result dict into a human-readable multi-line string."""
        if not isinstance(tx_result, dict):
            return 'TX on %s (rule: %s): %s' % (channel_name, rule_display, tx_result)
        ok = tx_result.get('ok', False)
        if not ok:
            err = tx_result.get('error', 'unknown')
            return 'TX FAILED on %s (rule: %s) — %s' % (channel_name, rule_display, err)
        parts = ['TX on %s (rule: %s)' % (channel_name, rule_display)]
        freq = tx_result.get('freq')
        if freq is not None:
            parts.append('  Frequency: %s MHz' % freq)
        datr = tx_result.get('datr', '')
        if datr:
            parts.append('  Data rate: %s' % datr)
        airtime = tx_result.get('airtime_ms')
        if airtime is not None:
            parts.append('  Airtime: %sms' % airtime)
        queue_wait = tx_result.get('queue_wait_ms')
        if queue_wait is not None:
            parts.append('  Queue wait: %.1fms' % queue_wait)
        send_ms = tx_result.get('send_ms')
        if send_ms is not None:
            parts.append('  Send: %sms' % send_ms)
        ack_ok = tx_result.get('tx_ack_ok')
        ack_err = tx_result.get('tx_ack_error', '')
        if ack_ok is not None:
            ack_str = 'OK' if ack_ok else ('FAIL (%s)' % ack_err)
            parts.append('  TX ACK: %s' % ack_str)
        return '\n'.join(parts)

    # ------------------------------------------------------------------ #
    # External endpoint handler registration
    # ------------------------------------------------------------------ #
    def set_repeater_handler(self, callback) -> None:
        """Register a callback for forwarding packets to the repeater."""
        self._repeater_handler = callback
        self._endpoint_handlers['repeater'] = callback
        logger.info('BridgeEngine: repeater handler registered')

    def set_repeater_engine(self, engine) -> None:
        """Store reference to RepeaterHandler for counter/recent_packets updates."""
        self._repeater_engine = engine
        logger.info('BridgeEngine: repeater engine reference set for counter updates')


    def set_mqtt_handler(self, callback) -> None:
        """Register a callback for forwarding packets to MQTT."""
        self._mqtt_handler = callback
        self._endpoint_handlers['mqtt'] = callback
        logger.info('BridgeEngine: MQTT handler registered')

    def register_on_raw_rx(self, callback) -> None:
        """Register a callback invoked for every RF-received packet BEFORE
        echo/dedup filtering.  Used by companion frame servers to deliver
        all packets (including TX echoes and duplicates) for features like
        Heard Repeats and node discovery.
        Callback signature: callback(data: bytes, source_name: str, rssi, snr)
        """
        self._on_raw_rx_callbacks.append(callback)
        logger.info('BridgeEngine: raw RX callback registered (%d total)',
                    len(self._on_raw_rx_callbacks))

    def _fire_raw_rx_callbacks(self, data: bytes, source_name: str,
                               rssi=None, snr=None) -> None:
        """Invoke all registered raw RX callbacks (best-effort, non-blocking)."""
        for cb in self._on_raw_rx_callbacks:
            try:
                cb(data, source_name, rssi, snr)
            except Exception as e:
                logger.error('BridgeEngine: raw RX callback error: %s', e)

    async def inject_packet(self, source_name: str, data: bytes,
                            origin_channel: str | None = None,
                            rssi: float | None = None,
                            snr: float | None = None) -> None:
        """Inject a packet into the bridge as if received from the given source.

        Allows external components (MQTT, repeater) to feed packets into
        the bridge rule engine for forwarding.

        Args:
            source_name: The source identifier (e.g. 'repeater', 'mqtt').
            data: Raw packet bytes.
            origin_channel: Optional channel_id where the packet was originally
                received. When set, radio TX for this channel is prioritized
                (sent first) so the origin channel gets the fastest repeat.
            rssi: Optional RSSI value from the receiving radio (dBm).
            snr: Optional SNR value from the receiving radio (dB).
        """
        if not self._running:
            logger.warning('BridgeEngine: inject_packet called but bridge not running')
            return

        # NOTE: Raw RX callbacks for companion frame servers are now
        # fired from WM1303Backend._process_rx_packet BEFORE this code
        # is reached, so companions receive ALL RF packets including
        # TX echoes and duplicates.  Do NOT call _fire_raw_rx_callbacks
        # here to avoid double-pushing non-echo packets.

        # TX echo check: drop packets that match our own recent transmissions
        # (must be checked BEFORE dedup, because dedup would also catch it
        #  but without the specific TX_ECHO classification)
        if self._is_tx_echo(data, source_name):
            _echo_hash8 = _stable_hash(data, 8)
            _trace(_echo_hash8, 'bridge_inject', channel=source_name, pkt_type=self._get_packet_type_name(data), detail='Injected %d bytes from %s' % (len(data), self._dn(source_name)))
            _trace(_echo_hash8, 'echo_dedup', channel=source_name, detail='Duplicate or echo detected (from %s)' % self._dn(source_name), status='ok')
            self.dropped_duplicate += 1
            # Fix (Bug 2 / packets.transmitted persistence): inject_packet must
            # mirror _rx_loop and update repeater counters so the packet is
            # recorded in the SQLite `packets` table. Without this, bridge-
            # injected packets (e.g. from channel_e_bridge) were silently
            # missing from the DB while stats counters kept ticking.
            self._update_repeater_counters(
                data, source_name, pkt_type_name=self._get_packet_type_name(data),
                pkt_hash=_stable_hash(data), was_echo=True,
                drop_reason="tx_echo",
                rssi=int(rssi) if rssi is not None else -120,
                snr=float(snr) if snr is not None else 0.0)
            return

        # Dedup check: only apply to RF/channel sources, skip for internal sources
        # Internal sources (repeater, mqtt) re-inject processed packets that would
        # share the same stable hash as the original RX, causing false duplicate drops.
        if source_name in self.DEDUP_BYPASS_SOURCES:
            logger.debug('BridgeEngine: dedup skipped for internal source %r', source_name)
            _dup_hash8 = _stable_hash(data, 8)
            _trace(_dup_hash8, 'dedup_skip', status='ok', detail='Internal source %s, dedup bypassed' % self._dn(source_name))
        elif self._is_duplicate(data):
            _dup_hash8 = _stable_hash(data, 8)
            _dup_hash = _stable_hash(data)
            _trace(_dup_hash8, 'bridge_inject', channel=source_name, pkt_type=self._get_packet_type_name(data), detail='Injected %d bytes from %s' % (len(data), self._dn(source_name)))
            _trace(_dup_hash8, 'dedup_drop', channel=source_name, detail='Dropped as duplicate (injected from %s)' % self._dn(source_name), status='warning')
            self.dropped_duplicate += 1
            self._record_dedup_event('duplicate', source_name, _dup_hash, len(data))
            logger.debug('BridgeEngine: injected duplicate dropped from %s (hash=%s)', source_name, _dup_hash)
            # Fix (Bug 2): record duplicate drop for injected packets as well.
            self._update_repeater_counters(
                data, source_name, pkt_type_name=self._get_packet_type_name(data),
                pkt_hash=_dup_hash, was_duplicate=True,
                drop_reason="duplicate",
                rssi=int(rssi) if rssi is not None else -120,
                snr=float(snr) if snr is not None else 0.0)
            return
        else:
            # Dedup check passed — packet is not a duplicate
            _pass_hash8 = _stable_hash(data, 8)
            _trace(_pass_hash8, 'dedup_check', channel=source_name, detail='Dedup check passed (from %s)' % self._dn(source_name), status='ok')

        pkt_hash = _stable_hash(data)
        pkt_hash8 = _stable_hash(data, 8)
        pkt_type_name = self._get_packet_type_name(data)
        # v2.4.7+: Parse MeshCore packet metadata once and attach to trace.
        # Used by the Tracing-tab UI to show hops / src / dst / path / payload.
        try:
            _pinfo = BridgeEngine._parse_path_info(data) or {}
        except Exception:
            _pinfo = {}
        _trace(pkt_hash8, 'bridge_inject',
               channel=source_name, pkt_type=pkt_type_name,
               detail='Injected %d bytes from %s (origin=%s)' % (
                   len(data), self._dn(source_name),
                   self._dn(origin_channel) if origin_channel else 'none'),
               pkt_hash_full=pkt_hash,
               hops=len(_pinfo.get('path_hashes') or []),
               hash_size=_pinfo.get('path_hash_size') or 0,
               src_hash=_pinfo.get('src_hash'),
               dst_hash=_pinfo.get('dst_hash'),
               path_hashes=_pinfo.get('path_hashes') or [],
               payload_hex=(data.hex().upper() if data else ''),
               payload_full_size=len(data) if data else 0)
        logger.info('[HEXDUMP] dir=INJECT src=%s sz=%d hdr=%s hex=%s', source_name, len(data), _mc_hdr(data), _hexdump(data))
        logger.info('BridgeEngine: injected %d bytes from %s (type=%s, hash=%s, origin_channel=%s)',
                   len(data), source_name, pkt_type_name, pkt_hash, origin_channel)

        # Store per-packet RX metric for spectrum-tab charts (Option B).
        # inject_packet is the RX path for channel_e and other injected sources.
        # We only store for RF endpoints (channel_e) to avoid duplicate rows
        # from internal sources like 'repeater' (which re-injects processed RX).
        if source_name in self.RF_ENDPOINTS:
            try:
                _hop = None
                if data and len(data) >= 2:
                    _hdr_byte = data[0]
                    _rt = _hdr_byte & 0x03
                    _has_tc = _rt in (0x00, 0x03)
                    _idx = 5 if _has_tc else 1
                    if _idx < len(data):
                        _hop = data[_idx] & 0x3F
                if self._sqlite_handler is not None:
                    self._sqlite_handler.store_packet_metric({
                        'timestamp': time.time(),
                        'channel_id': str(source_name),
                        'direction': 'rx',
                        'length': int(len(data)),
                        'hop_count': int(_hop) if _hop is not None else None,
                        'crc_ok': True,
                        'rssi': float(rssi) if rssi is not None else None,
                        'snr': float(snr) if snr is not None else None,
                        'pkt_hash': (pkt_hash[:16] if isinstance(pkt_hash, str) else None),
                    })
            except Exception as _pm_e:
                logger.warning('packet_metric RX(inject) store failed on %s: %s', source_name, _pm_e)


        # Snapshot forwarded counter so we can detect whether at least one
        # rule actually dispatched TX for this packet (matches _rx_loop logic).
        _fwd_before = self.forwarded_packets
        await self._forward_by_rules(source_name, data, pkt_hash, pkt_type_name,
                                     origin_channel=origin_channel,
                                     rssi=rssi, snr=snr)
        _fwd_after = self.forwarded_packets
        _was_forwarded = (_fwd_after > _fwd_before)

        # Fix (Bug 2 / packets.transmitted persistence): record the injected
        # packet into the SQLite `packets` table via the repeater engine.
        # For the 'repeater' re-inject source we intentionally skip recording:
        # the originating RX was already recorded (or will be) via the normal
        # RX path / other inject, and recording the repeater re-inject would
        # produce duplicate rows with stable hashes.
        if source_name != 'repeater':
            self._update_repeater_counters(
                data, source_name, pkt_type_name=pkt_type_name,
                pkt_hash=pkt_hash, was_forwarded=_was_forwarded,
                drop_reason=None if _was_forwarded else "no_rule_match",
                rssi=int(rssi) if rssi is not None else -120,
                snr=float(snr) if snr is not None else 0.0)

    async def _forward_by_rules(self, source_cid: str, data: bytes,
                                 pkt_hash: str, pkt_type_name: str,
                                 origin_channel: str | None = None,
                                 rssi: float | None = None,
                                 snr: float | None = None) -> None:
        """Apply bridge rules to forward a packet from the given source.

        Uses bidirectional alias matching via _source_matches() so that
        packets from 'channel_a' match rules with source='ch-1' (and vice
        versa) regardless of which direction the alias is defined.

        Handles both radio targets (via _radio_map) and external endpoint
        targets (via _endpoint_handlers).

        Radio sends are collected and serialized sequentially.
        When origin_channel is set, the origin channel is sent first
        (source-channel-first priority), then remaining channels follow
        in their normal rule order.
        """
        forwarded = False
        rules_checked = 0
        pkt_hash8 = pkt_hash[:8]

        # Track origin channel activity for metrics
        if origin_channel and source_cid == 'repeater':
            with self._origin_channel_lock:
                resolved_oc = self._resolve_channel(origin_channel)
                self._origin_channel_counts[resolved_oc] = self._origin_channel_counts.get(resolved_oc, 0) + 1

        # Collect RF send tasks (both WM1303 radio channels and RF endpoints
        # like channel_e). Non-RF endpoints (repeater, mqtt) are still processed
        # inline below because they do internal processing, not RF TX.
        # Entry shape: (tcid, rule_id, tx_delay, kind, dispatcher, rule_display)
        #   kind == 'radio'    -> dispatcher is the radio target (has .send())
        #   kind == 'endpoint' -> dispatcher is the endpoint handler callable
        rf_sends: list[tuple] = []

        for rule in self.rules:
            if not rule.get('enabled', True):
                continue

            rule_source_raw = rule.get('source', '')
            rules_checked += 1

            # Use bidirectional source matching
            if not self._source_matches(rule_source_raw, source_cid):
                continue

            if not self._matches_filter(rule, data):
                _rule_filter = ', '.join(rule.get('packet_types', [])) or rule.get('filter', 'all')
                _trace(pkt_hash8, 'filter_drop', channel=source_cid, pkt_type=pkt_type_name, detail='Filtered by rule %s (filter=%s, type=%s)' % (self._format_rule_name(rule), _rule_filter, pkt_type_name), status='warning')
                self.dropped_filtered += 1
                self._record_dedup_event('filtered', source_cid, pkt_hash, len(data), pkt_type_name)
                logger.info('BridgeEngine: filtered by rule %s (type=%s)',
                           rule.get('name', rule.get('id', '?')), pkt_type_name)
                continue

            rule_target_raw = rule.get('target', '')
            rule_target = self._resolve_channel(rule_target_raw)
            tx_delay = float(rule.get('tx_delay_ms', 0)) / 1000.0
            rule_id = rule.get('name', rule.get('id', '?'))
            rule_display = self._format_rule_name(rule)

            # Check if target is an external endpoint (mqtt, repeater, channel_e, ...)
            if rule_target in self._endpoint_handlers:
                handler = self._endpoint_handlers.get(rule_target)
                if not handler:
                    logger.debug('BridgeEngine: no handler registered for %s', rule_target)
                    continue

                # RF endpoints (e.g. channel_e) perform actual over-the-air TX
                # and MUST participate in origin-channel-first priority ordering
                # alongside WM1303 radio channels. Defer their execution to the
                # unified rf_sends serialization below, so we can reorder them.
                if rule_target in self.RF_ENDPOINTS:
                    rf_sends.append((rule_target, rule_id, tx_delay, 'endpoint', handler, rule_display))
                    _trace(pkt_hash8, 'tx_enqueue', channel=source_cid, pkt_type=pkt_type_name,
                           detail='Queued TX %s \u2192 %s (rule: %s)' % (
                               self._dn(source_cid), self._dn(rule_target), rule_display))
                    logger.info('BridgeEngine: queuing RF endpoint TX %d bytes: %s -> %s '
                               '(rule=%s, type=%s, hash=%s)',
                               len(data), source_cid, rule_target, rule_id,
                               pkt_type_name, pkt_hash)
                    continue

                # Non-RF endpoint (repeater, mqtt): process inline, no RF TX.
                logger.info('BridgeEngine: forwarding %d bytes: %s -> %s '
                           '(rule=%s, type=%s, hash=%s)',
                           len(data), source_cid, rule_target,
                           rule_id, pkt_type_name, pkt_hash)
                try:
                    if tx_delay > 0:
                        await asyncio.sleep(tx_delay)
                    # Pass origin_channel to repeater handler so the
                    # source RX channel is preserved through the repeater
                    # processing pipeline and can be used for TX priority.
                    # WM1303 v2.1.6+: emit bridge_forward BEFORE invoking
                    # the handler, so the trace step timestamp reflects
                    # when forwarding STARTS (near the top of the trace),
                    # not when it finishes (which could be after all
                    # downstream re-injection + TXs and would push the
                    # step to the bottom of the timeline).
                    _trace(pkt_hash8, 'bridge_forward', channel=source_cid, pkt_type=pkt_type_name, detail='Forwarded to %s (rule: %s)' % (self._dn(rule_target), rule_display))
                    if rule_target == 'repeater':
                        result = handler(data, origin_channel=source_cid,
                                         rssi=rssi, snr=snr)
                    else:
                        result = handler(data)
                    if asyncio.iscoroutine(result):
                        await result
                    self.forwarded_packets += 1
                    forwarded = True
                    self._record_dedup_event('forwarded', source_cid, pkt_hash, len(data), pkt_type_name)
                    logger.info('BridgeEngine: delivered to %s endpoint', rule_target)
                except Exception as e:
                    logger.error('BridgeEngine: handler error for %s: %s', rule_target, e)
                continue

            # Radio target - collect for serialized sending
            target = self._radio_map.get(rule_target)
            if not target:
                logger.warning('BridgeEngine: no radio found for target %r '
                              '(resolved from %r)', rule_target, rule_target_raw)
                continue
            # Don't forward back to same radio if source is also a radio
            source_radio = self._radio_map.get(source_cid)
            if target is source_radio and source_radio is not None:
                continue

            tcid = getattr(target, 'channel_id', 'unknown')
            rf_sends.append((tcid, rule_id, tx_delay, 'radio', target, rule_display))
            _trace(pkt_hash8, 'tx_enqueue', channel=source_cid, pkt_type=pkt_type_name, detail='Queued TX %s \u2192 %s (rule: %s)' % (self._dn(source_cid), self._dn(tcid), rule_display))
            logger.info('BridgeEngine: queuing TX %d bytes: %s -> %s '
                       '(rule=%s, type=%s, hash=%s)',
                       len(data), source_cid, tcid, rule_id,
                       pkt_type_name, pkt_hash)

        # Serialize RF sends (radios + RF endpoints) because all channels share
        # the same physical TX chain on the WM1303 + SX1261 board. Concurrent
        # dispatch could collide or overwrite scheduled TX.
        if rf_sends:
            # Origin-channel-first priority: when a packet was originally
            # received on a specific channel and is now being repeated to
            # multiple channels (WM1303 radios AND/OR RF endpoints like
            # channel_e), the origin channel gets TX priority (sent first).
            # Remaining channels keep their existing rule order.
            if origin_channel:
                resolved_origin = self._resolve_channel(origin_channel)
                origin_first = [r for r in rf_sends if r[0] == resolved_origin]
                others = [r for r in rf_sends if r[0] != resolved_origin]
                if origin_first:
                    was_already_first = (rf_sends[0][0] == resolved_origin)
                    rf_sends = origin_first + others
                    if not was_already_first:
                        logger.debug('BridgeEngine: origin-channel reordered: '
                                    'moved %s to front (%d total sends)',
                                    resolved_origin, len(rf_sends))
                    logger.info('BridgeEngine: origin-channel-first priority: '
                               '%s goes first (%d total sends)',
                               resolved_origin, len(rf_sends))

            logger.info('BridgeEngine: serializing %d RF sends for %s '
                       '(type=%s, hash=%s, order=%s)',
                       len(rf_sends), source_cid, pkt_type_name, pkt_hash,
                       [r[0] for r in rf_sends])

            for tcid, rule_id, tx_delay, kind, dispatcher, rule_display in rf_sends:
                try:
                    if tx_delay > 0:
                        await asyncio.sleep(tx_delay)
                    logger.info('[HEXDUMP] dir=TX ch=%s sz=%d hdr=%s hex=%s',
                                tcid, len(data), _mc_hdr(data), _hexdump(data))
                    if kind == 'radio':
                        # Pass pkt_hash8 through to backend so it can emit
                        # chronologically-correct TX-phase trace events
                        # (tx_noisefloor, cad_start, lbt_check, cad_check,
                        # rf_tx_start, rf_tx_end, sx1261_rx_restart, tx_ack,
                        # rf_guard). The backend knows the precise HAL
                        # timing; emitting post-TX from here would place
                        # CAD events too late in the timeline.
                        tx_result = await dispatcher.send(data, trace_hash=pkt_hash8)
                        # Legacy call removed: backend now emits lbt_check
                        # and cad_check at correct timestamps (pre-TX,
                        # backdated from ACK moment). Calling
                        # emit_lbt_cad_trace_steps here would produce
                        # duplicate steps at wrong times.
                        _trace(pkt_hash8, 'tx_send', channel=tcid, pkt_type=pkt_type_name, detail=self._format_tx_result(tx_result, self._dn(tcid), rule_display))
                        logger.info('BridgeEngine: TX result on %s (rule=%s): %s',
                                    tcid, rule_id, tx_result)
                        # Store per-packet TX metric for spectrum-tab charts (Option B).
                        try:
                            if self._sqlite_handler is not None and isinstance(tx_result, dict) and tx_result.get('ok'):
                                _tx_hop = None
                                if data and len(data) >= 2:
                                    _hdr_byte = data[0]
                                    _rt = _hdr_byte & 0x03
                                    _has_tc = _rt in (0x00, 0x03)
                                    _idx = 5 if _has_tc else 1
                                    if _idx < len(data):
                                        _tx_hop = data[_idx] & 0x3F
                                self._sqlite_handler.store_packet_metric({
                                    'timestamp': time.time(),
                                    'channel_id': str(tcid),
                                    'direction': 'tx',
                                    'length': int(len(data)),
                                    'airtime_ms': tx_result.get('airtime_ms'),
                                    'wait_time_ms': tx_result.get('queue_wait_ms'),
                                    'hop_count': int(_tx_hop) if _tx_hop is not None else None,
                                    'crc_ok': True,
                                    'rssi': None,
                                    'snr': None,
                                    'pkt_hash': (pkt_hash[:16] if isinstance(pkt_hash, str) else None),
                                })
                        except Exception as _pm_tx_e:
                            logger.warning('packet_metric TX store failed on %s: %s', tcid, _pm_tx_e)
                    else:  # kind == 'endpoint' (RF endpoint, e.g. channel_e)
                        # The endpoint handler itself emits lbt_check / cad_check
                        # and tx_send trace steps (see channel_e_bridge._tx_handler).
                        # We do NOT duplicate those traces here.
                        result = dispatcher(data)
                        if asyncio.iscoroutine(result):
                            await result
                        logger.info('BridgeEngine: RF endpoint TX dispatched to %s (rule=%s)',
                                    tcid, rule_id)
                except Exception as e:
                    _trace(pkt_hash8, 'tx_send', channel=tcid, pkt_type=pkt_type_name, detail='TX FAILED on %s: %s' % (self._dn(tcid), e), status='error')
                    logger.error('BridgeEngine: TX error to %s (kind=%s, rule=%s): %s',
                                tcid, kind, rule_id, e)
                    continue

                self.forwarded_packets += 1
                forwarded = True
                self._store_tx_echo_hash(data)  # store for echo detection
                self._record_dedup_event('forwarded', source_cid, pkt_hash, len(data), pkt_type_name)

                # Self-TX loopback (v2.5.3 follow-up; companion 'heard repeat' visibility).
                # The WM1303 cannot hear its own RF transmission, so companion apps
                # never see our outbound packets in their live activity log alongside
                # received traffic. Firing the raw-RX callbacks with the TX bytes
                # pushes a 0x88 frame to companion frame servers -- exactly the same
                # shape as if a neighbour had repeated the packet.
                #
                # Covers BOTH kind='radio' (chan_multiSF A-D) and kind='endpoint'
                # (channel_e SX1261) because this runs after the if/else dispatch
                # block and only when TX did not raise (the except branch above
                # uses `continue`, skipping this code on failure).
                #
                # Design principles satisfied:
                #   - RX #1: in-memory callback dispatch, no radio operation,
                #     no SX1302/SX1261 state change, zero RX-time impact.
                #   - TX ASAP: runs after TX completes, so it cannot delay TX.
                #   - No double-delivery: `_store_tx_echo_hash(data)` above already
                #     armed the engine's TX-echo filter, so when the same packet
                #     comes back via real RF from a neighbour repeating it, the
                #     engine drops it as TX_ECHO before re-forwarding.
                #
                # The source_name `self_tx:<channel>` distinguishes loopback frames
                # from real RF-received frames in companion-side filtering. Sentinel
                # rssi=0/snr=0.0 prevent garbage in companion-display signal metrics
                # (the packet did not arrive via RF, so no real values exist).
                try:
                    self._fire_raw_rx_callbacks(data, 'self_tx:%s' % tcid, 0, 0.0)
                except Exception as _loopback_e:
                    logger.debug('Self-TX loopback error on %s: %s', tcid, _loopback_e)

        if not forwarded:
            logger.debug('BridgeEngine: no rule matched for %s on %s (type=%s, '
                        'rules_checked=%d)',
                        pkt_hash, source_cid, pkt_type_name, rules_checked)

    @staticmethod
    def _get_packet_type(data: bytes) -> int | None:
        """Extract MeshCore payload type from the raw packet header."""
        if not data or len(data) < 1:
            return None
        return (data[0] >> 2) & 0x0F

    def _get_packet_type_name(self, data: bytes) -> str:
        """Get human-readable packet type name for logging."""
        pkt_type = self._get_packet_type(data)
        if pkt_type is None:
            return 'UNKNOWN'
        return self.PKT_TYPE_NAMES.get(pkt_type, f'TYPE_{pkt_type:#04x}')

    def _matches_filter(self, rule: dict, data: bytes) -> bool:
        """Check if a packet matches the rule's filter/packet_types."""
        filter_val = rule.get('filter', 'all')
        packet_types = rule.get('packet_types', [])

        if filter_val == 'all' and not packet_types:
            return True

        pkt_type = self._get_packet_type(data)
        if pkt_type is None:
            return True

        if packet_types:
            for pt in packet_types:
                pt_upper = pt.upper() if isinstance(pt, str) else str(pt)
                if pt_upper == 'ALL':
                    return True
                expected = self.PKT_TYPE_MAP.get(pt_upper)
                if expected is not None and expected == pkt_type:
                    return True
            return False

        return True

    def _resolve_channel(self, name: str) -> str:
        """Resolve a channel alias to its canonical channel_id."""
        resolved = self.CHANNEL_ALIASES.get(name, name)
        if resolved == name and name not in self.NON_RADIO_ENDPOINTS and name not in self._radio_map and name not in self._endpoint_handlers:
            logger.warning('BridgeEngine: alias miss for %r - not in CHANNEL_ALIASES or radio_map', name)
        return resolved

    def _is_duplicate(self, data: bytes) -> bool:
        """Check if a packet is a duplicate using stable payload hash.

        Uses _stable_hash() which hashes header byte + payload (excluding
        path data that changes per hop), so the same message at different
        hop counts produces the same dedup key.
        """
        now = time.monotonic()
        key = _stable_hash(data, length=24)  # longer hash for dedup accuracy
        # Periodic cleanup: only rebuild dict every 5 seconds
        if now - getattr(self, '_seen_cleanup_ts', 0) > 5.0:
            self._seen = {k: v for k, v in self._seen.items() if now - v < self.dedup_ttl}
            self._seen_cleanup_ts = now
        if key in self._seen:
            return True
        self._seen[key] = now
        return False

    def _is_tx_echo(self, data: bytes, source: str) -> bool:
        """Check if an incoming packet is a TX echo (our own transmission bounced back).

        Compares the stable hash of the incoming packet against recently
        transmitted packets.  Returns True if it's an echo that should be dropped.
        """
        now = time.monotonic()
        key = _stable_hash(data)
        # Periodic cleanup of expired TX echo entries
        if now - self._tx_echo_cleanup_ts > 5.0:
            self._tx_echo_hashes = {
                k: v for k, v in self._tx_echo_hashes.items()
                if now - v < self._tx_echo_ttl
            }
            self._tx_echo_cleanup_ts = now
        if key in self._tx_echo_hashes:
            age = now - self._tx_echo_hashes[key]
            if age < self._tx_echo_ttl:
                self._tx_echo_detected += 1
                logger.warning('[TX_ECHO] Dropped echo on %s (hash=%s, age=%.1fs, total=%d)',
                               source, key, age, self._tx_echo_detected)
                self._record_dedup_event('tx_echo', source, key, len(data),
                                         self._get_packet_type_name(data))
                return True
            else:
                del self._tx_echo_hashes[key]
        return False

    def _store_tx_echo_hash(self, data: bytes) -> None:
        """Store the stable hash of a transmitted packet for echo detection."""
        key = _stable_hash(data)
        self._tx_echo_hashes[key] = time.monotonic()
        logger.debug('BridgeEngine: TX echo hash stored: %s (active=%d)',
                     key, len(self._tx_echo_hashes))


    def set_sqlite_handler(self, handler) -> None:
        """Set the SQLiteHandler for persistent dedup event storage."""
        self._sqlite_handler = handler
        if handler is not None and not self._sqlite_writer_running:
            self._sqlite_writer_running = True
            self._sqlite_writer_thread = threading.Thread(
                target=self._sqlite_dedup_writer, daemon=True,
                name="dedup-sqlite-writer")
            self._sqlite_writer_thread.start()
            logger.info("BridgeEngine: SQLite dedup writer thread started")

    def _sqlite_dedup_writer(self) -> None:
        """Background thread: batch-write dedup events to SQLite."""
        batch: list = []
        while self._sqlite_writer_running:
            try:
                # Drain queue with timeout
                try:
                    evt = self._dedup_queue.get(timeout=2.0)
                    batch.append(evt)
                    # Drain any remaining items without blocking
                    while not self._dedup_queue.empty():
                        try:
                            batch.append(self._dedup_queue.get_nowait())
                        except queue.Empty:
                            break
                except queue.Empty:
                    pass

                # Write batch to SQLite
                if batch and self._sqlite_handler is not None:
                    try:
                        self._sqlite_handler.store_dedup_events_batch(batch)
                    except Exception as e:
                        logger.error("BridgeEngine: SQLite dedup write error: %s", e)
                    batch = []

                # Cleanup moved to metrics_retention.py
                # now = time.time()
                # if now - self._last_cleanup_ts > self._cleanup_interval:
                #     self._last_cleanup_ts = now
                #     if self._sqlite_handler is not None:
                #         try:
                #             deleted = self._sqlite_handler.cleanup_dedup_events(max_age_days=7)
                #             if deleted:
                #                 logger.info("BridgeEngine: dedup cleanup removed %d old events", deleted)
                #         except Exception as e:
                #             logger.error("BridgeEngine: dedup cleanup error: %s", e)

            except Exception as e:
                logger.error("BridgeEngine: dedup writer loop error: %s", e)
                time.sleep(1.0)

        # Flush remaining on shutdown
        if batch and self._sqlite_handler is not None:
            try:
                self._sqlite_handler.store_dedup_events_batch(batch)
            except Exception:
                pass

    def _record_dedup_event(self, event_type: str, source_channel: str,
                            pkt_hash: str, pkt_size: int = 0,
                            pkt_type: str = '') -> None:
        """Record a dedup detection event for visualization."""
        self._dedup_events.append({
            'ts': time.time(),
            'type': event_type,
            'src': source_channel,
            'hash': pkt_hash[:12],
            'size': pkt_size,
            'pkt_type': pkt_type,
        })
        # Queue for SQLite persistence (non-blocking)
        if self._sqlite_handler is not None:
            try:
                self._dedup_queue.put_nowait({
                    'ts': time.time(),
                    'event_type': event_type,
                    'source': source_channel,
                    'pkt_hash': pkt_hash[:12],
                    'pkt_size': pkt_size,
                    'pkt_type': pkt_type,
                })
            except queue.Full:
                pass  # drop if queue is full (should not happen)

    def get_dedup_events(self, since: float = 0, limit: int = 100) -> list:
        """Return recent dedup events, optionally filtered by timestamp."""
        events = list(self._dedup_events)
        if since > 0:
            events = [e for e in events if e['ts'] >= since]
        return events[-limit:]

    @staticmethod
    def _parse_path_info(data: bytes) -> dict:
        """Extract path hashes, src_hash, dst_hash from raw MeshCore packet bytes.

        Returns a dict with keys: path_hashes (list[str]), path_hash_size (int),
        path_hash (str|None), src_hash (str|None), dst_hash (str|None),
        original_path (list[str]|None).
        """
        result = {
            'path_hashes': [],
            'path_hash_size': 0,
            'path_hash': None,
            'src_hash': None,
            'dst_hash': None,
            'original_path': None,
        }
        if not data or len(data) < 2:
            return result

        hdr = data[0]
        rt = hdr & 0x03
        pt = (hdr >> 2) & 0x0F
        has_tc = rt in (0x00, 0x03)  # TFLOOD/TDIRECT have transport codes
        idx = 5 if has_tc else 1  # offset to path_raw byte
        if idx >= len(data):
            return result

        path_raw = data[idx]
        hops = path_raw & 0x3F
        hsz = ((path_raw >> 6) & 0x03) + 1  # hash size 1-3 bytes
        result['path_hash_size'] = hsz

        # Extract individual path hashes
        path_hashes = []
        for i in range(hops):
            start = idx + 1 + i * hsz
            end = start + hsz
            if end <= len(data):
                path_hashes.append(data[start:end].hex().upper())
        result['path_hashes'] = path_hashes
        result['original_path'] = path_hashes if path_hashes else None

        # Build display string like engine.py _path_hash_display
        if path_hashes:
            display = path_hashes[:8]
            if len(path_hashes) > 8:
                display = list(display) + ['...']
            result['path_hash'] = '[' + ', '.join(display) + ']'

        # Extract src_hash and dst_hash from payload
        pbytes = hops * hsz
        payload_start = idx + 1 + pbytes
        if payload_start < len(data):
            payload = data[payload_start:]
            if pt in (0x00, 0x01, 0x02, 0x08):  # REQ, RESP, TXT, PATH
                if len(payload) >= 2:
                    result['dst_hash'] = f'{payload[0]:02X}'
                    result['src_hash'] = f'{payload[1]:02X}'
            elif pt == 0x04:  # ADVERT
                if len(payload) >= 1:
                    result['src_hash'] = f'{payload[0]:02X}'
            elif pt == 0x07:  # ANON_REQ
                if len(payload) >= 1:
                    result['dst_hash'] = f'{payload[0]:02X}'
            elif pt in (0x05, 0x06):  # GRP_TXT, GRP_DATA
                # Group messages: first byte is channel_hash (identifies the
                # target group/channel). Sender identity is encrypted inside
                # the payload and is NOT available in cleartext.
                if len(payload) >= 1:
                    result['dst_hash'] = f'{payload[0]:02X}'
                    # src_hash stays None (encrypted, not extractable)

        return result


    def _update_repeater_counters(self, data: bytes, channel_id: str,
                                    rssi: int = -120, snr: float = 0.0,
                                    pkt_type_name: str = 'UNKNOWN',
                                    pkt_hash: str = '',
                                    was_forwarded: bool = False,
                                    was_duplicate: bool = False,
                                    was_echo: bool = False,
                                    drop_reason: str | None = None) -> None:
        """Update RepeaterHandler counters and recent_packets from bridge traffic."""
        eng = self._repeater_engine
        if not eng:
            return
        try:
            eng.rx_count += 1
            if was_forwarded:
                eng.forwarded_count += 1
            elif was_duplicate or was_echo or drop_reason:
                eng.dropped_count += 1

            # Build minimal packet_record compatible with dashboard
            packet_record = {
                'timestamp': time.time(),
                'header': f'0x{data[0]:02X}' if data else None,
                'payload': data.hex() if data else None,
                'payload_length': len(data) if data else 0,
                'type': self._get_packet_type(data) if data else None,
                'route': (data[0] & 0x03) if data else None,
                'length': len(data) if data else 0,
                'rssi': rssi,
                'snr': snr,
                'score': 0,
                'tx_delay_ms': 0,
                'transmitted': was_forwarded,
                'is_duplicate': was_duplicate,
                'packet_hash': pkt_hash[:16] if pkt_hash else '',
                'drop_reason': drop_reason,
                'raw_packet': data.hex() if data else None,
                'bridge_source': channel_id,
                'bridge_pkt_type': pkt_type_name,
            }

            # Parse path hashes from raw packet data
            _pi = self._parse_path_info(data)
            packet_record['path_hash'] = _pi['path_hash']
            packet_record['src_hash'] = _pi['src_hash']
            packet_record['dst_hash'] = _pi['dst_hash']
            packet_record['original_path'] = _pi['original_path']
            packet_record['forwarded_path'] = None
            packet_record['path_hash_size'] = _pi['path_hash_size']

            # Handle duplicates: attach to original if found
            if was_duplicate and len(eng.recent_packets) > 0:
                for idx in range(len(eng.recent_packets) - 1, -1, -1):
                    prev_pkt = eng.recent_packets[idx]
                    if prev_pkt.get('packet_hash') == packet_record['packet_hash']:
                        if 'duplicates' not in prev_pkt:
                            prev_pkt['duplicates'] = []
                        max_dup = getattr(eng, 'max_duplicates_per_packet', 10)
                        if len(prev_pkt['duplicates']) < max_dup:
                            prev_pkt['duplicates'].append(packet_record)
                        # Store duplicate to SQLite for dashboard
                        if hasattr(eng, 'storage') and eng.storage:
                            try:
                                eng.storage.record_packet(packet_record)
                            except Exception:
                                pass
                        return

            eng.recent_packets.append(packet_record)
            max_recent = getattr(eng, 'max_recent_packets', 50)
            if len(eng.recent_packets) > max_recent:
                eng.recent_packets.pop(0)

            # Store to persistent SQLite storage for dashboard API
            if hasattr(eng, 'storage') and eng.storage:
                try:
                    eng.storage.record_packet(packet_record)
                except Exception as store_err:
                    logger.debug('BridgeEngine: failed to store packet to SQLite: %s', store_err)
        except Exception as e:
            logger.error('BridgeEngine: error updating repeater counters: %s', e)

    async def _rx_loop(self, source) -> None:
        cid = getattr(source, 'channel_id', 'unknown')
        logger.info('BridgeEngine: RX loop started for channel %s', cid)
        while self._running:
            try:
                data = await asyncio.wait_for(source.wait_for_rx(), timeout=5.0)
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error('BridgeEngine: RX error on %s: %s', cid, e)
                await asyncio.sleep(1.0)
                continue
            # Collect RF metadata from the source radio for the `received` step.
            # All getters are optional — missing fields are simply omitted.
            _rx_rssi = None
            _rx_snr = None
            _rx_nf = None
            try:
                if hasattr(source, 'get_last_rssi'):
                    _rx_rssi = source.get_last_rssi()
                if hasattr(source, 'get_last_snr'):
                    _rx_snr = source.get_last_snr()
                if hasattr(source, 'get_noise_floor'):
                    _rx_nf = source.get_noise_floor()
            except Exception:
                pass

            # NOTE: Raw RX callbacks for companion frame servers are now
            # fired from WM1303Backend._process_rx_packet BEFORE this code
            # is reached, so companions receive ALL RF packets including
            # TX echoes and duplicates.  Do NOT call _fire_raw_rx_callbacks
            # here to avoid double-pushing non-echo packets.

            # TX echo check: drop our own transmissions bouncing back
            if self._is_tx_echo(data, cid):
                _echo_hash = _stable_hash(data, 8)
                self.emit_received_trace(_echo_hash, cid, self._get_packet_type_name(data), len(data), rssi=_rx_rssi, snr=_rx_snr, noise_floor=_rx_nf)
                _trace(_echo_hash, 'echo_dedup', channel=cid, detail='Duplicate or echo detected (from %s)' % self._dn(cid), status='ok')
                self.dropped_duplicate += 1
                self._update_repeater_counters(
                    data, cid, pkt_type_name=self._get_packet_type_name(data),
                    pkt_hash=_stable_hash(data), was_echo=True,
                    drop_reason="tx_echo",
                    rssi=int(_rx_rssi) if _rx_rssi is not None else -120,
                    snr=float(_rx_snr) if _rx_snr is not None else 0.0)
                continue

            if self._is_duplicate(data):
                _dup_hash8 = _stable_hash(data, 8)
                _dup_hash = _stable_hash(data)
                self.emit_received_trace(_dup_hash8, cid, self._get_packet_type_name(data), len(data), rssi=_rx_rssi, snr=_rx_snr, noise_floor=_rx_nf)
                _trace(_dup_hash8, 'dedup_drop', channel=cid, detail='Dropped as duplicate on %s' % self._dn(cid), status='warning')
                self.dropped_duplicate += 1
                self._record_dedup_event('duplicate', cid, _dup_hash, len(data), self._get_packet_type_name(data))
                logger.debug('BridgeEngine: duplicate dropped on %s (hash=%s)',
                            cid, _dup_hash)
                # Update repeater engine: duplicate drop
                self._update_repeater_counters(
                    data, cid, pkt_type_name=self._get_packet_type_name(data),
                    pkt_hash=_dup_hash, was_duplicate=True,
                    drop_reason="duplicate",
                    rssi=int(_rx_rssi) if _rx_rssi is not None else -120,
                    snr=float(_rx_snr) if _rx_snr is not None else 0.0)
                continue

            # Dedup check passed — packet is new
            _pass_hash8 = _stable_hash(data, 8)
            pkt_hash = _stable_hash(data)
            pkt_hash8 = _stable_hash(data, 8)
            pkt_type_name = self._get_packet_type_name(data)

            # Emit `received` BEFORE `dedup_check` to match channel_e ordering
            self.emit_received_trace(pkt_hash8, cid, pkt_type_name, len(data), rssi=_rx_rssi, snr=_rx_snr, noise_floor=_rx_nf)
            _trace(_pass_hash8, 'dedup_check', channel=cid, detail='Dedup check passed on %s' % self._dn(cid), status='ok')

            # Emit `bridge_inject` to match channel_e trace flow (received -> dedup_check -> bridge_inject)
            _trace(pkt_hash8, 'bridge_inject', channel=cid, pkt_type=pkt_type_name, detail='Injected %d bytes from %s' % (len(data), self._dn(cid)))

            logger.info('BridgeEngine: RX %d bytes on %s (type=%s, hash=%s)',
                       len(data), cid, pkt_type_name, pkt_hash)
            logger.info('[HEXDUMP] dir=RX ch=%s sz=%d hdr=%s hex=%s', cid, len(data), _mc_hdr(data), _hexdump(data))

            # Store per-packet RX metric for spectrum-tab charts (Option B).
            # Derive hop count from MeshCore header (byte 0) + path_raw (byte 1 or 5 if timestamped).
            try:
                _hop = None
                if data and len(data) >= 2:
                    _hdr_byte = data[0]
                    _rt = _hdr_byte & 0x03
                    _has_tc = _rt in (0x00, 0x03)
                    _idx = 5 if _has_tc else 1
                    if _idx < len(data):
                        _hop = data[_idx] & 0x3F
                if self._sqlite_handler is None:
                    logger.warning('packet_metric RX skipped: sqlite_handler is None on %s', cid)
                else:
                    _pm_rec = {
                        'timestamp': time.time(),
                        'channel_id': str(cid) if cid is not None else '',
                        'direction': 'rx',
                        'length': int(len(data)),
                        'hop_count': int(_hop) if _hop is not None else None,
                        'crc_ok': True,
                        'rssi': float(_rx_rssi) if _rx_rssi is not None else None,
                        'snr': float(_rx_snr) if _rx_snr is not None else None,
                        'pkt_hash': (pkt_hash[:16] if isinstance(pkt_hash, str) else None),
                    }
                    self._sqlite_handler.store_packet_metric(_pm_rec)
                    logger.debug('packet_metric RX stored: ch=%s len=%d rssi=%s', cid, len(data), _rx_rssi)
            except Exception as _pm_e:
                logger.warning('packet_metric RX store failed on %s: %s', cid, _pm_e, exc_info=True)

            # Snapshot forwarded counter for repeater engine tracking
            _fwd_before = self.forwarded_packets

            # --- Rule-based forwarding ---
            if self.rules:
                await self._forward_by_rules(cid, data, pkt_hash, pkt_type_name,
                                             rssi=_rx_rssi, snr=_rx_snr)
            else:
                # Fallback: no rules configured -> forward to all other radios
                for target in self.radios:
                    if target is source:
                        continue
                    tcid = getattr(target, 'channel_id', 'unknown')
                    logger.info('BridgeEngine: forwarding %d bytes (no rules): '
                               '%s -> %s (type=%s, hash=%s)',
                               len(data), cid, tcid, pkt_type_name, pkt_hash)
                    try:
                        if self.tx_delay > 0:
                            await asyncio.sleep(self.tx_delay)
                        await target.send(data)
                        self.forwarded_packets += 1
                    except Exception as e:
                        logger.error('BridgeEngine: TX error to %s: %s', tcid, e)

            # --- Update RepeaterHandler counters ---
            _fwd_after = self.forwarded_packets
            _was_forwarded = (_fwd_after > _fwd_before)
            self._update_repeater_counters(
                data, cid, pkt_type_name=pkt_type_name,
                pkt_hash=pkt_hash, was_forwarded=_was_forwarded,
                drop_reason=None if _was_forwarded else "no_rule_match",
                rssi=int(_rx_rssi) if _rx_rssi is not None else -120,
                snr=float(_rx_snr) if _rx_snr is not None else 0.0)

    async def run(self) -> None:
        self._running = True
        self._stop_event = asyncio.Event()
        logger.info('BridgeEngine: starting with %d channels, %d rules',
                    len(self.radios), len(self.rules))
        tasks = [asyncio.create_task(self._rx_loop(r)) for r in self.radios]
        try:
            if tasks:
                await asyncio.gather(*tasks)
            else:
                # No A-D radios but channel_e/f may inject packets via
                # inject_packet().  Keep the engine alive until stop().
                logger.info('BridgeEngine: no radio RX loops, idling '
                            '(channel_e/f inject via callbacks)')
                await self._stop_event.wait()
        except asyncio.CancelledError:
            pass
        finally:
            self._running = False
            for t in tasks:
                t.cancel()

    def stop(self) -> None:
        self._running = False
        if hasattr(self, '_stop_event') and self._stop_event:
            self._stop_event.set()

    def update_rules(self, new_rules: list) -> None:
        """Hot-reload bridge rules without restarting the engine."""
        old_count = len(self.rules)
        self.rules = new_rules or []
        # Rebuild reverse aliases in case aliases changed
        self._build_reverse_aliases()
        logger.info('BridgeEngine: rules hot-reloaded: %d -> %d rules',
                    old_count, len(self.rules))
        for rule in self.rules:
            rsrc = rule.get('source', '?')
            rtgt = rule.get('target', '?')
            logger.info('BridgeEngine: rule %s: %s -> %s (enabled=%s)',
                       rule.get('name', rule.get('id', '?')), rsrc, rtgt, rule.get('enabled', True))
        logger.info('BridgeEngine: current aliases: %s', dict(self.CHANNEL_ALIASES))
        logger.info('BridgeEngine: reverse aliases: %s',
                    {k: sorted(v) for k, v in self._reverse_aliases.items()})

    def get_stats(self) -> dict:
        return {
            'forwarded_packets': self.forwarded_packets,
            'dropped_duplicate': self.dropped_duplicate,
            'dropped_filtered': self.dropped_filtered,
            'tx_echo_detected': self._tx_echo_detected,
            'tx_echo_hashes_active': len(self._tx_echo_hashes),
            'dedup_events_buffered': len(self._dedup_events),
            'dedup_seen_active': len(self._seen),
            'channels': [getattr(r, 'channel_id', str(i)) for i, r in enumerate(self.radios)],
            'rules': self.rules,
        }

    def get_origin_counts(self) -> dict:
        """Return current origin channel counts without resetting (for live display)."""
        with self._origin_channel_lock:
            return dict(self._origin_channel_counts)

    def get_and_reset_origin_counts(self) -> dict:
        """Return current origin channel counts and reset them to zero."""
        with self._origin_channel_lock:
            counts = dict(self._origin_channel_counts)
            self._origin_channel_counts.clear()
            return counts
