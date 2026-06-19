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

"""The bridge core: Roborock telemetry and control over MQTT.

The bridge polls each configured vacuum on an interval, publishes changed state
to retained topics, subscribes to command topics, and dispatches commands back
to the vacuum. The MQTT connection is maintained by a reconnect loop with
jittered exponential backoff; a Last-Will message flips the bridge's
availability topic to `offline` if the process dies. All python-roborock access
goes through the injected `Backend`, so the bridge is tested without the
library or a real broker.
"""

from __future__ import annotations

import asyncio
import random
import ssl
import time
from collections.abc import Callable
from typing import TYPE_CHECKING

import aiomqtt
from prometheus_client import CollectorRegistry
from roborock.exceptions import RoborockException

from . import convert
from .log import get_logger
from .metrics import StatusServer, create_metrics
from .status_page import DeviceStatus, StatusData

if TYPE_CHECKING:
    from collections.abc import Awaitable

    from prometheus_client import Gauge

    from .config import Config, DeviceConfig
    from .domain import Telemetry
    from .roborock_client import Backend, VacuumHandle

_log = get_logger(__name__)

_MIN_BACKOFF = 1.0
_MAX_BACKOFF = 60.0
_BACKOFF_FACTOR = 2.0
_JITTER_FRACTION = 0.25
_BRIDGE_NAME = "rockville"
_DEFAULT_BIND = "0.0.0.0"  # noqa: S104 — operators bind metrics broadly on purpose

_DEFAULT_RAND = random.random

MQTTClientFactory = Callable[..., aiomqtt.Client]


class Bridge:
    """Bridges configured Roborock vacuums to an MQTT broker."""

    def __init__(
        self,
        config: Config,
        backend: Backend,
        *,
        mqtt_factory: MQTTClientFactory = aiomqtt.Client,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.time,
        rand: Callable[[], float] = _DEFAULT_RAND,
        version: str = "unknown",
    ) -> None:
        """Create a bridge; the injectable factories and clocks aid testing."""
        self._config = config
        self._backend = backend
        self._mqtt_factory = mqtt_factory
        self._sleep = sleep
        self._clock = clock
        self._rand = rand
        self._version = version
        self._registry = CollectorRegistry()
        self._metrics = create_metrics(self._registry)
        self._status_server = (
            StatusServer(self._registry, self._status) if config.metrics else None
        )
        self._state: dict[str, dict[str, str]] = {d.name: {} for d in config.devices}
        # The current label value of each single-series _info gauge, per device,
        # so a change can remove the stale series (see _set_current).
        self._last_state: dict[str, str] = {}
        self._last_dock: dict[str, str] = {}
        self._client: aiomqtt.Client | None = None
        self._mqtt_connected = False
        self._stopping = False
        self._tasks: list[asyncio.Task[None]] = []
        self._bridge_availability = f"{config.mqtt.topic_prefix}/bridge/availability"
        self._command_topics: dict[str, tuple[DeviceConfig, str]] = {}
        for device in config.devices:
            self._command_topics[self._topic(device.name, "command/set")] = (
                device,
                "command",
            )
            self._command_topics[self._topic(device.name, "fan_speed/set")] = (
                device,
                "fan_speed",
            )

    @property
    def registry(self) -> CollectorRegistry:
        """The Prometheus registry, exposed for testing."""
        return self._registry

    async def start(self) -> None:
        """Connect the backend, start serving metrics, and launch the loops."""
        await self._backend.start()
        self._metrics.auth_error.set(0 if self._backend.authenticated else 1)
        metrics = self._config.metrics
        if metrics is not None and self._status_server is not None:
            await self._status_server.start(metrics.bind or _DEFAULT_BIND, metrics.port)
        self._tasks.append(asyncio.create_task(self._mqtt_loop()))
        self._tasks.extend(
            asyncio.create_task(self._poll_loop(device))
            for device in self._config.devices
        )

    async def shutdown(self) -> None:
        """Stop the loops and tear everything down.

        The MQTT task announces ``offline`` from inside its own live connection
        when cancelled (see ``_mqtt_loop``); doing it here would race that task
        clearing and closing ``self._client``.
        """
        self._stopping = True
        for task in self._tasks:
            task.cancel()
        results = await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        for result in results:
            # A loop that died from an unexpected (non-cancellation) error would
            # otherwise vanish silently here; surface it so a supervisor sees it.
            # CancelledError is a BaseException, not an Exception, so the expected
            # shutdown cancellations are excluded.
            if isinstance(result, Exception):
                _log.error("background task exited with an error", exc_info=result)
        await self._backend.close()
        if self._status_server is not None:
            await self._status_server.close()

    def _topic(self, name: str, suffix: str) -> str:
        return f"{self._config.mqtt.topic_prefix}/{name}/{suffix}"

    def _connect(self) -> aiomqtt.Client:
        mqtt = self._config.mqtt
        will = aiomqtt.Will(
            topic=self._bridge_availability, payload="offline", retain=True
        )
        tls_context = ssl.create_default_context() if mqtt.tls else None
        return self._mqtt_factory(
            mqtt.host,
            mqtt.port,
            username=mqtt.username,
            password=mqtt.password,
            will=will,
            tls_context=tls_context,
        )

    async def _mqtt_loop(self) -> None:
        backoff = _MIN_BACKOFF
        while not self._stopping:
            try:
                async with self._connect() as client:
                    self._client = client
                    self._mqtt_connected = True
                    self._metrics.mqtt_connected.set(1)
                    try:
                        await self._on_connect(client)
                        # Reset only after a fully successful connect (subscribe +
                        # initial publish). A broker that accepts the socket but
                        # then rejects _on_connect must keep backing off rather
                        # than retry-storming at the minimum delay.
                        backoff = _MIN_BACKOFF
                        async for message in client.messages:
                            await self._on_message(message)
                    except asyncio.CancelledError:
                        # Graceful shutdown: announce offline while still connected.
                        await self._announce_offline(client)
                        raise
            except aiomqtt.MqttError as err:
                _log.warning("mqtt connection lost", error=str(err))
            finally:
                self._client = None
                self._mqtt_connected = False
                self._metrics.mqtt_connected.set(0)
                self._notify()
            if self._stopping:
                break
            await self._sleep(self._backoff_delay(backoff))
            backoff = min(backoff * _BACKOFF_FACTOR, _MAX_BACKOFF)

    async def _announce_offline(self, client: aiomqtt.Client) -> None:
        await self._publish_raw(client, self._bridge_availability, "offline")
        for device in self._config.devices:
            await self._publish_raw(
                client, self._topic(device.name, "availability"), "offline"
            )

    def _backoff_delay(self, base: float) -> float:
        return base + base * _JITTER_FRACTION * self._rand()

    async def _on_connect(self, client: aiomqtt.Client) -> None:
        _log.info("mqtt connected", url=self._config.mqtt.display_url)
        await self._publish_raw(client, self._bridge_availability, "online")
        for topic in self._command_topics:
            await client.subscribe(topic)
        for name, cache in self._state.items():
            for suffix, value in cache.items():
                await self._publish_raw(client, self._topic(name, suffix), value)
        for device in self._config.devices:
            await self._update_connection(device)
        self._notify()

    async def _on_message(self, message: aiomqtt.Message) -> None:
        entry = self._command_topics.get(str(message.topic.value))
        if entry is None:
            return
        device, kind = entry
        payload = message.payload
        text = (
            payload.decode(errors="replace")
            if isinstance(payload, (bytes, bytearray))
            else str(payload)
        )
        self._metrics.messages_received.labels(device=device.name).inc()
        handle = self._backend.handle(device.duid)
        if handle is None:
            _log.warning("command for absent device", device=device.name)
            return
        if kind == "command":
            await self._dispatch_command(device, handle, text)
        else:
            await self._dispatch_fan_speed(device, handle, text)

    async def _dispatch_command(
        self, device: DeviceConfig, handle: VacuumHandle, text: str
    ) -> None:
        command = convert.command_from_mqtt(text)
        if command is None:
            _log.warning("ignoring unknown command", device=device.name, payload=text)
            return
        self._metrics.commands.labels(device=device.name, command=command.value).inc()
        try:
            await handle.execute(command)
        except RoborockException as err:
            _log.error(
                "command failed",
                device=device.name,
                command=command.value,
                error=str(err),
            )

    async def _dispatch_fan_speed(
        self, device: DeviceConfig, handle: VacuumHandle, text: str
    ) -> None:
        try:
            ok = await handle.set_fan_speed(text.strip())
        except RoborockException as err:
            _log.error("set fan speed failed", device=device.name, error=str(err))
            return
        if not ok:
            _log.warning("ignoring unknown fan speed", device=device.name, payload=text)

    async def _poll_loop(self, device: DeviceConfig) -> None:
        while not self._stopping:
            handle = self._backend.handle(device.duid)
            if handle is not None:
                try:
                    telemetry = await handle.refresh()
                except RoborockException as err:
                    self._metrics.poll_errors.labels(device=device.name).inc()
                    _log.warning("poll failed", device=device.name, error=str(err))
                else:
                    await self._update_state(device, telemetry)
                    self._metrics.last_poll.labels(device=device.name).set(
                        self._clock()
                    )
            await self._update_connection(device)
            await self._sleep(self._config.poll_interval)

    async def _update_state(self, device: DeviceConfig, telemetry: Telemetry) -> None:
        changed = False
        for suffix, value in convert.telemetry_payloads(telemetry).items():
            changed = await self._set(device.name, suffix, value) or changed
        self._update_metrics(device.name, telemetry)
        if changed:
            self._notify()

    def _update_metrics(self, name: str, telemetry: Telemetry) -> None:
        """Mirror a telemetry snapshot into the device's Prometheus gauges.

        Only fields the device reported are set, matching the change-only MQTT
        publishing, so a gauge is never set from missing data.
        """
        if telemetry.battery is not None:
            self._metrics.battery.labels(device=name).set(telemetry.battery)
        for consumable, work_time, life in convert.consumable_metrics(telemetry):
            self._metrics.consumable_work_time.labels(
                device=name, consumable=consumable
            ).set(work_time)
            self._metrics.consumable_life.labels(
                device=name, consumable=consumable
            ).set(life)
        if telemetry.clean_area_m2 is not None:
            self._metrics.clean_area.labels(device=name).set(telemetry.clean_area_m2)
        if telemetry.clean_time_s is not None:
            self._metrics.clean_time.labels(device=name).set(telemetry.clean_time_s)
        if telemetry.error_code is not None:
            self._metrics.error_code.labels(device=name).set(telemetry.error_code)
        if telemetry.state is not None:
            self._set_current(
                self._metrics.state_info, self._last_state, name, telemetry.state
            )
        if telemetry.dock_state is not None:
            self._set_current(
                self._metrics.dock_state_info,
                self._last_dock,
                name,
                telemetry.dock_state,
            )

    @staticmethod
    def _set_current(gauge: Gauge, last: dict[str, str], name: str, value: str) -> None:
        """Point an `_info` gauge at `value`, dropping the device's prior series.

        These gauges encode a categorical value as a label set to 1. Removing
        the previous label series keeps exactly one series per device, so a stat
        panel reducing to last-not-null is unambiguous.
        """
        previous = last.get(name)
        if previous is not None and previous != value:
            gauge.remove(name, previous)
        gauge.labels(name, value).set(1)
        last[name] = value

    @staticmethod
    def _clear_current(gauge: Gauge, last: dict[str, str], name: str) -> None:
        """Drop a device's `_info` series — used when it goes offline.

        `_set_current` only removes the stale series when a poll succeeds with a
        changed value; once a device disconnects, polling stops, so without this
        the gauge would report the last-seen state (e.g. cleaning) indefinitely.
        """
        previous = last.pop(name, None)
        if previous is not None:
            gauge.remove(name, previous)

    async def _update_connection(self, device: DeviceConfig) -> None:
        handle = self._backend.handle(device.duid)
        online = handle is not None and handle.online
        local = handle is not None and handle.local
        self._metrics.roborock_connected.labels(device=device.name).set(
            1 if online else 0
        )
        self._metrics.roborock_local.labels(device=device.name).set(1 if local else 0)
        self._metrics.auth_error.set(0 if self._backend.authenticated else 1)
        # An offline device has no current state; clear the info gauges so they
        # don't report a stale state/dock long after the vacuum disconnected.
        if not online:
            self._clear_current(self._metrics.state_info, self._last_state, device.name)
            self._clear_current(
                self._metrics.dock_state_info, self._last_dock, device.name
            )
        connection = "local" if local else "cloud" if online else "offline"
        changed = await self._set(device.name, "connection", connection)
        changed = (
            await self._set(
                device.name, "availability", convert.availability_payload(online=online)
            )
            or changed
        )
        if changed:
            self._notify()

    async def _set(self, name: str, suffix: str, value: str) -> bool:
        cache = self._state[name]
        if cache.get(suffix) == value:
            return False
        cache[suffix] = value
        await self._publish(name, suffix, value)
        return True

    async def _publish(self, name: str, suffix: str, value: str) -> None:
        client = self._client
        if client is None:
            return
        await self._publish_raw(client, self._topic(name, suffix), value)

    async def _publish_raw(
        self, client: aiomqtt.Client, topic: str, payload: str
    ) -> None:
        try:
            await client.publish(topic, payload, retain=True)
            self._metrics.messages_published.inc()
        except aiomqtt.MqttError as err:
            _log.warning("publish failed", topic=topic, error=str(err))

    def _notify(self) -> None:
        if self._status_server is not None:
            self._status_server.notify()

    def _device_status(self, device: DeviceConfig) -> DeviceStatus:
        handle = self._backend.handle(device.duid)
        return DeviceStatus(
            name=device.name,
            duid=device.duid,
            present=handle is not None,
            online=handle.online if handle is not None else False,
            local=handle.local if handle is not None else False,
            payloads=dict(self._state[device.name]),
        )

    def _status(self) -> StatusData:
        return StatusData(
            bridge_name=_BRIDGE_NAME,
            version=self._version,
            mqtt_url=self._config.mqtt.display_url,
            mqtt_connected=self._mqtt_connected,
            roborock_authenticated=self._backend.authenticated,
            devices=tuple(
                self._device_status(device) for device in self._config.devices
            ),
        )
