import ast
import asyncio
import json
from pathlib import Path
from typing import Any, cast
from unittest.mock import AsyncMock, patch

import pytest
from aioesphomeapi import APIClient, ColorMode
from aioesphomeapi.api_pb2 import (
    ClimateCommandRequest,
    ClimateMode,
    SwitchStateResponse,
)
from aioesphomeapi.model import LightInfo
from aiohttp.test_utils import make_mocked_request

from aioesphomeserver import (
    BinarySensorEntity,
    ClimateEntity,
    Device,
    EntityListener,
    LightEntity,
    NativeApiServer,
    SensorEntity,
)


def test_device_entity_management_and_publish():
    async def run() -> None:
        device = Device(name="Device", mac_address="02:00:00:00:00:10")
        sensor = SensorEntity(name="Temperature")
        listener = AsyncMock()
        listener.object_id = "listener"
        listener.device = None
        listener.key = None
        listener.can_handle.return_value = True
        device.add_entity(sensor)
        device.add_entity(listener)

        assert device.get_entity("temperature") is sensor
        assert sensor.key is not None
        assert device.get_entity_by_key(sensor.key) is sensor
        assert device.get_entity_by_key(0) is None
        await device.publish(sensor, "custom", {"value": 1})
        listener.can_handle.assert_awaited_once_with("custom", {"value": 1})
        listener.handle.assert_awaited_once_with("custom", {"value": 1})

        with pytest.raises(ValueError, match="Duplicate object_id"):
            device.add_entity(SensorEntity(name="Temperature"))

    asyncio.run(run())


def test_device_uses_matching_esphome_node_and_friendly_names():
    async def run() -> None:
        device = Device(
            name="External Data Device",
            mac_address="02:00:00:00:00:15",
            esphome_version="2026.8.21",
        )
        assert device.name == "external-data-device"
        info = await device.build_device_info_response()
        assert info.name == "external-data-device"
        assert info.friendly_name == "External Data Device"
        assert info.esphome_version == "2026.8.21"

    asyncio.run(run())


def test_device_omits_non_dotted_project_name_for_home_assistant_compatibility():
    async def run() -> None:
        device = Device(
            name="Project Compatibility",
            mac_address="02:00:00:00:00:18",
            project_name="aioesphomeserver",
            project_version="1.0.0",
        )
        info = await device.build_device_info_response()
        assert info.project_name == ""
        assert info.project_version == ""

        zeroconf = AsyncMock()
        with (
            patch("aioesphomeserver.device.AsyncZeroconf", return_value=zeroconf),
            patch.object(device, "_get_ip_address", return_value="192.0.2.18"),
        ):
            await device.register_zeroconf(6053)

        service = zeroconf.async_register_service.await_args.args[0]
        assert b"project_name" not in service.properties
        assert b"project_version" not in service.properties

    asyncio.run(run())


def test_runnable_examples_use_home_assistant_compatible_project_names():
    root = Path(__file__).resolve().parents[1]
    paths = [
        *sorted((root / "examples").glob("*.py")),
        root / "aioesphomeserver" / "basic_server.py",
    ]
    invalid: list[str] = []
    for path in paths:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.keyword) or node.arg != "project_name":
                continue
            if isinstance(node.value, ast.Constant) and isinstance(
                node.value.value, str
            ):
                if "." not in node.value.value:
                    invalid.append(f"{path.relative_to(root)}:{node.value.value}")

    assert invalid == []


def test_zeroconf_uses_api_node_name_and_standard_txt_records():
    async def run() -> None:
        device = Device(
            name="External Data Device",
            mac_address="02:00:00:00:00:16",
            esphome_version="2026.8.21",
            project_name="aioesphomeserver.example",
            project_version="1.0.0",
        )
        zeroconf = AsyncMock()
        with (
            patch("aioesphomeserver.device.AsyncZeroconf", return_value=zeroconf),
            patch.object(device, "_get_ip_address", return_value="192.0.2.1"),
        ):
            result = await device.register_zeroconf(6053)

        assert result is zeroconf
        service = zeroconf.async_register_service.await_args.args[0]
        assert service.name == "external-data-device._esphomelib._tcp.local."
        assert service.server == "external-data-device.local."
        assert service.properties[b"version"] == b"2026.8.21"
        assert service.properties[b"project_name"] == b"aioesphomeserver.example"
        assert service.properties[b"project_version"] == b"1.0.0"
        assert service.properties[b"config_hash"]

    asyncio.run(run())


def test_encrypted_device_advertises_noise():
    async def run() -> None:
        device = Device(
            name="encrypted-device",
            encryption_key=bytes(range(32)),
        )
        zeroconf = AsyncMock()
        with (
            patch("aioesphomeserver.device.AsyncZeroconf", return_value=zeroconf),
            patch.object(device, "_get_ip_address", return_value="192.0.2.2"),
        ):
            await device.register_zeroconf(6053)

        service = zeroconf.async_register_service.await_args.args[0]
        assert service.properties[b"api_encryption"] == (
            b"Noise_NNpsk0_25519_ChaChaPoly_SHA256"
        )
        info = await device.build_device_info_response()
        assert info.api_encryption_supported is True
        assert info.api_encryption_provisionable is False

    asyncio.run(run())


def test_zeroconf_registration_failure_closes_the_instance():
    async def run() -> None:
        device = Device(name="zeroconf-failure", mac_address="02:00:00:00:00:19")
        zeroconf = AsyncMock()
        zeroconf.async_register_service.side_effect = OSError("no multicast route")
        with (
            patch("aioesphomeserver.device.AsyncZeroconf", return_value=zeroconf),
            patch.object(device, "_get_ip_address", return_value="192.0.2.3"),
        ):
            result = await device.register_zeroconf(6053)

        assert result is None
        zeroconf.async_close.assert_awaited_once()

    asyncio.run(run())


def test_device_run_starts_api_before_announcing():
    async def run() -> None:
        class RecordingDevice(Device):
            def __init__(self) -> None:
                super().__init__(
                    name="Run Order",
                    mac_address="02:00:00:00:00:17",
                )
                self.announced = asyncio.Event()

            async def register_zeroconf(self, port: int) -> None:
                api = self.get_entity("_server")
                assert isinstance(api, NativeApiServer)
                assert api.bound_port == port
                self.announced.set()
                return None

        device = RecordingDevice()
        device.add_entity(SensorEntity(name="Temperature"))
        task = asyncio.create_task(device.run(api_port=0, web_port=None))
        try:
            await asyncio.wait_for(device.announced.wait(), timeout=2)
            assert device.web_port is None
            assert device.api_port is not None
            client = APIClient(
                "127.0.0.1",
                device.api_port,
                expected_name="run-order",
            )
            await client.connect(login=True)
            info = await client.device_info()
            assert info.name == "run-order"
            await client.disconnect(force=True)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(run())


def test_binary_sensor_and_entity_listener_flow():
    async def run() -> None:
        class SwitchToBinary(EntityListener):
            async def handle(self, key: str, message: object) -> None:
                device = self.device
                assert device is not None
                binary = device.get_entity("door")
                assert isinstance(binary, BinarySensorEntity)
                await binary.set_state(bool(getattr(message, "state", False)))

        device = Device(name="Listener device", mac_address="02:00:00:00:00:14")
        binary = BinarySensorEntity(name="Door", object_id="door")
        listener = SwitchToBinary(name="Listener", entity_id="relay")
        device.add_entity(binary)
        device.add_entity(listener)
        switch_state = SwitchStateResponse(key=99, state=True)
        assert await listener.can_handle("state_change", switch_state) is False
        listener.entity_id = "door"
        binary.key = 99
        assert await listener.can_handle("state_change", switch_state) is True
        await listener.handle("state_change", switch_state)
        assert await binary.get_state() is True
        assert json.loads(await binary.state_json())["state"] == "ON"

    asyncio.run(run())


def test_light_http_style_commands_update_state_json():
    async def run() -> None:
        light = LightEntity(
            name="Desk light",
            color_modes=(ColorMode.RGB,),
            effects=("rainbow",),
        )
        device = Device(name="Light device", mac_address="02:00:00:00:00:11")
        device.add_entity(light)
        notify_state_change = AsyncMock()
        cast(Any, light).notify_state_change = notify_state_change

        await light.set_state_from_query(
            True,
            {
                "brightness": ["128"],
                "white_value": ["64"],
                "r": ["255"],
                "g": ["64"],
                "b": ["32"],
                "effect": ["rainbow"],
            },
        )
        state = json.loads(await light.state_json())
        assert state["state"] == "ON"
        assert state["brightness"] == 128
        assert state["white_value"] == pytest.approx(64 / 255)
        assert state["color"] == {
            "r": 1.0,
            "g": pytest.approx(64 / 255),
            "b": pytest.approx(32 / 255),
        }
        notify_state_change.assert_awaited_once()

        response = await light.route_turn_off(
            make_mocked_request("POST", "/light/desk_light/turn_off?brightness=255")
        )
        assert response.status == 200
        assert response.text is not None
        assert json.loads(response.text)["state"] == "OFF"

    asyncio.run(run())


def test_light_reports_configured_color_modes_to_native_api_clients():
    async def run() -> None:
        light = LightEntity(
            name="White light",
            color_modes=(ColorMode.WHITE, ColorMode.COLOR_TEMPERATURE),
        )
        device = Device(name="Light device", mac_address="02:00:00:00:00:22")
        device.add_entity(light)
        api = NativeApiServer(name="_api", port=0, host="127.0.0.1")
        device.add_entity(api)
        task = asyncio.create_task(api.run())
        try:
            await asyncio.wait_for(api.wait_started(), timeout=5)
            port = api.bound_port
            assert port is not None
            client = APIClient("127.0.0.1", port, keepalive=60)
            await client.connect()
            try:
                entities, _services = await client.list_entities_services()
                (found,) = [
                    entity for entity in entities if entity.name == "White light"
                ]
                assert isinstance(found, LightInfo)
                assert list(found.supported_color_modes) == [
                    ColorMode.WHITE,
                    ColorMode.COLOR_TEMPERATURE,
                ]
            finally:
                await client.disconnect()
        finally:
            await api.stop()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(run())


def test_climate_http_style_command_and_state_json():
    async def run() -> None:
        climate = ClimateEntity(
            name="Thermostat",
            supported_modes=(
                ClimateMode.CLIMATE_MODE_OFF,
                ClimateMode.CLIMATE_MODE_HEAT,
            ),
            supports_current_temperature=True,
        )
        device = Device(name="Climate device", mac_address="02:00:00:00:00:12")
        device.add_entity(climate)
        notify_state_change = AsyncMock()
        cast(Any, climate).notify_state_change = notify_state_change

        await climate.set_state_from_query(mode="heat", target_temperature=22)
        state = json.loads(await climate.state_json())
        assert state["mode"] == "HEAT"
        assert state["target_temperature"] == 22
        notify_state_change.assert_awaited_once()

        request = AsyncMock()
        request.json.return_value = {"mode": "off"}
        response = await climate.route_set_mode(request)
        assert response.status == 200
        assert response.text is not None
        assert json.loads(response.text)["mode"] == "OFF"

    asyncio.run(run())


def test_climate_ignores_target_temperature_fields_it_did_not_advertise():
    async def run() -> None:
        device = Device(name="Climate device", mac_address="02:00:00:00:00:23")
        single = ClimateEntity(name="Thermostat")
        two_point = ClimateEntity(
            name="Two point thermostat",
            supports_two_point_target_temperature=True,
        )
        device.add_entity(single)
        device.add_entity(two_point)

        await single.set_state_from_command(
            ClimateCommandRequest(
                key=single.key,
                has_target_temperature=True,
                target_temperature=21.0,
            )
        )
        assert single.target_temperature == 21.0

        # A single-point entity has no low/high targets, so the field must be
        # ignored instead of raising AttributeError.
        await single.set_state_from_command(
            ClimateCommandRequest(
                key=single.key,
                has_target_temperature_high=True,
                target_temperature_high=25.0,
            )
        )
        assert single.target_temperature == 21.0

        await two_point.set_state_from_command(
            ClimateCommandRequest(
                key=two_point.key,
                has_target_temperature_low=True,
                target_temperature_low=18.0,
            )
        )
        assert two_point.target_temperature_low == 18.0

        await two_point.set_state_from_command(
            ClimateCommandRequest(
                key=two_point.key,
                has_target_temperature=True,
                target_temperature=22.0,
            )
        )
        assert two_point.target_temperature_low == 18.0

    asyncio.run(run())
