from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Callable, Optional

from .base import LoRaRadio

logger = logging.getLogger(__name__)


class VirtualLoRaRadio(LoRaRadio):
    """Virtual radio backed by a WM1303Backend channel.

    Each VirtualLoRaRadio maps to one logical channel on the WM1303
    concentrator. The backend manages the actual hardware (lora_pkt_fwd)
    and routes received packets to the correct virtual radio instance.

    TX is routed through the Channel E TX queue (if available) for
    dedicated TX without interrupting SX1303 RX.
    """

    # Noise floor estimation settings
    NOISE_FLOOR_WINDOW = 300  # seconds (5 min rolling window)
    NOISE_FLOOR_DEFAULT = -115.0  # dBm typical EU868 noise floor
    NOISE_FLOOR_MIN_SAMPLES = 1  # min samples before reporting

    def __init__(self, backend, channel_id: str, channel_config: dict[str, Any]):
        super().__init__()
        self.backend = backend
        self.channel_id = channel_id
        self.channel_config = channel_config

        # Expose radio parameters as instance attributes so that
        # getattr(radio, "spreading_factor", ...) in engine.py picks up
        # the actual per-channel values instead of falling back to
        # hardcoded defaults.  Values come from the SSOT (wm1303_ui.json)
        # which is region- and channel-specific.
        self.frequency = int(channel_config.get("frequency", 0))
        self.spreading_factor = int(channel_config.get("spreading_factor", 7))
        self.bandwidth = int(channel_config.get("bandwidth", 125000))
        self.coding_rate = channel_config.get("coding_rate", "4/5")
        self.preamble_length = int(channel_config.get("preamble_length", 17))
        self.tx_power = int(channel_config.get("tx_power", 14))

        self._rx_queue: asyncio.Queue[bytes] = asyncio.Queue()
        self._rx_callback: Optional[Callable] = None
        self._started = False
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._last_rssi: int = 0
        self._last_snr: float = 0.0
        self._last_tx_metadata = None
        # Noise floor tracking: list of (timestamp, rssi) tuples
        self._rssi_history: list[tuple[float, float]] = []
        self._noise_floor_dbm: float = self.NOISE_FLOOR_DEFAULT
        self.backend.register_virtual_radio(self)
        logger.info("VirtualLoRaRadio[%s] __init__: freq=%d SF%d BW%d CR=%s tx_power=%d queue id=%s",
                    channel_id, self.frequency, self.spreading_factor,
                    self.bandwidth, self.coding_rate, self.tx_power,
                    id(self._rx_queue))

    def begin(self):
        self._started = True
        try:
            self._loop = asyncio.get_running_loop()
        except RuntimeError:
            self._loop = None
        logger.info(
            "VirtualLoRaRadio[%s] begin(): loop=%s loop_id=%s queue_id=%s",
            self.channel_id,
            self._loop is not None,
            id(self._loop) if self._loop else "None",
            id(self._rx_queue),
        )

    def set_event_loop(self, loop):
        """Set the asyncio event loop (call from async context)."""
        self._loop = loop
        logger.info(
            "VirtualLoRaRadio[%s] set_event_loop: loop_id=%s",
            self.channel_id, id(loop),
        )

    def set_rx_callback(self, callback: Callable):
        """Register a callback for received packets (used by Dispatcher)."""
        self._rx_callback = callback

    def enqueue_rx(self, payload: bytes, rssi: int = 0, snr: float = 0.0):
        """Called by the backend when a packet is received on this channel.

        NOTE: This is called from WM1303Backend._udp_loop() which runs in a
        threading.Thread, NOT in the asyncio event loop.  asyncio.Queue is NOT
        thread-safe, so we must use loop.call_soon_threadsafe() to wake the
        coroutine waiting on _rx_queue.get().
        """
        import threading
        logger.info(
            "VirtualLoRaRadio[%s] enqueue_rx ENTER: %d bytes rssi=%d snr=%.1f "
            "thread=%s loop=%s loop_running=%s queue_id=%s qsize=%d",
            self.channel_id, len(payload), rssi, snr,
            threading.current_thread().name,
            self._loop is not None,
            self._loop.is_running() if self._loop else "N/A",
            id(self._rx_queue),
            self._rx_queue.qsize(),
        )

        self._last_rssi = rssi
        self._last_snr = snr
        # Record RSSI for noise floor estimation
        now = time.time()
        self._rssi_history.append((now, float(rssi)))
        self._prune_rssi_history(now)
        self._update_noise_floor()

        # Thread-safe enqueue for BridgeEngine's _rx_queue.
        try:
            if self._loop and self._loop.is_running():
                logger.info(
                    "VirtualLoRaRadio[%s] enqueue_rx: using call_soon_threadsafe, loop_id=%s",
                    self.channel_id, id(self._loop),
                )
                self._loop.call_soon_threadsafe(self._rx_queue.put_nowait, payload)
                logger.info(
                    "VirtualLoRaRadio[%s] enqueue_rx: call_soon_threadsafe SUCCEEDED, qsize=%d",
                    self.channel_id, self._rx_queue.qsize(),
                )
            else:
                logger.warning(
                    "VirtualLoRaRadio[%s] enqueue_rx: NO loop or not running! "
                    "Using direct put_nowait. loop=%s is_running=%s",
                    self.channel_id, self._loop,
                    self._loop.is_running() if self._loop else "N/A",
                )
                self._rx_queue.put_nowait(payload)
                logger.info(
                    "VirtualLoRaRadio[%s] enqueue_rx: direct put_nowait done, qsize=%d",
                    self.channel_id, self._rx_queue.qsize(),
                )
        except Exception as exc:
            logger.error(
                "VirtualLoRaRadio[%s] enqueue_rx: EXCEPTION during queue put: %s: %s",
                self.channel_id, type(exc).__name__, exc,
                exc_info=True,
            )

        # Callback for Dispatcher - also needs thread-safe scheduling
        if self._rx_callback:
            try:
                result = self._rx_callback(payload)
                if asyncio.iscoroutine(result):
                    if self._loop and self._loop.is_running():
                        self._loop.call_soon_threadsafe(
                            lambda r=result: self._loop.create_task(r)
                        )
            except Exception as exc:
                logger.error(
                    "VirtualLoRaRadio[%s] enqueue_rx: EXCEPTION in rx_callback: %s: %s",
                    self.channel_id, type(exc).__name__, exc,
                    exc_info=True,
                )

    def _prune_rssi_history(self, now: float) -> None:
        """Remove RSSI samples older than the rolling window."""
        cutoff = now - self.NOISE_FLOOR_WINDOW
        self._rssi_history = [(t, r) for t, r in self._rssi_history if t >= cutoff]

    def _update_noise_floor(self) -> None:
        """Estimate noise floor from the lowest RSSI values in the window."""
        if len(self._rssi_history) < self.NOISE_FLOOR_MIN_SAMPLES:
            return
        rssi_values = sorted(r for _, r in self._rssi_history)
        idx = max(0, int(len(rssi_values) * 0.1))
        self._noise_floor_dbm = rssi_values[idx]

    def get_noise_floor(self) -> float:
        """Return the estimated noise floor in dBm."""
        return self._noise_floor_dbm

    async def send(self, data: bytes, trace_hash: str = None, **kwargs):
        """Send data on this channel.

        Routes through backend.send() which uses:
        - Channel E TX queue (primary) - dedicated TX radio, no RX interruption
        - SX1303 PULL_RESP (fallback) - if Channel E unavailable

        The optional ``trace_hash`` is forwarded to the backend so that
        chronologically-correct TX-phase trace events
        (tx_noisefloor/cad_start/lbt_check/cad_check/rf_tx_start/rf_tx_end/
        sx1261_rx_restart/tx_ack/rf_guard) can be emitted by the backend
        once the post-TX ACK arrives.
        """
        meta = await self.backend.send(
            self.channel_id, data,
            tx_power=int(self.channel_config.get("tx_power", 14)),
            trace_hash=trace_hash,
        )
        self._last_tx_metadata = meta
        return meta

    async def wait_for_rx(self) -> bytes:
        logger.debug(
            "VirtualLoRaRadio[%s] wait_for_rx: WAITING on queue_id=%s qsize=%d",
            self.channel_id, id(self._rx_queue), self._rx_queue.qsize(),
        )
        data = await self._rx_queue.get()
        logger.info(
            "VirtualLoRaRadio[%s] wait_for_rx: GOT %d bytes! qsize=%d",
            self.channel_id, len(data), self._rx_queue.qsize(),
        )
        return data

    def sleep(self):
        return None

    def get_last_rssi(self) -> int:
        return int(self._last_rssi)

    def get_last_snr(self) -> float:
        return float(self._last_snr)
