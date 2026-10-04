"""Scenario tests for the web dashboard: event streams and shutdown."""

import asyncio
import contextlib
import json
import socket
from collections.abc import AsyncIterator

import aiohttp
import pytest

from aioesphomeserver import Device, SensorEntity, WebServer


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


async def _wait_until_listening(port: int) -> None:
    async with asyncio.timeout(5):
        async with aiohttp.ClientSession() as session:
            while True:
                try:
                    async with session.get(f"http://127.0.0.1:{port}/"):
                        return
                except aiohttp.ClientError:
                    await asyncio.sleep(0.05)


async def _next_frame(response: aiohttp.ClientResponse) -> tuple[str, str]:
    """Read the next complete event frame from the stream."""
    async with asyncio.timeout(5):
        event = ""
        data = ""
        while True:
            raw = await response.content.readline()
            if not raw:
                raise AssertionError("the event stream ended early")
            line = raw.decode("utf-8").rstrip("\r\n")
            if not line:
                return event, data
            name, _, value = line.partition(":")
            value = value.lstrip(" ")
            if name == "event":
                event = value
            elif name == "data":
                data = value


async def _next_event(
    response: aiohttp.ClientResponse, event_name: str
) -> tuple[str, str]:
    """Read frames until the requested event arrives, skipping keepalives."""
    while True:
        event, data = await _next_frame(response)
        if event == event_name:
            return event, data


async def _start_dashboard() -> tuple[Device, SensorEntity, WebServer, int, asyncio.Task]:
    device = Device(name="dashboard-device", mac_address="02:00:00:00:00:20")
    sensor = SensorEntity(name="Temperature")
    port = _free_port()
    web_server = WebServer(name="_web", port=port)
    device.add_entity(sensor)
    device.add_entity(web_server)
    task = asyncio.create_task(web_server.run())
    try:
        await _wait_until_listening(port)
    except BaseException:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        raise
    return device, sensor, web_server, port, task


@contextlib.asynccontextmanager
async def _running_dashboard() -> AsyncIterator[tuple[Device, SensorEntity, str]]:
    device, sensor, _web_server, port, task = await _start_dashboard()
    try:
        yield device, sensor, f"http://127.0.0.1:{port}/events"
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


def test_every_open_dashboard_receives_state_and_log_events():
    async def run() -> None:
        async with _running_dashboard() as (device, sensor, url):
            async with aiohttp.ClientSession() as session:
                async with session.get(url) as first, session.get(url) as second:
                    await sensor.set_state(12.5)
                    for response in (first, second):
                        # Skip the snapshot that is sent on connect.
                        async with asyncio.timeout(5):
                            while True:
                                _event, data = await _next_event(response, "state")
                                if json.loads(data)["state"] == 12.5:
                                    break

                    await device.log(3, "test", "dashboard log probe")
                    for response in (first, second):
                        _event, data = await _next_event(response, "log")
                        assert "dashboard log probe" in data

    asyncio.run(run())


def test_idle_stream_gets_keepalives_and_the_next_stream_starts_fresh():
    async def run() -> None:
        async with _running_dashboard() as (_device, _sensor, url):
            async with aiohttp.ClientSession() as session:
                async with session.get(url) as first:
                    # An idle dashboard still receives keepalives; this is what
                    # drives the heartbeat in the page.
                    event, _data = await _next_event(first, "ping")
                    assert event == "ping"

                # A later visitor gets the current state first instead of the
                # keepalives the closed stream left behind.
                async with session.get(url) as second:
                    event, data = await _next_frame(second)
                    assert event == "state"
                    assert "state" in json.loads(data)

    asyncio.run(run())


def test_stop_serves_the_page_and_releases_the_port():
    async def run() -> None:
        device, _sensor, web_server, port, task = await _start_dashboard()
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(f"http://127.0.0.1:{port}/") as response:
                    assert response.status == 200
                    assert "esp-app" in await response.text()

            await web_server.stop()
            await asyncio.wait_for(task, timeout=5)

            with pytest.raises(aiohttp.ClientError):
                async with aiohttp.ClientSession() as session:
                    async with session.get(f"http://127.0.0.1:{port}/"):
                        pass
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(run())
