"""Register every ESPHome entity type without requiring physical hardware."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence

from aioesphomeapi import (
    ClimateFanMode,
    ClimateMode,
    ClimatePreset,
    ClimateSwingMode,
    ColorMode,
)
from aioesphomeapi.api_pb2 import AreaInfo, DeviceInfo
from aioesphomeapi.model import (
    AlarmControlPanelState,
    CoverOperation,
    EntityCategory,
    FanDirection,
    FanSpeed,
    LockState,
    MediaPlayerState,
    NumberMode,
    SensorStateClass,
    TemperatureUnit,
    WaterHeaterFeature,
    WaterHeaterMode,
    WaterHeaterStateFlag,
)

from aioesphomeserver import (
    AlarmControlPanelEntity,
    BinarySensorEntity,
    ButtonEntity,
    ClimateEntity,
    CoverEntity,
    DateEntity,
    DateTimeEntity,
    Device,
    EventEntity,
    FanEntity,
    InfraredEntity,
    LightEntity,
    LockEntity,
    MediaPlayerEntity,
    NumberEntity,
    RadioFrequencyEntity,
    SelectEntity,
    SensorEntity,
    SirenEntity,
    SwitchEntity,
    TextEntity,
    TextSensorEntity,
    TimeEntity,
    UpdateEntity,
    ValveEntity,
    WaterHeaterEntity,
)
from aioesphomeserver.basic_entity import BasicEntity
from examples.camera import ExampleCamera


class DemoButton(ButtonEntity):
    async def on_press(self) -> None:
        print("Button pressed: refresh data")


class ShowcaseCamera(ExampleCamera):
    """Serve snapshot and JPEG stream requests in the entity showcase."""

    def __init__(self) -> None:
        super().__init__(
            name="Showcase camera",
            object_id="showcase_camera",
            icon="mdi:camera",
        )


class DemoInfrared(InfraredEntity):
    async def on_transmit(
        self,
        carrier_frequency: int,
        repeat_count: int,
        timings: Sequence[int],
        modulation: int,
    ) -> None:
        print(
            "IR transmit:",
            carrier_frequency,
            "Hz,",
            len(timings),
            "timings, repeats=",
            repeat_count,
        )


class DemoRadioFrequency(RadioFrequencyEntity):
    async def on_transmit(
        self,
        carrier_frequency: int,
        repeat_count: int,
        timings: Sequence[int],
        modulation: int,
    ) -> None:
        print(
            "RF transmit:",
            carrier_frequency,
            "Hz,",
            len(timings),
            "timings, repeats=",
            repeat_count,
        )


class DemoUpdate(UpdateEntity):
    async def on_command(self, value: int) -> None:
        print("Update command received:", value)
        await super().on_command(value)


def build_device() -> tuple[Device, dict[str, BasicEntity]]:
    """Build a device containing one instance of every entity domain."""

    device = Device(
        name="entity-showcase-device",
        friendly_name="Entity Showcase",
        mac_address="02:00:00:00:10:06",
        model="Python entity showcase",
        esphome_version="1145.1.4",
        project_name="aioesphomeserver.entity-showcase",
        project_version="1.0.0",
        area=AreaInfo(area_id=1, name="Office"),
        areas=(AreaInfo(area_id=1, name="Office"),),
        devices=(DeviceInfo(device_id=1, name="Room module", area_id=1),),
    )

    entities: dict[str, BasicEntity] = {}

    def add(name: str, entity: BasicEntity) -> None:
        device.add_entity(entity)
        entities[name] = entity

    add(
        "alarm",
        AlarmControlPanelEntity(
            name="Alarm",
            object_id="alarm",
            state=AlarmControlPanelState.DISARMED,
        ),
    )
    add("door", BinarySensorEntity(name="Front door", object_id="front_door"))
    add(
        "button",
        DemoButton(
            name="Refresh data",
            object_id="refresh_data",
            entity_category=EntityCategory.CONFIG,
        ),
    )
    add("camera", ShowcaseCamera())
    add(
        "climate",
        ClimateEntity(
            name="Thermostat",
            object_id="thermostat",
            supported_modes=(ClimateMode.OFF, ClimateMode.HEAT, ClimateMode.COOL),
            supports_fan_mode=True,
            supported_fan_modes=(
                ClimateFanMode.AUTO,
                ClimateFanMode.LOW,
                ClimateFanMode.HIGH,
            ),
            custom_fan_modes=("silent",),
            supports_swing_mode=True,
            supported_swing_modes=(ClimateSwingMode.OFF, ClimateSwingMode.VERTICAL),
            supports_action=True,
            supports_preset=True,
            supported_presets=(
                ClimatePreset.NONE,
                ClimatePreset.ECO,
                ClimatePreset.COMFORT,
            ),
            custom_presets=("boost",),
            temperature_unit=TemperatureUnit.CELSIUS,
            visual_min_temperature=16.0,
            visual_max_temperature=30.0,
            visual_target_temperature_step=0.5,
        ),
    )
    add(
        "cover",
        CoverEntity(
            name="Window shade",
            object_id="window_shade",
            position=0.5,
            current_operation=CoverOperation.IDLE,
        ),
    )
    add(
        "date",
        DateEntity(
            name="Holiday date",
            object_id="holiday_date",
            year=2026,
            month=8,
            day=22,
        ),
    )
    add(
        "datetime",
        DateTimeEntity(name="Last sync", object_id="last_sync", epoch_seconds=0),
    )
    add(
        "event",
        EventEntity(
            name="Door events",
            object_id="door_events",
            event_types=("opened", "closed"),
        ),
    )
    add(
        "fan",
        FanEntity(
            name="Ceiling fan",
            object_id="ceiling_fan",
            speed=FanSpeed.LOW,
            direction=FanDirection.FORWARD,
            speed_level=1,
        ),
    )
    add(
        "infrared",
        DemoInfrared(
            name="IR transmitter",
            object_id="ir_transmitter",
            receiver_frequency=38000,
        ),
    )
    add(
        "light",
        LightEntity(
            name="Desk light",
            object_id="desk_light",
            color_modes=(
                ColorMode.RGB,
                ColorMode.COLOR_TEMPERATURE,
            ),
            min_mireds=153.0,
            max_mireds=500.0,
            effects=("reading", "relax"),
        ),
    )
    add(
        "lock",
        LockEntity(
            name="Front door lock",
            object_id="front_door_lock",
            state=LockState.LOCKED,
        ),
    )
    add(
        "media_player",
        MediaPlayerEntity(
            name="Kitchen speaker",
            object_id="kitchen_speaker",
            state=MediaPlayerState.IDLE,
        ),
    )
    add(
        "number",
        NumberEntity(
            name="Target humidity",
            object_id="target_humidity",
            min_value=20.0,
            max_value=80.0,
            step=1.0,
            unit_of_measurement="%",
            mode=NumberMode.SLIDER,
            entity_category=EntityCategory.CONFIG,
        ),
    )
    add(
        "radio_frequency",
        DemoRadioFrequency(
            name="RF transmitter",
            object_id="rf_transmitter",
            frequency_min=300000000,
            frequency_max=928000000,
        ),
    )
    add(
        "select",
        SelectEntity(
            name="Operating mode",
            object_id="operating_mode",
            options=("auto", "quiet", "boost"),
            initial_state="auto",
            entity_category=EntityCategory.CONFIG,
        ),
    )
    add(
        "sensor",
        SensorEntity(
            name="Temperature",
            object_id="temperature",
            device_id=1,
            unit_of_measurement="C",
            accuracy_decimals=1,
            state_class=SensorStateClass.MEASUREMENT,
        ),
    )
    add(
        "siren",
        SirenEntity(
            name="Alarm siren",
            object_id="alarm_siren",
            tones=("alarm", "chime"),
            supports_duration=True,
            supports_volume=True,
        ),
    )
    add("switch", SwitchEntity(name="Desk outlet", object_id="desk_outlet"))
    add(
        "text",
        TextEntity(
            name="Announcement",
            object_id="announcement",
            initial_state="Ready",
            max_length=80,
        ),
    )
    add(
        "text_sensor",
        TextSensorEntity(
            name="Device status",
            object_id="device_status",
            initial_state="Ready",
        ),
    )
    add(
        "time",
        TimeEntity(name="Wake time", object_id="wake_time", hour=7, minute=30),
    )
    update = DemoUpdate(name="Firmware", object_id="firmware")
    update.current_version = "1.0.0"
    update.latest_version = "1.1.0"
    add("update", update)
    add(
        "valve",
        ValveEntity(name="Garden valve", object_id="garden_valve", position=0.25),
    )

    water_heater = WaterHeaterEntity(
        name="Water heater",
        object_id="water_heater",
        current_temperature=48.0,
        target_temperature=52.0,
        mode=WaterHeaterMode.ECO,
        state=int(WaterHeaterStateFlag.ON),
    )
    water_heater.supported_modes = [
        WaterHeaterMode.OFF,
        WaterHeaterMode.ECO,
        WaterHeaterMode.ELECTRIC,
    ]
    water_heater.supported_features = int(
        WaterHeaterFeature.SUPPORTS_CURRENT_TEMPERATURE
        | WaterHeaterFeature.SUPPORTS_TARGET_TEMPERATURE
        | WaterHeaterFeature.SUPPORTS_OPERATION_MODE
        | WaterHeaterFeature.SUPPORTS_ON_OFF
    )
    water_heater.min_temperature = 35.0
    water_heater.max_temperature = 75.0
    water_heater.target_temperature_step = 0.5
    water_heater.temperature_unit = TemperatureUnit.CELSIUS
    add("water_heater", water_heater)

    return device, entities


async def update_demo_states(entities: dict[str, BasicEntity]) -> None:
    """Publish changing values so the state stream is visible in Home Assistant."""

    door = entities["door"]
    temperature = entities["sensor"]
    status = entities["text_sensor"]
    event = entities["event"]
    assert isinstance(door, BinarySensorEntity)
    assert isinstance(temperature, SensorEntity)
    assert isinstance(status, TextSensorEntity)
    assert isinstance(event, EventEntity)

    is_open = False
    try:
        while True:
            is_open = not is_open
            await door.set_state(is_open)
            await temperature.set_state(21.0 if is_open else 20.0)
            await status.set_state("Door open" if is_open else "All clear")
            await event.trigger("opened" if is_open else "closed")
            await asyncio.sleep(10)
    except asyncio.CancelledError:
        raise


async def main() -> None:
    device, entities = build_device()
    async with asyncio.TaskGroup() as tasks:
        tasks.create_task(update_demo_states(entities))
        tasks.create_task(device.run(api_port=6053, web_port=None))


if __name__ == "__main__":
    asyncio.run(main())
