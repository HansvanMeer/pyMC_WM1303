"""Centralized metrics retention for pyMC_Repeater (WM1303).

Implements tiered downsampling to reduce database size while preserving
historical trends:

  Tier      | Period    | Resolution    | Action
  ----------|-----------|---------------|-----------------------------------------
  Hot       | 0-7h      | Full          | Keep all original data points
  Warm      | 7-24h     | 1 minute      | Aggregate into _1m summary tables
  Cool      | 1-3 days  | 10 minutes    | Aggregate into _10m summary tables
  Cold      | 3-8 days  | 15 minutes    | Aggregate into _15m summary tables
  Expired   | >8 days   | Deleted       | Remove from all tables

Summary tables use option A: separate tables per resolution level.
After each cleanup pass, a WAL TRUNCATE checkpoint is performed.
"""
import logging
import os
import sqlite3
import threading
import ctypes
import ctypes.util

from contextlib import contextmanager as _contextmanager

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

import threading
import time
from typing import List, Tuple, Optional, Dict

logger = logging.getLogger("metrics_retention")

# --- malloc_trim helper ---------------------------------------------------
# Python's allocator (pymalloc + glibc malloc) holds onto freed memory in
# per-arena free lists and does not release it to the OS.  After a metrics
# cleanup or WAL checkpoint (which frees a lot of short-lived objects) we
# explicitly ask glibc to return released pages to the kernel via
# malloc_trim(0).  No-op on systems without glibc.
try:
    _libc = ctypes.CDLL(ctypes.util.find_library("c") or "libc.so.6", use_errno=True)
    if hasattr(_libc, "malloc_trim"):
        _libc.malloc_trim.argtypes = [ctypes.c_size_t]
        _libc.malloc_trim.restype = ctypes.c_int
        _HAS_MALLOC_TRIM = True
    else:
        _HAS_MALLOC_TRIM = False
except Exception:  # pragma: no cover - defensive
    _libc = None
    _HAS_MALLOC_TRIM = False


def malloc_trim() -> None:
    """Return freed memory pages to the kernel (glibc only)."""
    if _HAS_MALLOC_TRIM:
        try:
            _libc.malloc_trim(0)
        except Exception:  # pragma: no cover - defensive
            pass

DEFAULT_RETENTION_DAYS = 8
DEFAULT_CLEANUP_INTERVAL_S = 3600        # once per hour
DEFAULT_VACUUM_INTERVAL_S = 7 * 86400    # weekly

# Tier boundaries (in seconds from now)
TIER_HOT_SECONDS = 7 * 3600              # 7 hours
TIER_WARM_SECONDS = 24 * 3600            # 24 hours
TIER_COOL_SECONDS = 3 * 86400            # 3 days
# Cold = 3-8 days (until retention_days)

# Aggregation bucket sizes (in seconds)
BUCKET_1M = 60
BUCKET_10M = 600
BUCKET_15M = 900

# Tables that should only be deleted after retention (no downsampling)
# These are either already compact or not suitable for aggregation.
DELETE_ONLY_TABLES: List[Tuple[str, str, str]] = [
    ("repeater.db",         "packets",                 "timestamp"),
    ("repeater.db",         "adverts",                 "timestamp"),
    ("repeater.db",         "crc_errors",              "timestamp"),
    ("repeater.db",         "noise_floor",             "timestamp"),
    ("repeater.db",         "sx1261_health_events",    "timestamp"),
    ("spectrum_history.db", "spectrum_scans",          "timestamp"),
]

# Tables that get tiered downsampling.
# (db_name, source_table, ts_col, aggregation_config)
# The aggregation_config defines how to aggregate each table.
DOWNSAMPLE_TABLES: List[Dict] = [
    {
        "db": "repeater.db",
        "table": "packet_metrics",
        "ts_col": "timestamp",
        "group_cols": ["channel_id", "direction"],
        "agg_cols": [
            ("COUNT(*)",        "sample_count"),
            ("AVG(rssi)",       "avg_rssi"),
            ("MIN(rssi)",       "min_rssi"),
            ("MAX(rssi)",       "max_rssi"),
            ("AVG(snr)",        "avg_snr"),
            ("MIN(snr)",        "min_snr"),
            ("MAX(snr)",        "max_snr"),
            ("AVG(airtime_ms)", "avg_airtime_ms"),
            ("SUM(airtime_ms)", "total_airtime_ms"),
            ("SUM(length)",     "total_bytes"),
            ("AVG(hop_count)",  "avg_hop_count"),
            ("SUM(CASE WHEN crc_ok=0 THEN 1 ELSE 0 END)", "crc_error_count"),
        ],
    },
    {
        "db": "repeater.db",
        "table": "dedup_events",
        "ts_col": "ts",
        "group_cols": ["event_type", "source"],
        "agg_cols": [
            ("COUNT(*)",                "sample_count"),
            ("COUNT(DISTINCT pkt_hash)", "unique_packets"),
            ("SUM(pkt_size)",           "total_bytes"),
        ],
    },
    {
        "db": "repeater.db",
        "table": "noise_floor_history",
        "ts_col": "timestamp",
        "group_cols": ["channel_id"],
        "agg_cols": [
            ("COUNT(*)",                    "sample_count"),
            ("AVG(noise_floor_dbm)",        "avg_noise_floor_dbm"),
            ("MIN(noise_floor_dbm)",        "min_noise_floor_dbm"),
            ("MAX(noise_floor_dbm)",        "max_noise_floor_dbm"),
            ("SUM(samples_collected)",      "total_samples_collected"),
            ("SUM(samples_accepted)",       "total_samples_accepted"),
            ("MIN(min_rssi)",               "min_rssi"),
            ("MAX(max_rssi)",               "max_rssi"),
        ],
    },
    {
        "db": "repeater.db",
        "table": "cad_events",
        "ts_col": "timestamp",
        "group_cols": ["channel_id"],
        "agg_cols": [
            ("COUNT(*)",            "sample_count"),
            ("SUM(cad_clear)",      "total_cad_clear"),
            ("SUM(cad_detected)",   "total_cad_detected"),
            ("SUM(cad_skipped)",    "total_cad_skipped"),
            ("SUM(cad_hw_clear)",   "total_cad_hw_clear"),
            ("SUM(cad_hw_detected)", "total_cad_hw_detected"),
            ("SUM(cad_sw_clear)",   "total_cad_sw_clear"),
            ("SUM(cad_sw_detected)", "total_cad_sw_detected"),
        ],
    },
    {
        "db": "repeater.db",
        "table": "channel_stats_history",
        "ts_col": "timestamp",
        "group_cols": ["channel_id"],
        "agg_cols": [
            ("COUNT(*)",                "sample_count"),
            # NOTE: rx_count/tx_count/tx_failed/tx_airtime_ms/tx_bytes/
            # lbt_blocked/lbt_passed are CUMULATIVE counters in
            # channel_stats_history (monotonically increasing since service
            # start). Using SUM would multiply the cumulative value by the
            # number of samples in the bucket, which is meaningless. The
            # correct per-bucket delta is MAX(x) - MIN(x). With 1 sample per
            # bucket this yields 0 (no observable delta within that minute),
            # which is preferable to the previous fake number (cumulative
            # value masquerading as a delta). Higher-tier re-aggregation
            # (_1m -> _10m -> _15m) is handled by the existing SUM logic on
            # aliases starting with `total_`, which correctly sums the
            # per-minute deltas into 10/15-minute deltas.
            # NOTE: legacy `pkt_count` column was removed here — it is a
            # dead column in the WM1303 schema (never written by any
            # overlay code; RX totals come from packet_activity via
            # _pkt_counts_for). Referencing it caused
            # "no such column: pkt_count" WARNINGs that blocked every
            # channel_stats_history rollup tier (warm/cool/cold), leaving
            # channel_stats_history_{1m,10m,15m} empty and the base table
            # unbounded.
            ("MAX(rx_count) - MIN(rx_count)",             "total_rx_count"),
            ("AVG(avg_rssi)",                             "avg_rssi"),
            ("AVG(avg_snr)",                              "avg_snr"),
            ("MAX(tx_count) - MIN(tx_count)",             "total_tx_count"),
            ("MAX(tx_failed) - MIN(tx_failed)",           "total_tx_failed"),
            ("MAX(tx_airtime_ms) - MIN(tx_airtime_ms)",   "total_tx_airtime_ms"),
            ("MAX(tx_bytes) - MIN(tx_bytes)",             "total_tx_bytes"),
            ("MAX(lbt_blocked) - MIN(lbt_blocked)",       "total_lbt_blocked"),
            ("MAX(lbt_passed) - MIN(lbt_passed)",         "total_lbt_passed"),
            ("AVG(noise_floor_dbm)",                      "avg_noise_floor_dbm"),
            ("AVG(tx_noisefloor_dbm)",                    "avg_tx_noisefloor_dbm"),
        ],
    },
    {
        "db": "repeater.db",
        "table": "packet_activity",
        "ts_col": "timestamp",
        "group_cols": ["channel_id"],
        "agg_cols": [
            ("COUNT(*)",        "sample_count"),
            ("SUM(rx_count)",   "total_rx_count"),
            ("SUM(tx_count)",   "total_tx_count"),
        ],
    },
    {
        "db": "repeater.db",
        "table": "crc_error_rate",
        "ts_col": "timestamp",
        "group_cols": ["channel_id"],
        "agg_cols": [
            ("COUNT(*)",                "sample_count"),
            ("SUM(crc_error_count)",    "total_crc_errors"),
            ("SUM(crc_disabled_count)", "total_crc_disabled"),
        ],
    },
    {
        "db": "repeater.db",
        "table": "origin_channel_stats",
        "ts_col": "timestamp",
        "group_cols": ["channel_id"],
        "agg_cols": [
            ("COUNT(*)",    "sample_count"),
            ("SUM(count)",  "total_count"),
        ],
    },
]

DB_DIR = "/var/lib/pymc_repeater"


def _summary_table_name(base_table: str, suffix: str) -> str:
    """Generate summary table name: e.g., packet_metrics_1m"""
    return f"{base_table}_{suffix}"


def _create_summary_table(conn: sqlite3.Connection, cfg: Dict, suffix: str):
    """Create a summary table if it doesn't exist."""
    table_name = _summary_table_name(cfg["table"], suffix)
    group_cols = cfg["group_cols"]
    agg_cols = cfg["agg_cols"]

    cols = ["id INTEGER PRIMARY KEY AUTOINCREMENT",
            "bucket_ts REAL NOT NULL"]
    for gc in group_cols:
        cols.append(f"{gc} TEXT")
    for _, alias in agg_cols:
        cols.append(f"{alias} REAL")

    col_defs = ", ".join(cols)
    sql = f"CREATE TABLE IF NOT EXISTS {table_name} ({col_defs})"
    conn.execute(sql)

    # Create index on bucket_ts for fast range queries
    idx_name = f"idx_{table_name}_bucket_ts"
    conn.execute(f"CREATE INDEX IF NOT EXISTS {idx_name} ON {table_name}(bucket_ts)")

    # Create composite index for group+time queries
    if group_cols:
        idx_name2 = f"idx_{table_name}_grp_ts"
        grp_idx = ", ".join(group_cols + ["bucket_ts"])
        conn.execute(f"CREATE INDEX IF NOT EXISTS {idx_name2} ON {table_name}({grp_idx})")


def _aggregate_from_source(conn: sqlite3.Connection, cfg: Dict,
                           from_ts: float, to_ts: float,
                           bucket_seconds: int, target_suffix: str) -> int:
    """Aggregate raw source data into a summary table and delete originals.

    Used for the Warm tier (source → _1m).
    Returns the number of source rows deleted.
    """
    source_table = cfg["table"]
    ts_col = cfg["ts_col"]
    group_cols = cfg["group_cols"]
    agg_cols = cfg["agg_cols"]
    target_table = _summary_table_name(source_table, target_suffix)

    # Check if there's data in this range in the source table
    count_row = conn.execute(
        f"SELECT COUNT(*) FROM {source_table} WHERE {ts_col} >= ? AND {ts_col} < ?",
        (from_ts, to_ts)
    ).fetchone()
    if not count_row or count_row[0] == 0:
        return 0

    # Check if this range was already aggregated (avoid duplicates)
    existing = conn.execute(
        f"SELECT COUNT(*) FROM {target_table} WHERE bucket_ts >= ? AND bucket_ts < ?",
        (from_ts, to_ts)
    ).fetchone()
    if existing and existing[0] > 0:
        # Already aggregated — just delete source rows
        cur = conn.execute(
            f"DELETE FROM {source_table} WHERE {ts_col} >= ? AND {ts_col} < ?",
            (from_ts, to_ts)
        )
        return cur.rowcount

    # Build the aggregation query from raw source data
    bucket_expr = f"CAST(({ts_col} / {bucket_seconds}) AS INTEGER) * {bucket_seconds}"
    select_cols = [f"{bucket_expr} AS bucket_ts"]
    for gc in group_cols:
        select_cols.append(gc)
    for expr, alias in agg_cols:
        select_cols.append(f"{expr} AS {alias}")

    group_by = ["bucket_ts"] + group_cols
    select_sql = f"""SELECT {', '.join(select_cols)}
                     FROM {source_table}
                     WHERE {ts_col} >= ? AND {ts_col} < ?
                     GROUP BY {', '.join(group_by)}"""

    # Insert aggregated data into summary table
    insert_cols = ["bucket_ts"] + group_cols + [alias for _, alias in agg_cols]
    placeholders = ", ".join(["?"] * len(insert_cols))
    insert_sql = f"INSERT INTO {target_table} ({', '.join(insert_cols)}) VALUES ({placeholders})"

    rows = conn.execute(select_sql, (from_ts, to_ts)).fetchall()
    if rows:
        conn.executemany(insert_sql, rows)

    # Delete original rows that have been aggregated
    cur = conn.execute(
        f"DELETE FROM {source_table} WHERE {ts_col} >= ? AND {ts_col} < ?",
        (from_ts, to_ts)
    )
    return cur.rowcount


def _aggregate_from_summary(conn: sqlite3.Connection, cfg: Dict,
                            from_ts: float, to_ts: float,
                            source_suffix: str, bucket_seconds: int,
                            target_suffix: str) -> int:
    """Re-aggregate from a finer summary table into a coarser one.

    Used for cascading: _1m → _10m, _10m → _15m.
    Reads from the source summary table, aggregates into the target summary
    table, and deletes the consumed source summary rows.
    Returns the number of source summary rows deleted.
    """
    base_table = cfg["table"]
    group_cols = cfg["group_cols"]
    agg_cols = cfg["agg_cols"]
    source_table = _summary_table_name(base_table, source_suffix)
    target_table = _summary_table_name(base_table, target_suffix)

    # Check if there's data in this range in the source summary table
    count_row = conn.execute(
        f"SELECT COUNT(*) FROM {source_table} WHERE bucket_ts >= ? AND bucket_ts < ?",
        (from_ts, to_ts)
    ).fetchone()
    if not count_row or count_row[0] == 0:
        return 0

    # Check if this range was already aggregated in target
    existing = conn.execute(
        f"SELECT COUNT(*) FROM {target_table} WHERE bucket_ts >= ? AND bucket_ts < ?",
        (from_ts, to_ts)
    ).fetchone()
    if existing and existing[0] > 0:
        # Already aggregated — just delete source summary rows
        cur = conn.execute(
            f"DELETE FROM {source_table} WHERE bucket_ts >= ? AND bucket_ts < ?",
            (from_ts, to_ts)
        )
        return cur.rowcount

    # Build re-aggregation query from summary table.
    # Summary tables have: bucket_ts, group_cols, and agg columns.
    # For re-aggregation, we need to combine the summary values correctly:
    # - COUNT/SUM columns → SUM them
    # - AVG columns → weighted average using sample_count
    # - MIN columns → MIN
    # - MAX columns → MAX
    bucket_expr = f"CAST((bucket_ts / {bucket_seconds}) AS INTEGER) * {bucket_seconds}"
    select_cols = [f"{bucket_expr} AS new_bucket_ts"]
    for gc in group_cols:
        select_cols.append(gc)

    # Re-aggregate: for summary tables, all values are already aggregated.
    # We use the naming convention to determine re-aggregation strategy:
    # - *_count, total_* → SUM
    # - avg_* → weighted average (SUM(val * sample_count) / SUM(sample_count))
    # - min_* → MIN
    # - max_* → MAX
    reagg_exprs = []
    for _, alias in agg_cols:
        if alias == "sample_count":
            reagg_exprs.append((f"SUM({alias})", alias))
        elif alias.startswith("total_") or alias.endswith("_count"):
            reagg_exprs.append((f"SUM({alias})", alias))
        elif alias.startswith("avg_"):
            # Weighted average: SUM(avg_val * sample_count) / SUM(sample_count)
            reagg_exprs.append(
                (f"SUM({alias} * sample_count) / NULLIF(SUM(sample_count), 0)", alias))
        elif alias.startswith("min_"):
            reagg_exprs.append((f"MIN({alias})", alias))
        elif alias.startswith("max_"):
            reagg_exprs.append((f"MAX({alias})", alias))
        elif alias == "unique_packets":
            # Can't truly re-aggregate distinct counts, use SUM as approximation
            reagg_exprs.append((f"SUM({alias})", alias))
        else:
            # Default: SUM for counters, AVG for unknown
            reagg_exprs.append((f"SUM({alias})", alias))

    for expr, alias in reagg_exprs:
        select_cols.append(f"{expr} AS {alias}")

    group_by = ["new_bucket_ts"] + group_cols
    select_sql = f"""SELECT {', '.join(select_cols)}
                     FROM {source_table}
                     WHERE bucket_ts >= ? AND bucket_ts < ?
                     GROUP BY {', '.join(group_by)}"""

    # Insert into target
    insert_cols = ["bucket_ts"] + group_cols + [alias for _, alias in reagg_exprs]
    placeholders = ", ".join(["?"] * len(insert_cols))
    insert_sql = f"INSERT INTO {target_table} ({', '.join(insert_cols)}) VALUES ({placeholders})"

    rows = conn.execute(select_sql, (from_ts, to_ts)).fetchall()
    if rows:
        conn.executemany(insert_sql, rows)

    # Delete consumed source summary rows
    cur = conn.execute(
        f"DELETE FROM {source_table} WHERE bucket_ts >= ? AND bucket_ts < ?",
        (from_ts, to_ts)
    )
    return cur.rowcount


class MetricsRetention:
    def __init__(self,
                 retention_days: int = DEFAULT_RETENTION_DAYS,
                 cleanup_interval_s: int = DEFAULT_CLEANUP_INTERVAL_S,
                 vacuum_interval_s: int = DEFAULT_VACUUM_INTERVAL_S,
                 db_dir: str = DB_DIR):
        self.retention_days = retention_days
        self.cleanup_interval_s = cleanup_interval_s
        self.vacuum_interval_s = vacuum_interval_s
        self.db_dir = db_dir
        self._running = False
        self._thread: Optional[threading.Thread] = None
        # Persist last VACUUM timestamp so a service restart does not cause
        # an immediate VACUUM (which briefly uses 2-3x the DB size in RAM).
        self._vacuum_state_path = os.path.join(self.db_dir, ".last_vacuum")
        self._last_vacuum = self._load_last_vacuum()

    def _load_last_vacuum(self) -> float:
        """Load the last VACUUM timestamp from disk, or seed it to now.

        On first startup (no file yet) we seed with `time.time()` so the first
        VACUUM only runs after a full `vacuum_interval_s` has elapsed, instead
        of firing on every service restart.
        """
        try:
            with open(self._vacuum_state_path, "r") as fh:
                ts = float(fh.read().strip())
                if ts > 0 and ts <= time.time():
                    return ts
        except (OSError, ValueError):
            pass
        # No valid state found -> seed with now so first VACUUM is delayed.
        now = time.time()
        self._save_last_vacuum(now)
        return now

    def _save_last_vacuum(self, ts: float) -> None:
        """Persist the last VACUUM timestamp to disk."""
        try:
            os.makedirs(os.path.dirname(self._vacuum_state_path), exist_ok=True)
            with open(self._vacuum_state_path, "w") as fh:
                fh.write(str(ts))
        except OSError as exc:
            logger.debug("Could not persist vacuum timestamp: %s", exc)

    @property
    def retention_seconds(self) -> int:
        return self.retention_days * 86400

    def start(self):
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="MetricsRetention")
        self._thread.start()
        logger.info("MetricsRetention started (retention=%dd, cleanup_every=%ds, "
                    "tiers=7h/24h/3d/%dd)",
                    self.retention_days, self.cleanup_interval_s,
                    self.retention_days)

    def stop(self):
        self._running = False
        if self._thread:
            self._thread.join(timeout=5)

    def _run(self):
        # First run after 60s so service start is clean
        time.sleep(60)
        while self._running:
            try:
                self._ensure_summary_tables()
                self.cleanup_once()
                self._wal_truncate()
                if time.time() - self._last_vacuum >= self.vacuum_interval_s:
                    self.vacuum_once()
                    self._last_vacuum = time.time()
                    self._save_last_vacuum(self._last_vacuum)
                # Return freed pages to the OS to keep RSS low on small devices
                malloc_trim()
            except Exception as e:
                logger.error("Retention cycle error: %s", e)
            # Sleep in 5s chunks to allow clean shutdown
            for _ in range(self.cleanup_interval_s // 5):
                if not self._running:
                    return
                time.sleep(5)

    def _ensure_summary_tables(self):
        """Create summary tables if they don't exist yet."""
        by_db: Dict[str, list] = {}
        for cfg in DOWNSAMPLE_TABLES:
            by_db.setdefault(cfg["db"], []).append(cfg)

        for db_name, configs in by_db.items():
            db_path = os.path.join(self.db_dir, db_name)
            if not os.path.exists(db_path):
                continue
            try:
                with _db_conn(db_path, timeout=10) as conn:
                    for cfg in configs:
                        for suffix in ["1m", "10m", "15m"]:
                            _create_summary_table(conn, cfg, suffix)
                    conn.commit()
            except Exception as e:
                logger.warning("MetricsRetention: summary table creation failed for %s: %s",
                               db_name, e)

    def cleanup_once(self):
        """Run one complete cleanup cycle: downsample + delete expired."""
        now = time.time()
        total_deleted = 0
        total_aggregated = 0

        # --- Phase 1: Tiered downsampling ---
        # Warm tier: 7h-24h → 1 minute buckets
        warm_from = now - TIER_WARM_SECONDS
        warm_to = now - TIER_HOT_SECONDS

        # Cool tier: 24h-3d → 10 minute buckets
        cool_from = now - TIER_COOL_SECONDS
        cool_to = now - TIER_WARM_SECONDS

        # Cold tier: 3d-retention → 15 minute buckets
        cold_from = now - self.retention_seconds
        cold_to = now - TIER_COOL_SECONDS

        by_db: Dict[str, list] = {}
        for cfg in DOWNSAMPLE_TABLES:
            by_db.setdefault(cfg["db"], []).append(cfg)

        for db_name, configs in by_db.items():
            db_path = os.path.join(self.db_dir, db_name)
            if not os.path.exists(db_path):
                continue
            try:
                with _db_conn(db_path, timeout=30) as conn:
                    conn.execute("PRAGMA busy_timeout = 10000")
                    for cfg in configs:
                        table = cfg["table"]
                        ts_col = cfg["ts_col"]

                        # Check source table exists
                        exists = conn.execute(
                            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                            (table,)
                        ).fetchone()
                        if not exists:
                            continue

                        # Warm tier: aggregate 7h-24h into 1m buckets
                        try:
                            deleted = _aggregate_from_source(
                                conn, cfg, warm_from, warm_to, BUCKET_1M, "1m")
                            if deleted > 0:
                                total_deleted += deleted
                                total_aggregated += deleted
                                logger.debug("Tier warm: %s aggregated %d rows → _1m",
                                             table, deleted)
                        except Exception as e:
                            logger.warning("Tier warm %s failed: %s", table, e)

                        # Cool tier: cascade _1m → _10m (24h-3d)
                        try:
                            deleted = _aggregate_from_summary(
                                conn, cfg, cool_from, cool_to,
                                "1m", BUCKET_10M, "10m")
                            if deleted > 0:
                                total_deleted += deleted
                                total_aggregated += deleted
                                logger.debug("Tier cool: %s cascaded %d _1m rows → _10m",
                                             table, deleted)
                        except Exception as e:
                            logger.warning("Tier cool %s failed: %s", table, e)

                        # Cool tier fallback: source data older than 24h
                        # (first run or data that was never in _1m)
                        try:
                            deleted = _aggregate_from_source(
                                conn, cfg, cool_from, cool_to, BUCKET_10M, "10m")
                            if deleted > 0:
                                total_deleted += deleted
                                total_aggregated += deleted
                                logger.debug("Tier cool (source fallback): %s aggregated %d rows → _10m",
                                             table, deleted)
                        except Exception as e:
                            logger.warning("Tier cool fallback %s failed: %s", table, e)

                        # Cold tier: cascade _10m → _15m (3d-8d)
                        try:
                            deleted = _aggregate_from_summary(
                                conn, cfg, cold_from, cold_to,
                                "10m", BUCKET_15M, "15m")
                            if deleted > 0:
                                total_deleted += deleted
                                total_aggregated += deleted
                                logger.debug("Tier cold: %s cascaded %d _10m rows → _15m",
                                             table, deleted)
                        except Exception as e:
                            logger.warning("Tier cold %s failed: %s", table, e)

                        # Cold tier fallback: source data older than 3d
                        # (first run or data that was never in _1m/_10m)
                        try:
                            deleted = _aggregate_from_source(
                                conn, cfg, cold_from, cold_to, BUCKET_15M, "15m")
                            if deleted > 0:
                                total_deleted += deleted
                                total_aggregated += deleted
                                logger.debug("Tier cold (source fallback): %s aggregated %d rows → _15m",
                                             table, deleted)
                        except Exception as e:
                            logger.warning("Tier cold fallback %s failed: %s", table, e)

                        # Delete from source anything older than retention
                        try:
                            cutoff = now - self.retention_seconds
                            cur = conn.execute(
                                f"DELETE FROM {table} WHERE {ts_col} < ?",
                                (cutoff,)
                            )
                            if cur.rowcount > 0:
                                total_deleted += cur.rowcount
                                logger.info("MetricsRetention: %s.%s expired %d rows",
                                            db_name, table, cur.rowcount)
                        except Exception as e:
                            logger.warning("MetricsRetention: %s.%s expire failed: %s",
                                           db_name, table, e)

                    # Delete expired rows from summary tables too
                    for cfg in configs:
                        cutoff = now - self.retention_seconds
                        for suffix in ["1m", "10m", "15m"]:
                            summary_table = _summary_table_name(cfg["table"], suffix)
                            try:
                                cur = conn.execute(
                                    f"DELETE FROM {summary_table} WHERE bucket_ts < ?",
                                    (cutoff,)
                                )
                                if cur.rowcount > 0:
                                    total_deleted += cur.rowcount
                                    logger.debug("MetricsRetention: %s expired %d rows",
                                                 summary_table, cur.rowcount)
                            except Exception as e:
                                pass  # table might not exist yet

                    conn.commit()
            except Exception as e:
                logger.warning("MetricsRetention: %s downsample failed: %s", db_name, e)

        # --- Phase 2: Delete-only tables (no downsampling) ---
        cutoff = now - self.retention_seconds
        by_db_del: Dict[str, list] = {}
        for db_name, table, ts_col in DELETE_ONLY_TABLES:
            by_db_del.setdefault(db_name, []).append((table, ts_col))

        for db_name, tables in by_db_del.items():
            db_path = os.path.join(self.db_dir, db_name)
            if not os.path.exists(db_path):
                continue
            try:
                with _db_conn(db_path, timeout=10) as conn:
                    for table, ts_col in tables:
                        try:
                            exists = conn.execute(
                                "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                                (table,)
                            ).fetchone()
                            if not exists:
                                continue
                            cur = conn.execute(
                                f"DELETE FROM {table} WHERE {ts_col} < ?",
                                (cutoff,)
                            )
                            if cur.rowcount > 0:
                                total_deleted += cur.rowcount
                                logger.info("MetricsRetention: %s.%s deleted %d rows",
                                            db_name, table, cur.rowcount)
                        except Exception as e:
                            logger.warning("MetricsRetention: %s.%s cleanup failed: %s",
                                           db_name, table, e)
                    conn.commit()
            except Exception as e:
                logger.warning("MetricsRetention: %s open failed: %s", db_name, e)

        if total_aggregated > 0:
            logger.info("MetricsRetention cleanup complete: %d rows deleted "
                        "(%d aggregated into summary tables)",
                        total_deleted, total_aggregated)
        else:
            logger.info("MetricsRetention cleanup pass complete, %d rows deleted",
                        total_deleted)

    def _wal_truncate(self):
        """Perform WAL TRUNCATE checkpoint to keep WAL file compact."""
        db_names = set()
        for cfg in DOWNSAMPLE_TABLES:
            db_names.add(cfg["db"])
        for db_name, _, _ in DELETE_ONLY_TABLES:
            db_names.add(db_name)

        for db_name in db_names:
            db_path = os.path.join(self.db_dir, db_name)
            if not os.path.exists(db_path):
                continue
            try:
                with _db_conn(db_path, timeout=10) as conn:
                    result = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
                    if result and result[1] > 0:
                        logger.debug("WAL truncate %s: pages=%d, checkpointed=%d",
                                     db_name, result[1], result[2])
            except Exception as e:
                logger.debug("WAL truncate %s failed: %s", db_name, e)

    def vacuum_once(self):
        """Run VACUUM on all databases to reclaim disk space."""
        db_names = set()
        for cfg in DOWNSAMPLE_TABLES:
            db_names.add(cfg["db"])
        for db_name, _, _ in DELETE_ONLY_TABLES:
            db_names.add(db_name)

        for db_name in db_names:
            db_path = os.path.join(self.db_dir, db_name)
            if not os.path.exists(db_path):
                continue
            try:
                with _db_conn(db_path, timeout=30) as conn:
                    conn.execute("VACUUM")
                logger.info("MetricsRetention: VACUUM %s complete", db_name)
            except Exception as e:
                logger.warning("MetricsRetention: VACUUM %s failed: %s", db_name, e)


_singleton: Optional[MetricsRetention] = None


def get_retention() -> MetricsRetention:
    global _singleton
    if _singleton is None:
        # Read config if available
        retention_days = DEFAULT_RETENTION_DAYS
        try:
            from repeater import config as _cfg
            cfg = getattr(_cfg, "CONFIG", None) or {}
            retention_days = int(
                cfg.get("storage", {}).get("retention", {}).get("metrics_days",
                                                               DEFAULT_RETENTION_DAYS)
            )
        except Exception:
            pass
        _singleton = MetricsRetention(retention_days=retention_days)
    return _singleton


def start():
    get_retention().start()
