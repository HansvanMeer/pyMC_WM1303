"""Packet Trace Collector for WM1303.

Provides an in-memory ring buffer that collects per-packet trace events,
grouped by packet hash.  Each trace captures the step-by-step journey
of a packet through the bridge engine (RX -> dedup -> forward -> TX).

When the same packet hash reappears after a long gap (>GAP_THRESHOLD),
a new trace entry is created to avoid misleading multi-minute total
durations caused by mesh-delayed duplicates.

Thread-safe: all mutations are protected by a lock.
"""
from __future__ import annotations

import logging
import threading
import time
from collections import OrderedDict
from datetime import datetime, timezone

logger = logging.getLogger('PacketTrace')

_collector: TraceCollector | None = None

MAX_TRACES = 1000  # v2.4.7+: raised from 200 to support all-channel tracing (channel_a/b/c/d/e)
MAX_STEPS_PER_TRACE = 30
TRACE_TTL = 300       # seconds before a trace is considered stale for grouping
GAP_THRESHOLD = 450.0  # seconds — start new trace if gap since last event exceeds this

# v2.4.7+: Global mapping of channel IDs (e.g. 'channel_e') to UI-friendly
# names (e.g. 'EU-Narrow'). Populated at startup from the radio's
# `_ch_id_to_ui_name` dict so that every trace_event() call emits the same
# friendly channel name regardless of which module produced the trace.
# Callers may still pass a friendly name directly; passthrough is the default
# when the lookup misses.
_CHANNEL_FRIENDLY_NAMES: dict[str, str] = {}


def set_channel_name_map(mapping: dict[str, str]) -> None:
    """Install/overwrite the channel-id → friendly-name map used by
    `trace_event` to normalize the `channel` field. Idempotent."""
    global _CHANNEL_FRIENDLY_NAMES
    try:
        _CHANNEL_FRIENDLY_NAMES = {str(k): str(v) for k, v in (mapping or {}).items() if v}
        logger.info('Channel name map installed: %s', _CHANNEL_FRIENDLY_NAMES)
    except Exception as _e:
        logger.debug('set_channel_name_map failed: %s', _e)


def _friendly_channel(name: str) -> str:
    """Resolve a channel id (e.g. 'channel_e') to its friendly UI name
    (e.g. 'EU-Narrow'). Returns the input unchanged when no mapping exists."""
    if not name:
        return name
    return _CHANNEL_FRIENDLY_NAMES.get(name, name)


class TraceCollector:
    """Thread-safe in-memory ring buffer for packet traces."""

    def __init__(self, maxlen: int = MAX_TRACES):
        self._lock = threading.Lock()
        # OrderedDict keyed by lookup_key for fast lookup + insertion order.
        # lookup_key = pkt_hash or pkt_hash~N for duplicate arrivals.
        self._traces: OrderedDict[str, dict] = OrderedDict()
        self._maxlen = maxlen
        # Track the current active lookup key for each base pkt_hash
        self._active_key: dict[str, str] = {}  # pkt_hash -> current lookup_key
        self._seq: dict[str, int] = {}  # pkt_hash -> sequence counter

    def trace_event(self, pkt_hash: str, step_name: str,
                    channel: str = '', pkt_type: str = '',
                    detail: str = '', status: str = 'ok',
                    ts_offset_ms: float = 0,
                    delta_ms: float | None = None,
                    # v2.4.7+: optional MeshCore packet metadata. Attached once
                    # per trace; later events may backfill missing fields.
                    pkt_hash_full: str | None = None,
                    hops: int | None = None,
                    hash_size: int | None = None,
                    src_hash: str | None = None,
                    dst_hash: str | None = None,
                    path_hashes: list | None = None,
                    payload_hex: str | None = None,
                    payload_full_size: int | None = None) -> None:
        """Record a trace step for the given packet hash.

        If a trace for this hash already exists (within TTL and without
        a big gap), the step is appended; otherwise a new trace entry is
        created. The `channel` argument is normalised through the global
        friendly-name map so callers may pass either a channel id (e.g.
        'channel_e') or a friendly name (e.g. 'EU-Narrow') and the trace
        consistently shows the friendly name.
        """
        # v2.4.7+: normalise channel id -> friendly UI name (no-op when the
        # caller already passed a friendly name or no mapping exists).
        channel = _friendly_channel(channel)

        # ts_offset_ms: if positive, backdate the step display time and
        # elapsed_ms by this amount.  Internal tracking (gap detection,
        # TTL, _mono_last) uses real time.
        real_mono = time.monotonic()
        real_ts   = time.time()
        # Display time may be backdated
        display_ts   = real_ts - (ts_offset_ms / 1000.0)
        display_mono = real_mono - (ts_offset_ms / 1000.0)
        iso_now = datetime.fromtimestamp(display_ts, tz=timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3] + 'Z'

        with self._lock:
            # Find the current active trace for this base hash
            lookup_key = self._active_key.get(pkt_hash, pkt_hash)
            trace = self._traces.get(lookup_key)

            # Check if existing trace is stale (old packet with same hash)
            if trace and (real_mono - trace['_mono_first']) > TRACE_TTL:
                trace = None

            # Check for large gap since last event — start new trace
            if trace and (real_mono - trace['_mono_last']) > GAP_THRESHOLD:
                trace = None

            if trace is None:
                # Determine a unique lookup key
                seq = self._seq.get(pkt_hash, 0)
                if pkt_hash in self._active_key or pkt_hash in self._traces:
                    seq += 1
                self._seq[pkt_hash] = seq
                if seq == 0:
                    lookup_key = pkt_hash
                else:
                    lookup_key = f"{pkt_hash}~{seq}"
                self._active_key[pkt_hash] = lookup_key

                # New trace entry
                trace = {
                    'pkt_hash': pkt_hash,  # always the original hash for display
                    'first_seen': iso_now,
                    'channel': channel,
                    'type': pkt_type,
                    'status': 'ok',
                    'total_ms': 0.0,
                    'steps': [],
                    '_mono_first': display_mono,
                    '_mono_last': real_mono,
                    '_ts_first': display_ts,
                    # v2.4.7+: MeshCore packet metadata (optional)
                    'pkt_hash_full': pkt_hash_full,
                    'hops': hops,
                    'hash_size': hash_size,
                    'src_hash': src_hash,
                    'dst_hash': dst_hash,
                    'path_hashes': path_hashes or [],
                    'payload_hex': payload_hex,
                    'payload_full_size': payload_full_size,
                }
                # Evict oldest if at capacity
                if len(self._traces) >= self._maxlen:
                    evicted_key, _ = self._traces.popitem(last=False)
                    # Clean up _active_key if it points to the evicted key
                    for bh, ak in list(self._active_key.items()):
                        if ak == evicted_key:
                            del self._active_key[bh]
                            break
                self._traces[lookup_key] = trace
            else:
                # Move to end (most recent)
                self._traces.move_to_end(lookup_key)

            # Update last-event timestamp
            trace['_mono_last'] = real_mono

            # Update trace metadata
            if channel and not trace['channel']:
                trace['channel'] = channel
            if pkt_type and not trace['type']:
                trace['type'] = pkt_type

            # v2.4.7+: backfill MeshCore metadata if earlier event didn't have it
            if pkt_hash_full and not trace.get('pkt_hash_full'):
                trace['pkt_hash_full'] = pkt_hash_full
            if hops is not None and not trace.get('hops'):
                trace['hops'] = hops
            if hash_size is not None and not trace.get('hash_size'):
                trace['hash_size'] = hash_size
            if src_hash and not trace.get('src_hash'):
                trace['src_hash'] = src_hash
            if dst_hash and not trace.get('dst_hash'):
                trace['dst_hash'] = dst_hash
            if path_hashes and not trace.get('path_hashes'):
                trace['path_hashes'] = path_hashes
            if payload_hex and not trace.get('payload_hex'):
                trace['payload_hex'] = payload_hex
            if payload_full_size is not None and not trace.get('payload_full_size'):
                trace['payload_full_size'] = payload_full_size

            # Compute elapsed from first step
            elapsed_ms = round((display_mono - trace['_mono_first']) * 1000, 1)

            # Add step (cap at MAX_STEPS_PER_TRACE)
            if len(trace['steps']) < MAX_STEPS_PER_TRACE:
                _step = {
                    'name': step_name,
                    'time': iso_now,
                    'elapsed_ms': elapsed_ms,
                    'detail': detail,
                    'status': status,
                }
                # Authoritative measured duration for this step, supplied by
                # the caller when it knows the real value better than a
                # display-time reconstruction (e.g. the HAL-reported CAD scan
                # duration, which back-date clamping can shorten). Consumers
                # must prefer this over any elapsed_ms difference.
                if delta_ms is not None:
                    try:
                        _step['delta_ms'] = round(float(delta_ms), 1)
                    except (TypeError, ValueError):
                        pass
                # Chronological insert. Back-dated events (ts_offset_ms > 0)
                # are emitted AFTER events that happened later in real time,
                # so a blind append can leave trace['steps'] out of order and
                # every index-based consumer (phase grouping, delta
                # computation, playback, export) would derive wrong timings.
                # Stable: events with an identical elapsed_ms keep their
                # emission order.
                _steps = trace['steps']
                _pos = len(_steps)
                while (_pos > 0 and
                       _steps[_pos - 1].get('elapsed_ms', 0.0) > elapsed_ms):
                    _pos -= 1
                _steps.insert(_pos, _step)

            # Update total_ms and overall status. Highest elapsed_ms wins: a
            # back-dated event emitted last must never shrink the total span.
            if elapsed_ms > float(trace.get('total_ms', 0.0) or 0.0):
                trace['total_ms'] = elapsed_ms

            # Track TX success/failure counts for status computation
            if 'tx_ok' not in trace:
                trace['tx_ok'] = 0
                trace['tx_fail'] = 0
                trace['has_critical'] = False

            # echo_dedup is normal healthy behavior — does NOT affect status
            if step_name == 'echo_dedup':
                pass  # status stays 'ok'
            elif step_name == 'dedup_drop':
                trace['status'] = 'dedup'
            elif step_name == 'filter_drop':
                trace['status'] = 'filtered'
            elif step_name == 'tx_send':
                if status == 'error':
                    trace['tx_fail'] += 1
                elif status == 'ok':
                    trace['tx_ok'] += 1
            elif status == 'error':
                trace['has_critical'] = True

            # Compute overall status from TX success/failure ratio
            # Only override if not already set to a specific category (dedup/filtered)
            if trace['status'] not in ('dedup', 'filtered'):
                if trace['has_critical'] or (trace['tx_fail'] > 0 and trace['tx_ok'] == 0):
                    trace['status'] = 'error'
                elif trace['tx_fail'] > 0 and trace['tx_ok'] > 0:
                    trace['status'] = 'partial'
                # else stays 'ok'

    def get_traces(self, limit: int = 50, status: str = '',
                   channel: str = '') -> list[dict]:
        """Return recent traces, newest first, with optional filters."""
        with self._lock:
            traces = list(self._traces.values())

        # Filter
        if status:
            traces = [t for t in traces if t.get('status') == status]
        if channel:
            traces = [t for t in traces if t.get('channel') == channel]

        # Reverse for newest-first, apply limit
        traces = list(reversed(traces))[:limit]

        # Strip internal fields before returning
        result = []
        for t in traces:
            entry = {
                'pkt_hash': t['pkt_hash'],
                'first_seen': t['first_seen'],
                'channel': t['channel'],
                'type': t['type'],
                'status': t['status'],
                'total_ms': t['total_ms'],
                'steps': t['steps'],
                # v2.4.7+: MeshCore packet metadata
                'pkt_hash_full': t.get('pkt_hash_full'),
                'hops': t.get('hops'),
                'hash_size': t.get('hash_size'),
                'src_hash': t.get('src_hash'),
                'dst_hash': t.get('dst_hash'),
                'path_hashes': t.get('path_hashes') or [],
                'payload_hex': t.get('payload_hex'),
                'payload_full_size': t.get('payload_full_size'),
            }
            result.append(entry)
        return result

    def clear(self) -> None:
        """Clear all traces."""
        with self._lock:
            self._traces.clear()
            self._active_key.clear()
            self._seq.clear()


def get_collector() -> TraceCollector:
    """Get or create the module-level singleton TraceCollector."""
    global _collector
    if _collector is None:
        _collector = TraceCollector(maxlen=MAX_TRACES)
        logger.info('PacketTrace: collector initialized (maxlen=%d, gap_threshold=%.0fs)', MAX_TRACES, GAP_THRESHOLD)
    return _collector


def trace_event(pkt_hash: str, step_name: str, **kwargs) -> None:
    """Convenience wrapper: record a trace event on the global collector."""
    get_collector().trace_event(pkt_hash, step_name, **kwargs)


def get_traces(**kwargs) -> list[dict]:
    """Convenience wrapper: retrieve traces from the global collector."""
    return get_collector().get_traces(**kwargs)
