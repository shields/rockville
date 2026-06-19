# Copyright © 2026 Michael Shields
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Tests for the bridge core."""

import asyncio
import ssl

from aiomqtt import MqttError
from fakes import (
    SAMPLE_TELEMETRY,
    FakeBackend,
    FakeHandle,
    FakeMessage,
    FakeMQTTClient,
    make_config,
)
from roborock.exceptions import RoborockException
from structlog.testing import capture_logs

from rockville.bridge import Bridge
from rockville.config import MetricsConfig, MQTTConfig
from rockville.domain import Command, Telemetry


def build_bridge(
    *, handle=None, authenticated=True, metrics=None, sleep=None, factory=None
):
    handles = {"duid-1": handle} if handle is not None else {}
    backend = FakeBackend(handles, authenticated=authenticated)
    config = make_config(metrics=metrics)
    bridge = Bridge(
        config,
        backend,
        mqtt_factory=factory or (lambda *a, **k: FakeMQTTClient()),
        sleep=sleep or _noop_sleep,
        clock=lambda: 123.0,
        rand=lambda: 0.0,
        version="9.9",
    )
    return bridge, backend


async def _noop_sleep(_delay):
    await asyncio.sleep(0)


def _stop_after_first(bridge):
    async def sleep(_delay):
        bridge._stopping = True
        await asyncio.sleep(0)

    return sleep


def device_of(bridge):
    return bridge._config.devices[0]


# --- command routing -------------------------------------------------------


async def test_on_message_dispatches_start_command():
    handle = FakeHandle()
    bridge, _ = build_bridge(handle=handle)
    await bridge._on_message(FakeMessage("rockville/vac/command/set", b"start"))
    assert handle.commands == [Command.START]
    assert (
        bridge.registry.get_sample_value(
            "rockville_commands_total", {"device": "vac", "command": "start"}
        )
        == 1.0
    )


async def test_on_message_dispatches_fan_speed():
    handle = FakeHandle()
    bridge, _ = build_bridge(handle=handle)
    await bridge._on_message(FakeMessage("rockville/vac/fan_speed/set", b" balanced "))
    assert handle.fan_speeds == ["balanced"]


async def test_on_message_unknown_topic_ignored():
    handle = FakeHandle()
    bridge, _ = build_bridge(handle=handle)
    await bridge._on_message(FakeMessage("rockville/other/thing", b"x"))
    assert handle.commands == []


async def test_on_message_unknown_command_warns():
    handle = FakeHandle()
    bridge, _ = build_bridge(handle=handle)
    await bridge._on_message(FakeMessage("rockville/vac/command/set", b"explode"))
    assert handle.commands == []


async def test_on_message_absent_handle():
    bridge, _ = build_bridge(handle=None)
    await bridge._on_message(FakeMessage("rockville/vac/command/set", b"start"))
    assert (
        bridge.registry.get_sample_value(
            "rockville_mqtt_messages_received_total", {"device": "vac"}
        )
        == 1.0
    )


async def test_on_message_command_error_is_logged():
    handle = FakeHandle(execute_error=RoborockException("boom"))
    bridge, _ = build_bridge(handle=handle)
    await bridge._on_message(FakeMessage("rockville/vac/command/set", b"start"))


async def test_on_message_unknown_fan_speed_warns():
    handle = FakeHandle(fan_ok=False)
    bridge, _ = build_bridge(handle=handle)
    await bridge._on_message(FakeMessage("rockville/vac/fan_speed/set", b"jet"))


async def test_on_message_fan_speed_error_is_logged():
    handle = FakeHandle(fan_error=RoborockException("boom"))
    bridge, _ = build_bridge(handle=handle)
    await bridge._on_message(FakeMessage("rockville/vac/fan_speed/set", b"balanced"))


# --- state and connection --------------------------------------------------


async def test_update_state_publishes_changes_only():
    handle = FakeHandle()
    bridge, _ = build_bridge(handle=handle, metrics=MetricsConfig(port=0))
    client = FakeMQTTClient()
    bridge._client = client
    telemetry = Telemetry(state="cleaning", battery=80)
    await bridge._update_state(device_of(bridge), telemetry)
    published = {topic for topic, _, _ in client.published}
    assert "rockville/vac/state" in published
    assert "rockville/vac/battery" in published
    assert (
        bridge.registry.get_sample_value("rockville_battery_percent", {"device": "vac"})
        == 80.0
    )
    client.published.clear()
    await bridge._update_state(device_of(bridge), telemetry)
    assert client.published == []


async def test_update_state_without_battery():
    bridge, _ = build_bridge(handle=FakeHandle())
    bridge._client = FakeMQTTClient()
    await bridge._update_state(device_of(bridge), Telemetry(state="idle"))
    assert (
        bridge.registry.get_sample_value("rockville_battery_percent", {"device": "vac"})
        is None
    )


async def test_update_state_sets_vacuum_metrics():
    bridge, _ = build_bridge(handle=FakeHandle())
    await bridge._update_state(device_of(bridge), SAMPLE_TELEMETRY)
    get = bridge.registry.get_sample_value
    assert (
        get(
            "rockville_consumable_work_time_seconds",
            {"device": "vac", "consumable": "main_brush"},
        )
        == 180_000.0
    )
    assert (
        get(
            "rockville_consumable_life_seconds",
            {"device": "vac", "consumable": "main_brush"},
        )
        == 1_080_000.0
    )
    assert (
        get(
            "rockville_consumable_life_seconds",
            {"device": "vac", "consumable": "sensor"},
        )
        == 108_000.0
    )
    assert get("rockville_clean_area_square_meters", {"device": "vac"}) == 12.3
    assert get("rockville_clean_time_seconds", {"device": "vac"}) == 845.0
    assert get("rockville_error_code", {"device": "vac"}) == 0.0
    assert get("rockville_state_info", {"device": "vac", "state": "cleaning"}) == 1.0
    assert (
        get("rockville_dock_state_info", {"device": "vac", "dock_state": "idle"}) == 1.0
    )


async def test_update_state_skips_unreported_fields():
    # A freshly discovered device can report battery before state or dock.
    bridge, _ = build_bridge(handle=FakeHandle())
    get = bridge.registry.get_sample_value
    await bridge._update_state(device_of(bridge), Telemetry(battery=50))
    assert get("rockville_battery_percent", {"device": "vac"}) == 50.0
    assert get("rockville_state_info", {"device": "vac", "state": "idle"}) is None


async def test_update_state_info_keeps_only_the_current_value():
    bridge, _ = build_bridge(handle=FakeHandle())
    device = device_of(bridge)
    get = bridge.registry.get_sample_value
    await bridge._update_state(device, Telemetry(state="cleaning"))
    assert get("rockville_state_info", {"device": "vac", "state": "cleaning"}) == 1.0

    # A change removes the stale series, leaving exactly one at 1.
    await bridge._update_state(device, Telemetry(state="idle"))
    assert get("rockville_state_info", {"device": "vac", "state": "cleaning"}) is None
    assert get("rockville_state_info", {"device": "vac", "state": "idle"}) == 1.0

    # An unchanged state re-sets the same series without removing it.
    await bridge._update_state(device, Telemetry(state="idle"))
    assert get("rockville_state_info", {"device": "vac", "state": "idle"}) == 1.0


async def test_update_connection_local_cloud_offline():
    bridge, backend = build_bridge(handle=FakeHandle(online=True, local=True))
    bridge._client = FakeMQTTClient()
    await bridge._update_connection(device_of(bridge))
    assert bridge._state["vac"]["connection"] == "local"
    assert bridge._state["vac"]["availability"] == "online"

    backend._handles["duid-1"] = FakeHandle(online=True, local=False)
    await bridge._update_connection(device_of(bridge))
    assert bridge._state["vac"]["connection"] == "cloud"

    backend._handles.clear()
    await bridge._update_connection(device_of(bridge))
    assert bridge._state["vac"]["connection"] == "offline"
    assert bridge._state["vac"]["availability"] == "offline"

    # an identical update produces no change and skips notifying
    await bridge._update_connection(device_of(bridge))
    assert bridge._state["vac"]["connection"] == "offline"


async def test_update_connection_offline_clears_state_info():
    handle = FakeHandle(online=True, local=True)
    bridge, backend = build_bridge(handle=handle)
    get = bridge.registry.get_sample_value
    await bridge._update_state(
        device_of(bridge), Telemetry(state="cleaning", dock_state="idle")
    )
    assert get("rockville_state_info", {"device": "vac", "state": "cleaning"}) == 1.0

    # The device drops offline; its now-stale state/dock series are cleared.
    backend._handles.clear()
    await bridge._update_connection(device_of(bridge))
    assert get("rockville_state_info", {"device": "vac", "state": "cleaning"}) is None
    assert (
        get("rockville_dock_state_info", {"device": "vac", "dock_state": "idle"})
        is None
    )


async def test_update_connection_sets_auth_error_gauge():
    bridge, _ = build_bridge(handle=FakeHandle(), authenticated=False)
    bridge._client = FakeMQTTClient()
    await bridge._update_connection(device_of(bridge))
    assert bridge.registry.get_sample_value("rockville_auth_error") == 1.0


async def test_publish_without_client_is_noop():
    bridge, _ = build_bridge(handle=FakeHandle())
    await bridge._publish("vac", "state", "cleaning")  # no client; should not raise


async def test_publish_raw_swallows_mqtt_error():
    bridge, _ = build_bridge(handle=FakeHandle())
    client = FakeMQTTClient(publish_error=MqttError("down"))
    await bridge._publish_raw(client, "rockville/x", "y")  # logged, not raised


def test_backoff_delay_applies_jitter():
    bridge, _ = build_bridge(handle=FakeHandle())
    assert bridge._backoff_delay(4.0) == 4.0
    bridge._rand = lambda: 1.0
    assert bridge._backoff_delay(4.0) == 5.0


def test_connect_builds_tls_context():
    backend = FakeBackend({})
    config = make_config()
    tls_mqtt = MQTTConfig(
        host="b",
        port=8883,
        tls=True,
        topic_prefix="rockville",
        display_url="mqtts://b:8883",
    )
    config = config.__class__(
        roborock=config.roborock, mqtt=tls_mqtt, devices=config.devices
    )
    captured = {}

    def factory(*args, **kwargs):
        captured.update(kwargs)
        return FakeMQTTClient()

    bridge = Bridge(config, backend, mqtt_factory=factory)
    bridge._connect()
    assert isinstance(captured["tls_context"], ssl.SSLContext)


def test_status_reports_devices():
    bridge, _ = build_bridge(handle=FakeHandle(online=True, local=False))
    bridge._state["vac"]["state"] = "cleaning"
    status = bridge._status()
    assert status.bridge_name == "rockville"
    assert status.version == "9.9"
    device = status.devices[0]
    assert device.present is True
    assert device.online is True
    assert device.local is False
    assert device.payloads["state"] == "cleaning"


def test_status_absent_device():
    bridge, _ = build_bridge(handle=None)
    device = bridge._status().devices[0]
    assert device.present is False
    assert device.online is False


# --- poll loop -------------------------------------------------------------


async def test_poll_loop_one_iteration_publishes():
    handle = FakeHandle()
    bridge, _ = build_bridge(handle=handle, sleep=None)
    bridge._sleep = _stop_after_first(bridge)
    bridge._client = FakeMQTTClient()
    await bridge._poll_loop(device_of(bridge))
    assert handle.refresh_count == 1
    assert (
        bridge.registry.get_sample_value(
            "rockville_last_poll_timestamp_seconds", {"device": "vac"}
        )
        == 123.0
    )


async def test_poll_loop_handles_refresh_error():
    handle = FakeHandle(refresh_error=RoborockException("nope"))
    bridge, _ = build_bridge(handle=handle)
    bridge._sleep = _stop_after_first(bridge)
    bridge._client = FakeMQTTClient()
    await bridge._poll_loop(device_of(bridge))
    assert (
        bridge.registry.get_sample_value(
            "rockville_poll_errors_total", {"device": "vac"}
        )
        == 1.0
    )


async def test_poll_loop_absent_handle():
    bridge, _ = build_bridge(handle=None)
    bridge._sleep = _stop_after_first(bridge)
    bridge._client = FakeMQTTClient()
    await bridge._poll_loop(device_of(bridge))
    assert bridge._state["vac"]["connection"] == "offline"


# --- mqtt loop -------------------------------------------------------------


async def test_mqtt_loop_processes_messages_then_reconnects():
    handle = FakeHandle()
    messages = [
        FakeMessage("rockville/vac/command/set", b"start"),
        FakeMessage("rockville/vac/fan_speed/set", b"balanced"),
    ]
    client = FakeMQTTClient(queued=messages, disconnect_error=MqttError("drop"))
    bridge, _ = build_bridge(handle=handle, factory=lambda *a, **k: client)
    bridge._sleep = _stop_after_first(bridge)
    await bridge._mqtt_loop()
    assert client.entered and client.exited
    assert "rockville/bridge/availability" in {t for t, _, _ in client.published}
    assert "rockville/vac/command/set" in client.subscriptions
    assert handle.commands == [Command.START]
    assert handle.fan_speeds == ["balanced"]
    assert bridge.registry.get_sample_value("rockville_mqtt_connected") == 0.0


async def test_mqtt_loop_clean_stream_end():
    client = FakeMQTTClient(queued=[], disconnect_error=None)
    bridge, _ = build_bridge(handle=FakeHandle(), factory=lambda *a, **k: client)
    bridge._sleep = _stop_after_first(bridge)
    await bridge._mqtt_loop()
    assert client.exited


async def test_mqtt_loop_backoff_grows_when_on_connect_fails():
    # A broker that accepts the socket but rejects _on_connect must keep backing
    # off; backoff resets only after a fully successful connect, not before.
    client = FakeMQTTClient()
    bridge, _ = build_bridge(handle=FakeHandle(), factory=lambda *a, **k: client)

    async def failing_on_connect(_client):
        msg = "subscribe rejected"
        raise MqttError(msg)

    bridge._on_connect = failing_on_connect
    delays: list[float] = []

    async def sleep(delay):
        delays.append(delay)
        if len(delays) >= 3:
            bridge._stopping = True
        await asyncio.sleep(0)

    bridge._sleep = sleep
    await bridge._mqtt_loop()
    assert delays == [1.0, 2.0, 4.0]


# --- lifecycle -------------------------------------------------------------


async def test_start_and_shutdown_without_metrics():
    client = FakeMQTTClient(disconnect_error=MqttError("drop"))
    bridge, backend = build_bridge(handle=FakeHandle(), factory=lambda *a, **k: client)
    bridge._sleep = _stop_after_first(bridge)
    await bridge.start()
    assert backend.started
    await asyncio.sleep(0.05)
    bridge._client = client
    await bridge.shutdown()
    assert backend.closed
    assert "rockville/bridge/availability" in {t for t, _, _ in client.published}


async def test_start_and_shutdown_with_metrics():
    client = FakeMQTTClient(disconnect_error=MqttError("drop"))
    bridge, backend = build_bridge(
        handle=FakeHandle(),
        metrics=MetricsConfig(port=0, bind="127.0.0.1"),
        factory=lambda *a, **k: client,
    )
    bridge._sleep = _stop_after_first(bridge)
    await bridge.start()
    await asyncio.sleep(0.05)
    await bridge.shutdown()
    assert backend.closed


async def test_shutdown_without_client():
    bridge, backend = build_bridge(handle=FakeHandle())
    await bridge.shutdown()
    assert backend.closed


async def test_shutdown_logs_unexpected_task_error():
    # A loop that died from a non-cancellation error must not vanish silently.
    bridge, backend = build_bridge(handle=FakeHandle())

    async def boom():
        raise ValueError("unexpected")

    task = asyncio.create_task(boom())
    await asyncio.sleep(0)  # let it run and fail before shutdown gathers it
    bridge._tasks = [task]
    with capture_logs() as logs:
        await bridge.shutdown()
    assert backend.closed
    assert any(
        entry["event"] == "background task exited with an error" for entry in logs
    )


async def test_on_connect_republishes_cached_state():
    bridge, _ = build_bridge(handle=FakeHandle())
    client = FakeMQTTClient()
    bridge._client = client
    bridge._state["vac"]["state"] = "cleaning"
    await bridge._on_connect(client)
    topics = {topic for topic, _, _ in client.published}
    assert "rockville/bridge/availability" in topics
    assert "rockville/vac/state" in topics
    assert "rockville/vac/command/set" in client.subscriptions


async def test_mqtt_loop_breaks_when_stopped_mid_cycle():
    client = FakeMQTTClient(
        queued=[FakeMessage("rockville/vac/command/set", b"start")],
        disconnect_error=MqttError("drop"),
    )
    bridge, _ = build_bridge(handle=FakeHandle(), factory=lambda *a, **k: client)

    async def stop_handler(_message):
        bridge._stopping = True

    bridge._on_message = stop_handler
    await bridge._mqtt_loop()
    assert client.exited


async def test_graceful_shutdown_announces_offline():
    client = FakeMQTTClient(block=True)
    bridge, backend = build_bridge(
        handle=FakeHandle(),
        factory=lambda *a, **k: client,
        sleep=lambda _d: asyncio.sleep(1),
    )
    await bridge.start()
    await asyncio.sleep(0.05)  # let the loop connect and park on client.messages
    await bridge.shutdown()
    published = [(topic, payload) for topic, payload, _ in client.published]
    assert ("rockville/bridge/availability", "offline") in published
    assert ("rockville/vac/availability", "offline") in published
    assert backend.closed is True
