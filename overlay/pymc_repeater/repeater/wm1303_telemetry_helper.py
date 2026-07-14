"""
WM1303 Telemetry Helper
=======================

Extends pymc_repeater's ProtocolRequestHelper to:

1. Add support for REQ_TYPE_GET_TELEMETRY_DATA (0x03), which is defined in the
   MeshCore protocol but not implemented in upstream pymc_repeater. Provides
   Raspberry Pi + WM1303 hardware telemetry encoded in CayenneLPP format,
   accessible from any MeshCore companion (mobile app, T-Echo, etc).

2. Override _handle_get_owner_info (REQ_TYPE 0x07) to append Raspberry Pi
   hardware model and WM1303 SX1302 concentrator EUI to the standard
   firmware-version/node-name/owner-info response. Provides static device
   info to companions in a single info query.

Telemetry layout (Option B — uses CayenneLPP types with native unit labels):
- Channel 1: CPU temperature        (Temperature, 0x67, °C)
- Channel 2: WM1303 SX1302 temp     (Temperature, 0x67, °C)
- Channel 3: CPU usage              (Humidity,    0x68, %)
- Channel 4: Memory usage           (Humidity,    0x68, %)
- Channel 5: Disk usage             (Humidity,    0x68, %)

Owner info layout (appended after upstream version/name/owner):
- hw=<Raspberry Pi model string>   (when /proc/device-tree/model is readable)
- mem_total=<MiB>                   (always)
- disk_total=<GiB>                  (always)
- wm1303_eui=<8-byte hex EUI>      (when /tmp/concentrator_eui is present)

This module is part of the pyMC_WM1303 overlay and does NOT modify
upstream pymc_repeater or openhop_core code.
"""

import logging
import struct
import time

from openhop_core.node.handlers.protocol_request import (
    REQ_TYPE_GET_ACCESS_LIST,
    REQ_TYPE_GET_NEIGHBOURS,
    REQ_TYPE_GET_OWNER_INFO,
    REQ_TYPE_GET_STATUS,
    REQ_TYPE_GET_TELEMETRY_DATA,
    ProtocolRequestHandler,
)
from repeater.handler_helpers.protocol_request import ProtocolRequestHelper

# === WM1303 OVERLAY: Firmware version capability patch ===
# Upstream openhop_core sets FIRMWARE_VER_LEVEL = 1 in login_server, which the
# MeshCore companion treats as 'too old to support owner_info / telemetry'.
# We override it to 11 (matches FIRMWARE_VER_CODE used by companion side) so
# the companion app enables the Owner Info button and sends REQ_TYPE 0x07.
# This patch happens at module import time, before LoginHelper builds any
# login response packets.
import openhop_core.node.handlers.login_server as _login_server_module
_login_server_module.FIRMWARE_VER_LEVEL = 11
# =========================================================

logger = logging.getLogger("WM1303TelemetryHelper")
logger.info(
    "WM1303: Patched openhop_core FIRMWARE_VER_LEVEL = %d "
    "(claims owner_info + telemetry support in companion)",
    _login_server_module.FIRMWARE_VER_LEVEL,
)

# CayenneLPP data type identifiers (subset used here)
CLPP_TYPE_TEMPERATURE = 0x67    # 2 bytes, signed int * 0.1 °C
CLPP_TYPE_HUMIDITY = 0x68       # 1 byte, unsigned int / 2, range 0-100% (0.5% precision)


class WM1303ProtocolRequestHelper(ProtocolRequestHelper):
    """Drop-in replacement for ProtocolRequestHelper with WM1303 extensions.

    Adds:
    - REQ_TYPE_GET_TELEMETRY_DATA (0x03) handling: CayenneLPP-encoded Pi +
      WM1303 metrics (CPU/concentrator temperature, CPU/memory/disk usage %).
    - Override of REQ_TYPE_GET_OWNER_INFO (0x07) to append Pi model and
      WM1303 EUI to the standard firmware/name/owner string response.
    """

    # ------------------------------------------------------------------
    # Override register_identity to inject telemetry handler and override
    # owner-info handler with our extended version.
    # ------------------------------------------------------------------
    def register_identity(self, name, identity, identity_type="repeater"):
        """Replicate the upstream registration with WM1303 handler swaps.

        We can't simply call super() because the upstream method builds the
        request_handlers dict locally without exposing an extension point.
        Mirroring the logic here keeps the WM1303 features confined to the
        overlay.
        """
        hash_byte = identity.get_public_key()[0]

        identity_acl = self.acl_dict.get(hash_byte)
        if not identity_acl:
            logger.warning(
                f"Cannot register identity '{name}': no ACL for hash 0x{hash_byte:02X}"
            )
            return

        acl_contacts = self._create_acl_contacts_wrapper(identity_acl)

        # Build request handlers dict — same as upstream PLUS WM1303 extensions
        request_handlers = {
            REQ_TYPE_GET_STATUS: self._handle_get_status,
            REQ_TYPE_GET_ACCESS_LIST: self._make_handle_get_access_list(identity_acl),
            REQ_TYPE_GET_NEIGHBOURS: self._handle_get_neighbours,
            # === WM1303 OVERLAY EXTENSIONS ===
            REQ_TYPE_GET_OWNER_INFO: self._handle_get_owner_info_wm1303,
            REQ_TYPE_GET_TELEMETRY_DATA: self._handle_get_telemetry_data,
            # =================================
        }

        handler = ProtocolRequestHandler(
            local_identity=identity,
            contacts=acl_contacts,
            get_client_fn=lambda src_hash: self._get_client_from_acl(
                identity_acl, src_hash
            ),
            request_handlers=request_handlers,
            log_fn=logger.info,
        )

        self.handlers[hash_byte] = {
            "handler": handler,
            "identity": identity,
            "name": name,
            "type": identity_type,
        }

        logger.info(
            f"WM1303: Registered protocol request handler for '{name}': "
            f"hash=0x{hash_byte:02X} (with TELEMETRY and extended OWNER_INFO support)"
        )

    # ------------------------------------------------------------------
    # WM1303 telemetry handler — Option B (Humidity for percentages)
    # ------------------------------------------------------------------
    def _handle_get_telemetry_data(self, client, timestamp: int, req_data: bytes):
        """Build CayenneLPP-encoded telemetry response.

        Reads live system metrics from /sys, /proc, and /tmp and encodes them
        in CayenneLPP format using types that have native unit labels:
        - Temperature (0x67) for °C
        - Humidity (0x68) for percentages (note: 'Humidity' label is shown
          by companions, but the numeric value with '%' unit is correct)

        Each metric is: channel(1) + type(1) + data(variable bytes).
        """
        payload = bytearray()

        # --- Channel 1: CPU temperature (°C) ---
        cpu_temp = self._read_cpu_temperature()
        if cpu_temp is not None:
            payload += self._encode_temperature(channel=1, value_c=cpu_temp)

        # --- Channel 2: WM1303 SX1302 concentrator temperature (°C) ---
        conc_temp = self._read_concentrator_temperature()
        if conc_temp is not None:
            payload += self._encode_temperature(channel=2, value_c=conc_temp)

        # --- Channel 3: CPU usage % (encoded as Humidity for unit labeling) ---
        cpu_usage = self._read_cpu_usage()
        if cpu_usage is not None:
            payload += self._encode_humidity(channel=3, percent=cpu_usage)

        # --- Channel 4: Memory usage % ---
        mem_usage = self._read_memory_usage_percent()
        if mem_usage is not None:
            payload += self._encode_humidity(channel=4, percent=mem_usage)

        # --- Channel 5: Disk usage % (root filesystem) ---
        disk_usage = self._read_disk_usage_percent()
        if disk_usage is not None:
            payload += self._encode_humidity(channel=5, percent=disk_usage)

        logger.debug(
            "GET_TELEMETRY_DATA: cpu_temp=%s°C, wm1303_temp=%s°C, cpu=%s%%, mem=%s%%, disk=%s%%",
            cpu_temp,
            conc_temp,
            cpu_usage,
            mem_usage,
            disk_usage,
        )

        return bytes(payload)

    # ------------------------------------------------------------------
    # WM1303 owner-info handler — extends upstream with hardware info
    # ------------------------------------------------------------------
    def _handle_get_owner_info_wm1303(self, client, timestamp: int, req_data: bytes):
        """Build extended GET_OWNER_INFO response.

        Format (newline-separated UTF-8 string, matching upstream firmware
        layout `%s\\n%s\\n%s` for version/name/owner, with optional
        appended lines for hardware info):

            <fw_version>\n<node_name>\n<owner_info>
            [\nhw=<Raspberry Pi model>]
            [\nwm1303_eui=<8-byte hex>]

        Companions parse this as a free-form info blob; extra lines beyond
        the first three are typically displayed as additional metadata or
        ignored, so this is backwards compatible.
        """
        # --- Upstream-compatible portion: fw_version, node_name, owner_info ---
        repeater_cfg = self.config.get("repeater", {})
        node_name = repeater_cfg.get("node_name", "pyMC_Repeater")
        owner_info = repeater_cfg.get("owner_info", "")

        try:
            from importlib.metadata import version as pkg_version
            fw_version = pkg_version("pymc-repeater")
        except Exception:
            fw_version = "pyMC"

        # --- WM1303 extensions: hardware model + EUI ---
        lines = [fw_version, node_name, owner_info]

        pi_model = self._read_pi_model()
        if pi_model:
            lines.append(f"hw={pi_model}")

        # Static hardware capacity (helps user identify the device's specs)
        mem_total_mb = self._read_total_memory_mb()
        if mem_total_mb is not None:
            lines.append(f"mem_total={mem_total_mb} MiB")

        disk_total_gb = self._read_total_disk_gb()
        if disk_total_gb is not None:
            lines.append(f"disk_total={disk_total_gb} GiB")

        wm1303_eui = self._read_wm1303_eui()
        if wm1303_eui:
            lines.append(f"wm1303_eui={wm1303_eui}")

        result = "\n".join(lines).encode("utf-8")

        logger.debug(
            "GET_OWNER_INFO (WM1303 extended): %s",
            result.decode("utf-8", errors="replace"),
        )
        return result

    # ------------------------------------------------------------------
    # CayenneLPP encoding helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _encode_temperature(channel: int, value_c: float) -> bytes:
        """CayenneLPP Temperature: int16 big-endian × 0.1 °C."""
        raw = int(round(value_c * 10))
        raw = max(-32768, min(32767, raw))
        return struct.pack(">BBh", channel, CLPP_TYPE_TEMPERATURE, raw)

    @staticmethod
    def _encode_humidity(channel: int, percent: float) -> bytes:
        """CayenneLPP Humidity: uint8 × 0.5 % (range 0-100% = 0-200 raw).

        Used here for any percentage metric (CPU/mem/disk usage). The
        companion will display it as a 'Humidity' field with '%' unit and
        the correct numeric value with 0.5% precision.
        """
        raw = int(round(percent * 2))
        raw = max(0, min(255, raw))  # uint8 clamp; >100% allowed up to 127.5%
        return struct.pack(">BBB", channel, CLPP_TYPE_HUMIDITY, raw)

    # ------------------------------------------------------------------
    # Metric readers (all return None on failure to skip the field)
    # ------------------------------------------------------------------
    @staticmethod
    def _read_cpu_temperature() -> float:
        """Read CPU temperature from thermal zone (Raspberry Pi)."""
        try:
            with open("/sys/class/thermal/thermal_zone0/temp") as f:
                return int(f.read().strip()) / 1000.0
        except Exception as e:
            logger.debug("Failed to read CPU temperature: %s", e)
            return None

    @staticmethod
    def _read_concentrator_temperature() -> float:
        """Read SX1302 concentrator temperature written by pkt_fwd."""
        try:
            with open("/tmp/concentrator_temp") as f:
                return float(f.read().strip())
        except Exception as e:
            logger.debug("Failed to read concentrator temperature: %s", e)
            return None

    @staticmethod
    def _read_cpu_usage() -> float:
        """Read instantaneous CPU usage % from two /proc/stat samples (~0.1s apart)."""
        try:
            def _sample():
                with open("/proc/stat") as f:
                    parts = f.readline().split()
                idle = int(parts[4]) + int(parts[5])
                total = sum(int(x) for x in parts[1:])
                return idle, total

            idle1, total1 = _sample()
            time.sleep(0.1)
            idle2, total2 = _sample()

            d_idle = idle2 - idle1
            d_total = total2 - total1
            if d_total <= 0:
                return 0.0
            return (1.0 - d_idle / d_total) * 100.0
        except Exception as e:
            logger.debug("Failed to read CPU usage: %s", e)
            return None

    @staticmethod
    def _read_memory_usage_percent() -> float:
        """Calculate memory usage % from /proc/meminfo."""
        try:
            mem_total = None
            mem_available = None
            with open("/proc/meminfo") as f:
                for line in f:
                    if line.startswith("MemTotal:"):
                        mem_total = int(line.split()[1])  # kB
                    elif line.startswith("MemAvailable:"):
                        mem_available = int(line.split()[1])
                    if mem_total is not None and mem_available is not None:
                        break
            if mem_total and mem_total > 0 and mem_available is not None:
                used = mem_total - mem_available
                return (used / mem_total) * 100.0
        except Exception as e:
            logger.debug("Failed to read memory usage: %s", e)
        return None

    @staticmethod
    def _read_disk_usage_percent() -> float:
        """Calculate disk usage % of root filesystem using os.statvfs."""
        try:
            import os
            s = os.statvfs("/")
            total = s.f_blocks * s.f_frsize
            free = s.f_bavail * s.f_frsize
            if total <= 0:
                return None
            used = total - free
            return (used / total) * 100.0
        except Exception as e:
            logger.debug("Failed to read disk usage: %s", e)
            return None

    # ------------------------------------------------------------------
    # Static device-info readers for owner-info extension
    # ------------------------------------------------------------------
    @staticmethod
    def _read_total_memory_mb() -> int:
        """Read total system memory in MiB from /proc/meminfo."""
        try:
            with open("/proc/meminfo") as f:
                for line in f:
                    if line.startswith("MemTotal:"):
                        kb = int(line.split()[1])
                        return int(round(kb / 1024))
        except Exception as e:
            logger.debug("Failed to read total memory: %s", e)
        return None

    @staticmethod
    def _read_total_disk_gb() -> float:
        """Read total disk capacity of root filesystem in GiB (1 decimal)."""
        try:
            import os
            s = os.statvfs("/")
            total_bytes = s.f_blocks * s.f_frsize
            if total_bytes <= 0:
                return None
            return round(total_bytes / (1024 ** 3), 1)
        except Exception as e:
            logger.debug("Failed to read total disk: %s", e)
            return None

    @staticmethod
    def _read_pi_model() -> str:
        """Read Raspberry Pi model string from device-tree.

        Returns a clean string like 'Raspberry Pi 4 Model B Rev 1.4' or
        None if not running on a Pi or the device-tree node is unreadable.
        """
        try:
            with open("/proc/device-tree/model", "rb") as f:
                raw = f.read()
            # device-tree strings are null-terminated
            return raw.rstrip(b"\x00").decode("utf-8", errors="replace").strip()
        except Exception as e:
            logger.debug("Failed to read Pi model: %s", e)
            return None

    @staticmethod
    def _read_wm1303_eui() -> str:
        """Read WM1303 SX1302 concentrator EUI.

        Tries several fallback sources in order:
        1. /tmp/concentrator_eui (written by pkt_fwd if patched)
        2. /tmp/wm1303_status.json with 'eui' key (future backend hook)

        Returns the EUI as a hex string (uppercase, no separators) or None
        if no source is available. EUI is typically 8 bytes = 16 hex chars.
        """
        # Source 1: dedicated file written by pkt_fwd
        try:
            with open("/tmp/concentrator_eui") as f:
                eui = f.read().strip()
            if eui:
                # Normalize: strip 0x prefix, separators, uppercase
                eui = eui.replace("0x", "").replace(":", "").replace("-", "")
                eui = eui.replace(" ", "").upper()
                return eui
        except FileNotFoundError:
            pass
        except Exception as e:
            logger.debug("Failed to read /tmp/concentrator_eui: %s", e)

        # Source 2: backend status JSON (future hook)
        try:
            import json
            with open("/tmp/wm1303_status.json") as f:
                status = json.load(f)
            eui = status.get("eui") or status.get("concentrator_eui")
            if eui:
                return str(eui).replace("0x", "").replace(":", "").upper()
        except FileNotFoundError:
            pass
        except Exception as e:
            logger.debug("Failed to read /tmp/wm1303_status.json: %s", e)

        return None
