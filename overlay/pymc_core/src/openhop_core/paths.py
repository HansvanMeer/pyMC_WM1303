"""Central config-path resolution for WM1303 / OpenHop overlay files.

Historically the on-device config directory was ``/etc/pymc_repeater/``. From
v2.6.0 the canonical location is ``/etc/openhop_repeater/`` (see the OpenHop
upstream migration in ``install.sh`` / ``upgrade.sh``). Devices upgraded from
v2.5.x still have both dirs on disk; truly fresh installs only have the
OpenHop dir.

This module is the single point of truth for resolving config-file paths in
the WM1303 overlay. All overlay code (both ``openhop_core`` hardware helpers
and ``repeater`` daemon modules) should call :func:`resolve_config_path`
instead of hardcoding either path. The helper prefers the OpenHop dir but
transparently falls back to the legacy dir when the requested file only
exists there.

Historical context
------------------
Prior to v2.7, the overlay hardcoded ``/etc/pymc_repeater/...`` in 68 places
across 12 Python files. The v2.6.2 hotfix worked around this by dual-writing
the ``VERSION`` file to both dirs from the install/upgrade scripts. v2.7
replaces the hardcoded paths with :func:`resolve_config_path` calls so the
code is single-sourced against the OpenHop dir while remaining backwards
compatible with devices that still have the legacy dir.

Once every device in the field has been rebuilt cleanly (no legacy dir on
disk), the fallback branch can be removed and the dual-write shim in
``install.sh`` / ``upgrade.sh`` can be deleted.
"""
from __future__ import annotations

from pathlib import Path
from typing import Union

__all__ = [
    "CONFIG_DIR",
    "LEGACY_CONFIG_DIR",
    "resolve_config_path",
    "config_dir",
    "legacy_config_dir",
]

#: Canonical (OpenHop) config directory. Used for all future writes.
CONFIG_DIR: Path = Path("/etc/openhop_repeater")

#: Legacy config directory (pre-v2.6.0). Kept as a read-fallback only.
LEGACY_CONFIG_DIR: Path = Path("/etc/pymc_repeater")


def resolve_config_path(name: Union[str, Path]) -> Path:
    """Return the best available path for a config file named ``name``.

    Resolution order:

    1. ``/etc/openhop_repeater/<name>`` if that file exists (canonical location).
    2. ``/etc/pymc_repeater/<name>`` if only the legacy file exists
       (backwards compatibility for devices upgraded from v2.5.x).
    3. ``/etc/openhop_repeater/<name>`` as the default when neither exists
       (future writes always go to the canonical location).

    The returned :class:`~pathlib.Path` is safe for both reads and writes.
    Callers that write the file are creating it in the canonical location,
    which is the desired long-term behaviour.

    Parameters
    ----------
    name : str or pathlib.Path
        Basename of the config file (e.g. ``"wm1303_ui.json"``,
        ``"config.yaml"``, ``"version"``). Sub-paths are supported.

    Returns
    -------
    pathlib.Path
        Resolved absolute path to the config file.

    Examples
    --------
    >>> resolve_config_path("wm1303_ui.json")  # doctest: +SKIP
    PosixPath('/etc/openhop_repeater/wm1303_ui.json')

    >>> resolve_config_path("version").read_text().strip()  # doctest: +SKIP
    '2.6.2'
    """
    rel = Path(name)
    openhop_path = CONFIG_DIR / rel
    if openhop_path.exists():
        return openhop_path
    legacy_path = LEGACY_CONFIG_DIR / rel
    if legacy_path.exists():
        return legacy_path
    # Neither exists: default to the canonical location so future writes land
    # in /etc/openhop_repeater/ rather than perpetuating the legacy layout.
    return openhop_path


def config_dir() -> Path:
    """Return the canonical config directory (``/etc/openhop_repeater``).

    Prefer this over hardcoding the path.
    """
    return CONFIG_DIR


def legacy_config_dir() -> Path:
    """Return the legacy config directory (``/etc/pymc_repeater``).

    Only used by the install/upgrade scripts for the dual-write shim.
    Application code should not touch the legacy dir directly.
    """
    return LEGACY_CONFIG_DIR
