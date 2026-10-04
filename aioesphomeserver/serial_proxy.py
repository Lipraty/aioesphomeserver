"""Backend-independent ESPHome serial proxy ports."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from aioesphomeapi.api_pb2 import (
    SerialProxyConfigureRequest,
    SerialProxyDataReceived,
    SerialProxyGetModemPinsRequest,
    SerialProxyGetModemPinsResponse,
    SerialProxyIdentity,
    SerialProxyInfo,
    SerialProxyRequest,
    SerialProxyRequestResponse,
    SerialProxySetModeRequest,
    SerialProxySetModemPinsRequest,
    SerialProxyWriteRequest,
)
from aioesphomeapi.model import SerialProxyRequestType, SerialProxyStatus

if TYPE_CHECKING:
    from google.protobuf.message import Message
    from aioesphomeserver.native_api_server import NativeApiConnection

logger = logging.getLogger(__name__)
__all__ = ["SerialProxy", "SerialProxyError"]


class SerialProxyError(Exception):
    def __init__(self, status: SerialProxyStatus, message: str = "") -> None:
        super().__init__(message)
        self.status = status


class SerialProxy:
    """One serial port; subclass the async hooks to connect real hardware."""

    def __init__(
        self,
        name: str,
        *,
        port_type: int = 0,
        configured_line_states: int = 0,
        identity: SerialProxyIdentity | None = None,
    ) -> None:
        self.name = name
        self.port_type = port_type
        self.configured_line_states = configured_line_states
        self.identity = identity
        self.instance = 0
        self.owner: NativeApiConnection | None = None
        self._identity_clients: set[NativeApiConnection] = set()

    def info(self) -> SerialProxyInfo:
        return SerialProxyInfo(
            name=self.name,
            port_type=self.port_type,
            configured_line_states=self.configured_line_states,
        )

    async def on_subscribe(self) -> None:
        """Open the port before acknowledging a subscription."""

    async def on_unsubscribe(self) -> None:
        """Close the port when its owner leaves."""

    async def on_configure(self, request: SerialProxyConfigureRequest) -> None:
        raise SerialProxyError(SerialProxyStatus.NOT_SUPPORTED)

    async def on_write(self, data: bytes) -> None:
        raise SerialProxyError(SerialProxyStatus.NOT_SUPPORTED)

    async def on_flush(self) -> SerialProxyStatus:
        raise SerialProxyError(SerialProxyStatus.NOT_SUPPORTED)

    async def on_set_modem_pins(self, line_states: int) -> None:
        raise SerialProxyError(SerialProxyStatus.NOT_SUPPORTED)

    async def on_get_modem_pins(self) -> int:
        raise SerialProxyError(SerialProxyStatus.NOT_SUPPORTED)

    async def on_set_mode(self, mode: int) -> None:
        raise SerialProxyError(SerialProxyStatus.NOT_SUPPORTED)

    async def publish_data(self, data: bytes) -> None:
        """Forward bytes received from the hardware to the owning API client."""
        if self.owner is not None and self.owner.running:
            for offset in range(0, len(data), 1024):
                await self.owner.write_message(
                    SerialProxyDataReceived(
                        instance=self.instance, data=data[offset : offset + 1024]
                    )
                )

    async def publish_identity(self) -> None:
        """Send a changed identity to clients subscribed to identity updates."""
        if self.identity is None:
            return
        self.identity.instance = self.instance
        for client in tuple(self._identity_clients):
            if client.running:
                await client.write_message(self.identity)

    async def handle_message(
        self, client: NativeApiConnection, message: Message
    ) -> bool:
        if isinstance(message, SerialProxyRequest):
            kind = message.type
            try:
                if kind == SerialProxyRequestType.SUBSCRIBE:
                    if self.owner is not None and self.owner is not client:
                        raise SerialProxyError(SerialProxyStatus.PORT_IN_USE)
                    if self.owner is None:
                        # Reserve the port before awaiting the backend so that
                        # concurrent subscribers cannot both open it.
                        self.owner = client
                        try:
                            await self.on_subscribe()
                        except BaseException:
                            self.owner = None
                            raise
                elif kind == SerialProxyRequestType.UNSUBSCRIBE:
                    if self.owner is not client:
                        raise SerialProxyError(SerialProxyStatus.PORT_IN_USE)
                    await self.on_unsubscribe()
                    self.owner = None
                elif kind == SerialProxyRequestType.FLUSH:
                    self._require_owner(client)
                    status = await self.on_flush()
                    await client.write_message(
                        SerialProxyRequestResponse(
                            instance=self.instance, type=kind, status=status
                        )
                    )
                    return True
                elif kind in (
                    SerialProxyRequestType.CONFIGURE,
                    SerialProxyRequestType.SET_MODEM_PINS,
                    SerialProxyRequestType.SET_MODE,
                ):
                    raise SerialProxyError(SerialProxyStatus.INVALID_ARGUMENT)
                else:
                    raise SerialProxyError(SerialProxyStatus.NOT_SUPPORTED)
                status = SerialProxyStatus.OK
                detail = ""
            except SerialProxyError as err:
                status, detail = err.status, str(err)
            except Exception:
                logger.exception("Serial proxy request failed")
                status, detail = SerialProxyStatus.ERROR, "Serial backend error"
            await self._ack(client, kind, status, detail)
            return True

        operation_kind: SerialProxyRequestType | None = None
        try:
            if isinstance(message, SerialProxyConfigureRequest):
                operation_kind = SerialProxyRequestType.CONFIGURE
                self._require_owner(client)
                if (message.baudrate == 0 or message.parity not in (0, 1, 2)
                    or message.stop_bits not in (1, 2)
                    or message.data_size not in (5, 6, 7, 8)):
                    raise SerialProxyError(SerialProxyStatus.INVALID_ARGUMENT)
                await self.on_configure(message)
            elif isinstance(message, SerialProxySetModemPinsRequest):
                operation_kind = SerialProxyRequestType.SET_MODEM_PINS
                self._require_owner(client)
                if message.line_states & ~self.configured_line_states:
                    raise SerialProxyError(SerialProxyStatus.INVALID_ARGUMENT)
                await self.on_set_modem_pins(message.line_states)
            elif isinstance(message, SerialProxySetModeRequest):
                operation_kind = SerialProxyRequestType.SET_MODE
                self._require_owner(client)
                if message.mode not in (0, 1):
                    raise SerialProxyError(SerialProxyStatus.INVALID_ARGUMENT)
                await self.on_set_mode(message.mode)
            elif isinstance(message, SerialProxyGetModemPinsRequest):
                try:
                    line_states = await self.on_get_modem_pins()
                    status = SerialProxyStatus.OK
                except SerialProxyError as err:
                    line_states, status = 0, err.status
                except Exception:
                    logger.exception("Serial proxy pin read failed")
                    line_states, status = 0, SerialProxyStatus.ERROR
                await client.write_message(
                    SerialProxyGetModemPinsResponse(
                        instance=self.instance,
                        line_states=line_states,
                        status=status,
                    )
                )
                return True
            elif isinstance(message, SerialProxyWriteRequest):
                self._require_owner(client)
                await self.on_write(message.data)
                return True
            else:
                return False
            status, detail = SerialProxyStatus.OK, ""
        except SerialProxyError as err:
            status, detail = err.status, str(err)
        except Exception:
            logger.exception("Serial proxy operation failed")
            status, detail = SerialProxyStatus.ERROR, "Serial backend error"
        if operation_kind is not None:
            await self._ack(client, operation_kind, status, detail)
        return True

    def _require_owner(self, client: NativeApiConnection) -> None:
        if self.owner is not client:
            raise SerialProxyError(SerialProxyStatus.PORT_IN_USE)

    async def _ack(
        self,
        client: NativeApiConnection,
        kind: SerialProxyRequestType,
        status: SerialProxyStatus,
        detail: str = "",
    ) -> None:
        await client.write_message(
            SerialProxyRequestResponse(
                instance=self.instance, type=kind, status=status, error_message=detail
            )
        )

    async def on_api_client_disconnected(self, client: NativeApiConnection) -> None:
        self._identity_clients.discard(client)
        if self.owner is client:
            self.owner = None
            try:
                await self.on_unsubscribe()
            except Exception:
                logger.exception("Serial proxy cleanup failed")
