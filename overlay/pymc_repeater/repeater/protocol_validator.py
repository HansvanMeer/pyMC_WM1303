"""Central MeshCore protocol validator (Layer 2 of defensive filtering).

Validates RF/companion-injected packets against the MeshCore wire-protocol
spec.  Returns a structured result with forensic metadata so invalid packets
can be recorded in the database and inspected via the WM1303 Manager UI.

Design rules (see .notes/PROTOCOL_VALIDATOR_DESIGN.md):
  * No crypto / no Ed25519 — structural checks only (< 50 microseconds per call).
  * Must NOT impact RX availability or TX timing (project design principle).
  * Best-effort metadata extraction even for badly-formed packets, so the
    invalid-packets UI tab still has something to show.

Drop reasons (stable identifiers used by storage and UI):
  too_short
  invalid_route_type
  reserved_header_bits
  reserved_path_len_hash_size_4
  path_overflow
  transport_code_length_mismatch
  length_implausible

The full packet hex is recorded by the storage layer; this module only
parses fields, it does not access the database.
"""

from dataclasses import dataclass, field
from hashlib import sha256
from typing import Any, Dict, Optional, Tuple

# MeshCore protocol constants ──────────────────────────────────────────────
# Route type lives in bits[1:0] of the header byte.  Values 0x00..0x03 are
# defined; anything outside that range is unreachable (only two bits), but
# we keep the explicit list for documentation and to match validate_packet.
ROUTE_TYPE_MASK = 0x03
ROUTE_TYPE_TFLOOD = 0x00
ROUTE_TYPE_TDIRECT = 0x03
# Header bits[7:2] are currently defined as the packet-type field (4 bits)
# plus 2 bits reserved on certain transports.  We do NOT enforce specific
# values for the upper bits today — Layer 2 only catches malformed bytes
# that demonstrably break downstream parsing.

# path_len byte layout: hash_size in bits[7:6], hop_count in bits[5:0]
HASH_SIZE_MASK = 0xC0          # bits[7:6]
HASH_SIZE_SHIFT = 6
HOP_COUNT_MASK = 0x3F          # bits[5:0]
HASH_SIZE_RESERVED = 0x03      # = 0b11 → hash_size = 4 (reserved)

# Path layout limits.  MAX_PATH_SIZE is enforced by engine.validate_packet
# at the dispatcher level; we duplicate it here so the bridge-side check
# can fire before forwarding.
MAX_PATH_SIZE = 64
# Transport-code prefix length (in bytes) for TFLOOD / TDIRECT routes.
TRANSPORT_CODE_PREFIX_LEN = 4
# Minimum plausible packet length (header + path_len byte at minimum).
MIN_PACKET_LEN = 2
# Hard upper bound — MeshCore frames over LoRa are well under 256 bytes.
MAX_PACKET_LEN = 256


@dataclass
class ValidationResult:
    """Result of validating one packet against the MeshCore protocol spec."""

    is_valid: bool
    reason: str = ""          # stable identifier (see module docstring)
    severity: str = "drop"    # "drop" | "warn"  (only "drop" used today)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def __bool__(self) -> bool:  # convenience: `if result: ...`
        return self.is_valid


# Public helpers ──────────────────────────────────────────────────────────


def offender_id_from_metadata(metadata: Dict[str, Any], raw_packet: bytes) -> str:
    """Compute the offender id used by storage / UI grouping.

    Per HvM's design decision: when the packet has no recoverable source
    pubkey (e.g. hash_size=4 makes the path layout unparseable), use
    `unknown-<8hex>` so identical malformed shapes cluster together.

    The deterministic input is the raw packet hash; this matches HvM's
    requirement that the SAME spammer-shape always reports under one
    offender id, even if metadata extraction failed partway through.
    """
    hint = metadata.get("source_pubkey_hint")
    if hint:
        return hint
    if not raw_packet:
        return "unknown-empty"
    return "unknown-" + sha256(raw_packet).hexdigest()[:8]


def _route_type_name(route_type: int) -> str:
    return {
        0x00: "TFLOOD",
        0x01: "TFLOOD_TRANSPORT",
        0x02: "TDIRECT_TRANSPORT",
        0x03: "TDIRECT",
    }.get(route_type, f"UNKNOWN_0x{route_type:02X}")


def _path_len_index(route_type: int) -> int:
    """Byte offset of the path_len byte for a given route type.

    TFLOOD (0x00) and TDIRECT (0x03) carry a 4-byte transport-code prefix
    before the path_len byte; the others put path_len immediately after
    the header.  Mirrors `_has_reserved_path_len` in ViezeVingertjes' patch.
    """
    if route_type in (ROUTE_TYPE_TFLOOD, ROUTE_TYPE_TDIRECT):
        return 1 + TRANSPORT_CODE_PREFIX_LEN  # 5
    return 1


def _extract_metadata(data: bytes) -> Tuple[Dict[str, Any], Optional[int]]:
    """Best-effort structural parse for forensic display.

    Always returns a dict (possibly with `parse_partial=True`), even for
    truncated/malformed packets.  Second return is the resolved path_len
    byte index, or None when the packet is too short to locate it.
    """
    meta: Dict[str, Any] = {
        "packet_length": len(data),
        "parse_partial": False,
    }
    if not data:
        meta["parse_partial"] = True
        return meta, None

    header = data[0]
    route_type = header & ROUTE_TYPE_MASK
    meta["header_hex"] = f"{header:02x}"
    meta["route_type"] = route_type
    meta["route_type_name"] = _route_type_name(route_type)

    # Transport-code prefix (TFLOOD / TDIRECT only).
    if route_type in (ROUTE_TYPE_TFLOOD, ROUTE_TYPE_TDIRECT):
        if len(data) >= 1 + TRANSPORT_CODE_PREFIX_LEN:
            meta["transport_codes_hex"] = data[1:1 + TRANSPORT_CODE_PREFIX_LEN].hex()
        else:
            meta["parse_partial"] = True
            meta["transport_codes_hex"] = data[1:].hex()
            return meta, None

    pl_idx = _path_len_index(route_type)
    if pl_idx >= len(data):
        meta["parse_partial"] = True
        return meta, None

    path_len_byte = data[pl_idx]
    hash_size_bits = (path_len_byte >> HASH_SIZE_SHIFT) & 0x03
    # MeshCore encodes hash sizes 1..3 as bit-patterns 0b00..0b10 and uses
    # 0b11 as a reserved sentinel.  Surface both the raw bits and the
    # decoded hash-size for the UI tooltip.
    hash_size = 4 if hash_size_bits == HASH_SIZE_RESERVED else (hash_size_bits + 1)
    hop_count = path_len_byte & HOP_COUNT_MASK

    meta["path_len_byte"] = path_len_byte
    meta["hash_size"] = hash_size
    meta["hop_count"] = hop_count

    # Path bytes follow the path_len byte; we can only parse them if the
    # hash_size is sensible.  For reserved/invalid hash_size we still
    # surface the raw bytes so the UI can show "what came after path_len".
    path_start = pl_idx + 1
    if hash_size_bits == HASH_SIZE_RESERVED:
        # Reserved size — cannot trust the parsed hop layout, but we
        # still attach the raw tail for forensics.
        tail = data[path_start:]
        meta["path_hex"] = tail.hex()
        meta["payload_first_16_hex"] = tail[:16].hex()
    else:
        path_bytes_total = hop_count * hash_size
        path_end = path_start + path_bytes_total
        path_bytes = data[path_start:min(path_end, len(data))]
        meta["path_hex"] = path_bytes.hex()
        # First hop hash makes a useful "source hint" — it's the most-recent
        # hop, which on a directly-heard advert IS the originator.
        if hash_size <= len(path_bytes):
            meta["source_pubkey_hint"] = path_bytes[:hash_size].hex()
        # Payload (whatever follows the path) — first 16 bytes for display.
        meta["payload_first_16_hex"] = data[path_end:path_end + 16].hex() \
            if path_end <= len(data) else b"".hex()

    return meta, pl_idx


def _fail(reason: str, metadata: Dict[str, Any]) -> ValidationResult:
    return ValidationResult(is_valid=False, reason=reason, metadata=metadata)


def validate(data: bytes) -> ValidationResult:
    """Validate one packet against the MeshCore wire-protocol spec.

    Returns a ValidationResult with `.metadata` populated even on failure,
    so the invalid_packets storage layer can record forensic context.

    This function is hot — called once per RF RX and once per injected
    packet.  Keep it allocation-light and avoid logging here; the caller
    decides what to log/store.
    """
    metadata, pl_idx = _extract_metadata(data)

    # 1. Too short — even a minimal header + path_len byte is required.
    if len(data) < MIN_PACKET_LEN:
        return _fail("too_short", metadata)

    # 2. Length plausibility — defends against truncation and oversized garbage.
    if len(data) > MAX_PACKET_LEN:
        return _fail("length_implausible", metadata)

    route_type = data[0] & ROUTE_TYPE_MASK
    # 3. Route type — bits[1:0] can only be 0..3; no overlay defines any
    #    other value today, so this is currently a documentation guard.
    if route_type not in (0x00, 0x01, 0x02, 0x03):
        return _fail("invalid_route_type", metadata)

    # 4. Transport-code prefix length for TFLOOD / TDIRECT must fit.
    if route_type in (ROUTE_TYPE_TFLOOD, ROUTE_TYPE_TDIRECT):
        if len(data) < 1 + TRANSPORT_CODE_PREFIX_LEN:
            return _fail("transport_code_length_mismatch", metadata)

    # 5. We must be able to read the path_len byte itself.
    if pl_idx is None or pl_idx >= len(data):
        return _fail("too_short", metadata)

    path_len_byte = data[pl_idx]
    hash_size_bits = (path_len_byte >> HASH_SIZE_SHIFT) & 0x03

    # 6. Reserved hash_size (0b11) — the spammer signature ViezeVingertjes
    #    documented.  This is the most common drop reason in practice.
    if hash_size_bits == HASH_SIZE_RESERVED:
        return _fail("reserved_path_len_hash_size_4", metadata)

    hash_size = hash_size_bits + 1
    hop_count = path_len_byte & HOP_COUNT_MASK

    # 7. Path overflow — duplicates engine.validate_packet's MAX_PATH_SIZE
    #    check at the bridge layer so RF→radio forwards reject it too.
    if hop_count > MAX_PATH_SIZE:
        return _fail("path_overflow", metadata)

    # 8. Path bytes must actually fit inside the packet.
    path_start = pl_idx + 1
    path_end = path_start + hop_count * hash_size
    if path_end > len(data):
        return _fail("length_implausible", metadata)

    return ValidationResult(is_valid=True, metadata=metadata)


# ─────────────────────────────────────────────────────────────────────────
# Layer 3 storage hook — used by integration sites to persist drops.
# Registered once at process startup (see main.py); validate_and_record()
# is a thin wrapper that the bridge / engine / advert handlers call.
# ─────────────────────────────────────────────────────────────────────────

import logging
import time

_logger = logging.getLogger(__name__)

# Storage callback; signature: callback(record: dict) -> None.  Best-effort
# (fire-and-forget); validate_and_record() catches any exception so a slow
# or failing store NEVER blocks the RX/TX path (project design principle).
_invalid_packet_store = None


def set_invalid_packet_store(callback) -> None:
    """Register the callback used to persist invalid packets.

    Typically wired in main.py once the SQLiteHandler is initialised:
        from repeater.protocol_validator import set_invalid_packet_store
        set_invalid_packet_store(sqlite_handler.store_invalid_packet)
    """
    global _invalid_packet_store
    _invalid_packet_store = callback


def validate_and_record(data: bytes,
                        channel: str = "",
                        rssi=None,
                        snr=None) -> ValidationResult:
    """Validate ``data`` and (best-effort) record the drop if invalid.

    Returns the ValidationResult so the caller can branch on ``.is_valid``.
    Records are stored only when a callback has been registered via
    :func:`set_invalid_packet_store`; otherwise this behaves exactly like
    :func:`validate`.
    """
    result = validate(data)
    if not result.is_valid and _invalid_packet_store is not None:
        try:
            record = dict(result.metadata)
            record["timestamp"] = time.time()
            record["channel"] = channel
            record["drop_reason"] = result.reason
            record["raw_packet_hex"] = data.hex() if data else ""
            record["source_pubkey_hint"] = offender_id_from_metadata(
                result.metadata, data)
            record["rssi"] = rssi
            record["snr"] = snr
            _invalid_packet_store(record)
        except Exception as exc:  # noqa: BLE001
            _logger.debug("invalid-packet store failed (non-fatal): %s", exc)
    return result
