"""
Hardware abstraction layer for PyMC_Core

This overlay extends upstream openhop_core hardware exports with WM1303-specific
backends while preserving upstream USB/TCP LoRa radio support.
"""

from .base import LoRaRadio

# Conditional import for WsRadio (requires websockets)
try:
    from .wsradio import WsRadio

    _WS_AVAILABLE = True
except ImportError:
    _WS_AVAILABLE = False
    WsRadio = None

# Conditional import for SX1262Radio (requires spidev)
try:
    from .sx1262_wrapper import SX1262Radio

    _SX1262_AVAILABLE = True
except ImportError:
    _SX1262_AVAILABLE = False
    SX1262Radio = None

# Conditional import for KissSerialWrapper (requires pyserial)
try:
    from .kiss_serial_wrapper import KissSerialWrapper

    _KISS_SERIAL_AVAILABLE = True
except ImportError:
    _KISS_SERIAL_AVAILABLE = False
    KissSerialWrapper = None

# Conditional import for KissModemWrapper (requires pyserial)
try:
    from .kiss_modem_wrapper import KissModemWrapper

    _KISS_MODEM_AVAILABLE = True
except ImportError:
    _KISS_MODEM_AVAILABLE = False
    KissModemWrapper = None

# Conditional import for USBLoRaRadio (requires pyserial) [upstream 1.0.11+]
try:
    from .usb_radio import USBLoRaRadio

    _USB_AVAILABLE = True
except ImportError:
    _USB_AVAILABLE = False
    USBLoRaRadio = None

# Conditional import for TCPLoRaRadio (stdlib only) [upstream 1.0.11+]
try:
    from .tcp_radio import TCPLoRaRadio

    _TCP_AVAILABLE = True
except ImportError:
    _TCP_AVAILABLE = False
    TCPLoRaRadio = None

# Conditional import for WM1303Backend [pyMC_WM1303 overlay]
try:
    from .wm1303_backend import WM1303Backend

    _WM1303_AVAILABLE = True
except ImportError:
    _WM1303_AVAILABLE = False
    WM1303Backend = None

# Conditional import for VirtualLoRaRadio [pyMC_WM1303 overlay]
try:
    from .virtual_radio import VirtualLoRaRadio

    _VIRTUAL_AVAILABLE = True
except ImportError:
    _VIRTUAL_AVAILABLE = False
    VirtualLoRaRadio = None


__all__ = ["LoRaRadio"]

# Add WsRadio to exports if available
if _WS_AVAILABLE:
    __all__.append("WsRadio")

# Add SX1262Radio to exports if available
if _SX1262_AVAILABLE:
    __all__.append("SX1262Radio")

# Add KissSerialWrapper to exports if available
if _KISS_SERIAL_AVAILABLE:
    __all__.append("KissSerialWrapper")

# Add KissModemWrapper to exports if available
if _KISS_MODEM_AVAILABLE:
    __all__.append("KissModemWrapper")

# Add USBLoRaRadio to exports if available [upstream]
if _USB_AVAILABLE:
    __all__.append("USBLoRaRadio")

# Add TCPLoRaRadio to exports if available [upstream]
if _TCP_AVAILABLE:
    __all__.append("TCPLoRaRadio")

# Add WM1303Backend to exports if available [overlay]
if _WM1303_AVAILABLE:
    __all__.append("WM1303Backend")

# Add VirtualLoRaRadio to exports if available [overlay]
if _VIRTUAL_AVAILABLE:
    __all__.append("VirtualLoRaRadio")
