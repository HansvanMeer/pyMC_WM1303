"""TX Queue Manager for WM1303 RF0-TX Architecture (direct PULL_RESP).

Provides per-channel TX queues that serialize transmissions through
PULL_RESP to lora_pkt_fwd, which routes TX to RF0 via SKY66420 FEM.
RF0 handles TX (with PA) and RX. RF1 is RX-only.

Architecture:
  ChannelTXQueue  - Simple FIFO per channel (no burst loop)
  TXQueueManager  - Manages per-channel TX queues
  GlobalTXScheduler - Round-robin scheduler, sends one packet at a time
"""
from __future__ import annotations

import asyncio
import base64
import logging
import re
import time
from collections import deque
from typing import Any, Callable, Optional

logger = logging.getLogger("TXQueue")

# Maximum channels supported
MAX_CHANNELS = 4

# LBT RSSI rolling buffer size
LBT_RSSI_BUFFER_SIZE = 20


def _bw_hz_to_str(bw_hz: int) -> str:
    """Convert bandwidth in Hz to string for datr field."""
    mapping = {125000: "125", 250000: "250", 500000: "500"}
    return mapping.get(int(bw_hz), "125")


def _datr_str(sf: int, bw_hz: int) -> str:
    """Build datr string like SF8BW125."""
    return f"SF{sf}BW{_bw_hz_to_str(bw_hz)}"


class ChannelTXQueue:
    """Simple FIFO TX queue for a single channel.

    Stores packets with futures for result notification.
    Does NOT have its own processing loop - the GlobalTXScheduler
    pulls packets from all queues in round-robin order.

    Overflow policy: when full, DROP OLDEST packet to make room for new one.
    This ensures the most recent packets (which are more relevant) are always queued.
    """

    def __init__(self, channel_id: str, freq_hz: int, bw_khz: float,
                 sf: int, cr: int, preamble: int = 17,
                 tx_power: int = 14, queue_size: int = 15,
                 ttl_seconds: float = 5.0):
        self.channel_id = channel_id
        self.freq_hz = freq_hz
        self.bw_khz = bw_khz
        self.sf = sf
        self.cr = cr
        self.preamble = preamble
        self.tx_power = tx_power
        self.ttl_seconds = ttl_seconds
        self._queue_size = queue_size
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=queue_size)

        # Stats
        self.stats = {
            "pending": 0,
            "total_sent": 0,
            "total_failed": 0,
            "dropped_ttl": 0,
            "dropped_overflow": 0,
            "last_tx_time": None,
            "avg_tx_time_ms": 0,
            "avg_send_ms": 0,
            "avg_airtime_ms": 0,
            "avg_wait_ms": 0,
            "last_send_ms": 0,
            "last_airtime_ms": 0,
            "last_wait_ms": 0,
            "total_airtime_ms": 0,
            "total_send_ms": 0,
            "enabled": True,
            "lbt_blocked": 0,
            "lbt_passed": 0,
            "lbt_skipped": 0,
            "lbt_force_sent": 0,
            "lbt_last_blocked_at": None,
            "lbt_last_rssi": None,
            "lbt_last_threshold": None,
            "cad_clear": 0,
            "cad_detected": 0,
            "cad_timeout": 0,
            "cad_skipped": 0,
            "cad_hw_clear": 0,
            "cad_hw_detected": 0,
            "cad_sw_clear": 0,
            "cad_sw_detected": 0,
            "cad_last_result": None,
            "cad_last_source": None,
            # LBT RSSI noise floor stats (rolling buffer)
            "noise_floor_lbt_avg": None,
            "noise_floor_lbt_min": None,
            "noise_floor_lbt_max": None,
            "noise_floor_lbt_samples": 0,
        }
        self._tx_times: list[float] = []
        self._send_times: list[float] = []
        self._airtime_times: list[float] = []
        self._wait_times: list[float] = []

        # LBT RSSI rolling buffer for noise floor estimation
        self._lbt_rssi_buffer: deque = deque(maxlen=LBT_RSSI_BUFFER_SIZE)

    async def enqueue(self, payload: bytes, tx_power: int = None) -> dict:
        """Enqueue a TX request and wait for the GlobalTXScheduler to send it.

        Overflow policy: when full, drop the OLDEST packet to make room.
        New packets are more important than stale ones.

        Returns:
            dict with {"ok": True/False, ...}
        """
        if tx_power is None:
            tx_power = self.tx_power
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        request = {
            "payload": payload,
            "tx_power": tx_power,
            "future": future,
            "enqueue_time": time.time(),
        }
        try:
            self.queue.put_nowait(request)
            self.stats["pending"] = self.queue.qsize()
            logger.info("ChannelTXQueue[%s]: enqueued %d bytes (pending=%d)",
                       self.channel_id, len(payload), self.queue.qsize())
        except asyncio.QueueFull:
            # Drop OLDEST packet to make room for the new one
            try:
                old_req = self.queue.get_nowait()
                old_future = old_req.get("future")
                if old_future and not old_future.done():
                    old_future.set_result({"ok": False, "error": "dropped_overflow"})
                self.stats["dropped_overflow"] += 1
                old_age = time.time() - old_req.get("enqueue_time", 0)
                logger.warning(
                    "ChannelTXQueue[%s]: queue full (%d/%d), dropped OLDEST packet "
                    "(age=%.1fs, %d bytes) to make room for new one",
                    self.channel_id, self._queue_size, self._queue_size,
                    old_age, len(old_req.get("payload", b"")))
            except asyncio.QueueEmpty:
                pass  # shouldn't happen but be safe
            # Now enqueue the new packet
            try:
                self.queue.put_nowait(request)
                self.stats["pending"] = self.queue.qsize()
                logger.info("ChannelTXQueue[%s]: enqueued %d bytes after overflow drop (pending=%d)",
                           self.channel_id, len(payload), self.queue.qsize())
            except asyncio.QueueFull:
                # Still full somehow - reject
                logger.error("ChannelTXQueue[%s]: queue still full after drop, rejecting packet",
                            self.channel_id)
                self.stats["total_failed"] += 1
                return {"ok": False, "error": "queue_full"}

        try:
            result = await asyncio.wait_for(future, timeout=15.0)
            return result
        except asyncio.TimeoutError:
            logger.warning("ChannelTXQueue[%s]: TX wait timeout",
                          self.channel_id)
            self.stats["total_failed"] += 1
            return {"ok": False, "error": "timeout"}

    def dequeue_nowait(self):
        """Non-blocking dequeue. Returns request dict or raises asyncio.QueueEmpty."""
        request = self.queue.get_nowait()  # raises QueueEmpty if empty
        self.stats["pending"] = self.queue.qsize()
        return request

    def build_txpk(self, payload: bytes, tx_power: int = None) -> dict:
        """Build txpk JSON object for PULL_RESP.

        The lora_pkt_fwd HAL uses the freq/datr to select the right
        SX1250 radio. RF1 has tx_enable=true, so all TX
        goes through RF1/SX1250_1. RF0 is RX + Clock.
        """
        if tx_power is None:
            tx_power = self.tx_power
        freq_mhz = self.freq_hz / 1e6
        cr_str = f"4/{self.cr}"
        datr = _datr_str(self.sf, int(self.bw_khz * 1000))
        payload_b64 = base64.b64encode(payload).decode()
        return {
            "imme": True,
            "freq": round(freq_mhz, 6),
            "rfch": 0,  # RF0 = TX chain (SKY66420 FEM with PA)
            "powe": tx_power,
            "modu": "LORA",
            "datr": datr,
            "codr": cr_str,
            "ipol": False,
            "size": len(payload),
            "data": payload_b64,
            "prea": self.preamble,
            "ncrc": False,
        }

    def record_tx_time(self, tx_time_ms: float) -> None:
        """Record TX time for rolling average (legacy/compat)."""
        self._tx_times.append(tx_time_ms)
        if len(self._tx_times) > 50:
            self._tx_times = self._tx_times[-50:]
        self.stats["avg_tx_time_ms"] = round(
            sum(self._tx_times) / len(self._tx_times), 1)

    def record_tx_timing(self, send_ms: float, airtime_ms: float, wait_ms: float) -> None:
        """Record detailed TX timing metrics (send, airtime, queue wait)."""
        # Send time (wall-clock UDP send)
        self._send_times.append(send_ms)
        if len(self._send_times) > 50:
            self._send_times = self._send_times[-50:]
        self.stats["avg_send_ms"] = round(
            sum(self._send_times) / len(self._send_times), 1)
        self.stats["last_send_ms"] = round(send_ms, 1)

        # Airtime (calculated LoRa time-on-air)
        self._airtime_times.append(airtime_ms)
        if len(self._airtime_times) > 50:
            self._airtime_times = self._airtime_times[-50:]
        self.stats["avg_airtime_ms"] = round(
            sum(self._airtime_times) / len(self._airtime_times), 1)
        self.stats["last_airtime_ms"] = round(airtime_ms, 1)
        self.stats["total_airtime_ms"] = round(
            self.stats.get("total_airtime_ms", 0) + airtime_ms, 1)

        # Queue wait time
        self._wait_times.append(wait_ms)
        if len(self._wait_times) > 50:
            self._wait_times = self._wait_times[-50:]
        self.stats["avg_wait_ms"] = round(
            sum(self._wait_times) / len(self._wait_times), 1)
        self.stats["last_wait_ms"] = round(wait_ms, 1)

        # Total send time
        self.stats["total_send_ms"] = round(
            self.stats.get("total_send_ms", 0) + send_ms, 1)

    def record_lbt_rssi(self, rssi: float) -> None:
        """Record an LBT RSSI measurement in the rolling buffer.

        Called after every LBT check (pass or block) to build a
        noise floor estimate from real pre-TX RSSI measurements.
        """
        if rssi is None:
            return
        self._lbt_rssi_buffer.append(rssi)
        n = len(self._lbt_rssi_buffer)
        self.stats["lbt_last_rssi"] = round(rssi, 1)
        self.stats["noise_floor_lbt_samples"] = n
        if n > 0:
            vals = list(self._lbt_rssi_buffer)
            self.stats["noise_floor_lbt_avg"] = round(sum(vals) / n, 1)
            self.stats["noise_floor_lbt_min"] = round(min(vals), 1)
            self.stats["noise_floor_lbt_max"] = round(max(vals), 1)

    def get_status(self) -> dict:
        """Return queue status and stats."""
        self.stats["pending"] = self.queue.qsize()
        return {
            "channel_id": self.channel_id,
            "freq_hz": self.freq_hz,
            "bw_khz": self.bw_khz,
            "sf": self.sf,
            "cr": self.cr,
            "preamble": self.preamble,
            "queue_size": self._queue_size,
            "ttl_seconds": self.ttl_seconds,
            **self.stats,
        }


class TXQueueManager:
    """Manages up to 4 per-channel TX queues.

    Queues are simple FIFOs. The GlobalTXScheduler handles
    actual transmission in round-robin order.
    """

    def __init__(self):
        self.queues: dict[str, ChannelTXQueue] = {}

    def add_channel(self, channel_id: str, freq_hz: int,
                    bw_khz: float = 125.0, sf: int = 8,
                    cr: int = 5, preamble: int = 17,
                    tx_power: int = 14,
                    ttl_seconds: float = 5.0) -> None:
        """Add a channel TX queue."""
        if len(self.queues) >= MAX_CHANNELS:
            raise ValueError(f"Maximum {MAX_CHANNELS} TX queues supported")
        self.queues[channel_id] = ChannelTXQueue(
            channel_id=channel_id,
            freq_hz=freq_hz,
            bw_khz=bw_khz,
            sf=sf,
            cr=cr,
            preamble=preamble,
            tx_power=tx_power,
            ttl_seconds=ttl_seconds,
        )
        logger.info("TXQueueManager: added queue for %s "
                   "(freq=%d, SF%d, BW%.0fkHz, CR4/%d, TX%ddBm, qsize=%d, ttl=%.0fs)",
                   channel_id, freq_hz, sf, bw_khz, cr, tx_power, 15, ttl_seconds)

    async def enqueue(self, channel_id: str, payload: bytes,
                      tx_power: int = None) -> dict:
        """Enqueue a TX packet to the appropriate channel queue."""
        queue = self.queues.get(channel_id)
        if not queue:
            return {"ok": False, "error": f"unknown channel: {channel_id}"}
        return await queue.enqueue(payload, tx_power)

    def stop_all(self) -> None:
        """Stop all TX queue processing (no-op since queues are passive FIFOs)."""
        logger.info("TXQueueManager: all queues stopped")

    def get_status(self) -> dict:
        """Return status of all TX queues."""
        return {
            channel_id: queue.get_status()
            for channel_id, queue in self.queues.items()
        }


class GlobalTXScheduler:
    """Round-robin TX scheduler across all channel queues.

    Sends one packet at a time through a single TX radio,
    preventing collisions. No burst cycle, no collect window.
    Packets are sent within milliseconds of being enqueued.
    """

    def __init__(self, send_func: Callable, queues: dict[str, ChannelTXQueue],
                 post_tx_callback: Callable = None,
                 lbt_check: Callable = None,
                 tx_hold_getter: Callable = None):
        """
        Args:
            send_func: async callable(txpk_dict, channel_id) -> {"ok": bool, ...}
            queues: dict of channel_id -> ChannelTXQueue
            post_tx_callback: optional callable(channel_id, send_ms, airtime_ms, wait_ms, payload_len)
                              called after each successful TX for stats tracking
            tx_hold_getter: optional callable() -> float (monotonic timestamp until TX is held)
                            When time.monotonic() < tx_hold_getter(), TX is delayed.
                            Used to batch-collect RX packets before forwarding.
        """
        self._send_func = send_func
        self._queues = queues
        self._post_tx_callback = post_tx_callback
        self._lbt_check = lbt_check
        self._tx_hold_getter = tx_hold_getter
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._packets_scheduled = 0
        self._round_index = 0  # Rotating start index for fair round-robin

    async def start(self):
        """Start the scheduler loop."""
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._scheduler_loop())
        logger.info("GlobalTXScheduler: started (queues=%s)",
                   list(self._queues.keys()))

    async def stop(self):
        """Stop the scheduler loop."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        logger.info("GlobalTXScheduler: stopped (total_scheduled=%d)",
                   self._packets_scheduled)

    async def _scheduler_loop(self):
        """Round-robin poll all TX queues, send one packet at a time.

        Uses a rotating start index so each channel gets equal priority
        over time. Each round starts from the next channel in sequence:
        Round 1: a → b → c, Round 2: b → c → a, Round 3: c → a → b, etc.
        """
        queue_list = list(self._queues.items())  # [(channel_id, ChannelTXQueue), ...]
        n_queues = len(queue_list)
        logger.info("GlobalTXScheduler: scheduler loop running with %d queues",
                   n_queues)

        try:
            while self._running:
                sent_any = False
                # Rotate start position for fair scheduling
                start = self._round_index % n_queues
                rotated = queue_list[start:] + queue_list[:start]
                for channel_id, queue in rotated:
                    try:
                        request = queue.dequeue_nowait()
                    except asyncio.QueueEmpty:
                        continue

                    # TTL check
                    age = time.time() - request["enqueue_time"]
                    if age > queue.ttl_seconds:
                        logger.warning("GlobalTXScheduler: packet expired on %s "
                                      "(age=%.1fs, TTL=%.0fs)",
                                      channel_id, age, queue.ttl_seconds)
                        queue.stats["dropped_ttl"] += 1
                        future = request.get("future")
                        if future and not future.done():
                            future.set_result({"ok": False, "error": "ttl_expired",
                                               "age": round(age, 1)})
                        continue

                    # Build txpk using the queue's channel config
                    txpk = queue.build_txpk(
                        request["payload"],
                        request.get("tx_power", queue.tx_power))

                    # FIX Bug4: Measure queue wait BEFORE calling send_func
                    # This captures dequeue_time - enqueue_time only
                    queue_wait_ms = (time.time() - request["enqueue_time"]) * 1000

                    # --- Software LBT check ---
                    # When LBT is DISABLED: TX immediately, no check
                    # When LBT is ENABLED: 4 attempts with 2s/3s/4s delays, force-send on failure
                    lbt_tx_blocked = False
                    if self._lbt_check:
                        lbt_result = self._lbt_check(channel_id, queue.freq_hz, queue.sf)
                        if not lbt_result.get("lbt_enabled", False):
                            # LBT disabled on this channel - send immediately
                            queue.stats["lbt_skipped"] += 1
                            queue.stats["cad_skipped"] += 1
                        else:
                            # LBT enabled - check spectral scan RSSI vs threshold
                            lbt_delays = [0, 2.0, 3.0, 4.0]  # 4 attempts: immediate, +2s, +3s, +4s
                            lbt_passed = False
                            for attempt, delay in enumerate(lbt_delays):
                                if delay > 0:
                                    logger.info("GlobalTXScheduler: LBT retry %d/%d on %s, "
                                               "waiting %.0fs (rssi=%.1f, threshold=%.1f)",
                                               attempt + 1, len(lbt_delays), channel_id,
                                               delay,
                                               lbt_result.get("rssi", 0),
                                               lbt_result.get("threshold", 0))
                                    await asyncio.sleep(delay)
                                    lbt_result = self._lbt_check(channel_id, queue.freq_hz, queue.sf)

                                # Record LBT RSSI in rolling buffer (Task 2)
                                _lbt_rssi = lbt_result.get("rssi")
                                if _lbt_rssi is not None:
                                    queue.record_lbt_rssi(_lbt_rssi)

                                if lbt_result.get("allow", True):
                                    lbt_passed = True
                                    queue.stats["lbt_passed"] += 1
                                    # Track CAD stats from enhanced LBT result
                                    _cad = lbt_result.get('cad_result')
                                    if isinstance(_cad, dict):
                                        _src = _cad.get('source', 'unknown')
                                        if _cad.get('detected', False):
                                            queue.stats["cad_detected"] += 1
                                            if 'hardware' in _src:
                                                queue.stats["cad_hw_detected"] += 1
                                            else:
                                                queue.stats["cad_sw_detected"] += 1
                                        else:
                                            queue.stats["cad_clear"] += 1
                                            if 'hardware' in _src:
                                                queue.stats["cad_hw_clear"] += 1
                                            else:
                                                queue.stats["cad_sw_clear"] += 1
                                        queue.stats["cad_last_result"] = _cad.get('reason', 'unknown')
                                        queue.stats["cad_last_source"] = _src
                                    else:
                                        queue.stats["cad_skipped"] += 1
                                    queue.stats["lbt_last_threshold"] = lbt_result.get("threshold")
                                    if attempt > 0:
                                        logger.info("GlobalTXScheduler: LBT PASSED on %s "
                                                   "after %d attempts (rssi=%.1f, threshold=%.1f)",
                                                   channel_id, attempt + 1,
                                                   lbt_result.get("rssi", 0),
                                                   lbt_result.get("threshold", 0))
                                    break
                            if not lbt_passed:
                                # All 4 attempts failed - wait 5s then FORCE SEND
                                queue.stats["lbt_blocked"] += 1
                                # Track CAD stats for blocked case
                                _cad = lbt_result.get('cad_result')
                                if isinstance(_cad, dict):
                                    _src = _cad.get('source', 'unknown')
                                    if _cad.get('detected', False):
                                        queue.stats["cad_detected"] += 1
                                        if 'hardware' in _src:
                                            queue.stats["cad_hw_detected"] += 1
                                        else:
                                            queue.stats["cad_sw_detected"] += 1
                                    queue.stats["cad_last_result"] = _cad.get('reason', 'unknown')
                                    queue.stats["cad_last_source"] = _src
                                queue.stats["lbt_last_blocked_at"] = time.time()
                                queue.stats["lbt_last_threshold"] = lbt_result.get("threshold")
                                logger.warning("GlobalTXScheduler: LBT BLOCKED on %s after %d "
                                              "attempts - waiting 5s then FORCE SENDING "
                                              "(rssi=%.1f, threshold=%.1f, freq=%.3fMHz)",
                                              channel_id, len(lbt_delays),
                                              lbt_result.get("rssi", 0),
                                              lbt_result.get("threshold", 0),
                                              queue.freq_hz / 1e6)
                                await asyncio.sleep(5.0)
                                queue.stats["lbt_force_sent"] += 1
                                logger.warning("GlobalTXScheduler: FORCE SENDING packet on %s "
                                              "despite LBT block (rssi=%.1f, threshold=%.1f)",
                                              channel_id,
                                              lbt_result.get("rssi", 0),
                                              lbt_result.get("threshold", 0))

                    if lbt_tx_blocked:
                        continue  # Skip TX, move to next queue

                    # TX Hold check: if RX batching window active, wait before TX
                    if self._tx_hold_getter:
                        _hold_until = self._tx_hold_getter()
                        _hold_remaining = _hold_until - time.monotonic()
                        if _hold_remaining > 0.01:
                            logger.info("GlobalTXScheduler: TX hold active on %s, "
                                       "waiting %.1fs (batch window)",
                                       channel_id, _hold_remaining)
                            await asyncio.sleep(_hold_remaining)

                    # Send via the backend's PULL_RESP sender
                    try:
                        result = await self._send_func(txpk, channel_id)
                    except Exception as e:
                        logger.error("GlobalTXScheduler: send error on %s: %s",
                                    channel_id, e, exc_info=True)
                        result = {"ok": False, "error": str(e)}

                    # FIX Bug1: Use send_ms from result dict (UDP send only)
                    # not wall-clock around send_func (includes airtime wait)
                    send_ms = result.get("send_ms", 0)
                    airtime_ms = result.get("airtime_ms", 0)

                    if result.get("ok"):
                        queue.stats["total_sent"] += 1
                        queue.stats["last_tx_time"] = time.time()
                        queue.record_tx_time(send_ms)
                        queue.record_tx_timing(send_ms, airtime_ms, queue_wait_ms)
                        result["send_ms"] = send_ms
                        result["airtime_ms"] = airtime_ms
                        result["queue_wait_ms"] = queue_wait_ms
                        logger.info("GlobalTXScheduler: TX OK on %s (%d bytes, "
                                   "freq=%.3f, datr=%s, send=%.1fms, "
                                   "airtime=%.1fms, queue_wait=%.1fms)",
                                   channel_id, len(request["payload"]),
                                   txpk.get("freq", 0), txpk.get("datr", ""),
                                   send_ms, airtime_ms, queue_wait_ms)
                        # Notify backend for per-channel TX stats tracking
                        if self._post_tx_callback:
                            try:
                                self._post_tx_callback(
                                    channel_id, send_ms, airtime_ms,
                                    queue_wait_ms, len(request["payload"]))
                            except Exception as _cb_err:
                                logger.debug("GlobalTXScheduler: post_tx_callback error: %s", _cb_err)
                    else:
                        queue.stats["total_failed"] += 1
                        logger.warning("GlobalTXScheduler: TX FAIL on %s: %s "
                                      "(send=%.1fms, queue_wait=%.1fms)",
                                      channel_id, result.get("error", "unknown"),
                                      send_ms, queue_wait_ms)

                    # Resolve the caller's future
                    future = request.get("future")
                    if future and not future.done():
                        future.set_result(result)

                    self._packets_scheduled += 1
                    sent_any = True

                    # 50ms inter-packet gap for radio to settle
                    await asyncio.sleep(0.002)

                if sent_any:
                    # Rotate start position for next round
                    self._round_index += 1
                else:
                    # No packets in any queue - brief sleep to avoid busy-wait
                    await asyncio.sleep(0.01)  # 10ms poll interval

        except asyncio.CancelledError:
            logger.info("GlobalTXScheduler: loop cancelled")
            return
        except Exception as _loop_exc:
            logger.error("GlobalTXScheduler: FATAL scheduler loop crashed: %s",
                        _loop_exc, exc_info=True)
            # Try to restart after a brief pause
            await asyncio.sleep(1.0)
            logger.warning("GlobalTXScheduler: attempting to restart scheduler loop")
            self._task = asyncio.create_task(self._scheduler_loop())
            return
        logger.info("GlobalTXScheduler: loop exited")

    def get_stats(self) -> dict:
        """Return scheduler stats."""
        return {
            "running": self._running,
            "packets_scheduled": self._packets_scheduled,
            "round_index": self._round_index,
            "queues": list(self._queues.keys()),
        }
# Legacy TXQueue compatibility (deprecated)
# ======================================================================

class TXQueue:
    """DEPRECATED: Legacy SX1261 TX Queue.

    This class is kept for backward compatibility only.
    New code should use TXQueueManager + ChannelTXQueue + GlobalTXScheduler.
    """

    def __init__(self, radio=None, inter_packet_delay_ms: float = 20.0,
                 max_queue_size: int = 100):
        logger.warning("TXQueue: DEPRECATED - use TXQueueManager instead")
        self._running = False
        self._task = None
        self.tx_count = 0
        self.tx_errors = 0
        self.tx_queue_drops = 0
        self.last_tx_time = 0
        self.avg_tx_time_ms = 0

    async def start(self) -> None:
        logger.warning("TXQueue.start(): DEPRECATED - no-op")
        self._running = True

    def stop(self) -> None:
        self._running = False

    async def enqueue(self, **kwargs) -> dict:
        logger.warning("TXQueue.enqueue(): DEPRECATED - returning error")
        return {"ok": False, "error": "deprecated_sx1261_queue"}

    def get_stats(self) -> dict:
        return {
            "deprecated": True,
            "tx_count": self.tx_count,
            "tx_errors": self.tx_errors,
            "running": self._running,
        }

    @property
    def pending(self) -> int:
        return 0
