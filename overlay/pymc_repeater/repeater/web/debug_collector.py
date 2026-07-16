"""WM1303 Debug Bundle Collector.

Generates a comprehensive diagnostic bundle (.tar.gz) for remote
troubleshooting.  All sensitive data (identity keys, JWT secrets) is
automatically redacted before inclusion.

Usage (from API):
    collector = DebugCollector(config, backend, bridge_engine, repeater_engine)
    result = await collector.generate()   # returns dict with path, size, expires
"""

import asyncio
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
import time
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from openhop_core.paths import resolve_config_path  # WM1303 v2.7: central config-path helper

logger = logging.getLogger("DebugCollector")

# ---------------------------------------------------------------------------
# Detect user home dynamically (supports non-pi users and alternate distros)
# ---------------------------------------------------------------------------
def _detect_user_home() -> Path:
    """Find the home directory of the service user."""
    # 1. From config.yaml pktfwd_dir
    try:
        import yaml as _yaml
        with open(resolve_config_path('config.yaml')) as _f:
            _cfg = _yaml.safe_load(_f) or {}
        _pdir = _cfg.get('wm1303', {}).get('pktfwd_dir', '')
        if _pdir and Path(_pdir).is_dir():
            return Path(_pdir).parent
    except Exception:
        pass
    # 2. From systemd service User=
    try:
        _svc_user = subprocess.check_output(
            ['systemctl', 'show', 'pymc-repeater', '-p', 'User', '--value'],
            text=True, timeout=3
        ).strip()
        if _svc_user and _svc_user != 'root':
            import pwd
            return Path(pwd.getpwnam(_svc_user).pw_dir)
    except Exception:
        pass
    # 3. Scan for existing wm1303_pf directory
    try:
        import pwd
        for _pw in pwd.getpwall():
            if _pw.pw_uid >= 1000 and _pw.pw_uid < 65534 and _pw.pw_dir != '/':
                if (Path(_pw.pw_dir) / 'wm1303_pf').is_dir():
                    return Path(_pw.pw_dir)
    except Exception:
        pass
    return Path('/home/pi')

_USER_HOME   = _detect_user_home()
_PKTFWD_DIR  = _USER_HOME / 'wm1303_pf'
_HAL_DIR     = _USER_HOME / 'sx1302_hal'

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
BUNDLE_DIR = Path("/tmp/wm1303_debug")
BUNDLE_EXPIRY_SECONDS = 15 * 60  # 15 minutes
MIN_DISK_FREE_MB = 50  # Minimum free disk space required
JOURNAL_FULL_MINUTES = 30  # Full journal log window
JOURNAL_ERRORS_HOURS = 24  # Error-only journal window
MAX_JOURNAL_LINES = 10000  # Safety cap

# Secrets to redact in config files
_REDACT_KEYS = {
    "identity_key", "jwt_secret", "password", "secret",
    "private_key", "api_key", "token",
    "gateway_id", "gateway_ID", "concentrator_serial",
}


class DebugCollector:
    """Collects diagnostic data and packages it into a downloadable bundle."""

    def __init__(
        self,
        config: Dict[str, Any],
        backend: Any = None,
        bridge_engine: Any = None,
        repeater_engine: Any = None,
    ):
        self.config = config or {}
        self.backend = backend
        self.bridge_engine = bridge_engine
        self.repeater_engine = repeater_engine
        self._lock = threading.Lock()
        self._current_bundle: Optional[Dict[str, Any]] = None
        self._cleanup_timer: Optional[threading.Timer] = None
        # Purge any orphaned bundles left over from a previous run/restart
        self._purge_bundle_dir()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_status(self) -> Dict[str, Any]:
        """Return current bundle status for the UI."""
        if self._current_bundle and Path(self._current_bundle["path"]).exists():
            remaining = self._current_bundle["expires"] - time.time()
            if remaining > 0:
                return {
                    "available": True,
                    "filename": self._current_bundle["filename"],
                    "size_bytes": self._current_bundle["size_bytes"],
                    "size_mb": round(self._current_bundle["size_bytes"] / (1024 * 1024), 2),
                    "created": self._current_bundle["created"],
                    "expires": self._current_bundle["expires"],
                    "expires_in_seconds": int(remaining),
                }
            else:
                self._cleanup_bundle()
        return {"available": False}

    async def generate(self) -> Dict[str, Any]:
        """Generate a new debug bundle.  Returns status dict."""
        # Check disk space first
        disk_check = self._check_disk_space()
        if not disk_check["ok"]:
            return {"error": True, "message": disk_check["message"]}

        # Clean up any previous bundle
        self._cleanup_bundle()

        # Create temporary working directory
        work_dir = Path(tempfile.mkdtemp(prefix="wm1303_debug_"))
        try:
            # Collect all data
            await self._collect_system_info(work_dir)
            await self._collect_service_status(work_dir)
            await self._collect_configs(work_dir)
            await self._collect_logs(work_dir)
            await self._collect_runtime_stats(work_dir)
            await self._collect_file_integrity(work_dir)
            await self._collect_database_info(work_dir)
            await self._collect_identity_info(work_dir)
            self._collect_metrics_data(work_dir)
            # Compute health_snapshot.json last (depends on metrics being dumped
            # and the sx1261_health_events table being populated).
            self._compute_health_snapshot(work_dir)
            self._collect_packet_traces(work_dir)
            self._collect_versions(work_dir)
            # WM1303 v2.5.0 — extended diagnostics
            self._collect_wm1303_state(work_dir)
            self._collect_service_journal(work_dir, n=500)
            self._collect_overlay_summary(work_dir)
            self._write_manifest(work_dir)

            # Package into tar.gz
            hostname = self._run_cmd("hostname").strip() or "unknown"
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"wm1303-debug-{hostname}-{timestamp}.tar.gz"

            BUNDLE_DIR.mkdir(parents=True, exist_ok=True)
            bundle_path = BUNDLE_DIR / filename

            with tarfile.open(str(bundle_path), "w:gz") as tar:
                # Add all files from work_dir with a clean base name
                base_name = filename.replace(".tar.gz", "")
                for item in work_dir.rglob("*"):
                    if item.is_file():
                        arcname = f"{base_name}/{item.relative_to(work_dir)}"
                        tar.add(str(item), arcname=arcname)

            size_bytes = bundle_path.stat().st_size
            now = time.time()

            self._current_bundle = {
                "path": str(bundle_path),
                "filename": filename,
                "size_bytes": size_bytes,
                "created": now,
                "expires": now + BUNDLE_EXPIRY_SECONDS,
            }

            # Schedule auto-cleanup
            self._schedule_cleanup()

            logger.info("Debug bundle generated: %s (%.1f MB)",
                        filename, size_bytes / (1024 * 1024))

            return self.get_status()

        except Exception as exc:
            logger.exception("Failed to generate debug bundle")
            return {"error": True, "message": f"Bundle generation failed: {exc}"}
        finally:
            # Always clean up working directory
            shutil.rmtree(str(work_dir), ignore_errors=True)

    def get_bundle_path(self) -> Optional[str]:
        """Return the path to the current bundle if it exists and hasn't expired."""
        if self._current_bundle:
            path = Path(self._current_bundle["path"])
            if path.exists() and time.time() < self._current_bundle["expires"]:
                return str(path)
        return None

    # ------------------------------------------------------------------
    # Disk space check
    # ------------------------------------------------------------------

    def _check_disk_space(self) -> Dict[str, Any]:
        """Check if there's enough free disk space."""
        try:
            stat = os.statvfs("/tmp")
            free_mb = (stat.f_bavail * stat.f_frsize) / (1024 * 1024)
            if free_mb < MIN_DISK_FREE_MB:
                return {
                    "ok": False,
                    "message": f"Insufficient disk space: {free_mb:.0f} MB free, "
                               f"need at least {MIN_DISK_FREE_MB} MB",
                }
            return {"ok": True, "free_mb": free_mb}
        except Exception as exc:
            return {"ok": False, "message": f"Cannot check disk space: {exc}"}

    # ------------------------------------------------------------------
    # Cleanup / expiry
    # ------------------------------------------------------------------

    def _schedule_cleanup(self):
        """Schedule automatic bundle cleanup after BUNDLE_EXPIRY_SECONDS."""
        if self._cleanup_timer:
            self._cleanup_timer.cancel()
        self._cleanup_timer = threading.Timer(
            BUNDLE_EXPIRY_SECONDS, self._cleanup_bundle
        )
        self._cleanup_timer.daemon = True
        self._cleanup_timer.start()

    def _cleanup_bundle(self):
        """Remove current bundle and reset state."""
        if self._cleanup_timer:
            self._cleanup_timer.cancel()
            self._cleanup_timer = None
        if self._current_bundle:
            path = Path(self._current_bundle["path"])
            if path.exists():
                try:
                    path.unlink()
                    logger.info("Debug bundle expired/cleaned: %s",
                                self._current_bundle["filename"])
                except Exception:
                    pass
            self._current_bundle = None
        # Also purge any orphaned files (e.g. from a previous run)
        self._purge_bundle_dir()

    def _purge_bundle_dir(self):
        """Remove ALL files in BUNDLE_DIR and the directory itself.

        This catches orphaned bundles left behind after a service restart
        (when in-memory state is lost but the file remains on disk).
        """
        try:
            if BUNDLE_DIR.exists():
                for f in BUNDLE_DIR.iterdir():
                    try:
                        f.unlink()
                        logger.info("Purged orphaned debug bundle: %s", f.name)
                    except Exception:
                        pass
                # Remove dir if now empty
                if not any(BUNDLE_DIR.iterdir()):
                    BUNDLE_DIR.rmdir()
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Helper: run shell commands
    # ------------------------------------------------------------------

    def _run_cmd(self, cmd: str, timeout: int = 10) -> str:
        """Run a shell command and return stdout.  Never raises."""
        try:
            result = subprocess.run(
                cmd, shell=True, capture_output=True, text=True, timeout=timeout
            )
            return result.stdout
        except Exception:
            return ""

    def _write(self, path: Path, content: str):
        """Write content to a file, creating parent dirs as needed."""
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    def _write_json(self, path: Path, data: Any):
        """Write JSON data to a file."""
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")

    # ------------------------------------------------------------------
    # Sanitization
    # ------------------------------------------------------------------

    def _sanitize_config(self, data: Any, depth: int = 0) -> Any:
        """Recursively redact sensitive keys in a config dict."""
        if depth > 20:
            return data
        if isinstance(data, dict):
            result = {}
            for k, v in data.items():
                if any(secret in k.lower() for secret in _REDACT_KEYS):
                    result[k] = "[REDACTED]"
                else:
                    result[k] = self._sanitize_config(v, depth + 1)
            return result
        elif isinstance(data, list):
            return [self._sanitize_config(item, depth + 1) for item in data]
        return data

    # ------------------------------------------------------------------
    # Collectors
    # ------------------------------------------------------------------

    async def _collect_system_info(self, work_dir: Path):
        """Collect system-level information."""
        info = []
        info.append("=== WM1303 Debug Bundle ===")
        info.append(f"Generated: {datetime.now(timezone.utc).isoformat()}")
        info.append("")

        info.append("=== OS & Kernel ===")
        info.append(self._run_cmd("cat /etc/os-release"))
        info.append(self._run_cmd("uname -a"))

        info.append("=== Hostname ===")
        info.append(self._run_cmd("hostname"))

        info.append("=== Pi Model ===")
        info.append(self._run_cmd("cat /proc/device-tree/model 2>/dev/null || echo 'unknown'"))

        info.append("=== Uptime ===")
        info.append(self._run_cmd("uptime"))

        info.append("=== CPU Temperature ===")
        info.append(self._run_cmd(
            "awk '{printf \"%.1f°C\\n\", $1/1000}' /sys/class/thermal/thermal_zone0/temp 2>/dev/null || echo 'unknown'"
        ))

        info.append("=== Concentrator Temperature ===")
        info.append(self._run_cmd("cat /tmp/concentrator_temp 2>/dev/null || echo 'unavailable'"))

        info.append("=== Memory ===")
        info.append(self._run_cmd("free -h"))

        info.append("=== Disk Usage ===")
        info.append(self._run_cmd("df -h"))

        info.append("=== NTP Sync ===")
        info.append(self._run_cmd("timedatectl status 2>/dev/null || echo 'timedatectl not available'"))

        info.append("=== SPI Devices ===")
        info.append(self._run_cmd("ls -la /dev/spidev* 2>/dev/null || echo 'no SPI devices'"))

        info.append("=== I2C Devices ===")
        info.append(self._run_cmd("i2cdetect -y 1 2>/dev/null || echo 'i2cdetect not available'"))

        info.append("=== Network Interfaces ===")
        info.append(self._run_cmd("ip addr show"))

        info.append("=== Boot Config (SPI relevant) ===")
        info.append(self._run_cmd(
            "grep -E 'spi|dtoverlay|dtparam' /boot/firmware/config.txt 2>/dev/null "
            "|| grep -E 'spi|dtoverlay|dtparam' /boot/config.txt 2>/dev/null "
            "|| echo 'config.txt not found'"
        ))

        info.append("=== Kernel Messages (SPI/hardware related) ===")
        info.append(self._run_cmd("dmesg | grep -iE 'spi|lora|gpio|thermal|throttl' | tail -50"))

        info.append("=== Kernel Messages (dmesg tail -200, broader context) ===")
        try:
            info.append(self._run_cmd("dmesg -T 2>/dev/null | tail -200 || dmesg | tail -200", timeout=10))
        except Exception as exc:
            info.append(f"dmesg -T failed: {exc}")

        info.append("=== RPi Thermal/Power Throttling ===")
        try:
            info.append("--- vcgencmd get_throttled ---")
            info.append(self._run_cmd("vcgencmd get_throttled 2>/dev/null || echo 'vcgencmd not available'", timeout=10))
            info.append("--- voltages ---")
            for rail in ("core", "sdram_c", "sdram_i", "sdram_p"):
                info.append(f"volts {rail}: " + self._run_cmd(f"vcgencmd measure_volts {rail} 2>/dev/null", timeout=5).strip())
            info.append("--- clocks ---")
            for clk in ("arm", "core", "emmc"):
                info.append(f"clock {clk}: " + self._run_cmd(f"vcgencmd measure_clock {clk} 2>/dev/null", timeout=5).strip())
        except Exception as exc:
            info.append(f"throttling block failed: {exc}")

        info.append("=== SPI Interrupt Statistics ===")
        try:
            info.append(self._run_cmd("grep -i spi /proc/interrupts 2>/dev/null || echo 'no SPI entries in /proc/interrupts'", timeout=5))
            info.append("--- spi0.0 max_speed_hz ---")
            info.append(self._run_cmd("cat /sys/bus/spi/devices/spi0.0/max_speed_hz 2>/dev/null || echo 'spi0.0 not present'", timeout=5))
        except Exception as exc:
            info.append(f"SPI interrupt block failed: {exc}")

        info.append("=== Kernel warnings (journalctl -k -p warning, last 1h) ===")
        try:
            info.append(self._run_cmd(
                "journalctl -k -p warning --since '1 hour ago' --no-pager 2>/dev/null | tail -50 "
                "|| echo 'journalctl -k not available'",
                timeout=15,
            ))
        except Exception as exc:
            info.append(f"journalctl -k failed: {exc}")

        self._write(work_dir / "system_info.txt", "\n".join(info))

    async def _collect_service_status(self, work_dir: Path):
        """Collect service and process status."""
        info = []

        info.append("=== pymc-repeater Service Status ===")
        info.append(self._run_cmd("systemctl status pymc-repeater --no-pager -l 2>/dev/null"))

        info.append("=== pkt_fwd Process ===")
        info.append(self._run_cmd("ps aux | grep lora_pkt_fwd | grep -v grep"))

        info.append("=== Python Version ===")
        info.append(self._run_cmd("python3 --version"))

        info.append("=== openhop_core Package ===")
        info.append(self._run_cmd("pip3 show pymc-core 2>/dev/null || echo 'not installed'"))

        info.append("=== pymc_repeater Package ===")
        info.append(self._run_cmd("pip3 show pymc-repeater 2>/dev/null || echo 'not installed'"))

        info.append("=== WM1303 Version ===")
        info.append(self._run_cmd("cat /etc/openhop_repeater/version 2>/dev/null || cat /etc/pymc_repeater/version 2>/dev/null || echo 'unknown'"))

        info.append("=== Service Restarts (last 24h) ===")
        info.append(self._run_cmd(
            "journalctl -u pymc-repeater --no-pager --since '24 hours ago' "
            "| grep -iE 'start|stop|restart|failed' | tail -30"
        ))

        info.append("=== Open File Descriptors ===")
        pkt_pid = self._run_cmd("pgrep -x lora_pkt_fwd").strip()
        if pkt_pid:
            info.append(f"pkt_fwd (PID {pkt_pid}): " +
                        self._run_cmd(f"ls /proc/{pkt_pid}/fd 2>/dev/null | wc -l").strip() + " fds")
        svc_pid = self._run_cmd("pgrep -f 'pymc_repeater' | head -1").strip()
        if svc_pid:
            info.append(f"pymc-repeater (PID {svc_pid}): " +
                        self._run_cmd(f"ls /proc/{svc_pid}/fd 2>/dev/null | wc -l").strip() + " fds")

        self._write(work_dir / "service_status.txt", "\n".join(info))

    async def _collect_configs(self, work_dir: Path):
        """Collect configuration files (sanitized)."""
        config_dir = work_dir / "config"
        config_dir.mkdir(parents=True, exist_ok=True)

        # config.yaml - SANITIZED
        try:
            import yaml
            config_path = resolve_config_path('config.yaml')
            if config_path.exists():
                with open(config_path) as f:
                    raw_config = yaml.safe_load(f)
                sanitized = self._sanitize_config(raw_config)
                self._write(config_dir / "config.yaml.sanitized",
                            yaml.dump(sanitized, default_flow_style=False, sort_keys=False))
        except Exception as exc:
            self._write(config_dir / "config.yaml.sanitized", f"Error reading config: {exc}")

        # wm1303_ui.json
        try:
            ui_path = resolve_config_path('wm1303_ui.json')
            if ui_path.exists():
                with open(ui_path) as f:
                    ui_data = json.load(f)
                self._write_json(config_dir / "wm1303_ui.json", ui_data)
        except Exception as exc:
            self._write(config_dir / "wm1303_ui.json", f"Error: {exc}")

        # bridge_conf.json (generated)
        for name in ["bridge_conf.json", "global_conf.json"]:
            src = _PKTFWD_DIR / name
            if src.exists():
                try:
                    with open(src) as f:
                        self._write_json(config_dir / name, json.load(f))
                except Exception:
                    try:
                        shutil.copy2(str(src), str(config_dir / name))
                    except Exception:
                        pass

        # pymc-repeater.service
        svc_path = Path("/etc/systemd/system/pymc-repeater.service")
        if svc_path.exists():
            try:
                shutil.copy2(str(svc_path), str(config_dir / "pymc-repeater.service"))
            except Exception:
                pass

        # Boot config (SPI lines only)
        boot_spi = self._run_cmd(
            "grep -nE 'spi|dtoverlay|dtparam' /boot/firmware/config.txt 2>/dev/null "
            "|| grep -nE 'spi|dtoverlay|dtparam' /boot/config.txt 2>/dev/null"
        )
        self._write(config_dir / "boot_config_spi.txt", boot_spi or "not found")

    async def _collect_logs(self, work_dir: Path):
        """Collect journal logs."""
        logs_dir = work_dir / "logs"
        logs_dir.mkdir(parents=True, exist_ok=True)

        # Full journal (last 30 minutes)
        full_log = self._run_cmd(
            f"journalctl -u pymc-repeater --no-pager "
            f"--since '{JOURNAL_FULL_MINUTES} minutes ago' "
            f"| tail -{MAX_JOURNAL_LINES}",
            timeout=30
        )
        self._write(logs_dir / "journal_full_30min.log", full_log or "no logs")

        # Errors/warnings only (last 24 hours)
        error_log = self._run_cmd(
            f"journalctl -u pymc-repeater --no-pager -p warning "
            f"--since '{JOURNAL_ERRORS_HOURS} hours ago' "
            f"| tail -{MAX_JOURNAL_LINES}",
            timeout=30
        )
        self._write(logs_dir / "journal_errors_24h.log", error_log or "no errors")

        # pkt_fwd output (filtered)
        pkt_log = self._run_cmd(
            f"journalctl -u pymc-repeater --no-pager "
            f"--since '{JOURNAL_FULL_MINUTES} minutes ago' "
            f"| grep -E 'pkt_fwd:|\\[hal\\]|\\[down\\]|\\[up\\]|\\[jit\\]|\\[agc\\]|concentrator|SX126|SX130' "
            f"| tail -2000",
            timeout=30
        )
        self._write(logs_dir / "pkt_fwd_output.log", pkt_log or "no pkt_fwd output")

        # Python tracebacks
        traceback_log = self._run_cmd(
            f"journalctl -u pymc-repeater --no-pager "
            f"--since '{JOURNAL_ERRORS_HOURS} hours ago' "
            f"| grep -A5 -E 'Traceback|Error|Exception' "
            f"| tail -2000",
            timeout=30
        )
        self._write(logs_dir / "tracebacks.log", traceback_log or "no tracebacks")

        # TX log
        tx_log = self._run_cmd(
            f"journalctl -u pymc-repeater --no-pager "
            f"--since '{JOURNAL_FULL_MINUTES} minutes ago' "
            f"| grep -E 'TX OK|TX FAIL|TX timeout|TX_ACK|PULL_RESP|tx_queue|TXQueue|GlobalTXScheduler' "
            f"| tail -2000",
            timeout=30
        )
        self._write(logs_dir / "tx_log.log", tx_log or "no TX activity")

        # Bridge forwarding log
        bridge_log = self._run_cmd(
            f"journalctl -u pymc-repeater --no-pager "
            f"--since '{JOURNAL_FULL_MINUTES} minutes ago' "
            f"| grep -E 'BridgeEngine|bridge|forwarded|delivered|Channel E' "
            f"| tail -2000",
            timeout=30
        )
        self._write(logs_dir / "bridge_forwarding.log", bridge_log or "no bridge activity")

        # Startup sequence (from last service start)
        startup_log = self._run_cmd(
            "journalctl -u pymc-repeater --no-pager "
            "-b | head -200",
            timeout=15
        )
        self._write(logs_dir / "startup_sequence.log", startup_log or "no startup logs")

    async def _collect_runtime_stats(self, work_dir: Path):
        """Collect runtime statistics from the running services."""
        stats_dir = work_dir / "stats"
        stats_dir.mkdir(parents=True, exist_ok=True)

        # Channel stats from backend
        if self.backend:
            try:
                ch_stats = self.backend.get_channel_stats()
                self._write_json(stats_dir / "channel_stats.json", ch_stats)
            except Exception as exc:
                self._write(stats_dir / "channel_stats.json", f"Error: {exc}")

            # Backend internal stats
            try:
                bk_stats = {}
                for attr in ["_tx_echo_hashes", "_rx_dedup_cache",
                             "_channel_e_config_cache", "_channel_e_freq"]:
                    val = getattr(self.backend, attr, None)
                    if val is not None:
                        if isinstance(val, dict):
                            bk_stats[attr] = {"size": len(val)}
                        else:
                            bk_stats[attr] = str(val)
                self._write_json(stats_dir / "backend_stats.json", bk_stats)
            except Exception:
                pass

        # Queue stats from tx_queue
        if self.backend:
            try:
                tx_q = getattr(self.backend, "_tx_scheduler", None)
                if tx_q:
                    q_stats = {}
                    for attr in dir(tx_q):
                        if "stat" in attr.lower() or "queue" in attr.lower():
                            try:
                                val = getattr(tx_q, attr)
                                if not callable(val):
                                    q_stats[attr] = str(val)
                            except Exception:
                                pass
                    # Get per-channel queue stats
                    queues = getattr(tx_q, "_queues", {})
                    for ch_name, q in queues.items():
                        q_stats[f"queue_{ch_name}"] = {
                            "pending": q.qsize() if hasattr(q, "qsize") else "unknown",
                            "stats": getattr(q, "stats", {}),
                        }
                    self._write_json(stats_dir / "queue_stats.json", q_stats)
            except Exception as exc:
                self._write(stats_dir / "queue_stats.json", f"Error: {exc}")

        # Bridge engine stats
        if self.bridge_engine:
            try:
                bridge_stats = self.bridge_engine.get_stats()
                bridge_stats["_seen_cache_size"] = len(
                    getattr(self.bridge_engine, "_seen", {})
                )
                self._write_json(stats_dir / "bridge_stats.json", bridge_stats)
            except Exception as exc:
                self._write(stats_dir / "bridge_stats.json", f"Error: {exc}")

        # Repeater engine stats
        if self.repeater_engine:
            try:
                rpt_stats = {
                    "seen_packets_cache_size": len(
                        getattr(self.repeater_engine, "seen_packets", {})
                    ),
                    "cache_ttl": getattr(self.repeater_engine, "cache_ttl", "unknown"),
                }
                self._write_json(stats_dir / "repeater_stats.json", rpt_stats)
            except Exception as exc:
                self._write(stats_dir / "repeater_stats.json", f"Error: {exc}")

        # Spectral scan data
        try:
            scan_path = Path("/tmp/spectral_scan_latest.json")
            if scan_path.exists():
                with open(scan_path) as f:
                    self._write_json(stats_dir / "spectral_scan.json", json.load(f))
        except Exception:
            pass

        # pkt_fwd status (from PUSH_DATA stat)
        try:
            stat_path = Path("/tmp/pkt_fwd_status.json")
            if stat_path.exists():
                with open(stat_path) as f:
                    self._write_json(stats_dir / "pkt_fwd_status.json", json.load(f))
        except Exception:
            pass

    async def _collect_file_integrity(self, work_dir: Path):
        """Collect MD5 checksums of key overlay files."""
        integrity_dir = work_dir / "integrity"
        integrity_dir.mkdir(parents=True, exist_ok=True)

        # Files to checksum
        check_files = []

        # Python overlay files
        site_pkg_base = self._run_cmd(
            "python3 -c 'import openhop_core; import os; print(os.path.dirname(os.path.dirname(openhop_core.__file__)))' 2>/dev/null"
        ).strip()

        if site_pkg_base:
            python_files = [
                f"{site_pkg_base}/openhop_core/hardware/__init__.py",
                f"{site_pkg_base}/openhop_core/hardware/signal_utils.py",
                f"{site_pkg_base}/openhop_core/hardware/sx1261_driver.py",
                f"{site_pkg_base}/openhop_core/hardware/sx1302_hal.py",
                f"{site_pkg_base}/openhop_core/hardware/tx_queue.py",
                f"{site_pkg_base}/openhop_core/hardware/virtual_radio.py",
                f"{site_pkg_base}/openhop_core/hardware/wm1303_backend.py",
                # WM1303 v2.4.10+ overlay
                f"{site_pkg_base}/openhop_core/hardware/region_config.py",
            ]
            check_files.extend(python_files)

        # Repeater files
        repeater_base = "/opt/pymc_repeater/repos/pyMC_Repeater/repeater"
        repeater_files = [
            f"{repeater_base}/bridge_engine.py",
            f"{repeater_base}/channel_e_bridge.py",
            # WM1303 v2.4.9+ overlay (Channel F support)
            f"{repeater_base}/channel_f_bridge.py",
            f"{repeater_base}/config.py",
            f"{repeater_base}/config_manager.py",
            f"{repeater_base}/engine.py",
            f"{repeater_base}/identity_manager.py",
            f"{repeater_base}/main.py",
            # WM1303 v2.4.x overlays (metrics, tracing)
            f"{repeater_base}/metrics_retention.py",
            f"{repeater_base}/uniform_tracer.py",
            f"{repeater_base}/packet_router.py",
            # WM1303 v2.4.11+ overlay (companion telemetry + EUI/RAM/disk in owner.info)
            f"{repeater_base}/wm1303_telemetry_helper.py",
            # WM1303 v2.4.11+ overlay (mesh CLI owner.info handler)
            f"{repeater_base}/handler_helpers/mesh_cli.py",
            # WM1303 data acquisition overlays
            f"{repeater_base}/data_acquisition/sqlite_handler.py",
            f"{repeater_base}/data_acquisition/storage_collector.py",
            f"{repeater_base}/web/wm1303_api.py",
            f"{repeater_base}/web/api_endpoints.py",
            f"{repeater_base}/web/http_server.py",
            f"{repeater_base}/web/spectrum_collector.py",
            f"{repeater_base}/web/cad_calibration_engine.py",
            # WM1303 v2.5.0+ overlay (update check against fork)
            f"{repeater_base}/web/update_endpoints.py",
            # WM1303 v2.4.x overlays (web helpers)
            f"{repeater_base}/web/tiered_query.py",
            f"{repeater_base}/web/packet_trace.py",
            f"{repeater_base}/web/debug_collector.py",
            f"{repeater_base}/web/html/wm1303.html",
        ]
        check_files.extend(repeater_files)

        # HAL source files (WM1303 overlay tracks these for patch detection)
        _hal = str(_HAL_DIR)
        hal_files = [
            f"{_hal}/libloragw/src/loragw_sx1302.c",
            f"{_hal}/libloragw/src/loragw_hal.c",
            f"{_hal}/libloragw/src/loragw_lbt.c",
            f"{_hal}/libloragw/src/loragw_sx1261.c",
            f"{_hal}/libloragw/src/loragw_aux.c",
            f"{_hal}/libloragw/src/loragw_spi.c",
            f"{_hal}/libloragw/src/sx1261_spi.c",
            f"{_hal}/libloragw/inc/loragw_hal.h",
            f"{_hal}/libloragw/inc/sx1261_defs.h",
            f"{_hal}/packet_forwarder/src/lora_pkt_fwd.c",
            f"{_hal}/packet_forwarder/src/capture_thread.c",
        ]
        check_files.extend(hal_files)

        # pkt_fwd binary
        check_files.append(str(_PKTFWD_DIR / "lora_pkt_fwd"))

        # Generate checksums
        checksums = []
        for fpath in check_files:
            md5 = self._run_cmd(f"md5sum '{fpath}' 2>/dev/null").strip()
            if md5:
                checksums.append(md5)
            else:
                checksums.append(f"MISSING  {fpath}")

        self._write(integrity_dir / "file_checksums.txt", "\n".join(checksums))

        # Version file
        version = self._run_cmd("cat /etc/openhop_repeater/version 2>/dev/null || cat /etc/pymc_repeater/version 2>/dev/null").strip()
        self._write(integrity_dir / "version.txt", version or "unknown")

    async def _collect_database_info(self, work_dir: Path):
        """Collect database status and recent metrics."""
        db_dir = work_dir / "database"
        db_dir.mkdir(parents=True, exist_ok=True)

        info = []

        # Find database files
        db_files = self._run_cmd(
            "find /opt/pymc_repeater /var/lib/openhop_repeater /var/lib/pymc_repeater /etc/openhop_repeater /etc/pymc_repeater "
            "-name '*.db' -o -name '*.sqlite' -o -name '*.sqlite3' 2>/dev/null"
        ).strip()

        if db_files:
            info.append("=== Database Files ===")
            for db_path in db_files.split("\n"):
                db_path = db_path.strip()
                if db_path:
                    size = self._run_cmd(f"ls -lh '{db_path}' 2>/dev/null").strip()
                    info.append(size)

                    # Integrity check
                    integrity = self._run_cmd(
                        f"sqlite3 '{db_path}' 'PRAGMA integrity_check' 2>/dev/null"
                    ).strip()
                    info.append(f"  Integrity: {integrity}")

                    # Schema version
                    schema_ver = self._run_cmd(
                        f"sqlite3 '{db_path}' 'PRAGMA user_version' 2>/dev/null"
                    ).strip()
                    info.append(f"  Schema version: {schema_ver}")

                    # Table list
                    tables = self._run_cmd(
                        f"sqlite3 '{db_path}' '.tables' 2>/dev/null"
                    ).strip()
                    info.append(f"  Tables: {tables}")

                    # Row counts per table
                    if tables:
                        for table in tables.split():
                            count = self._run_cmd(
                                f"sqlite3 '{db_path}' 'SELECT COUNT(*) FROM {table}' 2>/dev/null"
                            ).strip()
                            info.append(f"    {table}: {count} rows")
                    info.append("")
        else:
            info.append("No database files found")

        self._write(db_dir / "db_info.txt", "\n".join(info))

        # Note: 'metrics' table does not exist in any current DB.
        # Use _collect_metrics_data() instead for structured metric dumps.

    async def _collect_identity_info(self, work_dir: Path):
        """Collect repeater identity (PUBLIC info only!)."""
        identity_dir = work_dir / "identity"
        identity_dir.mkdir(parents=True, exist_ok=True)

        info = {}

        if self.repeater_engine:
            try:
                # Only public info!
                identity = getattr(self.repeater_engine, "identity", None)
                if identity:
                    info["node_name"] = getattr(identity, "name", "unknown")
                    pub_key = getattr(identity, "public_key", None)
                    if pub_key:
                        if isinstance(pub_key, bytes):
                            info["public_key_hex"] = pub_key.hex()
                        else:
                            info["public_key_hex"] = str(pub_key)
                    short_hash = getattr(identity, "short_hash", None)
                    if short_hash:
                        info["short_hash"] = short_hash.hex() if isinstance(short_hash, bytes) else str(short_hash)
            except Exception:
                pass

        # JWT token validity (NOT the actual token)
        try:
            jwt_path = resolve_config_path('jwt_token')
            if jwt_path.exists():
                info["jwt_token_exists"] = True
                mtime = jwt_path.stat().st_mtime
                info["jwt_token_age_hours"] = round((time.time() - mtime) / 3600, 1)
            else:
                info["jwt_token_exists"] = False
        except Exception:
            pass

        # NEVER include private key or JWT secret
        info["_note"] = "Private keys and JWT secrets are NOT included in this bundle."

        self._write_json(identity_dir / "node_info.json", info)


    # ------------------------------------------------------------------
    # Extended collectors (v2)
    # ------------------------------------------------------------------

    def _dump_table_json(self, db_path, table, ts_col, out_path, days=3):
        """Dump recent rows of a table to JSONL. Returns row count or -1 on error."""
        import sqlite3 as _sql
        import time as _t
        cutoff = _t.time() - days * 86400
        try:
            if not os.path.exists(db_path):
                return 0
            with _sql.connect(db_path, timeout=5) as conn:
                conn.row_factory = _sql.Row
                exists = conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                    (table,)
                ).fetchone()
                if not exists:
                    return 0
                rows = conn.execute(
                    f"SELECT * FROM {table} WHERE {ts_col} > ? ORDER BY {ts_col}",
                    (cutoff,)
                ).fetchall()
            with open(out_path, "w") as f:
                for r in rows:
                    f.write(json.dumps(dict(r)) + "\n")
            return len(rows)
        except Exception:
            return -1

    def _collect_metrics_data(self, work_dir):
        """Dump recent metric rows from all known SQLite metric tables."""
        out_dir = os.path.join(str(work_dir), "database", "metrics")
        os.makedirs(out_dir, exist_ok=True)
        dumps = [
            ("repeater.db",         "channel_stats_history",   "timestamp"),
            ("repeater.db",         "noise_floor_history",     "timestamp"),
            ("repeater.db",         "cad_events",              "timestamp"),
            ("repeater.db",         "packet_activity",         "timestamp"),
            ("repeater.db",         "origin_channel_stats",    "timestamp"),
            ("repeater.db",         "sx1261_health_events",    "timestamp"),
            ("repeater.db",         "crc_error_rate",          "timestamp"),
            ("spectrum_history.db", "spectrum_scans",          "timestamp"),
        ]
        summary = {}
        for db, table, ts_col in dumps:
            out_path = os.path.join(out_dir, f"{table}.jsonl")
            n = self._dump_table_json(f"/var/lib/pymc_repeater/{db}", table, ts_col, out_path, days=3)
            summary[table] = n
        # packets tail (exclude payload and raw_packet)
        try:
            import sqlite3 as _sql
            with _sql.connect("/var/lib/pymc_repeater/repeater.db", timeout=5) as conn:
                conn.row_factory = _sql.Row
                rows = conn.execute(
                    "SELECT id, timestamp, type, rssi, snr, length, "
                    "src_hash, dst_hash, path_hash, packet_hash, "
                    "score, transmitted, is_duplicate, drop_reason, "
                    "tx_delay_ms, lbt_attempts, lbt_channel_busy "
                    "FROM packets ORDER BY timestamp DESC LIMIT 500"
                ).fetchall()
            with open(os.path.join(out_dir, "packets_tail.jsonl"), "w") as f:
                for r in rows:
                    f.write(json.dumps(dict(r)) + "\n")
            summary["packets_tail"] = len(rows)
        except Exception:
            summary["packets_tail"] = -1
        with open(os.path.join(out_dir, "_summary.json"), "w") as f:
            json.dump(summary, f, indent=2)

    def _compute_health_snapshot(self, work_dir):
        """Analyze recent metrics/events and write health_snapshot.json.

        Reads the last 30 minutes of data from noise_floor_history,
        channel_stats_history and sx1261_health_events, computes simple
        anomaly indicators (stuck noise floor, TX success rate, SX1261
        health score), and writes a compact summary plus human alerts.
        """
        import sqlite3 as _sql
        _db = "/var/lib/pymc_repeater/repeater.db"
        _now = time.time()
        _window_s = 30 * 60
        _since = _now - _window_s
        snapshot = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "period_analyzed_minutes": int(_window_s / 60),
            "period_start_epoch": _since,
            "period_end_epoch": _now,
            "noise_floor": {},
            "tx_success_rate": {},
            "sx1261_health": {
                "spectral_scans_attempted": 0,
                "timeout": 0,
                "status_unexpected": 0,
                "recoveries": 0,
                "cad_timeouts": 0,
                "cad_force_tx": 0,
                "lbt_rssi_busy": 0,
                "health_score": "good",
            },
            "cad_retry_histogram": {},
            "alerts": [],
        }

        # ---- Noise floor: detect stuck values per channel ------------------
        try:
            with _sql.connect(_db, timeout=5) as conn:
                conn.row_factory = _sql.Row
                rows = conn.execute(
                    "SELECT channel_id, timestamp, noise_floor_dbm "
                    "FROM noise_floor_history "
                    "WHERE timestamp >= ? "
                    "ORDER BY channel_id, timestamp ASC",
                    (_since,),
                ).fetchall()
            by_ch: dict[str, list] = {}
            for r in rows:
                by_ch.setdefault(r["channel_id"], []).append(
                    (r["timestamp"], r["noise_floor_dbm"]))
            for ch_id, samples in by_ch.items():
                vals = [v for _, v in samples if v is not None]
                if not vals:
                    continue
                _min = round(min(vals), 1)
                _max = round(max(vals), 1)
                _cur = round(vals[-1], 1)
                # Count trailing "stuck" samples (identical within ±0.1 dB)
                stuck = 1
                for i in range(len(vals) - 2, -1, -1):
                    if abs(vals[i] - _cur) <= 0.1:
                        stuck += 1
                    else:
                        break
                anomaly = stuck >= 10
                reason = "stuck_noise_floor" if anomaly else ""
                snapshot["noise_floor"][ch_id] = {
                    "min": _min,
                    "max": _max,
                    "current": _cur,
                    "samples": len(vals),
                    "stuck_samples": stuck,
                    "anomaly": anomaly,
                    "anomaly_reason": reason,
                }
                if anomaly:
                    snapshot["alerts"].append(
                        f"Noise floor appears stuck on {ch_id}: "
                        f"{stuck} consecutive samples at ~{_cur} dBm"
                    )
        except Exception as _e:
            snapshot["alerts"].append(f"noise_floor analysis failed: {_e}")

        # ---- TX success rate: from channel_stats_history deltas ------------
        try:
            with _sql.connect(_db, timeout=5) as conn:
                conn.row_factory = _sql.Row
                rows = conn.execute(
                    "SELECT channel_id, timestamp, tx_count, lbt_blocked, lbt_passed "
                    "FROM channel_stats_history "
                    "WHERE timestamp >= ? "
                    "ORDER BY channel_id, timestamp ASC",
                    (_since,),
                ).fetchall()
            by_ch: dict[str, list] = {}
            for r in rows:
                by_ch.setdefault(r["channel_id"], []).append(r)
            for ch_id, samples in by_ch.items():
                if len(samples) < 2:
                    continue
                first = samples[0]
                last = samples[-1]
                sent = max(0, (last["tx_count"] or 0) - (first["tx_count"] or 0))
                blocked = max(0, (last["lbt_blocked"] or 0) - (first["lbt_blocked"] or 0))
                passed = max(0, (last["lbt_passed"] or 0) - (first["lbt_passed"] or 0))
                _total = sent + blocked
                rate = round((sent / _total) * 100, 1) if _total > 0 else None
                snapshot["tx_success_rate"][ch_id] = {
                    "sent": sent,
                    "blocked": blocked,
                    "lbt_passed": passed,
                    "rate_pct": rate,
                }
                if rate is not None and rate < 50 and _total >= 5:
                    snapshot["alerts"].append(
                        f"Low TX success rate on {ch_id}: "
                        f"{rate}% ({sent}/{_total})"
                    )
        except Exception as _e:
            snapshot["alerts"].append(f"tx_success_rate analysis failed: {_e}")

        # ---- SX1261 health: counts by event_type ---------------------------
        try:
            with _sql.connect(_db, timeout=5) as conn:
                conn.row_factory = _sql.Row
                rows = conn.execute(
                    "SELECT event_type, COUNT(*) AS n "
                    "FROM sx1261_health_events "
                    "WHERE timestamp >= ? "
                    "GROUP BY event_type",
                    (_since,),
                ).fetchall()
            counts = {r["event_type"]: r["n"] for r in rows}
            h = snapshot["sx1261_health"]
            h["timeout"] = counts.get("spectral_scan_timeout", 0)
            h["status_unexpected"] = counts.get("spectral_scan_status_unexpected", 0)
            h["recoveries"] = counts.get("sx1261_recovery", 0)
            h["cad_timeouts"] = counts.get("cad_timeout", 0)
            h["cad_force_tx"] = counts.get("cad_force_tx", 0)
            h["lbt_rssi_busy"] = counts.get("lbt_rssi_busy", 0)
            # spectral_scans_attempted isn't directly tracked; expose the count
            # of completions we could detect (timeouts + status_unexpected are
            # failures). For now report only failures-derived numbers.
            h["spectral_scans_attempted"] = h["timeout"] + h["status_unexpected"]
            # Health score based on timeouts
            if h["timeout"] == 0:
                h["health_score"] = "good"
            elif h["timeout"] <= 3:
                h["health_score"] = "degraded"
                snapshot["alerts"].append(
                    f"SX1261 degraded: {h['timeout']} spectral scan timeouts in last "
                    f"{int(_window_s/60)} min"
                )
            else:
                h["health_score"] = "critical"
                snapshot["alerts"].append(
                    f"SX1261 CRITICAL: {h['timeout']} spectral scan timeouts in last "
                    f"{int(_window_s/60)} min"
                )
            if h["recoveries"] > 0:
                snapshot["alerts"].append(
                    f"SX1261 recoveries triggered {h['recoveries']}x in last "
                    f"{int(_window_s/60)} min"
                )
        except Exception as _e:
            snapshot["alerts"].append(f"sx1261_health analysis failed: {_e}")

        # ---- CAD retry histogram (best-effort; schema may vary) -----------
        try:
            with _sql.connect(_db, timeout=5) as conn:
                conn.row_factory = _sql.Row
                # Check if cad_events has a 'retries' column; if not, skip
                cols = [r[1] for r in conn.execute(
                    "PRAGMA table_info(cad_events)").fetchall()]
                if "channel_id" in cols and ("retries" in cols or "cad_retries" in cols):
                    _rcol = "retries" if "retries" in cols else "cad_retries"
                    rows = conn.execute(
                        f"SELECT channel_id, {_rcol} AS r, COUNT(*) AS n "
                        "FROM cad_events "
                        "WHERE timestamp >= ? "
                        f"GROUP BY channel_id, {_rcol}",
                        (_since,),
                    ).fetchall()
                    hist: dict[str, dict] = {}
                    for r in rows:
                        ch = r["channel_id"] or "unknown"
                        rv = int(r["r"] or 0)
                        n = int(r["n"] or 0)
                        _h = hist.setdefault(ch, {"r=0": 0, "r>=5": 0, "r=15_forced": 0, "total": 0})
                        _h["total"] += n
                        if rv == 0:
                            _h["r=0"] += n
                        elif rv >= 15:
                            _h["r=15_forced"] += n
                        elif rv >= 5:
                            _h["r>=5"] += n
                    snapshot["cad_retry_histogram"] = hist
        except Exception as _e:
            snapshot["alerts"].append(f"cad_retry_histogram analysis failed: {_e}")

        # Write output
        try:
            out_dir = Path(work_dir) / "stats"
            out_dir.mkdir(parents=True, exist_ok=True)
            (out_dir / "health_snapshot.json").write_text(
                json.dumps(snapshot, indent=2, default=str), encoding="utf-8"
            )
        except Exception as _e:
            logger.debug("DebugCollector: writing health_snapshot failed: %s", _e)

    def _collect_packet_traces(self, work_dir):
        """Dump recent packet traces from the packet_trace ring buffer."""
        try:
            from repeater.web.packet_trace import get_traces
            traces = get_traces(limit=200)
            out_dir = os.path.join(str(work_dir), "stats")
            os.makedirs(out_dir, exist_ok=True)
            with open(os.path.join(out_dir, "packet_traces.json"), "w") as f:
                json.dump(traces, f, indent=2, default=str)
        except Exception:
            pass

    def _collect_versions(self, work_dir):
        """Collect package, repo and binary versions.

        WM1303 v2.5.0 expanded: now includes per-repo branch + remote URL +
        ahead/behind counts vs. upstream tracking branch, plus setuptools-scm
        calculated versions and HAL patch detection (EXP-v markers).
        """
        out = []
        # WM1303 version file
        try:
            with open(resolve_config_path('version')) as f:
                out.append(f"pyMC_WM1303 (overlay): {f.read().strip()}")
        except Exception:
            pass

        import subprocess as _sp

        # Per-repo detailed info
        for repo in ["/opt/pymc_repeater/repos/pyMC_core",
                     "/opt/pymc_repeater/repos/pyMC_Repeater",
                     "/opt/pymc_repeater/repos/sx1302_hal"]:
            name = os.path.basename(repo)
            out.append(f"\n--- {name} ---")
            try:
                r = _sp.run(["git", "-C", repo, "describe", "--always", "--dirty"],
                            capture_output=True, text=True, timeout=5)
                out.append(f"  describe: {r.stdout.strip() or 'unknown'}")
            except Exception as e:
                out.append(f"  describe: ERROR {e}")
            try:
                r = _sp.run(["git", "-C", repo, "branch", "--show-current"],
                            capture_output=True, text=True, timeout=5)
                out.append(f"  branch:   {r.stdout.strip() or '(detached)'}")
            except Exception:
                pass
            try:
                r = _sp.run(["git", "-C", repo, "rev-parse", "HEAD"],
                            capture_output=True, text=True, timeout=5)
                out.append(f"  HEAD:     {r.stdout.strip()}")
            except Exception:
                pass
            try:
                r = _sp.run(["git", "-C", repo, "remote", "get-url", "origin"],
                            capture_output=True, text=True, timeout=5)
                out.append(f"  origin:   {r.stdout.strip()}")
            except Exception:
                pass
            # ahead/behind vs origin/<current branch>
            try:
                br = _sp.run(["git", "-C", repo, "branch", "--show-current"],
                             capture_output=True, text=True, timeout=5).stdout.strip()
                if br:
                    r = _sp.run(["git", "-C", repo, "rev-list", "--left-right",
                                 "--count", f"HEAD...origin/{br}"],
                                capture_output=True, text=True, timeout=5)
                    if r.returncode == 0 and r.stdout.strip():
                        ahead, behind = r.stdout.strip().split()
                        out.append(f"  vs origin/{br}: {ahead} ahead, {behind} behind")
            except Exception:
                pass

        # setuptools-scm calculated versions (different from git describe)
        out.append("\n--- Installed Python package versions (setuptools-scm) ---")
        for pkg in ["openhop_core", "pymc_repeater"]:
            try:
                r = _sp.run(["python3", "-c",
                             f"import importlib.metadata as m; print(m.version('{pkg}'))"],
                            capture_output=True, text=True, timeout=5)
                out.append(f"  {pkg}: {r.stdout.strip() or 'unknown'}")
            except Exception as e:
                out.append(f"  {pkg}: ERROR {e}")

        # pkt_fwd binary version
        out.append("\n--- pkt_fwd binary ---")
        try:
            r = _sp.run([str(_PKTFWD_DIR / "lora_pkt_fwd"), "-v"],
                        capture_output=True, text=True, timeout=5)
            out.append(f"  {(r.stdout or r.stderr).strip()[:200]}")
        except Exception as e:
            out.append(f"  ERROR {e}")

        # HAL patch detection (EXP-v markers in source files)
        out.append("\n--- HAL patch detection (EXP markers) ---")
        hal_src_files = [
            str(_HAL_DIR / "libloragw/src/loragw_sx1302.c"),
            str(_HAL_DIR / "packet_forwarder/src/lora_pkt_fwd.c"),
        ]
        for fpath in hal_src_files:
            try:
                r = _sp.run(["grep", "-c", "-E", r"EXP-?v[0-9]|WM1303 ", fpath],
                            capture_output=True, text=True, timeout=5)
                count = r.stdout.strip() or "0"
                out.append(f"  {os.path.basename(fpath)}: {count} WM1303/EXP markers")
                # Show first few markers if any
                if count != "0":
                    r2 = _sp.run(["grep", "-n", "-E", r"EXP-?v[0-9]|WM1303 ", fpath],
                                 capture_output=True, text=True, timeout=5)
                    for line in r2.stdout.splitlines()[:5]:
                        out.append(f"    {line[:150]}")
            except Exception:
                pass

        integrity_dir = os.path.join(str(work_dir), "integrity")
        os.makedirs(integrity_dir, exist_ok=True)
        with open(os.path.join(integrity_dir, "versions.txt"), "w") as f:
            f.write("\n".join(out))

    def _collect_wm1303_state(self, work_dir):
        """WM1303 v2.5.0: Collect WM1303-specific runtime state.

        Captures top-level UI config (region, sync_word, channels including
        Channel E and Channel F), available regional presets, and the
        generated bridge_conf.json that pkt_fwd uses. All sensitive fields
        flow through _sanitize_config().
        """
        state_dir = os.path.join(str(work_dir), "wm1303_state")
        os.makedirs(state_dir, exist_ok=True)

        # UI config (region, sync_word, channels)
        ui_path = str(resolve_config_path('wm1303_ui.json'))
        try:
            if os.path.exists(ui_path):
                with open(ui_path) as f:
                    raw = json.load(f)
                sanitized = self._sanitize_config(raw)
                self._write_json(Path(state_dir) / "wm1303_ui.json", sanitized)
                # Extracted summary for quick scanning
                summary_lines = [
                    "=== WM1303 UI Top-Level State ===",
                    f"region:    {raw.get('region', '<not set>')}",
                    f"sync_word: {raw.get('sync_word', '<not set>')}",
                    f"channels:  {len(raw.get('channels', []))} configured",
                ]
                ch_f = raw.get("channel_f")
                if ch_f:
                    summary_lines.append("")
                    summary_lines.append("=== Channel F (service modem) ===")
                    for k in ["enabled", "frequency", "bandwidth",
                              "spreading_factor", "coding_rate", "tx_power",
                              "friendly_name"]:
                        if k in ch_f:
                            summary_lines.append(f"  {k}: {ch_f[k]}")
                for ch in raw.get("channels", []):
                    cid = ch.get("id") or ch.get("channel_id") or "?"
                    summary_lines.append("")
                    summary_lines.append(f"=== Channel {cid} ===")
                    for k in ["enabled", "frequency", "bandwidth",
                              "spreading_factor", "coding_rate", "tx_power",
                              "lbt_enabled", "friendly_name"]:
                        if k in ch:
                            summary_lines.append(f"  {k}: {ch[k]}")
                self._write(Path(state_dir) / "wm1303_ui_summary.txt",
                            "\n".join(summary_lines))
        except Exception as e:
            self._write(Path(state_dir) / "wm1303_ui.ERROR.txt", str(e))

        # Region presets
        presets_path = str(resolve_config_path('presets.json'))
        try:
            if os.path.exists(presets_path):
                with open(presets_path) as f:
                    self._write_json(Path(state_dir) / "presets.json", json.load(f))
        except Exception as e:
            self._write(Path(state_dir) / "presets.ERROR.txt", str(e))

        # Generated bridge_conf.json (pkt_fwd input)
        for cfg_name in ["bridge_conf.json", "global_conf.json"]:
            cfg_path = fstr(resolve_config_path('{cfg_name}'))
            try:
                if os.path.exists(cfg_path):
                    with open(cfg_path) as f:
                        self._write_json(Path(state_dir) / cfg_name, json.load(f))
            except Exception as e:
                self._write(Path(state_dir) / f"{cfg_name}.ERROR.txt", str(e))

    def _collect_service_journal(self, work_dir, n=500):
        """WM1303 v2.5.0: Capture recent pymc-repeater service journal.

        Without this, diagnosing service startup, bridge, or TX/RX issues
        from a debug bundle requires the user to run journalctl manually.
        """
        logs_dir = os.path.join(str(work_dir), "logs")
        os.makedirs(logs_dir, exist_ok=True)
        try:
            output = self._run_cmd(
                f"journalctl -u pymc-repeater -n {n} --no-pager 2>/dev/null",
                timeout=15,
            )
            self._write(Path(logs_dir) / "service_journal.txt",
                        output or "<no output>")
        except Exception as e:
            self._write(Path(logs_dir) / "service_journal.ERROR.txt", str(e))

    def _collect_overlay_summary(self, work_dir):
        """WM1303 v2.5.0: Detect which WM1303 overlay features are active.

        Greps overlay files for known feature markers so we can verify at a
        glance whether v2.4.9 Channel F, v2.4.10 presets, v2.4.11 telemetry,
        v2.4.12 banner+IF-guard, and v2.5.0 update-fork overlays are present
        and correctly deployed on this device.
        """
        integrity_dir = os.path.join(str(work_dir), "integrity")
        os.makedirs(integrity_dir, exist_ok=True)

        repeater_base = "/opt/pymc_repeater/repos/pyMC_Repeater/repeater"
        # Detect overlay site-packages base for openhop_core
        sp_base = self._run_cmd(
            "python3 -c 'import openhop_core, os; print(os.path.dirname(os.path.dirname(openhop_core.__file__)))' 2>/dev/null"
        ).strip()

        checks = [
            ("v2.4.9 — Channel F bridge file",
             f"test -f '{repeater_base}/channel_f_bridge.py' && echo ACTIVE || echo MISSING"),
            ("v2.4.9 — region_config.py",
             f"test -f '{sp_base}/openhop_core/hardware/region_config.py' 2>/dev/null && echo ACTIVE || echo MISSING"),
            ("v2.4.10 — presets.json deployed",
             "test -f /etc/openhop_repeater/presets.json -o -f /etc/pymc_repeater/presets.json && echo ACTIVE || echo MISSING"),
            ("v2.4.11 — Telemetry helper",
             f"test -f '{repeater_base}/wm1303_telemetry_helper.py' && echo ACTIVE || echo MISSING"),
            ("v2.4.11 — Mesh CLI owner.info handler",
             f"test -f '{repeater_base}/handler_helpers/mesh_cli.py' && echo ACTIVE || echo MISSING"),
            ("v2.4.12 — Bug 2 region banner in wm1303.html",
             f"grep -q 'Issue #7 Bug 2' '{repeater_base}/web/html/wm1303.html' && echo ACTIVE || echo MISSING"),
            ("v2.4.12 — Bug 3 IF guard in wm1303_backend.py",
             f"grep -q 'Issue #7 Bug 3' '{sp_base}/openhop_core/hardware/wm1303_backend.py' 2>/dev/null && echo ACTIVE || echo MISSING"),
            ("v2.5.0 — update_endpoints.py overlay",
             f"test -f '{repeater_base}/web/update_endpoints.py' && echo PRESENT || echo MISSING"),
            ("v2.5.0 — update check points at fork (HansvanMeer)",
             f"grep -q 'GITHUB_OWNER = \"HansvanMeer\"' '{repeater_base}/web/update_endpoints.py' 2>/dev/null && echo ACTIVE || echo MISSING"),
            ("v2.5.0 — pyMC_Repeater on main branch",
             "git -C /opt/pymc_repeater/repos/pyMC_Repeater branch --show-current 2>/dev/null"),
            ("v2.5.0 — pyMC_core on main branch",
             "git -C /opt/pymc_repeater/repos/pyMC_core branch --show-current 2>/dev/null"),
            ("HAL — sx1302_hal current ref",
             "git -C /opt/pymc_repeater/repos/sx1302_hal describe --always --dirty 2>/dev/null"),
        ]

        lines = ["=== WM1303 Overlay Feature Detection ===", ""]
        for label, cmd in checks:
            try:
                result = self._run_cmd(cmd, timeout=5).strip() or "(no output)"
            except Exception as e:
                result = f"ERROR {e}"
            lines.append(f"  [{result:<10}] {label}")

        self._write(Path(integrity_dir) / "overlay_summary.txt",
                    "\n".join(lines))

    def _write_manifest(self, work_dir):
        """Write a manifest.json listing every file in the bundle with size."""
        import datetime as _dt
        manifest = {
            "generated_at": _dt.datetime.utcnow().isoformat() + "Z",
            "generator": "debug_collector v2",
            "files": []
        }
        for root, _, files in os.walk(str(work_dir)):
            for fn in files:
                full = os.path.join(root, fn)
                rel = os.path.relpath(full, str(work_dir))
                try:
                    manifest["files"].append({"path": rel, "size": os.path.getsize(full)})
                except Exception:
                    pass
        with open(os.path.join(str(work_dir), "manifest.json"), "w") as f:
            json.dump(manifest, f, indent=2)
