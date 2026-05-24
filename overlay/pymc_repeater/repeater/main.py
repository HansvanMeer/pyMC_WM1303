import asyncio
import functools
import logging
import os
import signal
import sys
import socket
import time

from repeater.companion.utils import validate_companion_node_name, normalize_companion_identity_key
from repeater.config import get_radio_for_board, load_config, save_config
from repeater.data_acquisition.gps_service import GPSService
from repeater.config_manager import ConfigManager
from repeater.data_acquisition.glass_handler import GlassHandler
from repeater.engine import RepeaterHandler
from repeater.handler_helpers import (
    AdvertHelper,
    DiscoveryHelper,
    LoginHelper,
    PathHelper,
    ProtocolRequestHelper,
    TextHelper,
    TraceHelper,
)
from repeater.identity_manager import IdentityManager
from repeater.packet_router import PacketRouter
from repeater.web.http_server import HTTPStatsServer, _log_buffer

# WM1303 overlay: telemetry-enabled protocol request helper (adds REQ_TYPE_GET_TELEMETRY_DATA)
try:
    from repeater.wm1303_telemetry_helper import WM1303ProtocolRequestHelper
except ImportError:
    # Fallback to upstream helper if WM1303 overlay is not present
    WM1303ProtocolRequestHelper = ProtocolRequestHelper

# WM1303 bridge (optional)
try:
    from repeater.bridge_engine import BridgeEngine  # noqa: F401
    _BRIDGE_AVAILABLE = True
except ImportError:
    _BRIDGE_AVAILABLE = False

# Uniform all-channel RX tracer (v2.4.7+)
try:
    from repeater.uniform_tracer import install_on_radio as _install_uniform_tracer
    from repeater.uniform_tracer import schedule_channel_e_wrap_retry as _uniform_tracer_retry_ch_e
    _UNIFORM_TRACER_AVAILABLE = True
except Exception:
    _install_uniform_tracer = None  # type: ignore[assignment]
    _uniform_tracer_retry_ch_e = None  # type: ignore[assignment]
    _UNIFORM_TRACER_AVAILABLE = False


# Register packet-trace callbacks with pymc_core modules so they can emit
# TX-phase trace events without importing repeater.web (layering boundary).
try:
    from repeater.web.packet_trace import trace_event as _trace_event_fn
    from pymc_core.hardware import wm1303_backend as _wm1303_backend_mod
    from pymc_core.hardware import tx_queue as _tx_queue_mod
    if hasattr(_wm1303_backend_mod, 'set_trace_callback'):
        _wm1303_backend_mod.set_trace_callback(_trace_event_fn)
    if hasattr(_tx_queue_mod, 'set_trace_callback'):
        _tx_queue_mod.set_trace_callback(_trace_event_fn)
except Exception as _trace_setup_err:
    logging.getLogger(__name__).warning(
        'Packet-trace callback registration failed: %s', _trace_setup_err)


logger = logging.getLogger("RepeaterDaemon")


class RepeaterDaemon:

    def __init__(self, config: dict, radio=None):

        self.config = config
        self.radio = radio
        self.dispatcher = None
        self.repeater_handler = None
        self.local_hash = None
        self.local_identity = None
        self.identity_manager = None
        self.config_manager = None
        self.http_server = None
        self.trace_helper = None
        self.advert_helper = None
        self.discovery_helper = None
        self.login_helper = None
        self.text_helper = None
        self.path_helper = None
        self.protocol_request_helper = None
        self.glass_handler = None
        self.acl = None
        self.router = None
        self.companion_bridges: dict[int, object] = {}
        self.companion_frame_servers: list = []
        self.bridge_engine = None
        self.gps_service = None
        self._shutdown_started = False
        self._main_task = None

        log_level = config.get("logging", {}).get("level", "INFO")
        logging.basicConfig(
            level=getattr(logging, log_level),
            format=config.get("logging", {}).get("format"),
        )

        root_logger = logging.getLogger()
        _log_buffer.setLevel(getattr(logging, log_level))
        root_logger.addHandler(_log_buffer)

    async def initialize(self):

        logger.info(f"Initializing repeater: {self.config['repeater']['node_name']}")

        #-----------------------------------------------
        # Get the actual Network IP Address 
        try:
            # This looks for the IP assigned to the default hostname
            host_name = socket.gethostname()
            # We try to get the IP associated with the hostname
            self.network_ip = socket.gethostbyname(host_name)
            
            # If that still gives 127.0.x.x, let's try a different internal method
            if self.network_ip.startswith("127."):
                s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                # We use a non-routable IP that doesn't require an actual connection
                s.connect(("10.255.255.255", 1))
                self.network_ip = s.getsockname()[0]
                s.close()
        except Exception as e:
            logger.warning(f"Could not determine network IP: {e}")
            self.network_ip = "Unknown"

        logger.info(f"System Network IP: {self.network_ip}")
        #-----------------------------------------------

        if self.radio is None:
            radio_type = self.config.get("radio_type", "sx1262")
            logger.info(f"Initializing radio hardware... (radio_type={radio_type})")
            try:
                self.radio = get_radio_for_board(self.config)

                # KISS modem: schedule RX callbacks on the event loop for thread safety
                if hasattr(self.radio, "set_event_loop"):
                    self.radio.set_event_loop(asyncio.get_running_loop())

                if hasattr(self.radio, "set_custom_cad_thresholds"):
                    # Load CAD settings from config, with defaults
                    cad_config = self.config.get("radio", {}).get("cad", {})
                    peak_threshold = cad_config.get("peak_threshold", 23)
                    min_threshold = cad_config.get("min_threshold", 11)

                    self.radio.set_custom_cad_thresholds(peak=peak_threshold, min_val=min_threshold)
                    logger.info(
                        f"CAD thresholds set from config: peak={peak_threshold}, min={min_threshold}"
                    )
                else:
                    logger.warning("Radio does not support CAD configuration")

                if hasattr(self.radio, "get_frequency"):
                    logger.info(f"Radio config - Freq: {self.radio.get_frequency():.1f}MHz")
                if hasattr(self.radio, "get_spreading_factor"):
                    logger.info(f"Radio config - SF: {self.radio.get_spreading_factor()}")
                if hasattr(self.radio, "get_bandwidth"):
                    logger.info(f"Radio config - BW: {self.radio.get_bandwidth()}kHz")
                if hasattr(self.radio, "get_coding_rate"):
                    logger.info(f"Radio config - CR: {self.radio.get_coding_rate()}")
                if hasattr(self.radio, "get_tx_power"):
                    logger.info(f"Radio config - TX Power: {self.radio.get_tx_power()}dBm")

                logger.info("Radio hardware initialized")

                # v2.4.7+: install uniform all-channel RX tracer
                # (hooks _dispatch_rx + channel_e callback so every RX emits
                # a 'received' trace for the Tracing tab, across all 5 channels,
                # with correct 1/2/3-byte hash-size detection).
                if _UNIFORM_TRACER_AVAILABLE and _install_uniform_tracer is not None:
                    try:
                        _install_uniform_tracer(self.radio)
                    except Exception as _ut_err:
                        logger.warning(f"Uniform tracer install failed: {_ut_err}")

                # v2.4.7+: register friendly channel-name map with packet_trace
                # so every trace_event() call (from bridge_engine, tx_queue,
                # uniform_tracer, etc.) displays the same UI-friendly channel
                # name (e.g. 'channel_e' -> 'EU-Narrow', 'channel_a' -> 'local-test').
                try:
                    from repeater.web.packet_trace import set_channel_name_map
                    _ch_map = getattr(self.radio, '_ch_id_to_ui_name', {}) or {}
                    if _ch_map:
                        set_channel_name_map(_ch_map)
                except Exception as _cn_err:
                    logger.debug(f"Channel-name map registration failed: {_cn_err}")
            except Exception as e:
                logger.error(f"Failed to initialize radio hardware: {e}")
                raise RuntimeError("Repeater requires real LoRa hardware") from e

        try:
            from pymc_core import LocalIdentity
            from pymc_core.node.dispatcher import Dispatcher

            self.dispatcher = Dispatcher(self.radio)
            logger.info("Dispatcher initialized")

            # Initialize Identity Manager for additional identities (e.g., room servers)
            self.identity_manager = IdentityManager(self.config)
            logger.info("Identity manager initialized")

            # Set up default repeater identity (not managed by identity manager)
            identity_key = self.config.get("repeater", {}).get("identity_key")
            if not identity_key:
                logger.error("No identity key found in configuration. Cannot init repeater.")
                raise RuntimeError("Identity key is required for repeater operation")

            local_identity = LocalIdentity(seed=identity_key)
            self.local_identity = local_identity
            self.dispatcher.local_identity = local_identity

            pubkey = local_identity.get_public_key()
            self.local_hash = pubkey[0]
            self.local_hash_bytes = bytes(pubkey[:3])

            logger.info(f"Local identity set: {local_identity.get_address_bytes().hex()}")
            local_hash_hex = f"0x{self.local_hash:02x}"
            logger.info(f"Local node hash (from identity): {local_hash_hex}")

            # v2.5.x: register local identity with the WM1303 radio backend so it
            # can do PATH-BASED echo classification (self_echo vs mesh_echo vs
            # unknown_echo) instead of the legacy time-based heuristic. This is
            # repeater-agnostic because each device has a unique pubkey.
            try:
                _path_hash_size = (self.config.get("mesh", {}).get("path_hash_mode", 0) or 0) + 1
                if hasattr(self.radio, "set_local_identity"):
                    self.radio.set_local_identity(pubkey, _path_hash_size)
            except Exception as _exc:
                logger.warning(f"Could not register local identity with radio backend: {_exc}")

            # Load additional identities from config (e.g., room servers)
            await self._load_additional_identities()

            self.dispatcher._is_own_packet = lambda pkt: False

            self.repeater_handler = RepeaterHandler(
                self.config, self.dispatcher, self.local_hash,
                local_hash_bytes=self.local_hash_bytes,
                send_advert_func=self.send_advert,
            )

            # Create router
            self.router = PacketRouter(self)
            await self.router.start()

            # Register router as entry point for ALL packets via fallback handler
            # All received packets flow through router → helpers → repeater engine
            self.dispatcher.register_fallback_handler(self._router_callback)
            logger.info("Packet router registered as fallback (catches all packets)")

            # Set default path hash mode for flood 0-hop packets (adverts, etc.)
            path_hash_mode = self.config.get("mesh", {}).get("path_hash_mode", 0)
            if path_hash_mode not in (0, 1, 2):
                logger.warning(
                    f"Invalid mesh.path_hash_mode={path_hash_mode}, must be 0/1/2; using 0"
                )
                path_hash_mode = 0
            self.dispatcher.set_default_path_hash_mode(path_hash_mode)
            mode_names = {0: "1-byte", 1: "2-byte", 2: "3-byte"}
            logger.info(
                f"Path hash mode set to {mode_names[path_hash_mode]} (mesh.path_hash_mode={path_hash_mode})"
            )

            # Create processing helpers (handlers created internally)
            self.trace_helper = TraceHelper(
                local_hash=self.local_hash,
                repeater_handler=self.repeater_handler,
                packet_injector=self.router.inject_packet,
                log_fn=logger.info,
                local_identity=self.local_identity,
            )
            logger.info("Trace processing helper initialized")

            # Create advert helper for neighbor tracking
            self.advert_helper = AdvertHelper(
                local_identity=self.local_identity,
                storage=self.repeater_handler.storage if self.repeater_handler else None,
                config=self.config,
                log_fn=logger.info,
            )
            logger.info("Advert processing helper initialized")

            # Set up discovery handler if enabled
            allow_discovery = self.config.get("repeater", {}).get("allow_discovery", True)
            if allow_discovery:
                self.discovery_helper = DiscoveryHelper(
                    local_identity=self.local_identity,
                    packet_injector=self.router.inject_packet,
                    node_type=2,
                    log_fn=logger.info,
                    debug_log_fn=logger.debug,
                )
                logger.info("Discovery processing helper initialized")
            else:
                logger.info("Discovery response handler disabled")

            # Create login helper (will create per-identity ACLs)
            # WM1303 v2.4.11: use bridge-aware _response_injector so login
            # responses go out via channel_e/f instead of classic radios[0]/[1].
            self.login_helper = LoginHelper(
                identity_manager=self.identity_manager,
                packet_injector=self._response_injector,
                log_fn=logger.info,
            )

            # Register default repeater identity
            self.login_helper.register_identity(
                name="repeater",
                identity=self.local_identity,
                identity_type="repeater",
                config=self.config,  # Pass full config so repeater can access top-level security section
            )

            # Register room server identities with their configs
            for name, identity, config in self.identity_manager.get_identities_by_type(
                "room_server"
            ):
                self.login_helper.register_identity(
                    name=name,
                    identity=identity,
                    identity_type="room_server",
                    config=config,  # Pass room-specific config
                )

            logger.info("Login processing helper initialized")

            # Initialize ConfigManager for centralized config management
            self.config_manager = ConfigManager(
                config_path=getattr(self, "config_path", "/etc/pymc_repeater/config.yaml"),
                config=self.config,
                daemon_instance=self,
            )
            logger.info("Config manager initialized")

            self.gps_service = GPSService(
                self.config,
                location_update_callback=self._update_repeater_location_from_gps,
            )
            self.gps_service.start()
            if self.config.get("gps", {}).get("enabled", False):
                logger.info("GPS diagnostics initialized")
            else:
                logger.info("GPS diagnostics disabled")

            # Initialize text message helper with per-identity ACLs
            # WM1303 v2.4.11: use bridge-aware _response_injector so CLI command
            # responses go out via channel_e/f instead of classic radios[0]/[1].
            self.text_helper = TextHelper(
                identity_manager=self.identity_manager,
                packet_injector=self._response_injector,
                acl_dict=self.login_helper.get_acl_dict(),  # Per-identity ACLs
                log_fn=logger.info,
                config_path=getattr(self, "config_path", None),  # For CLI to save changes
                config=self.config,  # For CLI to read/modify settings
                config_manager=self.config_manager,  # New centralized config manager
                sqlite_handler=(
                    self.repeater_handler.storage.sqlite_handler
                    if self.repeater_handler and self.repeater_handler.storage
                    else None
                ),  # For room server database
                send_advert_callback=self.send_advert,  # For CLI advert command
            )
            self.text_helper._loop = asyncio.get_running_loop()

            # Register default repeater identity for text messages
            self.text_helper.register_identity(
                name="repeater",
                identity=self.local_identity,
                identity_type="repeater",
                radio_config=self.config.get("radio", {}),
            )

            # Register room server identities for text messages
            for name, identity, config in self.identity_manager.get_identities_by_type(
                "room_server"
            ):
                self.text_helper.register_identity(
                    name=name,
                    identity=identity,
                    identity_type="room_server",
                    radio_config=config,  # Pass room-specific config (includes max_posts, etc.)
                )

            logger.info("Text message processing helper initialized")

            # Initialize PATH packet helper for updating client out_path
            self.path_helper = PathHelper(
                acl_dict=self.login_helper.get_acl_dict(),  # Per-identity ACLs
                log_fn=logger.info,
            )
            logger.info("PATH packet processing helper initialized")

            # Initialize protocol request handler for status/telemetry requests
            # WM1303 v2.4.11: use bridge-aware _response_injector so protocol
            # request responses go out via channel_e/f instead of classic radios.
            self.protocol_request_helper = WM1303ProtocolRequestHelper(
                identity_manager=self.identity_manager,
                packet_injector=self._response_injector,
                acl_dict=self.login_helper.get_acl_dict(),
                radio=self.radio,
                engine=self.repeater_handler,
                neighbor_tracker=self.advert_helper,
                config=self.config,
            )
            # Register repeater identity for protocol requests
            self.protocol_request_helper.register_identity(
                name="repeater", identity=self.local_identity, identity_type="repeater"
            )
            logger.info("Protocol request handler initialized")

            # Load companion identities (CompanionBridge + frame server per companion)
            await self._load_companion_identities()

            # Subscribe to raw RX in pyMC_core so we can push PUSH_CODE_LOG_RX_DATA to companion clients
            self.dispatcher.add_raw_rx_subscriber(self._on_raw_rx_for_companions)
            n = len(getattr(self, "companion_frame_servers", []))
            logger.info(
                "Raw RX subscriber registered (%s companion frame server(s)). Connect a client to see rx_log (0x88).",
                n,
            )

            # Subscribe to parsed packets (pre-dedup) so duplicate path variants
            # still appear in the web UI even though the Dispatcher blocks them.
            self.dispatcher.add_raw_packet_subscriber(self._on_raw_packet_for_dedup_logging)

            # When trace reaches final node, push PUSH_CODE_TRACE_DATA (0x89) to companion clients (firmware onTraceRecv)
            self.trace_helper.on_trace_complete = self._on_trace_complete_for_companions

            # Optional pyMC_Glass integration loop (inform/control plane)
            self.glass_handler = GlassHandler(
                config=self.config,
                daemon_instance=self,
                config_manager=self.config_manager,
            )
            await self.glass_handler.start()
            if (
                self.repeater_handler
                and self.repeater_handler.storage
                and hasattr(self.repeater_handler.storage, "set_glass_publisher")
            ):
                self.repeater_handler.storage.set_glass_publisher(self.glass_handler.publish_telemetry)

        except Exception as e:
            logger.error(f"Failed to initialize dispatcher: {e}")
            raise

    async def _load_additional_identities(self):
        from pymc_core import LocalIdentity

        identities_config = self.config.get("identities", {})

        # Load room server identities
        room_servers = identities_config.get("room_servers") or []
        for room_config in room_servers:
            try:
                name = room_config.get("name")
                identity_key = room_config.get("identity_key")

                if not name or not identity_key:
                    logger.warning(f"Skipping room server config: missing name or identity_key")
                    continue

                # Convert identity_key to bytes if it's a hex string
                if isinstance(identity_key, bytes):
                    identity_key_bytes = identity_key
                elif isinstance(identity_key, str):
                    try:
                        identity_key_bytes = bytes.fromhex(identity_key)
                        if len(identity_key_bytes) != 32:
                            logger.error(
                                f"Identity key for '{name}' is invalid length: {len(identity_key_bytes)} bytes (expected 32)"
                            )
                            continue
                    except ValueError as e:
                        logger.error(f"Identity key for '{name}' is not valid hex: {e}")
                        continue
                else:
                    logger.error(
                        f"Identity key for '{name}' has unknown type: {type(identity_key)}"
                    )
                    continue

                # Create the identity
                room_identity = LocalIdentity(seed=identity_key_bytes)

                # Register with the manager and all helpers
                success = self._register_identity_everywhere(
                    name=name,
                    identity=room_identity,
                    config=room_config,
                    identity_type="room_server",
                )

                if success:
                    room_hash = room_identity.get_public_key()[0]
                    logger.info(
                        f"Loaded room server '{name}': hash=0x{room_hash:02x}, "
                        f"address={room_identity.get_address_bytes().hex()}"
                    )

            except Exception as e:
                logger.error(f"Failed to load room server identity '{name}': {e}")

        # Summary logging
        total_identities = len(self.identity_manager.list_identities())
        logger.info(f"Identity manager loaded {total_identities} total identities")

    async def _load_companion_identities(self) -> None:
        """Load companion identities from config and create CompanionBridge + frame server for each."""
        from pymc_core import LocalIdentity
        from pymc_core.companion.models import Channel, Contact

        from repeater.companion import CompanionFrameServer, RepeaterCompanionBridge

        companions_config = self.config.get("identities", {}).get("companions") or []
        if not companions_config:
            return

        sqlite_handler = None
        if self.repeater_handler and self.repeater_handler.storage:
            sqlite_handler = self.repeater_handler.storage.sqlite_handler
        if not sqlite_handler and companions_config:
            logger.warning(
                "Companion persistence disabled: no storage (contacts/channels will not survive restart or disconnect)"
            )

        radio_config = (
            self.repeater_handler.radio_config
            if self.repeater_handler
            else self.config.get("radio", {})
        )

        for comp_config in companions_config:
            try:
                name = comp_config.get("name")
                identity_key = comp_config.get("identity_key")
                settings = comp_config.get("settings") or {}

                if not name or not identity_key:
                    logger.warning("Skipping companion config: missing name or identity_key")
                    continue

                if isinstance(identity_key, str):
                    try:
                        identity_key_bytes = bytes.fromhex(normalize_companion_identity_key(identity_key))
                    except ValueError as e:
                        logger.error(f"Companion '{name}' identity_key invalid hex: {e}")
                        continue
                elif isinstance(identity_key, bytes):
                    identity_key_bytes = identity_key
                else:
                    logger.error(f"Companion '{name}' identity_key has unknown type")
                    continue

                if len(identity_key_bytes) not in (32, 64):
                    logger.error(
                        f"Companion '{name}' identity_key must be 32 bytes (hex) or 64 bytes (MeshCore firmware key)"
                    )
                    continue

                identity = LocalIdentity(seed=identity_key_bytes)
                pubkey = identity.get_public_key()
                companion_hash = pubkey[0]
                companion_hash_str = f"0x{companion_hash:02x}"

                node_name = settings.get("node_name", name)
                tcp_port = settings.get("tcp_port", 5000)
                bind_address = settings.get("bind_address", "0.0.0.0")
                tcp_timeout_raw = settings.get("tcp_timeout", 8 * 60 * 60) # 8 hours
                client_idle_timeout_sec = None if tcp_timeout_raw == 0 else int(tcp_timeout_raw)

                def _make_sync_node_name_to_config(companion_name: str):
                    """Return a callback that syncs node_name to config for this companion (binds name at creation)."""
                    def _sync(new_node_name: str) -> None:
                        try:
                            validated = validate_companion_node_name(new_node_name)
                        except ValueError:
                            return
                        companions = (self.config.get("identities") or {}).get("companions") or []
                        for entry in companions:
                            if entry.get("name") == companion_name:
                                if "settings" not in entry:
                                    entry["settings"] = {}
                                entry["settings"]["node_name"] = validated
                                config_path = getattr(self, "config_path", None)
                                if config_path:
                                    save_config(self.config, config_path)
                                break
                    return _sync

                bridge = RepeaterCompanionBridge(
                    identity=identity,
                    packet_injector=self.router.inject_packet,
                    node_name=node_name,
                    radio_config=radio_config,
                    sqlite_handler=sqlite_handler,
                    companion_hash=companion_hash_str,
                    on_prefs_saved=_make_sync_node_name_to_config(name),
                )

                # Load contacts from SQLite
                if sqlite_handler:
                    contact_rows = sqlite_handler.companion_load_contacts(companion_hash_str)
                    if contact_rows:
                        records = []
                        for row in contact_rows:
                            d = dict(row)
                            d["public_key"] = d.pop("pubkey", d.get("public_key", b""))
                            records.append(d)
                        bridge.contacts.load_from_dicts(records)

                    # Load channels from SQLite (normalize secret to 32 bytes to match
                    # CompanionBase.set_channel and GroupTextHandler/PacketBuilder)
                    channel_rows = sqlite_handler.companion_load_channels(companion_hash_str)
                    for row in channel_rows:
                        s = row.get("secret", b"")
                        if isinstance(s, bytes):
                            raw = s
                        elif isinstance(s, (bytearray, memoryview)):
                            raw = bytes(s)
                        elif s:
                            raw = bytes.fromhex(s if isinstance(s, str) else str(s))
                        else:
                            raw = b""
                        if len(raw) < 32:
                            raw = raw + b"\x00" * (32 - len(raw))
                        elif len(raw) > 32:
                            raw = raw[:32]
                        ch = Channel(name=row.get("name", ""), secret=raw)
                        bridge.channels.set(row.get("channel_idx", 0), ch)

                    # Preload queued messages from SQLite into bridge
                    for msg_dict in sqlite_handler.companion_load_messages(companion_hash_str):
                        from pymc_core.companion.models import QueuedMessage

                        sk = msg_dict.get("sender_key", b"")
                        if isinstance(sk, str):
                            sk = bytes.fromhex(sk)
                        bridge.message_queue.push(
                            QueuedMessage(
                                sender_key=sk,
                                txt_type=msg_dict.get("txt_type", 0),
                                timestamp=msg_dict.get("timestamp", 0),
                                text=msg_dict.get("text", ""),
                                is_channel=bool(msg_dict.get("is_channel", False)),
                                channel_idx=msg_dict.get("channel_idx", 0),
                                path_len=msg_dict.get("path_len", 0),
                            )
                        )

                # Ensure public channel (0) exists with default key for new companions
                from repeater.companion.constants import DEFAULT_PUBLIC_CHANNEL_SECRET

                if bridge.get_channel(0) is None:
                    bridge.set_channel(0, "Public", DEFAULT_PUBLIC_CHANNEL_SECRET)

                self.companion_bridges[companion_hash] = bridge

                frame_server = CompanionFrameServer(
                    bridge=bridge,
                    companion_hash=companion_hash_str,
                    port=tcp_port,
                    bind_address=bind_address,
                    client_idle_timeout_sec=client_idle_timeout_sec,
                    sqlite_handler=sqlite_handler,
                    local_hash=self.local_hash,
                    stats_getter=self._get_companion_stats,
                    control_handler=(
                        self.discovery_helper.control_handler if self.discovery_helper else None
                    ),
                )
                await frame_server.start()
                self.companion_frame_servers.append(frame_server)

                self.identity_manager.register_identity(
                    name=name,
                    identity=identity,
                    config=comp_config,
                    identity_type="companion",
                )

                logger.info(
                    f"Loaded companion '{name}': hash=0x{companion_hash:02x}, "
                    f"port={tcp_port}, bind={bind_address}, client_idle_timeout_sec={client_idle_timeout_sec}"
                )

            except Exception as e:
                logger.error(f"Failed to load companion '{name}': {e}", exc_info=True)

    async def add_companion_from_config(self, comp_config: dict) -> None:
        """
        Load a single companion from config and register it (hot-reload).
        Creates RepeaterCompanionBridge, CompanionFrameServer, starts the server,
        and registers with identity_manager. Raises on error.
        """
        from pymc_core import LocalIdentity
        from pymc_core.companion.models import Channel

        from repeater.companion import CompanionFrameServer, RepeaterCompanionBridge
        from repeater.companion.constants import DEFAULT_PUBLIC_CHANNEL_SECRET

        name = comp_config.get("name")
        identity_key = comp_config.get("identity_key")
        settings = comp_config.get("settings") or {}

        if not name or not identity_key:
            raise ValueError("Companion config missing name or identity_key")

        if isinstance(identity_key, str):
            try:
                identity_key_bytes = bytes.fromhex(normalize_companion_identity_key(identity_key))
            except ValueError as e:
                raise ValueError(f"Companion '{name}' identity_key invalid hex: {e}") from e
        elif isinstance(identity_key, bytes):
            identity_key_bytes = identity_key
        else:
            raise ValueError(f"Companion '{name}' identity_key has unknown type")

        if len(identity_key_bytes) not in (32, 64):
            raise ValueError(
                f"Companion '{name}' identity_key must be 32 bytes (hex) or 64 bytes (MeshCore firmware key)"
            )

        # Already registered?
        if name in self.identity_manager.named_identities:
            raise ValueError(f"Companion '{name}' is already registered")

        identity = LocalIdentity(seed=identity_key_bytes)
        pubkey = identity.get_public_key()
        companion_hash = pubkey[0]
        companion_hash_str = f"0x{companion_hash:02x}"

        if companion_hash in self.companion_bridges:
            raise ValueError(f"Companion with hash 0x{companion_hash:02x} already loaded")

        sqlite_handler = None
        if self.repeater_handler and self.repeater_handler.storage:
            sqlite_handler = self.repeater_handler.storage.sqlite_handler

        radio_config = (
            self.repeater_handler.radio_config
            if self.repeater_handler
            else self.config.get("radio", {})
        )

        node_name = settings.get("node_name", name)
        tcp_port = settings.get("tcp_port", 5000)
        bind_address = settings.get("bind_address", "0.0.0.0")
        tcp_timeout_raw = settings.get("tcp_timeout", 120)
        client_idle_timeout_sec = None if tcp_timeout_raw == 0 else int(tcp_timeout_raw)

        bridge = RepeaterCompanionBridge(
            identity=identity,
            packet_injector=self.router.inject_packet,
            node_name=node_name,
            radio_config=radio_config,
            sqlite_handler=sqlite_handler,
            companion_hash=companion_hash_str,
        )

        if sqlite_handler:
            contact_rows = sqlite_handler.companion_load_contacts(companion_hash_str)
            if contact_rows:
                records = []
                for row in contact_rows:
                    d = dict(row)
                    d["public_key"] = d.pop("pubkey", d.get("public_key", b""))
                    records.append(d)
                bridge.contacts.load_from_dicts(records)

            channel_rows = sqlite_handler.companion_load_channels(companion_hash_str)
            for row in channel_rows:
                s = row.get("secret", b"")
                if isinstance(s, bytes):
                    raw = s
                elif isinstance(s, (bytearray, memoryview)):
                    raw = bytes(s)
                elif s:
                    raw = bytes.fromhex(s if isinstance(s, str) else str(s))
                else:
                    raw = b""
                if len(raw) < 32:
                    raw = raw + b"\x00" * (32 - len(raw))
                elif len(raw) > 32:
                    raw = raw[:32]
                ch = Channel(name=row.get("name", ""), secret=raw)
                bridge.channels.set(row.get("channel_idx", 0), ch)

            for msg_dict in sqlite_handler.companion_load_messages(companion_hash_str):
                from pymc_core.companion.models import QueuedMessage

                sk = msg_dict.get("sender_key", b"")
                if isinstance(sk, str):
                    sk = bytes.fromhex(sk)
                bridge.message_queue.push(
                    QueuedMessage(
                        sender_key=sk,
                        txt_type=msg_dict.get("txt_type", 0),
                        timestamp=msg_dict.get("timestamp", 0),
                        text=msg_dict.get("text", ""),
                        is_channel=bool(msg_dict.get("is_channel", False)),
                        channel_idx=msg_dict.get("channel_idx", 0),
                        path_len=msg_dict.get("path_len", 0),
                    )
                )

        if bridge.get_channel(0) is None:
            bridge.set_channel(0, "Public", DEFAULT_PUBLIC_CHANNEL_SECRET)

        self.companion_bridges[companion_hash] = bridge

        frame_server = CompanionFrameServer(
            bridge=bridge,
            companion_hash=companion_hash_str,
            port=tcp_port,
            bind_address=bind_address,
            client_idle_timeout_sec=client_idle_timeout_sec,
            sqlite_handler=sqlite_handler,
            local_hash=self.local_hash,
            stats_getter=self._get_companion_stats,
            control_handler=(
                self.discovery_helper.control_handler if self.discovery_helper else None
            ),
        )
        await frame_server.start()
        self.companion_frame_servers.append(frame_server)

        self.identity_manager.register_identity(
            name=name,
            identity=identity,
            config=comp_config,
            identity_type="companion",
        )

        logger.info(
            f"Hot-reload: Loaded companion '{name}': hash=0x{companion_hash:02x}, "
            f"port={tcp_port}, bind={bind_address}, client_idle_timeout_sec={client_idle_timeout_sec}"
        )

    async def _on_raw_rx_for_companions(self, data: bytes, rssi: int, snr: float) -> None:
        """Raw RX subscriber: push PUSH_CODE_LOG_RX_DATA (0x88) to connected companion clients."""
        servers = getattr(self, "companion_frame_servers", [])
        if not servers:
            return
        for fs in servers:
            try:
                fs.push_rx_raw(snr, rssi, data)
            except Exception as e:
                logger.debug("Push RX raw to companion: %s", e)

    def _on_raw_packet_for_dedup_logging(self, pkt, data: bytes, analysis: dict) -> None:
        """Record duplicate packets for UI visibility.

        Called by Dispatcher's raw_packet_subscriber (pre-dedup) so we see
        all path variants.  Only records packets the engine has already seen;
        novel packets are left for the normal handler path.
        """
        if not self.repeater_handler:
            return
        if not self.repeater_handler.is_duplicate(pkt):
            return  # First variant — will reach engine via normal handler path
        rssi = getattr(pkt, "_rssi", 0) or 0
        snr = getattr(pkt, "_snr", 0.0) or 0.0
        self.repeater_handler.record_duplicate(pkt, rssi=rssi, snr=snr)

    async def deliver_control_data(
        self,
        snr: float,
        rssi: int,
        path_len: int,
        path_bytes: bytes,
        payload_bytes: bytes,
    ) -> None:
        """Deliver CONTROL payload (e.g. discovery response) to companion clients (PUSH_CODE_CONTROL_DATA 0x8E)."""
        # Only push discovery responses (0x90); client expects these, not the request (0x80)
        if len(payload_bytes) < 6 or (payload_bytes[0] & 0xF0) != 0x90:
            return
        # Push every discovery response to the client, including our own (snr=0, rssi=0 = local node's response)
        servers = getattr(self, "companion_frame_servers", [])
        if not servers:
            return
        tag = int.from_bytes(payload_bytes[2:6], "little") if len(payload_bytes) >= 6 else 0
        logger.debug(
            "Delivering discovery response to %s companion(s): tag=0x%08X, len=%s",
            len(servers),
            tag,
            len(payload_bytes),
        )
        for fs in servers:
            try:
                await fs.push_control_data(snr, rssi, path_len, path_bytes, payload_bytes)
            except Exception as e:
                logger.warning("Companion push_control_data error: %s", e)

    async def _on_trace_complete_for_companions(self, packet, parsed_data) -> None:
        """Trace completed at this node: push PUSH_CODE_TRACE_DATA (0x89) to companion clients (firmware onTraceRecv)."""
        path_hashes = parsed_data.get("trace_path_bytes") or b""
        if not path_hashes:
            return
        flags = parsed_data.get("flags", 0)
        path_sz = flags & 0x03
        hash_len = len(path_hashes)
        expected_snr_len = hash_len >> path_sz
        if expected_snr_len <= 0:
            return
        tag = parsed_data.get("tag", 0)
        auth_code = parsed_data.get("auth_code", 0)
        snr_scaled = max(-128, min(127, int(round(packet.get_snr() * 4))))
        snr_byte = snr_scaled if snr_scaled >= 0 else (256 + snr_scaled)
        # Firmware: memcpy path_snrs from pkt->path (length hash_len >> path_sz), then final SNR byte
        raw = bytes(packet.path)[:expected_snr_len]
        if len(raw) < expected_snr_len:
            raw = raw + b"\x00" * (expected_snr_len - len(raw))
        path_snrs = raw
        for fs in getattr(self, "companion_frame_servers", []):
            try:
                await fs.push_trace_data_async(
                    hash_len, flags, tag, auth_code, path_hashes, path_snrs, snr_byte
                )
            except Exception as e:
                logger.debug("Push trace data to companion: %s", e)

    def _register_identity_everywhere(
        self, name: str, identity, config: dict, identity_type: str
    ) -> bool:
        """
        Register an identity with the manager and all helpers in one place.
        This is the single source of truth for identity registration.
        """
        # Register with identity manager
        success = self.identity_manager.register_identity(
            name=name, identity=identity, config=config, identity_type=identity_type
        )

        if not success:
            return False

        # Register with all helpers
        if self.login_helper:
            self.login_helper.register_identity(
                name=name, identity=identity, identity_type=identity_type, config=config
            )

        if self.text_helper:
            self.text_helper.register_identity(
                name=name,
                identity=identity,
                identity_type=identity_type,
                radio_config=self.config.get("radio", {}),
            )

        if self.protocol_request_helper:
            self.protocol_request_helper.register_identity(
                name=name, identity=identity, identity_type=identity_type
            )

        return True

    async def _router_callback(self, packet):
        """
        Single entry point for ALL packets.
        Enqueues packets for router processing.
        """
        if self.router:
            try:
                await self.router.enqueue(packet)
            except Exception as e:
                logger.error(f"Error enqueuing packet in router: {e}", exc_info=True)

    def register_text_handler_for_identity(
        self, name: str, identity, identity_type: str = "room_server", radio_config: dict = None
    ):

        if not self.text_helper:
            logger.warning("Text helper not initialized, cannot register identity")
            return False

        try:
            self.text_helper.register_identity(
                name=name,
                identity=identity,
                identity_type=identity_type,
                radio_config=radio_config or self.config.get("radio", {}),
            )
            logger.info(f"Registered text handler for {identity_type} '{name}'")
            return True
        except Exception as e:
            logger.error(f"Failed to register text handler for '{name}': {e}")
            return False

    async def _response_injector(self, packet, wait_for_ack: bool = False):
        """WM1303 v2.4.11: bridge-aware packet injector for helper responses.

        Used as the `packet_injector` for LoginHelper, TextHelper, and
        ProtocolRequestHelper so that responses (login success, CLI replies,
        status responses) go out via channel_e/f instead of the upstream
        PacketRouter (which targets classic radios[0]/[1] that are not active
        in a WM1303 setup).

        Why not per-request override?
          LoginHelper sends responses via asyncio.create_task(_delayed_send(...))
          which completes AFTER process_login_packet() returns. A per-request
          override-then-restore pattern would restore the original injector
          before the async task fires, causing the response to be routed via
          the wrong injector. A permanent bridge-aware injector avoids this.

        Args:
            packet: Packet object to send (helper-generated response).
            wait_for_ack: Ignored in bridge path; bridge fire-and-forget.
        """
        try:
            packet_bytes = packet.write_to()
        except Exception as e:
            logger.warning(
                "_response_injector: serialize failed: %s (falling back to router)",
                e,
            )
            packet_bytes = None

        if packet_bytes is not None and self.bridge_engine is not None:
            try:
                await self.bridge_engine.inject_packet('repeater', packet_bytes)
                return
            except Exception as e:
                logger.warning(
                    "_response_injector: bridge inject failed: %s (falling back to router)",
                    e,
                )

        # Fallback: classic router (used if bridge_engine is unavailable, e.g.
        # during a setup where only classic radios are active).
        if self.router is not None:
            try:
                await self.router.inject_packet(packet, wait_for_ack=wait_for_ack)
            except Exception as e:
                logger.error("_response_injector: router fallback failed: %s", e)
        else:
            logger.error(
                "_response_injector: no injector available (bridge_engine and router both None)"
            )

    async def _bridge_repeater_handler(self, data: bytes,
                                        origin_channel: str | None = None,
                                        rssi: float | None = None,
                                        snr: float | None = None) -> None:
        """Bridge-to-repeater handler: parse raw bytes, run repeater logic, re-inject result.

        Called by BridgeEngine when a rule forwards to 'repeater' endpoint.
        Parses raw bytes into a Packet, runs process_packet() for path/hop
        modification, then re-injects the forwarded packet back into the
        bridge engine as 'repeater' source so downstream rules (repeater -> channels)
        can fire and TX on all configured destination channels.

        Args:
            data: Raw packet bytes from the bridge engine.
            origin_channel: Optional channel_id where the packet was originally
                received (e.g. 'channel_a'). Passed through to inject_packet
                so the TX serializer can prioritize the origin channel.
            rssi: Optional RSSI value (dBm) from the receiving radio.
            snr: Optional SNR value (dB) from the receiving radio.

        NOTE: Do NOT call mark_seen() before process_packet()! The
        flood_forward/direct_forward methods inside process_packet check
        is_duplicate() first and call mark_seen() themselves after the check
        passes.  Calling mark_seen early causes ALL packets to be silently
        dropped as duplicates.
        """
        from pymc_core.protocol.packet import Packet
        from pymc_core.node.handlers.advert import AdvertHandler
        from pymc_core.node.handlers.trace import TraceHandler
        from pymc_core.node.handlers.login_server import LoginServerHandler
        from pymc_core.node.handlers.text import TextMessageHandler
        from pymc_core.node.handlers.protocol_request import ProtocolRequestHandler

        # Parse raw bytes into Packet object
        pkt = Packet()
        if not pkt.read_from(data):
            logger.warning("BridgeRepeaterHandler: failed to parse %d bytes", len(data))
            return

        # Attach RSSI/SNR from bridge metadata to the parsed Packet so
        # downstream handlers (advert processing, neighbor tracking) can
        # access signal quality even though it's not in the wire format.
        if rssi is not None:
            pkt._rssi = int(rssi)
        if snr is not None:
            pkt._snr = float(snr)

        logger.info(
            "BridgeRepeaterHandler: parsed %d bytes, header=0x%02x, origin_channel=%s, rssi=%s, snr=%s",
            len(data), pkt.header, origin_channel, rssi, snr
        )

        # Process ADVERT packets for neighbor tracking
        payload_type = pkt.get_payload_type() if hasattr(pkt, 'get_payload_type') else None
        if payload_type == AdvertHandler.payload_type() and self.advert_helper:
            try:
                _rssi = int(rssi) if rssi is not None else 0
                _snr = float(snr) if snr is not None else 0.0
                await self.advert_helper.process_advert_packet(pkt, _rssi, _snr)
                logger.info("BridgeRepeaterHandler: processed ADVERT for neighbor tracking (rssi=%s, snr=%s)",
                            _rssi, _snr)
            except Exception as e:
                logger.warning("BridgeRepeaterHandler: advert processing error: %s", e)

        # WM1303 v2.4.11: dispatch TRACE packets to TraceHelper for proper
        # response generation. The upstream pymc_repeater packet_router.py
        # routes TRACE via TraceHelper.process_trace_packet(), but our bridge
        # bypasses that router entirely. Without this dispatch, TRACE pings
        # to the repeater are silently forwarded as flood broadcasts and
        # never produce a TRACE response, so companion node pings time out.
        #
        # We temporarily override the TraceHelper packet_injector so that the
        # forwarded TRACE (with our SNR appended) goes through the bridge
        # engine TX path (channel_e/f), not via the router (which only knows
        # about classic radios[0]/[1]).
        if payload_type == TraceHandler.payload_type() and self.trace_helper:
            _saved_injector = self.trace_helper.packet_injector

            async def _bridge_trace_injector(fwd_packet, wait_for_ack=False):
                try:
                    fwd_bytes = fwd_packet.write_to()
                except Exception as e:
                    logger.warning("BridgeRepeaterHandler: trace forward serialize failed: %s", e)
                    return
                if self.bridge_engine:
                    await self.bridge_engine.inject_packet(
                        'repeater', fwd_bytes, origin_channel=origin_channel
                    )

            self.trace_helper.packet_injector = _bridge_trace_injector
            try:
                await self.trace_helper.process_trace_packet(pkt)
                logger.info(
                    "BridgeRepeaterHandler: TRACE dispatched to TraceHelper "
                    "(origin_channel=%s, rssi=%s, snr=%s)",
                    origin_channel, rssi, snr
                )
            except Exception as e:
                logger.warning("BridgeRepeaterHandler: trace processing error: %s", e)
            finally:
                self.trace_helper.packet_injector = _saved_injector
            return

        # WM1303 v2.4.11: dispatch ANON_REQ / TXT_MSG / PROTOCOL_REQ packets to
        # their respective helpers so remote admin from a companion node works
        # over channel_e/f, just like over classic radios[0]/[1] via the upstream
        # packet_router. Without this dispatch, login/text/protocol-request
        # packets are silently forwarded as floods and the helpers never run, so
        # companion login + CLI commands time out.
        #
        # The helpers are initialised with self._response_injector as their
        # packet_injector (see helper init in _initialise_repeater_helpers),
        # which routes any response through the BridgeEngine. So we just need
        # to call process_*_packet(pkt). If handled=True we return (skip the
        # generic forward). If handled=False we fall through so the packet is
        # still re-broadcast for mesh propagation.

        # ANON_REQ -> LoginHelper (companion login / authentication)
        if payload_type == LoginServerHandler.payload_type() and self.login_helper:
            try:
                handled = await self.login_helper.process_login_packet(pkt)
                logger.info(
                    "BridgeRepeaterHandler: ANON_REQ dispatched to LoginHelper "
                    "(handled=%s, origin_channel=%s)",
                    handled, origin_channel
                )
                if handled:
                    return
            except Exception as e:
                logger.warning("BridgeRepeaterHandler: login processing error: %s", e)

        # TXT_MSG -> TextHelper (admin CLI commands like reboot, neighbors, ...)
        if payload_type == TextMessageHandler.payload_type() and self.text_helper:
            try:
                handled = await self.text_helper.process_text_packet(pkt)
                logger.info(
                    "BridgeRepeaterHandler: TXT_MSG dispatched to TextHelper "
                    "(handled=%s, origin_channel=%s)",
                    handled, origin_channel
                )
                if handled:
                    return
            except Exception as e:
                logger.warning("BridgeRepeaterHandler: text processing error: %s", e)

        # PROTOCOL_REQ -> ProtocolRequestHelper (status queries, etc.)
        if (payload_type == ProtocolRequestHandler.payload_type()
                and self.protocol_request_helper):
            try:
                handled = await self.protocol_request_helper.process_request_packet(pkt)
                logger.info(
                    "BridgeRepeaterHandler: PROTOCOL_REQ dispatched to "
                    "ProtocolRequestHelper (handled=%s, origin_channel=%s)",
                    handled, origin_channel
                )
                if handled:
                    return
            except Exception as e:
                logger.warning(
                    "BridgeRepeaterHandler: protocol_request processing error: %s", e
                )

        # Run repeater forwarding logic
        result = self.repeater_handler.process_packet(pkt)
        if result is None:
            drop_reason = getattr(pkt, 'drop_reason', 'unknown')
            logger.info(
                "BridgeRepeaterHandler: process_packet returned None "
                "(drop_reason=%s)", drop_reason
            )
            return

        fwd_pkt, delay = result
        fwd_bytes = fwd_pkt.write_to()

        logger.info(
            "BridgeRepeaterHandler: forwarding %d -> %d bytes (delay=%.3fs, origin_channel=%s)",
            len(data), len(fwd_bytes), delay, origin_channel
        )

        # Re-inject into bridge engine as 'repeater' source
        if self.bridge_engine:
            await self.bridge_engine.inject_packet('repeater', fwd_bytes,
                                                   origin_channel=origin_channel)

    def _init_wm1303_bridge(self):
        """Initialize the WM1303 bridge engine with dual-channel radios."""
        from pymc_core.hardware import WM1303Backend

        cfg_bridge = self.config.get("bridge", {})

        # Load rules from SSOT (wm1303_ui.json)
        rules = self._load_bridge_rules_from_ui()
        if not rules:
            rules_yaml = cfg_bridge.get("bridge_rules", [])
            if rules_yaml:
                logger.info("No SSOT bridge rules -- falling back to config.yaml bridge_rules")
                rules = rules_yaml
            else:
                logger.info("No bridge rules in SSOT or config.yaml -- skipping bridge init")
                return

        # Get radios from WM1303Backend
        backend = None
        if hasattr(self, "radio") and isinstance(self.radio, WM1303Backend):
            backend = self.radio
        else:
            logger.warning("Radio is not WM1303Backend -- bridge not available")
            return

        radios = backend.get_radios()
        if len(radios) < 1:
            # Channel E / F operate independently of channels A-D.
            # If at least one of them is enabled we must still start the
            # BridgeEngine so their RX callbacks get registered.
            import json as _json_br
            _has_ef = False
            try:
                with open('/etc/pymc_repeater/wm1303_ui.json') as _uif_br:
                    _ui_br = _json_br.load(_uif_br)
                _has_ef = (bool(_ui_br.get('channel_e', {}).get('enabled'))
                           or bool(_ui_br.get('channel_f', {}).get('enabled')))
            except Exception:
                pass
            if not _has_ef:
                logger.warning(f"Need >= 1 radio for bridge, got {len(radios)}")
                return
            logger.info(
                'WM1303: no A-D channels active but channel_e/f enabled '
                '— proceeding with bridge init (radios=%d)', len(radios))

        dedup_ttl = cfg_bridge.get("dedup_ttl_seconds",
                                    cfg_bridge.get("dedup_ttl", 300.0))

        from repeater.bridge_engine import BridgeEngine
        self.bridge_engine = BridgeEngine(
            radios=radios,
            rules=rules,
            dedup_ttl=dedup_ttl,
        )

        # Register repeater handler for bridge->repeater forwarding
        if self.repeater_handler:
            if hasattr(self.repeater_handler, "process_packet"):
                self.bridge_engine.set_repeater_handler(
                    self._bridge_repeater_handler
                )
                logger.info("WM1303: repeater handler registered with bridge engine")
                self.bridge_engine.set_repeater_engine(self.repeater_handler)
                logger.info("WM1303: repeater engine reference set with bridge engine")
            else:
                logger.warning(
                    "WM1303: repeater_handler exists but has no process_packet method! "
                    "Available methods: %s",
                    [m for m in dir(self.repeater_handler) if not m.startswith('_')]
                )
        else:
            logger.warning(
                "WM1303: repeater_handler is None at bridge init time! "
                "Will attempt re-registration after startup."
            )

        logger.info(
            f"BridgeEngine initialized: {len(radios)} radios, {len(rules)} rules, "
            f"dedup_ttl={dedup_ttl}s"
        )

        # Connect SQLite handler for dedup event persistence
        _sh = None
        if self.repeater_handler and self.repeater_handler.storage:
            _sh = self.repeater_handler.storage.sqlite_handler
        if _sh:
            self.bridge_engine.set_sqlite_handler(_sh)
            logger.info("WM1303: SQLite handler connected to BridgeEngine for dedup persistence")
        else:
            logger.warning("WM1303: No SQLite handler available for BridgeEngine dedup persistence")

    @staticmethod
    def _load_bridge_rules_from_ui() -> list:
        """Load bridge rules from SSOT: /etc/pymc_repeater/wm1303_ui.json."""
        import json
        ui_path = "/etc/pymc_repeater/wm1303_ui.json"
        try:
            with open(ui_path) as f:
                ui = json.load(f)
            raw_rules = ui.get("bridge", {}).get("rules", [])
            rules = []
            for r in raw_rules:
                rule = dict(r)
                if "from" in rule:
                    rule["source"] = rule.pop("from")
                if "to" in rule:
                    rule["target"] = rule.pop("to")
                rules.append(rule)
            logger.info(f"SSOT: loaded {len(rules)} bridge rules from {ui_path}")
            return rules
        except FileNotFoundError:
            logger.warning(f"SSOT file not found: {ui_path}")
            return []
        except Exception as e:
            logger.error(f"Failed to load bridge rules from SSOT: {e}")
            return []

    def reload_bridge_rules(self) -> bool:
        """Hot-reload bridge rules from SSOT without restarting."""
        if not self.bridge_engine:
            logger.warning("Cannot reload bridge rules: bridge_engine is None")
            return False
        try:
            rules = self._load_bridge_rules_from_ui()
            self.bridge_engine.update_rules(rules)
            logger.info(f"Bridge rules hot-reloaded: {len(rules)} rules")
            return True
        except Exception as e:
            logger.error(f"Failed to hot-reload bridge rules: {e}")
            return False

    def get_stats(self) -> dict:
        stats = {}

        if self.repeater_handler:
            stats = self.repeater_handler.get_stats()
            # Add public key if available
            if self.local_identity:
                try:
                    pubkey = self.local_identity.get_public_key()
                    stats["public_key"] = pubkey.hex()
                except Exception:
                    stats["public_key"] = None


        # Add WM1303 bridge stats
        if getattr(self, "bridge_engine", None):
            try:
                bstats = self.bridge_engine.get_stats()
                stats["bridge"] = bstats
                stats["bridge_forwarded"] = bstats.get("forwarded_packets", 0)
                stats["bridge_dropped_duplicate"] = bstats.get("dropped_duplicate", 0)
            except Exception as e:
                logger.debug(f"Failed to get bridge stats: {e}")

        if self.gps_service:
            stats["gps"] = self.gps_service.get_summary()


        return stats

    async def _get_companion_stats(self, stats_type: int) -> dict:
        """Return stats dict for companion CMD_GET_STATS (format expected by frame_server + meshcore_py)."""
        from repeater.companion.constants import (
            STATS_TYPE_CORE,
            STATS_TYPE_PACKETS,
            STATS_TYPE_RADIO,
        )

        if not self.repeater_handler:
            return {}
        engine = self.repeater_handler
        airtime = engine.airtime_mgr.get_stats()
        uptime_secs = int(time.time() - engine.start_time)
        queue_len = 0
        for bridge in getattr(self, "companion_bridges", {}).values():
            queue_len += getattr(getattr(bridge, "message_queue", None), "count", 0) or 0
        if stats_type == STATS_TYPE_CORE:
            return {
                "battery_mv": 0,
                "uptime_secs": uptime_secs,
                "errors": 0,
                "queue_len": min(255, queue_len),
            }
        if stats_type == STATS_TYPE_RADIO:
            noise_floor = int(engine.get_cached_noise_floor() or 0)
            radio = getattr(self, "dispatcher", None) and getattr(self.dispatcher, "radio", None)
            if radio:
                _r = getattr(radio, "get_last_rssi", lambda: 0)
                _s = getattr(radio, "get_last_snr", lambda: 0.0)
                last_rssi = _r() if callable(_r) else _r
                last_snr = _s() if callable(_s) else _s
            else:
                last_rssi, last_snr = 0, 0.0
            tx_air_secs = int(airtime.get("total_airtime_ms", 0) / 1000)
            return {
                "noise_floor": noise_floor,
                "last_rssi": int(last_rssi) if last_rssi is not None else 0,
                "last_snr": float(last_snr) if last_snr is not None else 0.0,
                "tx_air_secs": tx_air_secs,
                "rx_air_secs": 0,
            }
        if stats_type == STATS_TYPE_PACKETS:
            return {
                "recv": getattr(engine, "rx_count", 0),
                "sent": getattr(engine, "forwarded_count", 0),
                "flood_tx": getattr(engine, "forwarded_count", 0),
                "direct_tx": 0,
                "flood_rx": getattr(engine, "rx_count", 0),
                "direct_rx": 0,
                "recv_errors": getattr(engine, "dropped_count", 0),
            }
        return {}

    async def send_advert(self) -> bool:

        if not self.dispatcher or not self.local_identity:
            logger.error("Cannot send advert: dispatcher or identity not initialized")
            return False

        mode = self.config.get("repeater", {}).get("mode", "forward")
        if mode == "no_tx":
            logger.debug("Adverts disabled in no_tx mode")
            return False

        try:
            from pymc_core.protocol import PacketBuilder
            from pymc_core.protocol.constants import ADVERT_FLAG_HAS_NAME, ADVERT_FLAG_IS_REPEATER

            # Get node name and location from config
            repeater_config = self.config.get("repeater", {})
            node_name = repeater_config.get("node_name", "Repeater")
            latitude = repeater_config.get("latitude", 0.0)
            longitude = repeater_config.get("longitude", 0.0)
            location_source = "config"

            if self.gps_service:
                location = self.gps_service.get_repeater_location()
                latitude = location.get("latitude", latitude)
                longitude = location.get("longitude", longitude)
                location_source = str(location.get("source", location_source))


            flags = ADVERT_FLAG_IS_REPEATER | ADVERT_FLAG_HAS_NAME

            packet = PacketBuilder.create_advert(
                local_identity=self.local_identity,
                name=node_name,
                lat=latitude,
                lon=longitude,
                feature1=0,
                feature2=0,
                flags=flags,
                route_type="flood",
            )

            # Route through bridge engine (rules determine which channels get advert)
            if self.bridge_engine:
                raw_bytes = packet.write_to()
                await self.bridge_engine.inject_packet("repeater", raw_bytes)
                logger.info("Advert injected into bridge engine as repeater source")
            else:
                # Fallback: direct send if no bridge engine
                await self.dispatcher.send_packet(packet, wait_for_ack=False)

            # Mark our own advert as seen to prevent re-forwarding it
            if self.repeater_handler:
                self.repeater_handler.mark_seen(packet)
                logger.debug("Marked own advert as seen in duplicate cache")

            logger.info(f"Sent flood advert '{node_name}' at ({latitude: .6f}, {longitude: .6f})")
            return True

        except Exception as e:
            logger.error(f"Failed to send advert: {e}", exc_info=True)
            return False

    def _signal_shutdown(self, sig, loop):
        """Handle SIGTERM/SIGINT by setting the stop event for cooperative exit."""
        if self._shutdown_started:
            logger.info(f"Received signal {sig.name}, shutdown already in progress")
            return
        logger.info(f"Received signal {sig.name}, shutting down...")
        self._stop_event.set()

    def _update_repeater_location_from_gps(self, location: dict) -> bool:
        """Persist the latest valid GPS fix as the repeater's advertised location."""
        latitude = location.get("latitude")
        longitude = location.get("longitude")
        if latitude is None or longitude is None:
            return False

        repeater_config = self.config.setdefault("repeater", {})
        current_latitude = repeater_config.get("latitude")
        current_longitude = repeater_config.get("longitude")
        try:
            if (
                current_latitude is not None
                and current_longitude is not None
                and abs(float(current_latitude) - float(latitude)) < 0.000001
                and abs(float(current_longitude) - float(longitude)) < 0.000001
            ):
                return False
        except (TypeError, ValueError):
            pass

        updates = {
            "repeater": {
                "latitude": float(latitude),
                "longitude": float(longitude),
            }
        }
        if self.config_manager:
            result = self.config_manager.update_and_save(
                updates=updates,
                live_update=True,
                live_update_sections=["repeater"],
            )
            if not result.get("success"):
                logger.warning(
                    "GPS location fix could not update repeater config: %s",
                    result.get("error", "unknown error"),
                )
                return False
        else:
            repeater_config.update(updates["repeater"])

        logger.info(
            "Updated repeater location from GPS fix: latitude=%.6f longitude=%.6f",
            latitude,
            longitude,
        )
        return True

    async def _shutdown(self):
        """Best-effort shutdown: stop background services and release hardware."""
        if self._shutdown_started:
            return
        self._shutdown_started = True

        # Stop WM1303 bridge engine
        if getattr(self, "bridge_engine", None):
            try:
                self.bridge_engine.stop()
                logger.info("Bridge engine stopped")
            except Exception as e:
                logger.warning(f"Error stopping bridge engine: {e}")

        # Stop GPS diagnostics.
        if self.gps_service:
            try:
                self.gps_service.stop()
            except Exception as e:
                logger.warning(f"Error stopping GPS diagnostics: {e}")



        # Stop companion frame servers first to close client sockets and child workers.
        for frame_server in getattr(self, "companion_frame_servers", []):
            try:
                await frame_server.stop()
            except Exception as e:
                logger.warning(f"Companion frame server stop error: {e}")

        # Stop companion bridges to flush/persist state.
        if hasattr(self, "companion_bridges"):
            for bridge in self.companion_bridges.values():
                if hasattr(bridge, "stop"):
                    try:
                        await bridge.stop()
                    except Exception as e:
                        logger.warning(f"Companion bridge stop error: {e}")

        # Stop router
        if self.router:
            try:
                await self.router.stop()
            except Exception as e:
                logger.warning(f"Error stopping router: {e}")

        # Stop HTTP server
        if self.http_server:
            try:
                await asyncio.wait_for(asyncio.to_thread(self.http_server.stop), timeout=3)
            except asyncio.TimeoutError:
                logger.warning("Timeout stopping HTTP server")
            except Exception as e:
                logger.warning(f"Error stopping HTTP server: {e}")

        # Stop Glass inform loop
        if self.glass_handler:
            try:
                await self.glass_handler.stop()
            except Exception as e:
                logger.warning(f"Error stopping Glass handler: {e}")

        # Close storage publishers (MQTT/LetsMesh) to stop their worker threads.
        try:
            if self.repeater_handler and self.repeater_handler.storage:
                await asyncio.wait_for(
                    asyncio.to_thread(self.repeater_handler.storage.close), timeout=5
                )
        except asyncio.TimeoutError:
            logger.warning("Timeout closing storage publishers")
        except Exception as e:
            logger.warning(f"Error closing storage: {e}")

        # Release radio resources
        if self.radio and hasattr(self.radio, "cleanup"):
            try:
                self.radio.cleanup()
            except Exception as e:
                logger.warning(f"Error cleaning up radio: {e}")

        # Release CH341 USB device if in use
        try:
            if self.config.get("radio_type", "sx1262").lower() == "sx1262_ch341":
                from pymc_core.hardware.ch341.ch341_async import CH341Async

                CH341Async.reset_instance()
        except Exception as e:
            logger.debug(f"CH341 reset skipped/failed: {e}")

        # Do not force-stop the event loop here; asyncio.run() owns loop lifecycle.

    @staticmethod
    def _detect_container() -> bool:
        """Detect if running inside an LXC/Docker/systemd-nspawn container."""
        try:
            with open("/proc/1/environ", "rb") as f:
                if b"container=" in f.read():
                    return True
        except (OSError, PermissionError):
            pass
        return os.path.exists("/run/host/container-manager")

    async def run(self):

        logger.info("Repeater daemon started")
        self._main_task = asyncio.current_task()

        # Shutdown event — set by signal handler to unblock the main wait
        self._stop_event = asyncio.Event()

        # Register signal handlers for graceful shutdown
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(
                sig,
                functools.partial(self._signal_shutdown, sig, loop),
            )

        # Warn if running inside a container (udev rules won't work here)
        if os.path.exists("/.dockerenv") or os.environ.get("container") or self._detect_container():
            logger.warning(
                "Container environment detected. "
                "USB device udev rules must be configured on the HOST, not inside this container."
            )

        try:
            await self.initialize()

            # --- WM1303 Bridge Engine ---
            try:
                self._init_wm1303_bridge()
            except Exception as e:
                logger.warning(f"WM1303 Bridge init failed (non-fatal): {e}")
                self.bridge_engine = None

            # --- WM1303 TX Queue Scheduler (CRITICAL for TX) ---
            try:
                from pymc_core.hardware import WM1303Backend as _WM1303BE
                if hasattr(self, "radio") and isinstance(self.radio, _WM1303BE):
                    # Fix virtual radio event loops (they need the async loop)
                    _loop = asyncio.get_running_loop()
                    for _vr in self.radio.virtual_radios.values():
                        _vr._loop = _loop
                        logger.info("Set event loop on VirtualLoRaRadio[%s]", _vr.channel_id)
                    # Start GlobalTXScheduler (sends PULL_RESP to pkt_fwd)
                    if hasattr(self.radio, "ensure_tx_queues_started"):
                        await self.radio.ensure_tx_queues_started()
                        logger.info("WM1303: GlobalTXScheduler started (TX via PULL_RESP)")
            except Exception as e:
                logger.warning("WM1303 TX scheduler init failed (non-fatal): %s", e)

            # --- Start BridgeEngine RX loops (CRITICAL for cross-channel forwarding) ---
            if self.bridge_engine:
                asyncio.create_task(self.bridge_engine.run())
                logger.info("BridgeEngine: RX loops started (cross-channel forwarding active)")

                # --- Channel E (native LoRa) ---
                try:
                    from repeater.channel_e_bridge import ChannelEBridge
                    self._channel_e = ChannelEBridge(self.bridge_engine, backend=self.radio)
                    asyncio.create_task(self._channel_e.run())
                    logger.info("Channel E (native LoRa): bridge listener started")
                    # v2.4.7+: schedule background retry to wrap channel_e RX
                    # callback. ChannelEBridge installs its handler inside its
                    # async run() task, which may not have completed yet; retry
                    # for up to ~20 s until the callback appears.
                    if _UNIFORM_TRACER_AVAILABLE and _uniform_tracer_retry_ch_e is not None:
                        try:
                            _uniform_tracer_retry_ch_e(self.radio)
                        except Exception as _we:
                            logger.debug(f"Uniform tracer channel_e retry failed: {_we}")

                    # v2.4.7+: re-register the friendly channel-name map now
                    # that ChannelEBridge has populated channel_e in
                    # _ch_id_to_ui_name. Without this second call, bridge_engine
                    # traces (which emit `channel='channel_e'`) would not be
                    # rewritten to 'EU-Narrow' because the initial registration
                    # ran before channel_e was added.
                    try:
                        from repeater.web.packet_trace import set_channel_name_map
                        _ch_map2 = getattr(self.radio, '_ch_id_to_ui_name', {}) or {}
                        # Ensure channel_e is present even if the radio hasn't
                        # registered it yet — use the UI config fallback.
                        if 'channel_e' not in _ch_map2:
                            try:
                                import json
                                with open('/etc/pymc_repeater/wm1303_ui.json', 'r') as _uif:
                                    _uicfg = json.load(_uif)
                                _che = _uicfg.get('channel_e', {}) or {}
                                _friendly = _che.get('friendly_name') or _che.get('name')
                                if _friendly:
                                    _ch_map2 = dict(_ch_map2)
                                    _ch_map2['channel_e'] = _friendly
                            except Exception:
                                pass
                        if _ch_map2:
                            set_channel_name_map(_ch_map2)
                    except Exception as _cn_err:
                        logger.debug(f"Channel-name map re-registration failed: {_cn_err}")
                except Exception as e:
                    logger.warning("Channel E init failed: %s", e)

                # --- Channel F (chan_Lora_std on SX1302 RF0) ---
                # Channel F shares the HAL pkt_fwd UDP port with channels A-D
                # (no separate UDP listener), so the bridge plugin only registers
                # a TX handler + RX callback. The backend dispatches matching
                # chan_Lora_std packets via _channel_f_rx_callback.
                try:
                    from repeater.channel_f_bridge import ChannelFBridge
                    self._channel_f = ChannelFBridge(self.bridge_engine, backend=self.radio)
                    asyncio.create_task(self._channel_f.run())
                    logger.info("Channel F (chan_Lora_std on RF0): bridge handler started")
                    # Re-register friendly channel-name map so 'channel_f' resolves
                    # to its UI-configured friendly_name in trace events.
                    try:
                        from repeater.web.packet_trace import set_channel_name_map
                        _ch_map3 = getattr(self.radio, '_ch_id_to_ui_name', {}) or {}
                        if 'channel_f' not in _ch_map3:
                            try:
                                import json
                                with open('/etc/pymc_repeater/wm1303_ui.json', 'r') as _uif:
                                    _uicfg = json.load(_uif)
                                _chf = _uicfg.get('channel_f', {}) or {}
                                _friendly = _chf.get('friendly_name') or _chf.get('name')
                                if _friendly:
                                    _ch_map3 = dict(_ch_map3)
                                    _ch_map3['channel_f'] = _friendly
                            except Exception:
                                pass
                        if _ch_map3:
                            set_channel_name_map(_ch_map3)
                    except Exception as _cnf_err:
                        logger.debug(f"Channel F name map registration failed: {_cnf_err}")
                except Exception as e:
                    logger.warning("Channel F init failed: %s", e)

            # Safety-net: re-register repeater handler if not yet registered
            if (self.bridge_engine and self.repeater_handler
                    and not getattr(self.bridge_engine, '_repeater_handler', None)):
                if hasattr(self.repeater_handler, "process_packet"):
                    self.bridge_engine.set_repeater_handler(
                        self._bridge_repeater_handler
                    )
                    logger.info("WM1303: repeater handler registered (safety-net, post-startup)")
                    self.bridge_engine.set_repeater_engine(self.repeater_handler)
                    logger.info("WM1303: repeater engine reference set (safety-net, post-startup)")
                else:
                    logger.warning("WM1303: safety-net registration failed - no process_packet")

            # --- FIX: Unregister Dispatcher direct RX callback ---
            # Bridge engine handles ALL RX via VirtualLoRaRadio queues.
            if hasattr(self, "radio") and hasattr(self.radio, "set_rx_callback"):
                self.radio.set_rx_callback(None)
                logger.info("Unregistered Dispatcher direct RX callback (bridge engine handles RX)")

            # Start HTTP stats server
            http_port = self.config.get("http", {}).get("port", 8000)
            http_host = self.config.get("http", {}).get("host", "0.0.0.0")

            node_name = self.config.get("repeater", {}).get("node_name", "Repeater")

            # Format public key for display
            pub_key_formatted = ""
            if self.local_identity:
                pub_key_hex = self.local_identity.get_public_key().hex()
                # Format as <first8...last8>
                if len(pub_key_hex) >= 16:
                    pub_key_formatted = f"{pub_key_hex[:8]}...{pub_key_hex[-8:]}"
                else:
                    pub_key_formatted = pub_key_hex

            current_loop = asyncio.get_event_loop()

            self.http_server = HTTPStatsServer(
                host=http_host,
                port=http_port,
                stats_getter=self.get_stats,
                node_name=node_name,
                pub_key=pub_key_formatted,
                send_advert_func=self.send_advert,
                config=self.config,
                event_loop=current_loop,
                daemon_instance=self,
                config_path=getattr(self, "config_path", "/etc/pymc_repeater/config.yaml"),
            )

            try:
                self.http_server.start()
            except Exception as e:
                logger.error(f"Failed to start HTTP server: {e}")

            # Centralized metrics retention (8-day default, hourly cleanup, weekly VACUUM)
            try:
                from repeater.metrics_retention import start as _start_retention
                _start_retention()
            except Exception as _e:
                logger.warning(f"metrics_retention start failed: {_e}")

            # Keep the daemon alive until a shutdown signal is received.
            # The dispatcher RX/TX processing happens via callbacks and
            # background tasks; we just need to block here until SIGTERM.
            logger.info("Repeater daemon running (waiting for shutdown signal)")
            await self._stop_event.wait()
            logger.info("Shutdown signal received, cleaning up...")

            # Best-effort companion cleanup
            for frame_server in getattr(self, "companion_frame_servers", []):
                try:
                    await frame_server.stop()
                except Exception as e:
                    logger.debug(f"Companion frame server stop: {e}")
            if hasattr(self, "companion_bridges"):
                for bridge in self.companion_bridges.values():
                    if hasattr(bridge, "stop"):
                        try:
                            await bridge.stop()
                        except Exception as e:
                            logger.debug(f"Companion bridge stop: {e}")
        finally:
            await self._shutdown()


def main():

    import argparse

    parser = argparse.ArgumentParser(description="pyMC Repeater Daemon")
    parser.add_argument(
        "--config",
        help="Path to config file (default: /etc/pymc_repeater/config.yaml)",
    )
    parser.add_argument(
        "--log-level",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Log level (default: INFO)",
    )

    args = parser.parse_args()

    # Load configuration
    config = load_config(args.config)
    config_path = args.config if args.config else "/etc/pymc_repeater/config.yaml"

    if args.log_level:
        if "logging" not in config:
            config["logging"] = {}
        config["logging"]["level"] = args.log_level

    # Don't initialize radio here - it will be done inside the async event loop
    daemon = RepeaterDaemon(config, radio=None)
    daemon.config_path = config_path

    # Run
    try:
        asyncio.run(daemon.run())
    except KeyboardInterrupt:
        logger.info("Repeater stopped")
    except asyncio.CancelledError:
        # Expected when _shutdown() cancels all tasks for clean exit
        logger.info("Repeater stopped (clean shutdown)")
    except RuntimeError as e:
        # 'Event loop stopped before Future completed' may still occur
        if "Event loop stopped" in str(e) or "cannot schedule" in str(e).lower():
            logger.info("Repeater stopped (clean shutdown)")
        else:
            logger.error(f"Fatal error: {e}", exc_info=True)
            sys.exit(1)
    except Exception as e:
        logger.error(f"Fatal error: {e}", exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
