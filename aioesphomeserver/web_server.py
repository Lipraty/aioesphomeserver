from __future__ import annotations

import asyncio
import os
from typing import Any

from aiohttp import web
from aiohttp_sse import sse_response

from aioesphomeserver.basic_entity import BasicEntity


class WebServer(BasicEntity):
    def __init__(self, *args: Any, port: int = 8080, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.port = port
        self._subscribers: set[asyncio.Queue[tuple[str, Any]]] = set()
        self._shutdown = asyncio.Event()
        self._runner: web.AppRunner | None = None

    async def index(self, _request: web.Request) -> web.FileResponse:
        return web.FileResponse(path=os.path.dirname(__file__) + "/index.html")

    def _broadcast(self, event: tuple[str, Any]) -> None:
        for queue in tuple(self._subscribers):
            queue.put_nowait(event)

    async def handle(self, key: str, message: Any) -> None:
        if key == "state_change":
            entity_key = message.key
            device = self.device
            if device is None:
                raise RuntimeError("web server is not attached to a device")
            entity = device.get_entity_by_key(entity_key)
            if entity is None:
                return
            data = await entity.state_json()
            if data is not None:
                self._broadcast(("state", data))

        if key == "log":
            self._broadcast(("log", message))

    async def events(self, request: web.Request) -> web.StreamResponse:
        device = self.device
        if device is None:
            raise RuntimeError("web server is not attached to a device")
        queue: asyncio.Queue[tuple[str, Any]] = asyncio.Queue()
        async with sse_response(request) as resp:
            self._subscribers.add(queue)
            try:
                for entity in device.entities:
                    state = await entity.state_json()
                    if state is not None:
                        await resp.send(state, event="state")

                while resp.is_connected():
                    try:
                        event, payload = await asyncio.wait_for(queue.get(), timeout=1)
                    except asyncio.TimeoutError:
                        event, payload = "ping", ""
                    if event == "log":
                        payload = payload[1]

                    try:
                        await resp.send(payload, event=event)
                    except ConnectionResetError:
                        break
            finally:
                self._subscribers.discard(queue)

        return resp

    async def run(self) -> None:
        device = self.device
        if device is None:
            raise RuntimeError("web server is not attached to a device")
        app = web.Application()
        app.router.add_route("GET", "/events", self.events)
        app.router.add_route("GET", "/", self.index)

        for entity in device.entities:
            await entity.add_routes(app.router)

        runner = web.AppRunner(app)
        await runner.setup()
        try:
            site = web.TCPSite(runner, "0.0.0.0", self.port)
            await site.start()
            self._runner = runner
            if runner.addresses:
                # Report the port the OS assigned when the configured one is 0.
                self.port = runner.addresses[0][1]
            await device.log(2, "web", f"Starting web server on port {self.port}!")

            await self._shutdown.wait()
        finally:
            self._runner = None
            await runner.cleanup()

    async def stop(self) -> None:
        self._shutdown.set()
