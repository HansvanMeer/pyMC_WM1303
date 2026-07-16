"""Channel F (chan_Lora_std on SX1302 RF0) Bridge Plugin for pymc-repeater.

Channel F is the SX1302 LoRa Service channel (chan_Lora_std). Unlike Channel E
(SX1261 separate radio chip with its own UDP path), Channel F packets arrive
through the SAME HAL packet_forwarder UDP port as channels A-D, because
chan_Lora_std physically lives on the same SX1302 silicon as chan_multiSF_0-3.

What this plugin does:
  * Registers a TX handler in BridgeEngine for the 'channel_f' endpoint so
    bridge rules with target=channel_f can route packets through the
    GlobalTXScheduler / TXQueueManager (parallel to channels A-D and E).
  * Registers an RX callback on WM1303Backend (`_channel_f_rx_callback`) so the
    backend can hand chan_Lora_std-classified packets back to us. We then
    inject them into BridgeEngine with origin_channel='channel_f' so bridge
    rules with source=channel_f fire correctly.

Channel F parameters (frequency, BW, SF) are read dynamically from
/etc/openhop_repeater/wm1303_ui.json (or legacy /etc/pymc_repeater/wm1303_ui.json) at runtime by the backend. This plugin only
needs the TX queue id and a friendly name resolver.

Usage (inside pymc-repeater main):
    from repeater.channel_f_bridge import ChannelFBridge
from openhop_core.paths import resolve_config_path  # WM1303 v2.7: central config-path helper
    ch_f = ChannelFBridge(bridge_engine, backend=radio)
    asyncio.create_task(ch_f.run())
"""
import asyncio
import json
import logging
from pathlib import Path
from repeater.bridge_engine import _stable_hash
from repeater.web.packet_trace import trace_event as _trace

logger = logging.getLogger(__name__)

CHANNEL_F_NAME = 'channel_f'
CHANNEL_F_TX_CHANNEL = 'channel_f'
UI_CONFIG_PATH = resolve_config_path('wm1303_ui.json')


def _load_channel_f_ui() -> dict:
    """Load channel_f settings from wm1303_ui.json."""
    try:
        if UI_CONFIG_PATH.exists():
            return json.loads(UI_CONFIG_PATH.read_text()).get('channel_f', {})
    except Exception as e:
        logger.warning('ChannelFBridge: failed to read UI config: %s', e)
    return {}


class ChannelFBridge:
    """Bridge plugin that hooks Channel F (chan_Lora_std) into BridgeEngine.

    No UDP listener: Channel F packets share the HAL pkt_fwd UDP port with
    channels A-D and are dispatched by WM1303Backend via the registered RX
    callback. The TX handler routes outgoing packets through the existing
    per-channel TXQueue managed by TXQueueManager / GlobalTXScheduler.
    """

    def __init__(self, bridge_engine, backend=None):
        self.bridge = bridge_engine
        self.backend = backend  # WM1303Backend for TX + RX callback
        self.packets_received = 0
        self.packets_injected = 0
        self.packets_errors = 0
        self.tx_packets = 0
        self.tx_errors = 0
        self._running = False
        self._loop = None  # stored in run() for cross-thread async injection

    async def _tx_handler(self, data: bytes):
        """Handle packets forwarded TO Channel F - TX via HAL chan_Lora_std.

        Routes through the SAME TXQueueManager / GlobalTXScheduler /
        _send_pull_resp path as all other channels, so by passing
        `trace_hash=pkt_hash8` into `queue.enqueue()` the backend emits the
        full set of enriched trace events (tx_noisefloor, cad_start,
        lbt_check, cad_check, rf_tx_start, rf_tx_end, sx1261_rx_restart,
        tx_ack) automatically. This makes channel_f traces visually identical
        to other channels in the Tracing UI.
        """
        pkt_hash8 = _stable_hash(data)[:8]
        try:
            _friendly = self.bridge._dn(CHANNEL_F_TX_CHANNEL) if self.bridge else CHANNEL_F_TX_CHANNEL
        except Exception:
            _friendly = CHANNEL_F_TX_CHANNEL
        if self.backend is None:
            logger.warning('Channel F TX: no backend, cannot send %d bytes', len(data))
            _trace(pkt_hash8, 'tx_send', channel=CHANNEL_F_TX_CHANNEL,
                   detail='TX FAILED on %s: no backend available' % _friendly,
                   status='error')
            return

        try:
            if hasattr(self.backend, '_tx_queue_manager') and self.backend._tx_queue_manager:
                queue = self.backend._tx_queue_manager.queues.get(CHANNEL_F_TX_CHANNEL)
                if queue:
                    result = await queue.enqueue(data, trace_hash=pkt_hash8)
                    if result.get('ok'):
                        self.tx_packets += 1
                        logger.info(
                            'Channel F TX: sent %d bytes via HAL '
                            '(send=%.1fms, airtime=%.1fms)',
                            len(data),
                            result.get('send_ms', 0),
                            result.get('airtime_ms', 0)
                        )
                        try:
                            _detail = self.bridge._format_tx_result(
                                result, _friendly, 'Repeater \u2192 %s' % _friendly)
                        except Exception:
                            _send_ms = result.get('send_ms', 0)
                            _airtime_ms = result.get('airtime_ms', 0)
                            _queue_wait = result.get('queue_wait_ms', 0)
                            _detail = ('TX on %s\n  Send: %.1fms\n  Airtime: %.1fms'
                                       '\n  Queue wait: %.1fms' % (
                                           _friendly, _send_ms, _airtime_ms, _queue_wait))
                        _trace(pkt_hash8, 'tx_send', channel=CHANNEL_F_TX_CHANNEL,
                               detail=_detail,
                               status='ok')
                        # Per-packet TX metric for spectrum-tab charts.
                        try:
                            _handler = getattr(self.bridge, '_sqlite_handler', None) if self.bridge else None
                            if _handler is not None:
                                import time as _t
                                _handler.store_packet_metric({
                                    'timestamp': _t.time(),
                                    'channel_id': str(CHANNEL_F_TX_CHANNEL),
                                    'direction': 'tx',
                                    'length': int(len(data)),
                                    'hop_count': None,
                                    'crc_ok': True,
                                    'airtime_ms': float(result.get('airtime_ms', 0) or 0),
                                    'wait_time_ms': float(result.get('queue_wait_ms', 0) or 0),
                                    'pkt_hash': pkt_hash8 if isinstance(pkt_hash8, str) else None,
                                })
                        except Exception as _pm_e:
                            logger.warning('packet_metric TX(ch_f) store failed: %s', _pm_e)
                    else:
                        self.tx_errors += 1
                        logger.warning(
                            'Channel F TX FAIL: %s (%d bytes)',
                            result.get('error', 'unknown'), len(data)
                        )
                        _trace(pkt_hash8, 'tx_send', channel=CHANNEL_F_TX_CHANNEL,
                               detail='TX FAILED on %s: %s' % (_friendly, result.get('error', 'unknown')),
                               status='error')
                    return
                else:
                    logger.warning('Channel F TX: no channel_f queue found in TXQueueManager')
            else:
                logger.warning('Channel F TX: no TXQueueManager on backend')

            # Fallback: backend.send() with UI-configured tx_power
            _ui = _load_channel_f_ui()
            _tx_power = int(_ui.get('tx_power', 14))
            meta = await self.backend.send(CHANNEL_F_TX_CHANNEL, data, tx_power=_tx_power, trace_hash=pkt_hash8)
            self.tx_packets += 1
            logger.info('Channel F TX: sent %d bytes via backend.send() (tx_power=%d)',
                       len(data), _tx_power)
            _trace(pkt_hash8, 'tx_send', channel=CHANNEL_F_TX_CHANNEL,
                   detail='TX on %s via backend.send() (tx_power=%d dBm, %d bytes)' % (_friendly, _tx_power, len(data)),
                   status='ok')
        except Exception as e:
            self.tx_errors += 1
            logger.error('Channel F TX error: %s', e)
            _trace(pkt_hash8, 'tx_send', channel=CHANNEL_F_TX_CHANNEL,
                   detail='TX FAILED on %s: %s' % (_friendly, e),
                   status='error')

    def _rx_from_backend(self, payload, rssi=0, snr=0.0):
        """Callback invoked by WM1303Backend when a chan_Lora_std packet is matched.

        Channel F shares the HAL pkt_fwd UDP port with channels A-D. The
        backend RX dispatcher first tries to match incoming packets against
        the multi-SF virtual radios (A-D), then channel_e (SX1261), and finally
        channel_f (this hook) — using frequency + bandwidth + spreading factor
        from wm1303_ui.json's channel_f section.
        """
        self.packets_received += 1
        pkt_hash = _stable_hash(payload)
        pkt_hash8 = pkt_hash[:8] if pkt_hash else _stable_hash(payload)[:8]
        logger.info("Channel F RX (via backend): %dB rssi=%d snr=%.1f hash=%s",
                    len(payload), rssi, snr, pkt_hash)
        # Emit a `received` trace step BEFORE inject_packet so it becomes the
        # first step in the Tracing UI for channel_f packets.
        try:
            if self.bridge is not None and hasattr(self.bridge, 'emit_received_trace'):
                _pt = 'UNKNOWN'
                try:
                    _pt = self.bridge._get_packet_type_name(payload)
                except Exception:
                    pass
                self.bridge.emit_received_trace(
                    pkt_hash8, CHANNEL_F_NAME, _pt, len(payload),
                    rssi=rssi, snr=snr)
        except Exception as _te:
            logger.debug('Channel F RX: received-trace emit failed: %s', _te)
        try:
            if self._loop is not None:
                self._loop.call_soon_threadsafe(
                    self._loop.create_task,
                    self.bridge.inject_packet(CHANNEL_F_NAME, payload, origin_channel='channel_f',
                                              rssi=rssi, snr=snr)
                )
                self.packets_injected += 1
            else:
                logger.warning("Channel F RX: no event loop stored, cannot inject")
        except Exception as e:
            self.packets_errors += 1
            logger.error("Channel F RX inject error: %s", e)

    async def _wait_for_bridge(self, timeout=30):
        """Wait for bridge_engine to be fully initialized."""
        for i in range(timeout * 10):
            if (self.bridge is not None
                    and hasattr(self.bridge, '_endpoint_handlers')
                    and self.bridge._endpoint_handlers is not None):
                return True
            await asyncio.sleep(0.1)
        return False

    async def run(self):
        """Register TX handler + RX callback. No UDP listener (shared with HAL)."""
        if not await self._wait_for_bridge():
            logger.error('Channel F: bridge engine not ready after timeout')
            return

        self.bridge._endpoint_handlers[CHANNEL_F_NAME] = self._tx_handler
        logger.info('Channel F: registered TX handler for %r (backend=%s)',
                    CHANNEL_F_NAME, type(self.backend).__name__ if self.backend else 'None')

        # Register RX callback with backend for chan_Lora_std packet matching
        if self.backend is not None:
            self.backend._channel_f_rx_callback = self._rx_from_backend
            logger.info('Channel F: registered RX callback with backend for chan_Lora_std matching')

        # Store event loop for cross-thread RX callback injection.
        self._loop = asyncio.get_event_loop()
        self._running = True
        logger.info('Channel F: bridge active (no UDP listener, packets via HAL pkt_fwd)')

        # Idle loop: keep the task alive so cancellation works correctly. We
        # don't poll anything; the RX callback drives all incoming work.
        try:
            while self._running:
                await asyncio.sleep(60)
        except asyncio.CancelledError:
            pass

    def stop(self):
        self._running = False

    def get_stats(self) -> dict:
        return {
            'received': self.packets_received,
            'injected': self.packets_injected,
            'errors': self.packets_errors,
            'tx_packets': self.tx_packets,
            'tx_errors': self.tx_errors,
        }
