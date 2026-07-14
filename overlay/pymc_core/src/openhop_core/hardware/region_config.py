"""
Region configuration for pyMC_WM1303.

Provides regulatory frequency bands, SX1261 image calibration values,
and typical EIRP limits for the regions supported by the WM1303 backend.

Used by:
- wm1303_backend.py     -> _generate_bridge_conf() (tx_freq_min/max for HAL)
- sx1261_driver.py      -> image calibration command
- wm1303_api.py         -> spectral scan range and API exposure
- web UI                -> REGION & REGULATORY block in Channels tab

References:
- LoRaWAN Regional Parameters RP002-1.0.4
- Semtech AN1200.48 "SX1261/2 image calibration procedure"

This module is read-only data; mutations happen in the user's wm1303_ui.json.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional


# Region code -> regulatory data table
# All frequencies in Hz unless noted otherwise.
REGIONS: Dict[str, Dict[str, Any]] = {
    "EU868": {
        "display_name": "EU868 (Europe)",
        "tx_freq_min": 863_000_000,
        "tx_freq_max": 870_000_000,
        "max_eirp_dbm": 16,
        "sx1261_calib": (0xD7, 0xDB),
        "description": "Europe 863-870 MHz ISM band (SRD)",
    },
    "US915": {
        "display_name": "US915 (United States / Canada / Mexico)",
        "tx_freq_min": 902_000_000,
        "tx_freq_max": 928_000_000,
        "max_eirp_dbm": 30,
        "sx1261_calib": (0xE1, 0xE9),
        "description": "North America 902-928 MHz ISM band",
    },
    "AU915": {
        "display_name": "AU915 (Australia / Brazil)",
        "tx_freq_min": 915_000_000,
        "tx_freq_max": 928_000_000,
        "max_eirp_dbm": 30,
        "sx1261_calib": (0xE1, 0xE9),  # 900-932 MHz — must cover full AU915 band from 915.0 MHz
        "description": "Australia / Brazil 915-928 MHz ISM band",
    },
    "AS923": {
        "display_name": "AS923 (Asia)",
        "tx_freq_min": 915_000_000,
        "tx_freq_max": 928_000_000,
        "max_eirp_dbm": 16,
        "sx1261_calib": (0xE5, 0xE9),
        "description": "Asia 915-928 MHz (sub-regions AS923-1/2/3/4 vary)",
    },
    "IN865": {
        "display_name": "IN865 (India)",
        "tx_freq_min": 865_000_000,
        "tx_freq_max": 867_000_000,
        "max_eirp_dbm": 30,
        "sx1261_calib": (0xD7, 0xDB),
        "description": "India 865-867 MHz ISM band",
    },
    "JP920": {
        "display_name": "JP920 (Japan)",
        "tx_freq_min": 920_000_000,
        "tx_freq_max": 928_000_000,
        "max_eirp_dbm": 16,
        "sx1261_calib": (0xE5, 0xE9),
        "description": "Japan 920-928 MHz (ARIB STD-T108)",
    },
    "KR920": {
        "display_name": "KR920 (Korea)",
        "tx_freq_min": 920_900_000,
        "tx_freq_max": 923_300_000,
        "max_eirp_dbm": 14,
        "sx1261_calib": (0xE5, 0xE9),
        "description": "South Korea 920.9-923.3 MHz",
    },
    "CUSTOM": {
        "display_name": "Custom (user-defined)",
        "tx_freq_min": None,  # filled by user via UI
        "tx_freq_max": None,  # filled by user via UI
        "max_eirp_dbm": None,
        "sx1261_calib": None,  # derived from custom center freq
        "description": "User-defined frequency limits — use with care",
    },
}

# Backwards-compatible default when no region is set in wm1303_ui.json
DEFAULT_REGION = "EU868"


def get_region(code: Optional[str]) -> Dict[str, Any]:
    """Return region data dict for the given code.

    If code is None, empty, or unknown, returns the default EU868 region.
    """
    if not code:
        return REGIONS[DEFAULT_REGION]
    code_upper = str(code).strip().upper()
    return REGIONS.get(code_upper, REGIONS[DEFAULT_REGION])


def list_region_codes() -> List[str]:
    """Return all region codes (including CUSTOM) in display order."""
    return list(REGIONS.keys())


def get_tx_bounds(
    region_code: Optional[str],
    custom_min: Optional[int] = None,
    custom_max: Optional[int] = None,
    fallback_channels: Optional[List[int]] = None,
) -> tuple[int, int]:
    """Resolve TX frequency bounds (Hz) for the given region.

    Resolution order:
    1. If region is CUSTOM and custom_min/custom_max supplied -> use those.
    2. If region is a known non-CUSTOM region -> use its hard-coded bounds.
    3. If fallback_channels is supplied (list of active channel frequencies in Hz)
       -> auto-derive min - 5 MHz and max + 5 MHz.
    4. Default to EU868 bounds.
    """
    code = (region_code or DEFAULT_REGION).strip().upper()

    if code == "CUSTOM":
        if custom_min is not None and custom_max is not None:
            return int(custom_min), int(custom_max)
        # CUSTOM without explicit bounds -> try auto-derive from channels
        if fallback_channels:
            return (
                int(min(fallback_channels)) - 5_000_000,
                int(max(fallback_channels)) + 5_000_000,
            )
        # Last resort: EU868 bounds
        return REGIONS[DEFAULT_REGION]["tx_freq_min"], REGIONS[DEFAULT_REGION]["tx_freq_max"]

    region = REGIONS.get(code, REGIONS[DEFAULT_REGION])
    return int(region["tx_freq_min"]), int(region["tx_freq_max"])


def get_sx1261_calib(
    region_code: Optional[str],
    fallback_center_hz: Optional[int] = None,
) -> tuple[int, int]:
    """Resolve SX1261 image calibration command bytes (cal_freq_low, cal_freq_high).

    For CUSTOM or unknown regions, derive from fallback_center_hz using the
    Semtech AN1200.48 mapping:
        430-440 MHz   -> [0x6B, 0x6F]
        470-510 MHz   -> [0x75, 0x81]
        779-787 MHz   -> [0xC1, 0xC5]
        863-870 MHz   -> [0xD7, 0xDB]
        902-928 MHz   -> [0xE1, 0xE9]
    """
    code = (region_code or DEFAULT_REGION).strip().upper()
    region = REGIONS.get(code, REGIONS[DEFAULT_REGION])
    calib = region.get("sx1261_calib")
    if calib is not None:
        return int(calib[0]), int(calib[1])

    # CUSTOM or unknown -> derive from center
    if fallback_center_hz is not None:
        f_mhz = fallback_center_hz / 1_000_000.0
        if 430 <= f_mhz <= 440:
            return 0x6B, 0x6F
        if 470 <= f_mhz <= 510:
            return 0x75, 0x81
        if 779 <= f_mhz <= 787:
            return 0xC1, 0xC5
        if 863 <= f_mhz <= 870:
            return 0xD7, 0xDB
        if 902 <= f_mhz <= 928:
            return 0xE1, 0xE9

    # Last resort: EU868
    return 0xD7, 0xDB


def get_region_summary(region_code: Optional[str]) -> Dict[str, Any]:
    """Return a UI-friendly summary dict for the API / wm1303_ui frontend.

    Example:
        {
            'code': 'AU915',
            'display_name': 'AU915 (Australia / Brazil)',
            'tx_freq_min_mhz': 915.0,
            'tx_freq_max_mhz': 928.0,
            'max_eirp_dbm': 30,
            'description': '...',
            'is_custom': False,
        }
    """
    code = (region_code or DEFAULT_REGION).strip().upper()
    if code not in REGIONS:
        code = DEFAULT_REGION
    region = REGIONS[code]
    tx_min = region.get("tx_freq_min")
    tx_max = region.get("tx_freq_max")
    return {
        "code": code,
        "display_name": region["display_name"],
        "tx_freq_min_hz": tx_min,
        "tx_freq_max_hz": tx_max,
        "tx_freq_min_mhz": (tx_min / 1_000_000.0) if tx_min is not None else None,
        "tx_freq_max_mhz": (tx_max / 1_000_000.0) if tx_max is not None else None,
        "max_eirp_dbm": region.get("max_eirp_dbm"),
        "description": region["description"],
        "is_custom": (code == "CUSTOM"),
    }


def list_region_summaries() -> List[Dict[str, Any]]:
    """Return summary for every region (for UI dropdown population)."""
    return [get_region_summary(code) for code in REGIONS.keys()]


# Sync word constants (16-bit register values for SX126x family)
# MeshCore convention:
#   PRIVATE (0x1424) - default for community MeshCore networks
#   PUBLIC  (0x3444) - LoRaWAN public sync word
SYNC_WORD_PRIVATE = 0x1424
SYNC_WORD_PUBLIC = 0x3444

SYNC_WORD_LABELS: Dict[int, str] = {
    SYNC_WORD_PRIVATE: "Private (0x1424)",
    SYNC_WORD_PUBLIC: "Public (0x3444)",
}


def get_sync_word_label(sync_word: int) -> str:
    """Return UI label for known sync words, or 'Custom (0xXXXX)' for others."""
    try:
        sw = int(sync_word)
    except (TypeError, ValueError):
        return "Custom (invalid)"
    if sw in SYNC_WORD_LABELS:
        return SYNC_WORD_LABELS[sw]
    return f"Custom (0x{sw:04X})"
