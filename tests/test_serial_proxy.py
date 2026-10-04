"""Scenario tests for serial proxy subscriptions."""

import asyncio
from typing import Any, cast

from aioesphomeapi.api_pb2 import SerialProxyRequest, SerialProxyRequestResponse
from aioesphomeapi.model import SerialProxyRequestType, SerialProxyStatus

from aioesphomeserver import SerialProxy
from aioesphomeserver.native_api_server import NativeApiConnection


class MemoryClient:
    """Stand-in for a native API connection; the proxy only writes to it."""

    def __init__(self) -> None:
        self.messages: list[Any] = []

    async def write_message(self, message: SerialProxyRequestResponse) -> None:
        self.messages.append(message)


def _as_client(client: MemoryClient) -> NativeApiConnection:
    return cast(NativeApiConnection, client)


class BlockingProxy(SerialProxy):
    """Keep a subscription pending until the test releases it."""

    def __init__(self) -> None:
        super().__init__("UART")
        self.opened = 0
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def on_subscribe(self) -> None:
        self.opened += 1
        self.started.set()
        await self.release.wait()


def _subscribe() -> SerialProxyRequest:
    return SerialProxyRequest(instance=0, type=int(SerialProxyRequestType.SUBSCRIBE))


def test_one_subscriber_opens_the_port_and_others_are_rejected():
    async def run() -> None:
        proxy = BlockingProxy()
        first, second, third = MemoryClient(), MemoryClient(), MemoryClient()

        pending = asyncio.create_task(
            proxy.handle_message(_as_client(first), _subscribe())
        )
        async with asyncio.timeout(2):
            await proxy.started.wait()

        # The port is already taken while the first client is still being
        # served, so the second subscriber must be rejected.
        async with asyncio.timeout(2):
            await proxy.handle_message(_as_client(second), _subscribe())
        assert second.messages[-1].status == SerialProxyStatus.PORT_IN_USE

        proxy.release.set()
        await pending
        assert proxy.opened == 1
        assert first.messages[-1].status == SerialProxyStatus.OK

        # Once the owner leaves, the port is available again.
        await proxy.on_api_client_disconnected(_as_client(first))
        await proxy.handle_message(_as_client(third), _subscribe())
        assert third.messages[-1].status == SerialProxyStatus.OK
        assert proxy.opened == 2

    asyncio.run(run())


class FlakyProxy(SerialProxy):
    """Fail the first attempt to open the port, then work."""

    def __init__(self) -> None:
        super().__init__("UART")
        self.fail_next = True

    async def on_subscribe(self) -> None:
        if self.fail_next:
            self.fail_next = False
            raise RuntimeError("the port is gone")


def test_a_failed_backend_open_does_not_strand_the_port():
    async def run() -> None:
        proxy = FlakyProxy()
        first, second = MemoryClient(), MemoryClient()

        await proxy.handle_message(_as_client(first), _subscribe())
        assert first.messages[-1].status == SerialProxyStatus.ERROR

        # The failed attempt must not keep the port for itself.
        await proxy.handle_message(_as_client(second), _subscribe())
        assert second.messages[-1].status == SerialProxyStatus.OK

    asyncio.run(run())
