import asyncio
import json
import logging
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

from .mqtt_handler import MeshCoreToMqttPusher
from .rrdtool_handler import RRDToolHandler
from .sqlite_handler import SQLiteHandler
from .storage_utils import PacketRecord
import threading
import sqlite3
from contextlib import contextmanager as _contextmanager

@_contextmanager
class _SharedConn:
    """Module-level shared SQLite connection with thread-safe access."""

    def __init__(self, path):
        self._path = str(path)
        self._conn = None
        self._lock = threading.RLock()

    def _ensure_conn(self):
        if self._conn is None:
            self._conn = sqlite3.connect(
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


logger = logging.getLogger("StorageCollector")


class StorageCollector:
    def __init__(self, config: dict, local_identity=None, repeater_handler=None):
        self.config = config
        self.repeater_handler = repeater_handler
        self.glass_publish_callback = None
        self._pending_tasks = set()

        storage_dir_cfg = (
            config.get("storage", {}).get("storage_dir")
            or config.get("storage_dir")
            or "/var/lib/pymc_repeater"
        )
        self.storage_dir = Path(storage_dir_cfg)
        self.storage_dir.mkdir(parents=True, exist_ok=True)

        self.sqlite_handler = SQLiteHandler(self.storage_dir)
        self.rrd_handler = RRDToolHandler(self.storage_dir)

        # Initialize MQTT handler if configured
        self.mqtt_handler = None
        if (config.get("mqtt_brokers", {}) or config.get("letsmesh", {}) or config.get("mqtt", {})) and local_identity:
            try:
                # Pass local_identity directly (supports both standard and firmware keys)
                self.mqtt_handler = MeshCoreToMqttPusher(
                    local_identity=local_identity,
                    config=config,
                    stats_provider=self._get_live_stats,
                )
                self.mqtt_handler.connect()

                public_key_hex = local_identity.get_public_key().hex()
                logger.info(
                    f"MQTT handler initialized with public key: {public_key_hex[:16]}..."
                )
            except Exception as e:
                logger.error(f"Failed to initialize MQTT handler: {e}")
                self.mqtt_handler = None

        # Initialize hardware stats collector
        from .hardware_stats import HardwareStatsCollector

        self.hardware_stats = HardwareStatsCollector()
        logger.info("Hardware stats collector initialized")

        # Initialize WebSocket handler for real-time updates
        self.websocket_available = False
        self.websocket_has_connected_clients = lambda: False
        self._last_ws_stats_broadcast: float = 0.0
        self._ws_stats_broadcast_interval_sec: float = 5.0
        try:
            from .websocket_handler import (
                broadcast_packet,
                broadcast_stats,
                has_connected_clients,
            )

            self.websocket_broadcast_packet = broadcast_packet
            self.websocket_broadcast_stats = broadcast_stats
            self.websocket_has_connected_clients = has_connected_clients
            self.websocket_available = True
            logger.info("WebSocket handler initialized for real-time updates")
        except ImportError:
            logger.debug("WebSocket handler not available")

    def _track_task(self, task: asyncio.Task):
        """Track background task for lifecycle management and error handling."""
        self._pending_tasks.add(task)

        def on_done(t: asyncio.Task):
            self._pending_tasks.discard(t)
            try:
                t.result()
            except asyncio.CancelledError:
                pass
            except Exception as e:
                logger.error(f"Background task error: {e}", exc_info=True)

        task.add_done_callback(on_done)

    def _schedule_background(self, coro_factory, *args, sync_fallback=None):
        """Schedule a coroutine if a loop exists; otherwise run sync fallback."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            if sync_fallback is not None:
                sync_fallback(*args)
            return

        task = loop.create_task(coro_factory(*args))
        self._track_task(task)

    def _get_live_stats(self) -> dict:
        """Get live stats from RepeaterHandler"""
        if not self.repeater_handler:
            return {
                "uptime_secs": 0,
                "packets_sent": 0,
                "packets_received": 0,
                "errors": 0,
                "queue_len": 0,
            }

        uptime_secs = int(time.time() - self.repeater_handler.start_time)

        # Get airtime stats
        airtime_stats = self.repeater_handler.airtime_mgr.get_stats()

        # Get latest noise floor from database
        noise_floor = None
        try:
            recent_noise = self.sqlite_handler.get_noise_floor_history(hours=0.5, limit=1)
            if recent_noise and len(recent_noise) > 0:
                noise_floor = recent_noise[-1].get("noise_floor_dbm")
        except Exception as e:
            logger.debug(f"Could not fetch noise floor: {e}")

        stats = {
            "uptime_secs": uptime_secs,
            "packets_sent": self.repeater_handler.forwarded_count,
            "packets_received": self.repeater_handler.rx_count,
            "errors": 0,
            "queue_len": 0,  # N/A for Python repeater
        }

        # Add airtime stats
        if airtime_stats:
            stats["tx_air_secs"] = airtime_stats["total_airtime_ms"] / 1000
            stats["current_airtime_ms"] = airtime_stats["current_airtime_ms"]
            stats["utilization_percent"] = airtime_stats["utilization_percent"]

        # Add noise floor if available
        if noise_floor is not None:
            stats["noise_floor"] = noise_floor

        return stats

    def record_packet(self, packet_record: dict, skip_letsmesh_if_invalid: bool = True):
        """Record packet to storage and publish to MQTT

        Args:
            packet_record: Dictionary containing packet information
            skip_letsmesh_if_invalid: If True, don't publish packets with drop_reason to MQTT/LetsMesh
        """
        logger.debug(
            f"Recording packet: type={packet_record.get('type')}, "
            f"transmitted={packet_record.get('transmitted')}"
        )

        # HOT PATH: Store to local databases only (fast, non-blocking)
        self.sqlite_handler.store_packet(packet_record)
        cumulative_counts = self.sqlite_handler.get_cumulative_counts()
        self.rrd_handler.update_packet_metrics(packet_record, cumulative_counts)

        # DEFERRED: Publish to network sinks and WebSocket in background tasks
        # This prevents network latency from blocking packet processing
        self._schedule_background(
            self._deferred_publish,
            packet_record,
            skip_letsmesh_if_invalid,
            sync_fallback=self._publish_packet_sync,
        )

    async def _deferred_publish(self, packet_record: dict, skip_letsmesh: bool):
        """Deferred background task for all network publishing operations."""
        try:
            self._publish_packet_sync(packet_record, skip_letsmesh)
        except Exception as e:
            logger.error(f"Deferred publish failed: {e}", exc_info=True)

    def _publish_packet_sync(self, packet_record: dict, skip_letsmesh: bool):
        """Publish packet updates synchronously (used when no asyncio loop is active)."""
        self._publish_to_glass(packet_record, "packet")

        if self.websocket_available:
            try:
                self.websocket_broadcast_packet(packet_record)
                if self.websocket_has_connected_clients():
                    now_mono = time.monotonic()
                    if (
                        now_mono - self._last_ws_stats_broadcast
                        >= self._ws_stats_broadcast_interval_sec
                    ):
                        self._last_ws_stats_broadcast = now_mono
                        packet_stats_24h = self.sqlite_handler.get_packet_stats(hours=24)
                        uptime_seconds = (
                            time.time() - self.repeater_handler.start_time if self.repeater_handler else 0
                        )
                        self.websocket_broadcast_stats(
                            {
                                "packet_stats": packet_stats_24h,
                                "system_stats": {"uptime_seconds": uptime_seconds},
                            }
                        )
            except Exception as e:
                logger.debug(f"WebSocket broadcast failed: {e}")


        self._publish_packet_to_mqtt(packet_record)

    def _publish_packet_to_mqtt(self, packet_record: dict):
        """Publish packet to mqtt broker if enabled and allowed"""
        if not self.mqtt_handler:
            return

        try:
            packet_type = packet_record.get("type")
            if packet_type is None:
                logger.error("Cannot publish to mqtt: packet_record missing 'type' field")
                return

            node_name = self.config.get("repeater", {}).get("node_name", "Unknown")
            packet = PacketRecord.from_packet_record(
                packet_record, origin=node_name, origin_id=self.mqtt_handler.public_key
            )

            if packet:
                self.mqtt_handler.publish_packet(packet.to_dict())
                logger.debug(f"Published packet type 0x{packet_type:02X} to mqtt")
            else:
                logger.debug("Skipped mqtt publish: packet missing raw_packet data")

        except Exception as e:
            logger.error(f"Failed to publish packet to mqtt: {e}", exc_info=True)

    def record_advert(self, advert_record: dict):
        """Record advert to storage and defer network publishing to background tasks."""
        self.sqlite_handler.store_advert(advert_record)
        self._schedule_background(
            self._deferred_publish_advert,
            advert_record,
            sync_fallback=self._publish_advert_sync,
        )

    async def _deferred_publish_advert(self, advert_record: dict):
        """Deferred background task for advert publishing."""
        try:
            self._publish_advert_sync(advert_record)
        except Exception as e:
            logger.error(f"Deferred advert publish failed: {e}", exc_info=True)

    def _publish_advert_sync(self, advert_record: dict):
        if self.mqtt_handler:
            self.mqtt_handler.publish_mqtt(advert_record, "advert")
        self._publish_to_glass(advert_record, "advert")

    def record_noise_floor(self, noise_floor_dbm: float):
        """Record noise floor to storage and defer network publishing to background tasks."""
        noise_record = {"timestamp": time.time(), "noise_floor_dbm": noise_floor_dbm}
        self.sqlite_handler.store_noise_floor(noise_record)
        self._schedule_background(
            self._deferred_publish_noise_floor,
            noise_record,
            sync_fallback=self._publish_noise_floor_sync,
        )

    async def _deferred_publish_noise_floor(self, noise_record: dict):
        """Deferred background task for noise floor publishing."""
        try:
            self._publish_noise_floor_sync(noise_record)
        except Exception as e:
            logger.error(f"Deferred noise floor publish failed: {e}", exc_info=True)

    def _publish_noise_floor_sync(self, noise_record: dict):
        if self.mqtt_handler:
            self.mqtt_handler.publish_mqtt(noise_record, "noise_floor")
        self._publish_to_glass(noise_record, "noise_floor")

    def record_crc_errors(self, count: int):
        """Record a batch of CRC errors detected since last poll and defer publishing."""
        crc_record = {"timestamp": time.time(), "count": count}
        self.sqlite_handler.store_crc_errors(crc_record)
        self._schedule_background(
            self._deferred_publish_crc_errors,
            crc_record,
            sync_fallback=self._publish_crc_errors_sync,
        )

    async def _deferred_publish_crc_errors(self, crc_record: dict):
        """Deferred background task for CRC error publishing."""
        try:
            self._publish_crc_errors_sync(crc_record)
        except Exception as e:
            logger.error(f"Deferred CRC errors publish failed: {e}", exc_info=True)

    def _publish_crc_errors_sync(self, crc_record: dict):
        if self.mqtt_handler:
            self.mqtt_handler.publish_mqtt(crc_record, "crc_errors")
        self._publish_to_glass(crc_record, "crc_errors")

    def get_crc_error_count(self, hours: int = 24) -> int:
        return self.sqlite_handler.get_crc_error_count(hours)

    def get_crc_error_history(self, hours: int = 24, limit: int = None) -> list:
        return self.sqlite_handler.get_crc_error_history(hours, limit)

    def get_packet_stats(self, hours: int = 24) -> dict:
        return self.sqlite_handler.get_packet_stats(hours)

    def get_recent_packets(self, limit: int = 100) -> list:
        return self.sqlite_handler.get_recent_packets(limit)

    def get_filtered_packets(
        self,
        packet_type: Optional[int] = None,
        route: Optional[int] = None,
        start_timestamp: Optional[float] = None,
        end_timestamp: Optional[float] = None,
        limit: int = 1000,
        offset: int = 0,
    ) -> list:
        return self.sqlite_handler.get_filtered_packets(
            packet_type, route, start_timestamp, end_timestamp, limit, offset
        )

    def get_airtime_data(
        self,
        start_timestamp: Optional[float] = None,
        end_timestamp: Optional[float] = None,
        limit: int = 50000,
    ) -> list:
        return self.sqlite_handler.get_airtime_data(start_timestamp, end_timestamp, limit)

    def get_airtime_buckets(
        self,
        start_timestamp: float,
        end_timestamp: float,
        bucket_seconds: int = 60,
        sf: int = 9,
        bw_hz: int = 62500,
        cr: int = 5,
        preamble: int = 17,
    ) -> dict:
        return self.sqlite_handler.get_airtime_buckets(
            start_timestamp, end_timestamp, bucket_seconds, sf, bw_hz, cr, preamble
        )

    def get_packet_by_hash(self, packet_hash: str) -> Optional[dict]:
        return self.sqlite_handler.get_packet_by_hash(packet_hash)

    def get_packet_by_id(self, packet_id: int) -> Optional[dict]:
        """Return a single packet row by its integer primary key.

        Proxy for :meth:`SQLiteHandler.get_packet_by_id`. Backs
        GET /api/packet_by_id, which the Vue UI's Packet Details drawer
        calls after a user clicks a row in the packets list.
        """
        return self.sqlite_handler.get_packet_by_id(packet_id)

    def get_rrd_data(
        self,
        start_time: Optional[int] = None,
        end_time: Optional[int] = None,
        resolution: str = "average",
    ) -> Optional[dict]:
        return self.rrd_handler.get_data(start_time, end_time, resolution)

    def get_packet_type_stats(self, hours: int = 24) -> dict:
        rrd_stats = self.rrd_handler.get_packet_type_stats(hours)
        if rrd_stats:
            return rrd_stats

        logger.warning("Falling back to SQLite for packet type stats")
        return self.sqlite_handler.get_packet_type_stats(hours)

    def get_route_stats(self, hours: int = 24) -> dict:
        return self.sqlite_handler.get_route_stats(hours)

    def get_neighbors(self) -> dict:
        return self.sqlite_handler.get_neighbors()

    # v2.5.7: persistent neighbour sample ring buffer
    def record_neighbour_sample(self, pubkey: str, rssi, snr, channel: str = "") -> None:
        self.sqlite_handler.record_neighbour_sample(pubkey, rssi, snr, channel)

    def get_neighbour_samples(self, pubkey: str, limit: int = 200) -> list:
        return self.sqlite_handler.get_neighbour_samples(pubkey, limit)

    def prune_neighbour_samples(self, max_age_seconds: int = 691200, max_per_node: int = 200) -> int:
        return self.sqlite_handler.prune_neighbour_samples(max_age_seconds, max_per_node)

    def delete_neighbours(self, pubkeys: list) -> int:
        return self.sqlite_handler.delete_neighbours(pubkeys)

    def record_advert_duplicate(self, pubkey: str, count: int = 1) -> None:
        """Increment duplicate_count for a neighbour (v2.5.7 Pack 3)."""
        self.sqlite_handler.record_advert_duplicate(pubkey, count)

    def get_node_name_by_pubkey(self, pubkey: str) -> Optional[str]:
        """
        Lookup node name from adverts table by public key.

        Args:
            pubkey: Public key in hex string format

        Returns:
            Node name if found, None otherwise
        """
        try:


            with _db_conn(self.sqlite_handler.sqlite_path) as conn:
                result = conn.execute(
                    "SELECT node_name FROM adverts WHERE pubkey = ? AND node_name IS NOT NULL ORDER BY last_seen DESC LIMIT 1",
                    (pubkey,),
                ).fetchone()
                return result[0] if result else None
        except Exception as e:
            logger.debug(f"Could not lookup node name for {pubkey[:8] if pubkey else 'None'}: {e}")
            return None

    def cleanup_old_data(self, days: int = 7):
        self.sqlite_handler.cleanup_old_data(days)

    def get_noise_floor_history(self, hours: int = 24, limit: int = None) -> list:
        return self.sqlite_handler.get_noise_floor_history(hours, limit)

    def get_noise_floor_stats(self, hours: int = 24) -> dict:
        return self.sqlite_handler.get_noise_floor_stats(hours)

    def close(self):
        # Cancel all pending background tasks
        for task in self._pending_tasks:
            if not task.done():
                task.cancel()

        if self.mqtt_handler:
            try:
                self.mqtt_handler.disconnect()
                logger.info("MQTT handler disconnected")
            except Exception as e:
                logger.error(f"Error disconnecting MQTT handler: {e}")

    def set_glass_publisher(self, publish_callback):
        self.glass_publish_callback = publish_callback

    def _publish_to_glass(self, record: dict, record_type: str):
        if not self.glass_publish_callback:
            return
        try:
            self.glass_publish_callback(record_type, record)
        except Exception as e:
            logger.debug(f"Failed to publish telemetry to Glass MQTT: {e}")

    def create_transport_key(
        self,
        name: str,
        flood_policy: str,
        transport_key: Optional[str] = None,
        parent_id: Optional[int] = None,
        last_used: Optional[float] = None,
    ) -> Optional[int]:
        return self.sqlite_handler.create_transport_key(
            name, flood_policy, transport_key, parent_id, last_used
        )

    def get_transport_keys(self) -> list:
        return self.sqlite_handler.get_transport_keys()

    def get_transport_key_by_id(self, key_id: int) -> Optional[dict]:
        return self.sqlite_handler.get_transport_key_by_id(key_id)

    def update_transport_key(
        self,
        key_id: int,
        name: Optional[str] = None,
        flood_policy: Optional[str] = None,
        transport_key: Optional[str] = None,
        parent_id: Optional[int] = None,
        last_used: Optional[float] = None,
    ) -> bool:
        return self.sqlite_handler.update_transport_key(
            key_id, name, flood_policy, transport_key, parent_id, last_used
        )

    def delete_transport_key(self, key_id: int) -> bool:
        return self.sqlite_handler.delete_transport_key(key_id)

    def delete_advert(self, advert_id: int) -> bool:
        return self.sqlite_handler.delete_advert(advert_id)

    def get_hardware_stats(self) -> Optional[dict]:
        """Get current hardware statistics"""
        try:
            return self.hardware_stats.get_stats()
        except Exception as e:
            logger.error(f"Error getting hardware stats: {e}")
            return None

    def get_hardware_processes(self) -> Optional[list]:
        """Get current process summary"""
        try:
            return self.hardware_stats.get_processes_summary()
        except Exception as e:
            logger.error(f"Error getting hardware processes: {e}")
            return None
