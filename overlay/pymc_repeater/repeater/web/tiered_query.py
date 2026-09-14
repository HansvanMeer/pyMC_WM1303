"""Tiered query utility for pyMC_Repeater (WM1303).

Provides seamless querying across the multi-tier metrics storage system.
When the metrics retention system aggregates raw data into summary tables,
this module transparently queries the correct tier for each time segment
and returns a unified result set.

Tier layout:
  Tier   | Age           | Table suffix | Resolution
  -------|---------------|--------------|------------
  Hot    | 0 - 7 h       | (raw)        | Full
  Warm   | 7 h - 24 h    | _1m          | 1 minute
  Cool   | 24 h - 3 d    | _10m         | 10 minutes
  Cold   | 3 d - 8 d     | _15m         | 15 minutes

Usage:
    from repeater.web.tiered_query import tiered_channel_query

    with _db_conn(db_path) as conn:
        rows = tiered_channel_query(
            conn,
            table_name="packet_activity",
            channel_id="channel_a",
            since_ts=time.time() - 86400,
            until_ts=time.time(),
            bucket_seconds=900,
        )
        # rows = [{"bucket_ts": 1234567800, "total_rx_count": 5, ...}, ...]
"""
import logging
import time
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger("tiered_query")

# ---------------------------------------------------------------------------
# Tier boundary constants (mirrored from metrics_retention)
# ---------------------------------------------------------------------------
TIER_HOT_SECONDS = 7 * 3600        # 7 hours
TIER_WARM_SECONDS = 24 * 3600      # 24 hours
TIER_COOL_SECONDS = 3 * 86400      # 3 days
TIER_RETENTION_SECONDS = 8 * 86400  # 8 days (default max)

# Summary table resolutions (seconds)
RES_1M = 60
RES_10M = 600
RES_15M = 900

# Small overlap at tier boundaries to avoid gaps caused by timing jitter
# between the retention cleanup cycle and real-time queries.
TIER_OVERLAP_S = 60

# ---------------------------------------------------------------------------
# Table registry — maps base table names to aggregation metadata.
# This mirrors DOWNSAMPLE_TABLES from metrics_retention.py but is kept
# self-contained so the query module has no import dependency on the
# retention thread.
# ---------------------------------------------------------------------------
# Each entry: {
#   "ts_col":      timestamp column in the raw table,
#   "group_cols":  grouping columns (present in both raw and summary),
#   "agg_cols":    [(raw_expr, summary_alias), ...]
# }
# The summary_alias is the column name in _1m/_10m/_15m tables.
# The raw_expr is the SQL aggregation applied to the raw table.
# TODO #234: marker for source columns holding a CUMULATIVE (monotonically
# increasing since service start) counter rather than an interval value.
# An agg_cols entry of the form ("CUMDELTA:<source_column>", "<alias>") tells
# _build_raw_query() to emit the per-bucket delta relative to the PREVIOUS
# bucket (window function LAG), clamped at 0 so a counter reset on service
# restart yields 0 instead of a large negative spike.
#
# This mirrors the identical constant in metrics_retention.py. The write side
# (rollup into _1m/_10m/_15m) and this read side (raw hot-tier queries) must
# use the same delta definition, otherwise a single series shows a step at the
# 7-hour hot/warm tier boundary.
CUMDELTA_PREFIX = "CUMDELTA:"

_TABLE_REGISTRY: Dict[str, Dict] = {
    "packet_activity": {
        "ts_col": "timestamp",
        "group_cols": ["channel_id"],
        "agg_cols": [
            ("COUNT(*)",        "sample_count"),
            ("SUM(rx_count)",   "total_rx_count"),
            ("SUM(tx_count)",   "total_tx_count"),
        ],
    },
    "noise_floor_history": {
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
    "channel_stats_history": {
        "ts_col": "timestamp",
        "group_cols": ["channel_id"],
        "agg_cols": [
            ("COUNT(*)",                                  "sample_count"),
            # NOTE: cumulative counters in channel_stats_history
            # (rx_count, tx_count, tx_failed, tx_airtime_ms, tx_bytes,
            # lbt_blocked, lbt_passed) are monotonic since
            # service start.
            # TODO #234: these were previously differenced WITHIN the bucket
            # via MAX(x) - MIN(x). The snapshot loop writes this table once
            # per 60 s, so a 60 s bucket holds exactly one sample and that
            # expression is 0 by construction. Coarser buckets only appeared
            # to work: they still dropped every increment that fell between
            # the last sample of one bucket and the first of the next.
            # They are now marked with CUMDELTA_PREFIX so _build_raw_query()
            # differences against the PREVIOUS bucket with a LAG() window
            # function, which is correct at every bucket size.
            # Must match the aggregation in metrics_retention.py, otherwise a
            # series crossing the 7-hour hot/warm boundary shows a step.
            # NOTE: legacy `pkt_count` column was removed here — it is a
            # dead column in the WM1303 schema (never written by any
            # overlay code; RX totals come from packet_activity via
            # _pkt_counts_for). Referencing it caused
            # "no such column: pkt_count" errors that broke tiered
            # channel_stats_history queries.
            ("CUMDELTA:rx_count",                         "total_rx_count"),
            ("AVG(avg_rssi)",                             "avg_rssi"),
            ("AVG(avg_snr)",                              "avg_snr"),
            ("CUMDELTA:tx_count",                         "total_tx_count"),
            ("CUMDELTA:tx_failed",                        "total_tx_failed"),
            ("CUMDELTA:tx_airtime_ms",                    "total_tx_airtime_ms"),
            ("CUMDELTA:tx_bytes",                         "total_tx_bytes"),
            ("CUMDELTA:lbt_blocked",                      "total_lbt_blocked"),
            ("CUMDELTA:lbt_passed",                       "total_lbt_passed"),
            ("AVG(noise_floor_dbm)",                      "avg_noise_floor_dbm"),
            ("AVG(tx_noisefloor_dbm)",                    "avg_tx_noisefloor_dbm"),
            # TODO #243: must mirror metrics_retention.py exactly.
            ("AVG(lbt_last_rssi)",                        "avg_lbt_last_rssi"),
        ],
    },
    "dedup_events": {
        "ts_col": "ts",
        "group_cols": ["event_type", "source"],
        "agg_cols": [
            ("COUNT(*)",                 "sample_count"),
            ("COUNT(DISTINCT pkt_hash)", "unique_packets"),
            ("SUM(pkt_size)",            "total_bytes"),
        ],
    },
    "cad_events": {
        "ts_col": "timestamp",
        "group_cols": ["channel_id"],
        "agg_cols": [
            ("COUNT(*)",              "sample_count"),
            ("SUM(cad_clear)",        "total_cad_clear"),
            ("SUM(cad_detected)",     "total_cad_detected"),
            ("SUM(cad_skipped)",      "total_cad_skipped"),
            ("SUM(cad_hw_clear)",     "total_cad_hw_clear"),
            ("SUM(cad_hw_detected)",  "total_cad_hw_detected"),
            ("SUM(cad_sw_clear)",     "total_cad_sw_clear"),
            ("SUM(cad_sw_detected)",  "total_cad_sw_detected"),
            # TODO #242: must mirror metrics_retention.py exactly.
            ("SUM(cad_busy_events)",  "total_cad_busy_events"),
        ],
    },
    "crc_error_rate": {
        "ts_col": "timestamp",
        "group_cols": ["channel_id"],
        "agg_cols": [
            ("COUNT(*)",                "sample_count"),
            ("SUM(crc_error_count)",    "total_crc_errors"),
            ("SUM(crc_disabled_count)", "total_crc_disabled"),
        ],
    },
    "packet_metrics": {
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
            # TODO #245: must mirror metrics_retention.py exactly.
            ("SUM(wait_time_ms)", "total_wait_time_ms"),
            ("SUM(length)",     "total_bytes"),
            ("AVG(hop_count)",  "avg_hop_count"),
            ("SUM(CASE WHEN crc_ok=0 THEN 1 ELSE 0 END)", "crc_error_count"),
        ],
    },
    "origin_channel_stats": {
        "ts_col": "timestamp",
        "group_cols": ["channel_id"],
        "agg_cols": [
            ("COUNT(*)",    "sample_count"),
            ("SUM(count)",  "total_count"),
        ],
    },
}


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _table_exists(conn, table_name: str) -> bool:
    """Check if a table exists in the connected database."""
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (table_name,)
    ).fetchone()
    return row is not None


def _summary_table_name(base_table: str, suffix: str) -> str:
    """Generate summary table name, e.g. packet_activity_1m."""
    return f"{base_table}_{suffix}"


def _tier_for_age(age_seconds: float) -> str:
    """Return the tier name for a given age in seconds from now."""
    if age_seconds <= TIER_HOT_SECONDS:
        return "hot"
    if age_seconds <= TIER_WARM_SECONDS:
        return "warm"
    if age_seconds <= TIER_COOL_SECONDS:
        return "cool"
    return "cold"


def _tier_suffix(tier: str) -> Optional[str]:
    """Return the summary table suffix for a tier, or None for hot (raw)."""
    return {"hot": None, "warm": "1m", "cool": "10m", "cold": "15m"}.get(tier)


def _tier_resolution(tier: str) -> int:
    """Return the native resolution in seconds for a tier."""
    return {"hot": 1, "warm": RES_1M, "cool": RES_10M, "cold": RES_15M}.get(tier, 1)


def _compute_tier_segments(
    since_ts: float, until_ts: float, now: float = None,
) -> List[Dict]:
    """Split a time range into tier segments.

    Returns a list of dicts, each with:
      - tier:    'hot' | 'warm' | 'cool' | 'cold'
      - start:   segment start timestamp
      - end:     segment end timestamp
      - suffix:  summary table suffix (None for hot/raw)
      - resolution: native resolution in seconds

    Segments are ordered oldest-first and have small overlaps at boundaries
    to prevent data gaps.
    """
    if now is None:
        now = time.time()

    # Define tier boundaries as absolute timestamps.
    # Each boundary is (start_ts, end_ts, tier_name).
    boundaries = [
        (now - TIER_RETENTION_SECONDS, now - TIER_COOL_SECONDS,  "cold"),
        (now - TIER_COOL_SECONDS,      now - TIER_WARM_SECONDS,  "cool"),
        (now - TIER_WARM_SECONDS,      now - TIER_HOT_SECONDS,   "warm"),
        (now - TIER_HOT_SECONDS,       now,                      "hot"),
    ]

    segments = []
    for tier_start, tier_end, tier_name in boundaries:
        # Apply overlap tolerance: extend the tier start slightly earlier
        # so we don't miss rows right at the boundary.
        effective_start = tier_start - TIER_OVERLAP_S

        # Intersect with requested range
        seg_start = max(effective_start, since_ts)
        seg_end = min(tier_end, until_ts)

        if seg_start >= seg_end:
            continue

        segments.append({
            "tier": tier_name,
            "start": seg_start,
            "end": seg_end,
            "suffix": _tier_suffix(tier_name),
            "resolution": _tier_resolution(tier_name),
        })

    return segments


def _reagg_expr(alias: str) -> str:
    """Return the SQL re-aggregation expression for a summary column.

    Uses the naming convention established in metrics_retention.py:
      - sample_count, total_*, *_count  → SUM
      - avg_*                           → weighted average via sample_count
      - min_*                           → MIN
      - max_*                           → MAX
      - unique_packets                  → SUM (approximation)
    """
    if alias == "sample_count":
        return f"SUM({alias})"
    if alias.startswith("total_") or alias.endswith("_count"):
        return f"SUM({alias})"
    if alias.startswith("avg_"):
        return f"SUM({alias} * sample_count) / NULLIF(SUM(sample_count), 0)"
    if alias.startswith("min_"):
        return f"MIN({alias})"
    if alias.startswith("max_"):
        return f"MAX({alias})"
    if alias == "unique_packets":
        return f"SUM({alias})"
    # Default: SUM (safe for counters)
    return f"SUM({alias})"


def _build_raw_query(
    table_name: str,
    ts_col: str,
    bucket_seconds: int,
    agg_cols: List[Tuple[str, str]],
    group_cols: List[str],
    filter_col: Optional[str],
    filter_val,
    seg_start: float,
    seg_end: float,
    extra_group_cols: Optional[List[str]] = None,
) -> Tuple[str, list]:
    """Build a bucketed aggregation query against the raw source table.

    Returns (sql, params).
    """
    bucket_expr = f"CAST(({ts_col} / {bucket_seconds}) AS INTEGER) * {bucket_seconds}"

    all_group_cols = list(group_cols)
    if extra_group_cols:
        for gc in extra_group_cols:
            if gc not in all_group_cols:
                all_group_cols.append(gc)

    where_parts = [f"{ts_col} >= ?", f"{ts_col} < ?"]
    params = [seg_start, seg_end]

    if filter_col and filter_val is not None:
        where_parts.append(f"{filter_col} = ?")
        params.append(filter_val)

    group_by = ["bucket_ts"] + all_group_cols
    has_cumdelta = any(expr.startswith(CUMDELTA_PREFIX) for expr, _ in agg_cols)

    if not has_cumdelta:
        select_parts = [f"{bucket_expr} AS bucket_ts"]
        for gc in all_group_cols:
            select_parts.append(gc)
        for raw_expr, alias in agg_cols:
            select_parts.append(f"{raw_expr} AS {alias}")

        sql = (
            f"SELECT {', '.join(select_parts)} "
            f"FROM {table_name} "
            f"WHERE {' AND '.join(where_parts)} "
            f"GROUP BY {', '.join(group_by)} "
            f"ORDER BY bucket_ts ASC"
        )
        return sql, params

    # TODO #234: two-stage query for cumulative counters.
    #   Stage 1 (_buckets) collapses each bucket and keeps the LAST cumulative
    #           level seen in it. The counters are monotonic, so MAX is the
    #           last value.
    #   Stage 2 turns those per-bucket levels into per-bucket deltas with LAG
    #           across the bucket boundary.
    # The outer column order must stay identical to agg_cols, because
    # _rows_to_dicts() maps result columns onto aliases by position.
    inner_parts = [f"{bucket_expr} AS bucket_ts"]
    outer_parts = ["bucket_ts"]
    for gc in all_group_cols:
        inner_parts.append(gc)
        outer_parts.append(gc)

    for raw_expr, alias in agg_cols:
        if raw_expr.startswith(CUMDELTA_PREFIX):
            src_col = raw_expr[len(CUMDELTA_PREFIX):]
            level = f"_cum_{alias}"
            inner_parts.append(f"MAX({src_col}) AS {level}")
            # COALESCE(LAG(...), level) makes the first bucket of the segment
            # yield 0 rather than NULL; the 2-argument scalar MAX clamps at 0
            # so a counter reset on service restart cannot produce a large
            # negative spike.
            outer_parts.append(
                f"MAX(0, {level} - COALESCE(LAG({level}) OVER _w, {level})) "
                f"AS {alias}"
            )
        else:
            inner_parts.append(f"{raw_expr} AS {alias}")
            outer_parts.append(alias)

    partition = (
        f"PARTITION BY {', '.join(all_group_cols)} " if all_group_cols else ""
    )
    sql = (
        f"WITH _buckets AS ("
        f"SELECT {', '.join(inner_parts)} "
        f"FROM {table_name} "
        f"WHERE {' AND '.join(where_parts)} "
        f"GROUP BY {', '.join(group_by)}"
        f") "
        f"SELECT {', '.join(outer_parts)} "
        f"FROM _buckets "
        f"WINDOW _w AS ({partition}ORDER BY bucket_ts) "
        f"ORDER BY bucket_ts ASC"
    )
    return sql, params


def _build_summary_query(
    summary_table: str,
    bucket_seconds: int,
    summary_resolution: int,
    agg_cols: List[Tuple[str, str]],
    group_cols: List[str],
    filter_col: Optional[str],
    filter_val,
    seg_start: float,
    seg_end: float,
    extra_group_cols: Optional[List[str]] = None,
) -> Tuple[str, list]:
    """Build a query against a summary table, re-bucketing if needed.

    If bucket_seconds equals the summary resolution, a simple SELECT is used.
    If bucket_seconds is larger, we re-aggregate into coarser buckets.
    If bucket_seconds is smaller than the summary resolution, we still return
    the summary data at its native resolution (cannot increase resolution).

    Returns (sql, params).
    """
    all_group_cols = list(group_cols)
    if extra_group_cols:
        for gc in extra_group_cols:
            if gc not in all_group_cols:
                all_group_cols.append(gc)

    where_parts = ["bucket_ts >= ?", "bucket_ts < ?"]
    params = [seg_start, seg_end]

    if filter_col and filter_val is not None:
        where_parts.append(f"{filter_col} = ?")
        params.append(filter_val)

    needs_rebucket = bucket_seconds > summary_resolution

    if needs_rebucket:
        # Re-aggregate into coarser buckets
        bucket_expr = f"CAST((bucket_ts / {bucket_seconds}) AS INTEGER) * {bucket_seconds}"
        select_parts = [f"{bucket_expr} AS rebucket_ts"]
        for gc in all_group_cols:
            select_parts.append(gc)
        for _, alias in agg_cols:
            select_parts.append(f"{_reagg_expr(alias)} AS {alias}")

        group_by = ["rebucket_ts"] + all_group_cols

        sql = (
            f"SELECT {', '.join(select_parts)} "
            f"FROM {summary_table} "
            f"WHERE {' AND '.join(where_parts)} "
            f"GROUP BY {', '.join(group_by)} "
            f"ORDER BY rebucket_ts ASC"
        )
    else:
        # Direct read (summary resolution matches or is coarser than requested)
        select_parts = ["bucket_ts"]
        for gc in all_group_cols:
            select_parts.append(gc)
        for _, alias in agg_cols:
            select_parts.append(alias)

        sql = (
            f"SELECT {', '.join(select_parts)} "
            f"FROM {summary_table} "
            f"WHERE {' AND '.join(where_parts)} "
            f"ORDER BY bucket_ts ASC"
        )

    return sql, params


def _rows_to_dicts(
    cursor_rows,
    agg_cols: List[Tuple[str, str]],
    group_cols: List[str],
    extra_group_cols: Optional[List[str]] = None,
) -> List[Dict]:
    """Convert raw cursor rows into a list of dicts.

    Expected column order: bucket_ts, *group_cols, *extra_group_cols, *agg_alias
    """
    all_group_cols = list(group_cols)
    if extra_group_cols:
        for gc in extra_group_cols:
            if gc not in all_group_cols:
                all_group_cols.append(gc)

    col_names = ["bucket_ts"] + all_group_cols + [alias for _, alias in agg_cols]
    results = []
    for row in cursor_rows:
        d = {}
        for i, name in enumerate(col_names):
            d[name] = row[i] if i < len(row) else None
        results.append(d)
    return results


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def tiered_channel_query(
    conn,
    table_name: str,
    channel_id: Optional[str],
    since_ts: float,
    until_ts: float,
    bucket_seconds: int,
    columns: Optional[List[str]] = None,
    group_cols: Optional[List[str]] = None,
    extra_filters: Optional[Dict[str, str]] = None,
) -> List[Dict]:
    """Query data across all tiers for a given timeframe.

    Transparently reads from raw and summary tables as needed, then returns
    a single merged result set sorted by ``bucket_ts`` ascending.

    Parameters
    ----------
    conn : sqlite3.Connection
        An open database connection (use ``_db_conn`` context manager).
    table_name : str
        Base table name, e.g. ``"packet_activity"``.
        Must be registered in the internal table registry.
    channel_id : str or None
        Channel filter value.  Pass ``None`` to skip channel filtering
        (useful for tables like ``dedup_events`` that group differently).
    since_ts : float
        Start of the query window (Unix timestamp, inclusive).
    until_ts : float
        End of the query window (Unix timestamp, exclusive).
    bucket_seconds : int
        Desired bucket width in seconds for the output.
    columns : list of str, optional
        Subset of summary column aliases to return.  ``None`` returns all
        columns defined in the table registry.
    group_cols : list of str, optional
        Override the default group columns from the registry.  Useful for
        queries that need additional or fewer grouping dimensions.
    extra_filters : dict, optional
        Additional column=value filters applied to all tier queries.
        Example: ``{"direction": "rx"}`` for packet_metrics.

    Returns
    -------
    list of dict
        Each dict contains ``"bucket_ts"`` plus the requested aggregation
        columns.  Rows are sorted by ``bucket_ts`` ascending.
        If the table is not registered or does not exist, returns ``[]``.
    """
    cfg = _TABLE_REGISTRY.get(table_name)
    if cfg is None:
        logger.warning("tiered_query: unknown table %r", table_name)
        return []

    ts_col = cfg["ts_col"]
    default_group_cols = cfg["group_cols"]
    agg_cols = cfg["agg_cols"]

    # Determine filter column for channel-based filtering
    filter_col = None
    filter_val = None
    if channel_id is not None and "channel_id" in default_group_cols:
        filter_col = "channel_id"
        filter_val = channel_id

    # Allow caller to override group columns
    effective_group_cols = group_cols if group_cols is not None else default_group_cols

    # Collect any extra group cols not in the default set
    extra_group_list = None
    if group_cols is not None:
        extra_group_list = [g for g in group_cols if g not in default_group_cols]

    # Filter output columns if requested
    if columns is not None:
        col_set = set(columns)
        # Always include sample_count for weighted averages in re-aggregation
        col_set.add("sample_count")
        filtered_agg_cols = [(expr, alias) for expr, alias in agg_cols
                             if alias in col_set]
    else:
        filtered_agg_cols = list(agg_cols)

    # Apply extra filters
    extra_filter_parts = []
    extra_filter_params = []
    if extra_filters:
        for col, val in extra_filters.items():
            extra_filter_parts.append(f"{col} = ?")
            extra_filter_params.append(val)

    # Compute tier segments for the requested time range
    now = time.time()
    segments = _compute_tier_segments(since_ts, until_ts, now)

    if not segments:
        return []

    # Collect rows from all tier segments
    all_rows = []
    seen_buckets = set()  # Track (bucket_ts, *group_vals) to deduplicate overlaps

    for seg in segments:
        tier = seg["tier"]
        seg_start = seg["start"]
        seg_end = seg["end"]
        suffix = seg["suffix"]
        resolution = seg["resolution"]

        try:
            if suffix is None:
                # Hot tier: query raw source table
                if not _table_exists(conn, table_name):
                    continue

                sql, params = _build_raw_query(
                    table_name, ts_col, bucket_seconds,
                    filtered_agg_cols, effective_group_cols,
                    filter_col, filter_val,
                    seg_start, seg_end,
                    extra_group_list,
                )
                # Append extra filters
                if extra_filter_parts:
                    # Insert extra WHERE clauses
                    where_idx = sql.index("GROUP BY")
                    extra_where = " AND ".join(extra_filter_parts)
                    sql = sql[:where_idx] + f"AND {extra_where} " + sql[where_idx:]
                    params.extend(extra_filter_params)

                rows = conn.execute(sql, params).fetchall()
                dicts = _rows_to_dicts(rows, filtered_agg_cols,
                                       effective_group_cols, extra_group_list)
            else:
                # Summary tier: query the summary table
                summary_table = _summary_table_name(table_name, suffix)
                if not _table_exists(conn, summary_table):
                    # Fall back to raw table if summary doesn't exist yet
                    if _table_exists(conn, table_name):
                        sql, params = _build_raw_query(
                            table_name, ts_col, bucket_seconds,
                            filtered_agg_cols, effective_group_cols,
                            filter_col, filter_val,
                            seg_start, seg_end,
                            extra_group_list,
                        )
                        if extra_filter_parts:
                            where_idx = sql.index("GROUP BY")
                            extra_where = " AND ".join(extra_filter_parts)
                            sql = sql[:where_idx] + f"AND {extra_where} " + sql[where_idx:]
                            params.extend(extra_filter_params)
                        rows = conn.execute(sql, params).fetchall()
                        dicts = _rows_to_dicts(rows, filtered_agg_cols,
                                               effective_group_cols,
                                               extra_group_list)
                    else:
                        continue
                else:
                    sql, params = _build_summary_query(
                        summary_table, bucket_seconds, resolution,
                        filtered_agg_cols, effective_group_cols,
                        filter_col, filter_val,
                        seg_start, seg_end,
                        extra_group_list,
                    )
                    if extra_filter_parts:
                        if "GROUP BY" in sql:
                            where_idx = sql.index("GROUP BY")
                        else:
                            where_idx = sql.index("ORDER BY")
                        extra_where = " AND ".join(extra_filter_parts)
                        sql = sql[:where_idx] + f"AND {extra_where} " + sql[where_idx:]
                        params.extend(extra_filter_params)

                    rows = conn.execute(sql, params).fetchall()
                    dicts = _rows_to_dicts(rows, filtered_agg_cols,
                                           effective_group_cols,
                                           extra_group_list)

            # Deduplicate rows in the overlap zone
            for d in dicts:
                group_key_parts = [d.get("bucket_ts")]
                for gc in effective_group_cols:
                    group_key_parts.append(d.get(gc))
                key = tuple(group_key_parts)
                if key not in seen_buckets:
                    seen_buckets.add(key)
                    all_rows.append(d)

        except Exception as e:
            logger.warning(
                "tiered_query: tier %s query failed for %s: %s",
                tier, table_name, e,
            )
            continue

    # Sort by bucket_ts ascending
    all_rows.sort(key=lambda r: (r.get("bucket_ts", 0),))

    # Optionally strip sample_count if the caller didn't request it
    if columns is not None and "sample_count" not in columns:
        for row in all_rows:
            row.pop("sample_count", None)

    return all_rows


# ---------------------------------------------------------------------------
# Specialized helpers for common query patterns
# ---------------------------------------------------------------------------

def tiered_packet_activity_query(
    conn,
    channel_id: str,
    since_ts: float,
    until_ts: float,
    bucket_seconds: int,
) -> List[Dict]:
    """Tiered query for packet_activity (RX/TX counts per channel).

    Returns rows with: bucket_ts, total_rx_count, total_tx_count
    """
    return tiered_channel_query(
        conn,
        table_name="packet_activity",
        channel_id=channel_id,
        since_ts=since_ts,
        until_ts=until_ts,
        bucket_seconds=bucket_seconds,
        columns=["total_rx_count", "total_tx_count"],
    )


def tiered_noise_floor_query(
    conn,
    channel_id: str,
    since_ts: float,
    until_ts: float,
    bucket_seconds: int,
) -> List[Dict]:
    """Tiered query for noise_floor_history (per-channel noise floor).

    Returns rows with: bucket_ts, avg_noise_floor_dbm, min_noise_floor_dbm,
    max_noise_floor_dbm, min_rssi, max_rssi
    """
    return tiered_channel_query(
        conn,
        table_name="noise_floor_history",
        channel_id=channel_id,
        since_ts=since_ts,
        until_ts=until_ts,
        bucket_seconds=bucket_seconds,
        columns=[
            "avg_noise_floor_dbm", "min_noise_floor_dbm",
            "max_noise_floor_dbm", "min_rssi", "max_rssi",
            "total_samples_collected", "total_samples_accepted",
        ],
    )


def tiered_channel_stats_query(
    conn,
    channel_id: str,
    since_ts: float,
    until_ts: float,
    bucket_seconds: int,
    columns: Optional[List[str]] = None,
) -> List[Dict]:
    """Tiered query for channel_stats_history.

    Returns all aggregated channel stats columns unless a subset is specified.
    """
    return tiered_channel_query(
        conn,
        table_name="channel_stats_history",
        channel_id=channel_id,
        since_ts=since_ts,
        until_ts=until_ts,
        bucket_seconds=bucket_seconds,
        columns=columns,
    )


def tiered_dedup_query(
    conn,
    since_ts: float,
    until_ts: float,
    bucket_seconds: int,
) -> List[Dict]:
    """Tiered query for dedup_events (groups by event_type and source).

    Returns rows with: bucket_ts, event_type, source, sample_count,
    unique_packets, total_bytes

    Note: Unlike channel-based tables, dedup_events does not filter by
    channel_id.  The group columns are ``event_type`` and ``source``.
    """
    return tiered_channel_query(
        conn,
        table_name="dedup_events",
        channel_id=None,
        since_ts=since_ts,
        until_ts=until_ts,
        bucket_seconds=bucket_seconds,
    )


def tiered_cad_events_query(
    conn,
    channel_id: str,
    since_ts: float,
    until_ts: float,
    bucket_seconds: int,
) -> List[Dict]:
    """Tiered query for cad_events (CAD clear/detected/skipped per channel).

    Returns rows with: bucket_ts, total_cad_clear, total_cad_detected,
    total_cad_skipped, total_cad_hw_clear, total_cad_hw_detected,
    total_cad_sw_clear, total_cad_sw_detected
    """
    return tiered_channel_query(
        conn,
        table_name="cad_events",
        channel_id=channel_id,
        since_ts=since_ts,
        until_ts=until_ts,
        bucket_seconds=bucket_seconds,
        columns=[
            "total_cad_clear", "total_cad_detected", "total_cad_skipped",
            "total_cad_hw_clear", "total_cad_hw_detected",
            "total_cad_sw_clear", "total_cad_sw_detected",
            "total_cad_busy_events",
        ],
    )


def tiered_crc_error_rate_query(
    conn,
    channel_id: str,
    since_ts: float,
    until_ts: float,
    bucket_seconds: int,
) -> List[Dict]:
    """Tiered query for crc_error_rate (per-channel CRC errors).

    Returns rows with: bucket_ts, total_crc_errors, total_crc_disabled
    """
    return tiered_channel_query(
        conn,
        table_name="crc_error_rate",
        channel_id=channel_id,
        since_ts=since_ts,
        until_ts=until_ts,
        bucket_seconds=bucket_seconds,
        columns=["total_crc_errors", "total_crc_disabled"],
    )


def tiered_packet_metrics_query(
    conn,
    channel_id: str,
    since_ts: float,
    until_ts: float,
    bucket_seconds: int,
    direction: Optional[str] = None,
) -> List[Dict]:
    """Tiered query for packet_metrics (RSSI/SNR/airtime per channel).

    Parameters
    ----------
    direction : str, optional
        Filter by direction (``"rx"`` or ``"tx"``).  ``None`` includes both.

    Returns rows with: bucket_ts, direction, avg_rssi, min_rssi, max_rssi,
    avg_snr, min_snr, max_snr, avg_airtime_ms, total_airtime_ms,
    total_bytes, avg_hop_count, crc_error_count
    """
    extra_filters = {}
    if direction is not None:
        extra_filters["direction"] = direction

    return tiered_channel_query(
        conn,
        table_name="packet_metrics",
        channel_id=channel_id,
        since_ts=since_ts,
        until_ts=until_ts,
        bucket_seconds=bucket_seconds,
        extra_filters=extra_filters if extra_filters else None,
    )


# ---------------------------------------------------------------------------
# Convenience: auto-select bucket size based on timeframe
# ---------------------------------------------------------------------------

def auto_bucket_seconds(hours: int) -> int:
    """Choose an appropriate bucket size for a given timeframe.

    Matches the bucket logic used by the existing API endpoints:
      <=1h  → 60s   (1 min)
      <=6h  → 300s  (5 min)
      <=24h → 900s  (15 min)
      <=72h → 3600s (1 hour)
      >72h  → 14400s (4 hours)
    """
    if hours <= 1:
        return 60
    if hours <= 6:
        return 300
    if hours <= 24:
        return 900
    if hours <= 72:
        return 3600
    return 14400
