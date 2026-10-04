import asyncio
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch

import pytest
from aioesphomeapi import APIClient
from aioesphomeapi.api_pb2 import (
    BluetoothDeviceRequest,
    BluetoothGATTGetServicesRequest,
    BluetoothGATTReadRequest,
    ListEntitiesSensorResponse,
    ListEntitiesTextSensorResponse,
)
from aioesphomeapi.model import (
    BluetoothDeviceRequestType,
    BluetoothProxyFeature,
    BluetoothScannerMode,
    BluetoothScannerState,
    EntityCategory,
    SensorStateClass,
)

from aioesphomeserver import Device, NativeApiServer
from aioesphomeserver.native_api_server import NativeApiConnection
from aioesphomeserver.bluetooth_proxy import (
    GATT_ERROR,
    BluetoothAdvertisement,
    BluetoothGATTCharacteristic,
    BluetoothGATTDescriptor,
    BluetoothGATTService,
    BluetoothProxy,
)


class MemoryClient:
    """Stand-in for a native API connection; the proxy only writes to it."""

    def __init__(self):
        self.messages = []

    async def write_message(self, message):
        self.messages.append(message)


def _as_client(client: MemoryClient) -> NativeApiConnection:
    return cast(NativeApiConnection, client)


class MemoryProxy(BluetoothProxy):
    def __init__(self):
        super().__init__(max_connections=1)
        self.connected = set()

    async def connect(self, address, address_type, use_cache):
        self.connected.add(address)
        return 247

    async def get_services(self, address):
        return [
            BluetoothGATTService(
                uuid="180d",
                handle=1,
                characteristics=[
                    BluetoothGATTCharacteristic(
                        uuid="2a37",
                        handle=2,
                        properties=0x10,
                        descriptors=[BluetoothGATTDescriptor("2902", 3)],
                    )
                ],
            )
        ]

    async def read_characteristic(self, address, handle):
        return b"ok"


class ScanningProxy(BluetoothProxy):
    def __init__(self, bluetooth_mac_address: str = "") -> None:
        super().__init__(
            bluetooth_mac_address=bluetooth_mac_address,
            max_connections=2,
            feature_flags=int(
                BluetoothProxyFeature.PASSIVE_SCAN
                | BluetoothProxyFeature.ACTIVE_CONNECTIONS
                | BluetoothProxyFeature.REMOTE_CACHING
                | BluetoothProxyFeature.RAW_ADVERTISEMENTS
                | BluetoothProxyFeature.FEATURE_STATE_AND_MODE
            ),
        )
        self.scan_active = False

    async def start_scan(self, active: bool) -> None:
        self.scan_active = active

    async def stop_scan(self) -> None:
        self.scan_active = False


def test_gatt_messages_are_translated_to_native_api_responses():
    asyncio.run(_test_gatt_messages_are_translated_to_native_api_responses())


async def _test_gatt_messages_are_translated_to_native_api_responses():
    proxy = MemoryProxy()
    client = MemoryClient()
    address = 0xAABBCCDDEEFF

    await proxy.handle_api_message(
        client,
        BluetoothDeviceRequest(
            address=address,
            request_type=BluetoothDeviceRequestType.CONNECT_V3_WITHOUT_CACHE,
            has_address_type=True,
            address_type=0,
        ),
    )
    assert client.messages[-1].connected is True
    assert client.messages[-1].mtu == 247

    await proxy.handle_api_message(
        client, BluetoothGATTGetServicesRequest(address=address)
    )
    assert client.messages[-2].services[0].short_uuid == 0x180D
    assert client.messages[-1].address == address

    await proxy.handle_api_message(
        client, BluetoothGATTReadRequest(address=address, handle=2)
    )
    assert client.messages[-1].data == b"ok"


class SlowConnectProxy(BluetoothProxy):
    """Keep a connect request pending until the test releases it."""

    def __init__(self) -> None:
        super().__init__(max_connections=1)
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.connected: set[int] = set()

    async def connect(self, address: int, address_type: int, use_cache: bool) -> int:
        self.started.set()
        await self.release.wait()
        self.connected.add(address)
        return 247


def _connect_request(address: int) -> BluetoothDeviceRequest:
    return BluetoothDeviceRequest(
        address=address,
        request_type=BluetoothDeviceRequestType.CONNECT_V3_WITHOUT_CACHE,
        has_address_type=True,
        address_type=0,
    )


def test_concurrent_connects_do_not_oversubscribe_max_connections() -> None:
    asyncio.run(_test_concurrent_connects_do_not_oversubscribe_max_connections())


async def _test_concurrent_connects_do_not_oversubscribe_max_connections() -> None:
    proxy = SlowConnectProxy()
    first, second, duplicate = MemoryClient(), MemoryClient(), MemoryClient()
    address = 0xAABBCCDDEE01

    pending = asyncio.create_task(
        proxy.handle_api_message(_as_client(first), _connect_request(address))
    )
    await proxy.started.wait()

    # The slot is reserved while the backend is still connecting.
    async with asyncio.timeout(2):
        await proxy.handle_api_message(_as_client(second), _connect_request(0xAABBCCDDEE02))
    assert second.messages[-1].connected is False
    assert second.messages[-1].error == GATT_ERROR

    # A second request for the same address must not replace the reservation.
    async with asyncio.timeout(2):
        await proxy.handle_api_message(_as_client(duplicate), _connect_request(address))
    assert duplicate.messages[-1].connected is False
    assert proxy.connected == set()

    proxy.release.set()
    await pending
    assert first.messages[-1].connected is True
    assert proxy.connected == {address}


def test_official_client_can_register_bluetooth_scanner() -> None:
    asyncio.run(_test_official_client_can_register_bluetooth_scanner())


async def _test_official_client_can_register_bluetooth_scanner() -> None:
    proxy = ScanningProxy(bluetooth_mac_address="02:00:00:00:20:02")
    device = Device(
        name="Bluetooth Proxy",
        mac_address="02:00:00:00:10:02",
        bluetooth_proxy=proxy,
    )
    api = NativeApiServer(name="_api", port=0, host="127.0.0.1")
    device.add_entity(api)
    task = asyncio.create_task(api.run())
    client: APIClient | None = None
    try:
        while api.bound_port is None:
            await asyncio.sleep(0)
        client = APIClient("127.0.0.1", api.bound_port, keepalive=60)
        await client.connect()
        info = await client.device_info()
        assert client.api_version is not None
        assert info.bluetooth_mac_address == "02:00:00:00:20:02"
        assert info.bluetooth_proxy_feature_flags_compat(client.api_version) == (
            proxy.feature_flags
        )

        states: list[Any] = []
        slots: list[tuple[int, int, list[int]]] = []
        advertisements: list[Any] = []
        structured_advertisements: list[Any] = []
        client.subscribe_bluetooth_scanner_state(states.append)
        client.subscribe_bluetooth_connections_free(
            lambda free, limit, allocated: slots.append((free, limit, allocated))
        )
        client.subscribe_bluetooth_le_raw_advertisements(advertisements.append)
        client.bluetooth_scanner_set_mode(BluetoothScannerMode.ACTIVE)

        async with asyncio.timeout(2):
            while not states or states[-1].state is not BluetoothScannerState.RUNNING:
                await asyncio.sleep(0)
        assert proxy.scan_active is True
        assert slots == [(2, 2, [])]

        await proxy.publish_advertisement(
            BluetoothAdvertisement(
                address=0xAABBCCDDEEFF,
                rssi=-55,
                name="Test sensor",
                service_uuids=["180d"],
            )
        )
        async with asyncio.timeout(2):
            while not advertisements:
                await asyncio.sleep(0)
        raw = advertisements[0].advertisements[0]
        assert raw.address == 0xAABBCCDDEEFF
        assert raw.rssi == -55
        assert raw.data

        # A backend such as Bleak that only exposes parsed fields must use the
        # structured response instead of claiming that it has original HCI bytes.
        client.subscribe_bluetooth_le_advertisements(structured_advertisements.append)
        async with asyncio.timeout(2):
            while not proxy._advertisement_clients or any(
                proxy._advertisement_clients.values()
            ):
                await asyncio.sleep(0)
        await proxy.publish_advertisement(
            BluetoothAdvertisement(
                address=0x112233445566,
                rssi=-61,
                address_type=1,
                name="Structured sensor",
                service_uuids=["180f"],
                service_data={"180f": b"\x64"},
                manufacturer_data={0x004C: b"\x01\x02"},
            )
        )
        async with asyncio.timeout(2):
            while not structured_advertisements:
                await asyncio.sleep(0)
        structured = structured_advertisements[0]
        assert structured.address == 0x112233445566
        assert structured.address_type == 1
        assert structured.name == "Structured sensor"
        assert structured.manufacturer_data == {0x004C: b"\x01\x02"}
    finally:
        if client is not None:
            await client.disconnect(force=True)
        await api.stop()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


def test_bluetooth_proxy_device_info_has_stable_fallback_identity() -> None:
    async def run() -> None:
        proxy = BluetoothProxy()
        device = Device(
            name="Fallback Bluetooth Proxy",
            mac_address="02:00:00:00:10:03",
            bluetooth_proxy=proxy,
        )
        info = await device.build_device_info_response()
        assert info.bluetooth_mac_address == device.mac_address
        assert info.legacy_bluetooth_proxy_version == 1

    asyncio.run(run())


def test_bleak_example_restores_runtime_scan_mode_support() -> None:
    pytest.importorskip("bleak")
    from examples.bleak_proxy import BleakBluetoothProxy

    proxy = BleakBluetoothProxy()
    assert proxy.feature_flags & int(BluetoothProxyFeature.FEATURE_STATE_AND_MODE)
    # ESPHome's current Bluetooth proxy always advertises RAW_ADVERTISEMENTS,
    # and newer HA/ESPHome protocol paths no longer rely on the legacy
    # structured advertisement response. The base proxy reconstructs a valid
    # ADV payload when Bleak does not expose the original HCI bytes.
    assert proxy.feature_flags & int(BluetoothProxyFeature.RAW_ADVERTISEMENTS)


def test_bleak_example_device_exposes_diagnostics_and_bluetooth_info() -> None:
    pytest.importorskip("bleak")
    async def run() -> None:
        from examples.bleak_proxy import build_device

        device = build_device()
        assert device.bluetooth_proxy is not None

        info = await device.build_device_info_response()
        assert (
            info.bluetooth_mac_address == device.bluetooth_proxy.bluetooth_mac_address
        )
        assert info.bluetooth_proxy_feature_flags & int(
            BluetoothProxyFeature.FEATURE_STATE_AND_MODE
        )
        assert info.bluetooth_proxy_feature_flags & int(
            BluetoothProxyFeature.RAW_ADVERTISEMENTS
        )

        responses = [
            await entity.build_list_entities_response() for entity in device.entities
        ]
        text_sensors = [
            response
            for response in responses
            if type(response) is ListEntitiesTextSensorResponse
        ]
        assert len(text_sensors) == 1
        ip_sensor = text_sensors[0]
        assert ip_sensor is not None
        assert ip_sensor.object_id == "ip_address"
        assert ip_sensor.name == "IP address"

        sensors = [
            response
            for response in responses
            if type(response) is ListEntitiesSensorResponse
        ]
        assert len(sensors) == 2
        sensors_by_object_id = {
            sensor.object_id: sensor for sensor in sensors if sensor is not None
        }
        cpu_temperature = sensors_by_object_id["cpu_temperature"]
        assert cpu_temperature is not None
        assert cpu_temperature.object_id == "cpu_temperature"
        assert cpu_temperature.name == "CPU temperature"
        assert cpu_temperature.device_class == "temperature"
        assert cpu_temperature.unit_of_measurement == "°C"
        assert cpu_temperature.accuracy_decimals == 1
        assert cpu_temperature.state_class == SensorStateClass.MEASUREMENT
        assert cpu_temperature.entity_category == EntityCategory.DIAGNOSTIC

        gpu_temperature = sensors_by_object_id["gpu_temperature"]
        assert gpu_temperature.name == "GPU temperature"
        assert gpu_temperature.device_class == "temperature"
        assert gpu_temperature.unit_of_measurement == "°C"
        assert gpu_temperature.accuracy_decimals == 1
        assert gpu_temperature.state_class == SensorStateClass.MEASUREMENT
        assert gpu_temperature.entity_category == EntityCategory.DIAGNOSTIC

        ip_entity = device.get_entity("ip_address")
        assert ip_entity is not None
        state = await ip_entity.build_state_response()
        assert state is not None
        assert state.state
        assert state.key == ip_sensor.key

    asyncio.run(run())


def test_bleak_example_reads_cpu_temperature_zone(tmp_path: Path) -> None:
    pytest.importorskip("bleak")
    from examples.bleak_proxy import (
        _cpu_temperature_path,
        _gpu_temperature_path,
        _read_cpu_temperature,
        _read_gpu_temperature,
    )

    gpu_zone = tmp_path / "thermal_zone0"
    gpu_zone.mkdir()
    (gpu_zone / "type").write_text("gpu-thermal\n", encoding="ascii")
    (gpu_zone / "temp").write_text("61000\n", encoding="ascii")

    cpu_zone = tmp_path / "thermal_zone1"
    cpu_zone.mkdir()
    (cpu_zone / "type").write_text("cpu-thermal\n", encoding="ascii")
    (cpu_zone / "temp").write_text("48750\n", encoding="ascii")

    assert _cpu_temperature_path(tmp_path) == cpu_zone / "temp"
    assert _read_cpu_temperature(tmp_path) == 48.75
    assert _gpu_temperature_path(tmp_path) == gpu_zone / "temp"
    assert _read_gpu_temperature(tmp_path) == 61.0


def test_bluetooth_proxy_normalizes_and_validates_adapter_mac() -> None:
    proxy = BluetoothProxy(bluetooth_mac_address="aa-bb-cc-dd-ee-ff")
    assert proxy.bluetooth_mac_address == "AA:BB:CC:DD:EE:FF"

    try:
        BluetoothProxy(bluetooth_mac_address="not-a-mac")
    except ValueError as error:
        assert "AA:BB:CC:DD:EE:FF" in str(error)
    else:
        raise AssertionError("invalid Bluetooth MAC was accepted")


def test_bleak_example_maps_bluez_random_address_type() -> None:
    pytest.importorskip("bleak")
    from bleak.backends.scanner import AdvertisementData

    from examples.bleak_proxy import _address_type

    advertisement = AdvertisementData(
        local_name=None,
        manufacturer_data={},
        service_data={},
        service_uuids=[],
        tx_power=None,
        rssi=-60,
        platform_data=(
            "/org/bluez/hci0/dev_AA_BB_CC_DD_EE_FF",
            {"AddressType": "random"},
        ),
    )
    with patch("examples.bleak_proxy.sys.platform", "linux"):
        assert _address_type(advertisement) == 1


def test_bleak_example_fallback_identities_are_stable_and_distinct() -> None:
    pytest.importorskip("bleak")
    from examples.bleak_proxy import _stable_host_mac

    device_mac = _stable_host_mac("esphome:bleak-bluetooth-proxy")
    adapter_mac = _stable_host_mac("bluetooth:hci0")
    assert device_mac == _stable_host_mac("esphome:bleak-bluetooth-proxy")
    assert device_mac != adapter_mac
    assert int(device_mac.split(":")[0], 16) & 0x03 == 0x02


def test_bleak_example_passive_scan_uses_bluez_patterns_and_falls_back() -> None:
    pytest.importorskip("bleak")
    from bleak import BleakError

    from examples.bleak_proxy import BleakBluetoothProxy

    class FakeScanner:
        instances: list["FakeScanner"] = []

        def __init__(self, **kwargs: Any) -> None:
            self.kwargs = kwargs
            self.started = False
            self.stopped = False
            self.__class__.instances.append(self)

        async def start(self) -> None:
            if self.kwargs["scanning_mode"] == "passive":
                raise BleakError("passive scanning mode requires bluez or_patterns")
            self.started = True

        async def stop(self) -> None:
            self.stopped = True

    async def run() -> None:
        from examples.bleak_proxy import BleakBluetoothProxy

        proxy = BleakBluetoothProxy(bluez_adapter="hci0")
        with patch("examples.bleak_proxy.BleakScanner", FakeScanner):
            await proxy.start_scan(False)
        assert proxy._scan_mode is BluetoothScannerMode.ACTIVE
        assert len(FakeScanner.instances) == 2
        passive, active = FakeScanner.instances
        assert passive.kwargs["scanning_mode"] == "passive"
        assert passive.kwargs["bluez"]["adapter"] == "hci0"
        assert passive.kwargs["bluez"]["or_patterns"]
        assert passive.stopped is True
        assert active.kwargs["scanning_mode"] == "active"
        assert active.started is True
        await proxy.stop_scan()

    asyncio.run(run())


def test_bleak_example_reports_backend_disconnects() -> None:
    pytest.importorskip("bleak")

    from examples.bleak_proxy import BleakBluetoothProxy

    class FakeBleakClient:
        instances: list["FakeBleakClient"] = []

        def __init__(self, address: str, **kwargs: Any) -> None:
            self.address = address
            self.kwargs = kwargs
            self.mtu_size = 247
            self.__class__.instances.append(self)

        async def connect(self) -> None:
            pass

        async def disconnect(self) -> None:
            pass

    async def run() -> None:
        address = 0xAABBCCDDEE03
        proxy = BleakBluetoothProxy()
        with patch("examples.bleak_proxy.BleakClient", FakeBleakClient):
            owner = MemoryClient()
            await proxy.handle_api_message(_as_client(owner), _connect_request(address))
            assert owner.messages[-1].connected is True

            # The peripheral drops the link on its own; Bleak reports it
            # through the callback the proxy registered.
            client = FakeBleakClient.instances[-1]
            client.kwargs["disconnected_callback"](client)
            async with asyncio.timeout(2):
                while owner.messages[-1].connected:
                    await asyncio.sleep(0)

            # Home Assistant can connect the address again.
            second = MemoryClient()
            await proxy.handle_api_message(_as_client(second), _connect_request(address))
            assert second.messages[-1].connected is True
            assert len(FakeBleakClient.instances) == 2

    asyncio.run(run())
