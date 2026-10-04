from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any
from uuid import UUID

from aioesphomeapi.api_pb2 import (
    BluetoothConnectionsFreeResponse,
    BluetoothDeviceClearCacheResponse,
    BluetoothDeviceConnectionResponse,
    BluetoothDevicePairingResponse,
    BluetoothDeviceRequest,
    BluetoothDeviceUnpairingResponse,
)
from aioesphomeapi.api_pb2 import (
    BluetoothGATTCharacteristic as BluetoothGATTCharacteristicProto,
)  # type: ignore
from aioesphomeapi.api_pb2 import (
    BluetoothGATTDescriptor as BluetoothGATTDescriptorProto,
)
from aioesphomeapi.api_pb2 import (
    BluetoothGATTErrorResponse,
    BluetoothGATTGetServicesDoneResponse,
    BluetoothGATTGetServicesRequest,
    BluetoothGATTGetServicesResponse,
    BluetoothGATTNotifyDataResponse,
    BluetoothGATTNotifyRequest,
    BluetoothGATTNotifyResponse,
    BluetoothGATTReadDescriptorRequest,
    BluetoothGATTReadRequest,
    BluetoothGATTReadResponse,
)
from aioesphomeapi.api_pb2 import BluetoothGATTService as BluetoothGATTServiceProto
from aioesphomeapi.api_pb2 import (
    BluetoothGATTWriteDescriptorRequest,
    BluetoothGATTWriteRequest,
    BluetoothGATTWriteResponse,
    BluetoothLEAdvertisementResponse,
    BluetoothLERawAdvertisement,
    BluetoothLERawAdvertisementsResponse,
    BluetoothScannerSetModeRequest,
    BluetoothScannerStateResponse,
    BluetoothServiceData,
    BluetoothSetConnectionParamsRequest,
    BluetoothSetConnectionParamsResponse,
    SubscribeBluetoothConnectionsFreeRequest,
    SubscribeBluetoothLEAdvertisementsRequest,
    UnsubscribeBluetoothLEAdvertisementsRequest,
)
from aioesphomeapi.model import (
    BluetoothDeviceRequestType,
    BluetoothProxyFeature,
    BluetoothProxySubscriptionFlag,
    BluetoothScannerMode,
    BluetoothScannerState,
)

if TYPE_CHECKING:
    from google.protobuf.message import Message

    from aioesphomeserver.device import Device
    from aioesphomeserver.native_api_server import NativeApiConnection


logger = logging.getLogger(__name__)

GATT_NOT_CONNECTED = -1
GATT_REQUEST_NOT_SUPPORTED = 6
GATT_ERROR = 133
BLUETOOTH_MAC_RE = re.compile(r"^[0-9A-F]{2}(?::[0-9A-F]{2}){5}$")


@dataclass(frozen=True, slots=True)
class BluetoothAdvertisement:
    """Library-neutral Bluetooth LE advertisement."""

    address: int
    rssi: int
    address_type: int = 0
    name: str = ""
    service_uuids: Sequence[str] = ()
    service_data: dict[str, bytes] = field(default_factory=dict)
    manufacturer_data: dict[int, bytes] = field(default_factory=dict)
    raw_data: bytes | None = None


@dataclass(frozen=True, slots=True)
class BluetoothGATTDescriptor:
    uuid: str
    handle: int


@dataclass(frozen=True, slots=True)
class BluetoothGATTCharacteristic:
    uuid: str
    handle: int
    properties: int = 0
    descriptors: Sequence[BluetoothGATTDescriptor] = ()


@dataclass(frozen=True, slots=True)
class BluetoothGATTService:
    uuid: str
    handle: int
    characteristics: Sequence[BluetoothGATTCharacteristic] = ()


class BluetoothProxyError(Exception):
    """An operation error that should be returned over the native API."""

    def __init__(self, error: int = GATT_ERROR, message: str = "") -> None:
        super().__init__(message or f"Bluetooth operation failed with error {error}")
        self.error = error


class BluetoothProxy:
    """Native API Bluetooth proxy with an overridable Bluetooth backend.

    Subclasses implement the Bluetooth operations below. This class owns API
    subscriptions and translates backend values to and from protobuf messages.
    """

    DEFAULT_FEATURE_FLAGS = int(
        BluetoothProxyFeature.PASSIVE_SCAN
        | BluetoothProxyFeature.RAW_ADVERTISEMENTS
        | BluetoothProxyFeature.FEATURE_STATE_AND_MODE
    )

    def __init__(
        self,
        *,
        bluetooth_mac_address: str = "",
        max_connections: int = 3,
        feature_flags: int = DEFAULT_FEATURE_FLAGS,
        active_scan: bool = False,
    ) -> None:
        if max_connections < 0:
            raise ValueError("max_connections must be non-negative")
        normalized_mac = bluetooth_mac_address.replace("-", ":").upper()
        if normalized_mac and not BLUETOOTH_MAC_RE.fullmatch(normalized_mac):
            raise ValueError(
                "bluetooth_mac_address must use the AA:BB:CC:DD:EE:FF format"
            )
        self.bluetooth_mac_address = normalized_mac
        self.max_connections = max_connections
        self.feature_flags = int(feature_flags)
        self.device: Device | None = None
        self._configured_scan_mode = (
            BluetoothScannerMode.ACTIVE if active_scan else BluetoothScannerMode.PASSIVE
        )
        self._scan_mode = self._configured_scan_mode
        self._scanner_running = False
        self._advertisement_clients: dict[NativeApiConnection, int] = {}
        self._connections_clients: set[NativeApiConnection] = set()
        self._connection_owners: dict[int, NativeApiConnection] = {}
        self._notification_owners: dict[
            tuple[int, int], NativeApiConnection
        ] = {}

    # Backend interface -------------------------------------------------
    async def start_scan(self, active: bool) -> None:
        """Start scanning. Call ``publish_advertisement`` for every result."""
        raise BluetoothProxyError(GATT_REQUEST_NOT_SUPPORTED, "scanning is unsupported")

    def _set_actual_scan_mode(self, active: bool) -> None:
        """Record a backend fallback so scanner state reports the real mode."""
        self._scan_mode = (
            BluetoothScannerMode.ACTIVE if active else BluetoothScannerMode.PASSIVE
        )

    async def stop_scan(self) -> None:
        """Stop scanning."""

    async def connect(self, address: int, address_type: int, use_cache: bool) -> int:
        """Connect and return the negotiated MTU."""
        raise BluetoothProxyError(GATT_REQUEST_NOT_SUPPORTED)

    async def disconnect(self, address: int) -> None:
        raise BluetoothProxyError(GATT_NOT_CONNECTED)

    async def pair(self, address: int) -> bool:
        raise BluetoothProxyError(GATT_REQUEST_NOT_SUPPORTED)

    async def unpair(self, address: int) -> None:
        raise BluetoothProxyError(GATT_REQUEST_NOT_SUPPORTED)

    async def clear_cache(self, address: int) -> None:
        raise BluetoothProxyError(GATT_REQUEST_NOT_SUPPORTED)

    async def get_services(self, address: int) -> Sequence[BluetoothGATTService]:
        raise BluetoothProxyError(GATT_NOT_CONNECTED)

    async def read_characteristic(self, address: int, handle: int) -> bytes:
        raise BluetoothProxyError(GATT_NOT_CONNECTED)

    async def write_characteristic(
        self, address: int, handle: int, data: bytes, response: bool
    ) -> None:
        raise BluetoothProxyError(GATT_NOT_CONNECTED)

    async def read_descriptor(self, address: int, handle: int) -> bytes:
        raise BluetoothProxyError(GATT_NOT_CONNECTED)

    async def write_descriptor(self, address: int, handle: int, data: bytes) -> None:
        raise BluetoothProxyError(GATT_NOT_CONNECTED)

    async def set_notify(
        self,
        address: int,
        handle: int,
        enable: bool,
        callback: Callable[[bytes], Awaitable[None]],
    ) -> None:
        raise BluetoothProxyError(GATT_NOT_CONNECTED)

    async def set_connection_params(
        self,
        address: int,
        min_interval: int,
        max_interval: int,
        latency: int,
        timeout: int,
    ) -> None:
        raise BluetoothProxyError(GATT_REQUEST_NOT_SUPPORTED)

    # Native API translation -------------------------------------------
    async def handle_api_message(
        self, client: NativeApiConnection, message: Message
    ) -> bool:
        handlers: dict[type[Message], Callable[..., Awaitable[None]]] = {
            SubscribeBluetoothLEAdvertisementsRequest: self._subscribe_advertisements,
            UnsubscribeBluetoothLEAdvertisementsRequest: self._unsubscribe_advertisements,
            SubscribeBluetoothConnectionsFreeRequest: self._subscribe_connections_free,
            BluetoothScannerSetModeRequest: self._set_scanner_mode,
            BluetoothDeviceRequest: self._device_request,
            BluetoothGATTGetServicesRequest: self._gatt_get_services,
            BluetoothGATTReadRequest: self._gatt_read_characteristic,
            BluetoothGATTWriteRequest: self._gatt_write_characteristic,
            BluetoothGATTReadDescriptorRequest: self._gatt_read_descriptor,
            BluetoothGATTWriteDescriptorRequest: self._gatt_write_descriptor,
            BluetoothGATTNotifyRequest: self._gatt_notify,
            BluetoothSetConnectionParamsRequest: self._set_connection_params,
        }
        handler = handlers.get(type(message))
        if handler is None:
            return False
        await handler(client, message)
        return True

    async def on_api_client_disconnected(self, client: NativeApiConnection) -> None:
        self._advertisement_clients.pop(client, None)
        self._connections_clients.discard(client)
        addresses = [
            address
            for address, owner in self._connection_owners.items()
            if owner is client
        ]
        for address in addresses:
            try:
                await self.disconnect(address)
            except Exception:
                logger.debug(
                    "Bluetooth disconnect during API cleanup failed", exc_info=True
                )
            self._connection_owners.pop(address, None)
        notification_keys = [
            key for key, owner in self._notification_owners.items() if owner is client
        ]
        for address, handle in notification_keys:
            try:
                await self.set_notify(address, handle, False, _discard_notification)
            except Exception:
                logger.debug("Bluetooth notify cleanup failed", exc_info=True)
            self._notification_owners.pop((address, handle), None)
        if not self._advertisement_clients and self._scanner_running:
            await self._stop_scanner()

    async def publish_advertisement(
        self, advertisement: BluetoothAdvertisement
    ) -> None:
        """Publish one backend advertisement to all subscribed API clients."""
        for client, flags in tuple(self._advertisement_clients.items()):
            if flags & int(BluetoothProxySubscriptionFlag.RAW_ADVERTISEMENTS):
                raw_data = advertisement.raw_data
                if raw_data is None:
                    raw_data = _encode_advertisement_data(advertisement)
                response = BluetoothLERawAdvertisementsResponse(
                    advertisements=[
                        BluetoothLERawAdvertisement(
                            address=advertisement.address,
                            rssi=advertisement.rssi,
                            address_type=advertisement.address_type,
                            data=raw_data[:62],
                        )
                    ]
                )
            else:
                response = BluetoothLEAdvertisementResponse(
                    address=advertisement.address,
                    name=advertisement.name.encode("utf-8"),
                    rssi=advertisement.rssi,
                    service_uuids=list(advertisement.service_uuids),
                    service_data=[
                        BluetoothServiceData(uuid=uuid, data=data)
                        for uuid, data in advertisement.service_data.items()
                    ],
                    manufacturer_data=[
                        BluetoothServiceData(uuid=f"{company_id:04x}", data=data)
                        for company_id, data in advertisement.manufacturer_data.items()
                    ],
                    address_type=advertisement.address_type,
                )
            await client.write_message(response)

    async def publish_disconnect(self, address: int, error: int = 0) -> None:
        """Report an unsolicited backend disconnection to the owning client."""
        client = self._connection_owners.pop(address, None)
        if client is not None:
            await client.write_message(
                BluetoothDeviceConnectionResponse(
                    address=address, connected=False, error=error
                )
            )
        await self._send_connections_free()

    async def _subscribe_advertisements(
        self,
        client: NativeApiConnection,
        message: SubscribeBluetoothLEAdvertisementsRequest,
    ) -> None:
        self._advertisement_clients[client] = message.flags
        if not self._scanner_running:
            await self._start_scanner()
        else:
            await self._send_scanner_state(client, BluetoothScannerState.RUNNING)

    async def _unsubscribe_advertisements(
        self,
        client: NativeApiConnection,
        _message: UnsubscribeBluetoothLEAdvertisementsRequest,
    ) -> None:
        self._advertisement_clients.pop(client, None)
        if not self._advertisement_clients and self._scanner_running:
            await self._stop_scanner()

    async def _start_scanner(self) -> None:
        await self._broadcast_scanner_state(BluetoothScannerState.STARTING)
        try:
            await self.start_scan(self._scan_mode == BluetoothScannerMode.ACTIVE)
        except Exception:
            logger.exception("Bluetooth scanner failed to start")
            self._scanner_running = False
            await self._broadcast_scanner_state(BluetoothScannerState.FAILED)
            return
        self._scanner_running = True
        await self._broadcast_scanner_state(BluetoothScannerState.RUNNING)

    async def _stop_scanner(self) -> None:
        try:
            await self.stop_scan()
        finally:
            self._scanner_running = False

    async def _set_scanner_mode(
        self, client: NativeApiConnection, message: BluetoothScannerSetModeRequest
    ) -> None:
        self._scan_mode = BluetoothScannerMode(message.mode)
        if self._scanner_running:
            await self._stop_scanner()
            await self._start_scanner()
        else:
            await self._send_scanner_state(client, BluetoothScannerState.IDLE)

    async def _send_scanner_state(
        self, client: NativeApiConnection, state: BluetoothScannerState
    ) -> None:
        await client.write_message(
            BluetoothScannerStateResponse(
                state=int(state),
                mode=int(self._scan_mode),
                configured_mode=int(self._configured_scan_mode),
            )
        )

    async def _broadcast_scanner_state(self, state: BluetoothScannerState) -> None:
        for client in tuple(self._advertisement_clients):
            await self._send_scanner_state(client, state)

    async def _subscribe_connections_free(
        self,
        client: NativeApiConnection,
        _message: SubscribeBluetoothConnectionsFreeRequest,
    ) -> None:
        self._connections_clients.add(client)
        await client.write_message(self._connections_free_response())

    def _connections_free_response(self) -> BluetoothConnectionsFreeResponse:
        allocated = list(self._connection_owners)
        return BluetoothConnectionsFreeResponse(
            free=max(0, self.max_connections - len(allocated)),
            limit=self.max_connections,
            allocated=allocated,
        )

    async def _send_connections_free(self) -> None:
        response = self._connections_free_response()
        for client in tuple(self._connections_clients):
            await client.write_message(response)

    async def _device_request(
        self, client: NativeApiConnection, message: BluetoothDeviceRequest
    ) -> None:
        request_type = BluetoothDeviceRequestType(message.request_type)
        if request_type in (
            BluetoothDeviceRequestType.CONNECT_V3_WITH_CACHE,
            BluetoothDeviceRequestType.CONNECT_V3_WITHOUT_CACHE,
        ):
            if message.address in self._connection_owners:
                # The address is already connected, or a connection attempt is
                # in flight; connecting again would leak the earlier handle.
                await client.write_message(
                    BluetoothDeviceConnectionResponse(
                        address=message.address, connected=False, error=GATT_ERROR
                    )
                )
                return
            if len(self._connection_owners) >= self.max_connections:
                await client.write_message(
                    BluetoothDeviceConnectionResponse(
                        address=message.address, connected=False, error=GATT_ERROR
                    )
                )
                return
            # Reserve the slot before awaiting the backend so that concurrent
            # requests cannot oversubscribe max_connections.
            self._connection_owners[message.address] = client
            await self._send_connections_free()
            try:
                address_type = (
                    message.address_type if message.has_address_type else 0
                )
                mtu = await self.connect(
                    message.address,
                    address_type,
                    request_type == BluetoothDeviceRequestType.CONNECT_V3_WITH_CACHE,
                )
            except BluetoothProxyError as error:
                self._connection_owners.pop(message.address, None)
                await client.write_message(
                    BluetoothDeviceConnectionResponse(
                        address=message.address, connected=False, error=error.error
                    )
                )
                await self._send_connections_free()
                return
            except Exception:
                self._connection_owners.pop(message.address, None)
                logger.exception("Bluetooth connection failed")
                await client.write_message(
                    BluetoothDeviceConnectionResponse(
                        address=message.address, connected=False, error=GATT_ERROR
                    )
                )
                await self._send_connections_free()
                return
            except BaseException:
                # A cancelled request must not keep the reserved slot.
                self._connection_owners.pop(message.address, None)
                raise
            # The backend connection is live now; keep the slot reserved.
            self._connection_owners[message.address] = client
            await client.write_message(
                BluetoothDeviceConnectionResponse(
                    address=message.address, connected=True, mtu=mtu, error=0
                )
            )
            await self._send_connections_free()
            return

        if request_type == BluetoothDeviceRequestType.DISCONNECT:
            disconnect_error = 0
            try:
                await self.disconnect(message.address)
            except BluetoothProxyError as exc:
                disconnect_error = exc.error
            except Exception:
                logger.exception("Bluetooth disconnect failed")
                disconnect_error = GATT_ERROR
            self._connection_owners.pop(message.address, None)
            await client.write_message(
                BluetoothDeviceConnectionResponse(
                    address=message.address, connected=False, error=disconnect_error
                )
            )
            await self._send_connections_free()
            return

        if request_type == BluetoothDeviceRequestType.PAIR:
            try:
                paired = await self.pair(message.address)
                pairing_error = 0
            except BluetoothProxyError as exc:
                paired, pairing_error = False, exc.error
            except Exception:
                logger.exception("Bluetooth pairing failed")
                paired, pairing_error = False, GATT_ERROR
            await client.write_message(
                BluetoothDevicePairingResponse(
                    address=message.address, paired=paired, error=pairing_error
                )
            )
            return

        if request_type == BluetoothDeviceRequestType.UNPAIR:
            success, unpair_error = await self._run_simple_operation(
                self.unpair, message.address
            )
            await client.write_message(
                BluetoothDeviceUnpairingResponse(
                    address=message.address, success=success, error=unpair_error
                )
            )
            return

        if request_type == BluetoothDeviceRequestType.CLEAR_CACHE:
            success, cache_error = await self._run_simple_operation(
                self.clear_cache, message.address
            )
            await client.write_message(
                BluetoothDeviceClearCacheResponse(
                    address=message.address, success=success, error=cache_error
                )
            )
            return

        await client.write_message(
            BluetoothDeviceConnectionResponse(
                address=message.address,
                connected=False,
                error=GATT_REQUEST_NOT_SUPPORTED,
            )
        )

    async def _run_simple_operation(
        self, operation: Callable[..., Awaitable[Any]], *args: Any
    ) -> tuple[bool, int]:
        try:
            await operation(*args)
            return True, 0
        except BluetoothProxyError as error:
            return False, error.error
        except Exception:
            logger.exception("Bluetooth operation failed")
            return False, GATT_ERROR

    async def _gatt_get_services(
        self, client: NativeApiConnection, message: BluetoothGATTGetServicesRequest
    ) -> None:
        try:
            services = await self.get_services(message.address)
            response = BluetoothGATTGetServicesResponse(address=message.address)
            response.services.extend(_service_to_proto(service) for service in services)
            await client.write_message(response)
            await client.write_message(
                BluetoothGATTGetServicesDoneResponse(address=message.address)
            )
        except BluetoothProxyError as error:
            await self._send_gatt_error(client, message.address, 0, error.error)
        except Exception:
            logger.exception("Bluetooth GATT service discovery failed")
            await self._send_gatt_error(client, message.address, 0, GATT_ERROR)

    async def _gatt_read_characteristic(
        self, client: NativeApiConnection, message: BluetoothGATTReadRequest
    ) -> None:
        await self._gatt_read(client, message, self.read_characteristic)

    async def _gatt_read_descriptor(
        self, client: NativeApiConnection, message: BluetoothGATTReadDescriptorRequest
    ) -> None:
        await self._gatt_read(client, message, self.read_descriptor)

    async def _gatt_read(
        self,
        client: NativeApiConnection,
        message: BluetoothGATTReadRequest | BluetoothGATTReadDescriptorRequest,
        operation: Callable[[int, int], Awaitable[bytes]],
    ) -> None:
        try:
            data = await operation(message.address, message.handle)
            await client.write_message(
                BluetoothGATTReadResponse(
                    address=message.address, handle=message.handle, data=data
                )
            )
        except BluetoothProxyError as error:
            await self._send_gatt_error(
                client, message.address, message.handle, error.error
            )
        except Exception:
            logger.exception("Bluetooth GATT read failed")
            await self._send_gatt_error(
                client, message.address, message.handle, GATT_ERROR
            )

    async def _gatt_write_characteristic(
        self, client: NativeApiConnection, message: BluetoothGATTWriteRequest
    ) -> None:
        await self._gatt_write(
            client,
            message,
            self.write_characteristic,
            message.response,
        )

    async def _gatt_write_descriptor(
        self, client: NativeApiConnection, message: BluetoothGATTWriteDescriptorRequest
    ) -> None:
        await self._gatt_write(client, message, self.write_descriptor)

    async def _gatt_write(
        self,
        client: NativeApiConnection,
        message: BluetoothGATTWriteRequest | BluetoothGATTWriteDescriptorRequest,
        operation: Callable[..., Awaitable[None]],
        *extra_args: Any,
    ) -> None:
        try:
            await operation(
                message.address, message.handle, bytes(message.data), *extra_args
            )
            await client.write_message(
                BluetoothGATTWriteResponse(
                    address=message.address, handle=message.handle
                )
            )
        except BluetoothProxyError as error:
            await self._send_gatt_error(
                client, message.address, message.handle, error.error
            )
        except Exception:
            logger.exception("Bluetooth GATT write failed")
            await self._send_gatt_error(
                client, message.address, message.handle, GATT_ERROR
            )

    async def _gatt_notify(
        self, client: NativeApiConnection, message: BluetoothGATTNotifyRequest
    ) -> None:
        async def send_notification(data: bytes) -> None:
            await client.write_message(
                BluetoothGATTNotifyDataResponse(
                    address=message.address, handle=message.handle, data=data
                )
            )

        try:
            await self.set_notify(
                message.address,
                message.handle,
                message.enable,
                send_notification,
            )
            key = (message.address, message.handle)
            if message.enable:
                self._notification_owners[key] = client
            else:
                self._notification_owners.pop(key, None)
            await client.write_message(
                BluetoothGATTNotifyResponse(
                    address=message.address, handle=message.handle
                )
            )
        except BluetoothProxyError as error:
            await self._send_gatt_error(
                client, message.address, message.handle, error.error
            )
        except Exception:
            logger.exception("Bluetooth GATT notify request failed")
            await self._send_gatt_error(
                client, message.address, message.handle, GATT_ERROR
            )

    async def _set_connection_params(
        self, client: NativeApiConnection, message: BluetoothSetConnectionParamsRequest
    ) -> None:
        success, error = await self._run_simple_operation(
            self.set_connection_params,
            message.address,
            message.min_interval,
            message.max_interval,
            message.latency,
            message.timeout,
        )
        await client.write_message(
            BluetoothSetConnectionParamsResponse(
                address=message.address, error=0 if success else error
            )
        )

    async def _send_gatt_error(
        self,
        client: NativeApiConnection,
        address: int,
        handle: int,
        error: int,
    ) -> None:
        await client.write_message(
            BluetoothGATTErrorResponse(address=address, handle=handle, error=error)
        )


def bluetooth_address_to_int(address: str) -> int:
    """Convert ``AA:BB:CC:DD:EE:FF`` to the native API uint64 form."""
    return int(address.replace(":", "").replace("-", ""), 16)


def bluetooth_address_to_str(address: int) -> str:
    """Convert a native API uint64 address to ``AA:BB:CC:DD:EE:FF``."""
    value = f"{address:012X}"
    return ":".join(value[index : index + 2] for index in range(0, 12, 2))


def _uuid_from_text(value: str) -> UUID:
    value = value.lower().removeprefix("0x")
    if len(value) <= 8 and "-" not in value:
        value = f"{int(value, 16):08x}-0000-1000-8000-00805f9b34fb"
    return UUID(value)


def _set_proto_uuid(
    message: (
        BluetoothGATTDescriptorProto
        | BluetoothGATTCharacteristicProto
        | BluetoothGATTServiceProto
    ),
    value: str,
) -> None:
    uuid = _uuid_from_text(value)
    bluetooth_base = UUID("00000000-0000-1000-8000-00805f9b34fb")
    if (uuid.int & ((1 << 96) - 1)) == (bluetooth_base.int & ((1 << 96) - 1)):
        message.short_uuid = uuid.int >> 96
    else:
        message.uuid.extend((uuid.int >> 64, uuid.int & ((1 << 64) - 1)))


def _descriptor_to_proto(
    descriptor: BluetoothGATTDescriptor,
) -> BluetoothGATTDescriptorProto:
    message = BluetoothGATTDescriptorProto(handle=descriptor.handle)
    _set_proto_uuid(message, descriptor.uuid)
    return message


def _characteristic_to_proto(
    characteristic: BluetoothGATTCharacteristic,
) -> BluetoothGATTCharacteristicProto:
    message = BluetoothGATTCharacteristicProto(
        handle=characteristic.handle, properties=characteristic.properties
    )
    _set_proto_uuid(message, characteristic.uuid)
    message.descriptors.extend(
        _descriptor_to_proto(descriptor) for descriptor in characteristic.descriptors
    )
    return message


def _service_to_proto(service: BluetoothGATTService) -> BluetoothGATTServiceProto:
    message = BluetoothGATTServiceProto(handle=service.handle)
    _set_proto_uuid(message, service.uuid)
    message.characteristics.extend(
        _characteristic_to_proto(characteristic)
        for characteristic in service.characteristics
    )
    return message


def _append_ad_structure(target: bytearray, data_type: int, data: bytes) -> None:
    available = 62 - len(target)
    if available < 2:
        return
    data = data[: available - 2]
    target.extend((len(data) + 1, data_type))
    target.extend(data)


async def _discard_notification(_data: bytes) -> None:
    return None


def _encode_advertisement_data(advertisement: BluetoothAdvertisement) -> bytes:
    """Build a valid LE advertisement payload when a backend has no raw bytes."""
    result = bytearray()
    if advertisement.name:
        _append_ad_structure(result, 0x09, advertisement.name.encode("utf-8"))
    for uuid_text in advertisement.service_uuids:
        uuid = _uuid_from_text(uuid_text)
        short_uuid = uuid.int >> 96
        if short_uuid <= 0xFFFF:
            _append_ad_structure(result, 0x03, short_uuid.to_bytes(2, "little"))
        else:
            _append_ad_structure(result, 0x07, uuid.bytes_le)
    for uuid_text, data in advertisement.service_data.items():
        uuid = _uuid_from_text(uuid_text)
        short_uuid = uuid.int >> 96
        if short_uuid <= 0xFFFF:
            _append_ad_structure(result, 0x16, short_uuid.to_bytes(2, "little") + data)
        else:
            _append_ad_structure(result, 0x21, uuid.bytes_le + data)
    for company_id, data in advertisement.manufacturer_data.items():
        _append_ad_structure(result, 0xFF, company_id.to_bytes(2, "little") + data)
    return bytes(result)
