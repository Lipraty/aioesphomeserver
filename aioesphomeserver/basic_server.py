import asyncio
from typing import Any

from aioesphomeapi import ColorMode

from aioesphomeserver.binary_sensor import BinarySensorEntity
from aioesphomeserver.device import Device
from aioesphomeserver.light import LightEntity
from aioesphomeserver.listener import EntityListener
from aioesphomeserver.native_api_server import NativeApiServer
from aioesphomeserver.sensor import SensorEntity
from aioesphomeserver.switch import SwitchEntity
from aioesphomeserver.web_server import WebServer


class SwitchListener(EntityListener):
    async def handle(self, key: str, message: Any) -> None:
        if self.device is None:
            return
        sensor = self.device.get_entity("test_binary_sensor")
        if isinstance(sensor, BinarySensorEntity):
            await sensor.set_state(message.state)


if __name__ == "__main__":
    device = Device(
        name="Test Device",
        mac_address="AC:BC:32:89:0E:C9",
        model="Test Device",
        project_name="aioesphomeserver.basic-server",
        project_version="1.0.0",
    )

    device.add_entity(
        BinarySensorEntity(
            name="Test Binary Sensor",
        )
    )

    device.add_entity(
        SwitchEntity(
            name="Test Switch",
        )
    )

    device.add_entity(SensorEntity(name="Test Sensor"))

    device.add_entity(SwitchListener(name="_listener", entity_id="test_switch"))

    device.add_entity(
        LightEntity(
            name="Text Light",
            effects=["Foo", "Bar", "Sparkle"],
            color_modes=[
                ColorMode.RGB_WHITE,
            ],
        )
    )

    asyncio.run(device.run())
