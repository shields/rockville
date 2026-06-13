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

"""Test doubles and a config factory shared across the test suite."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Self

from rockville.config import (
    Config,
    DeviceConfig,
    MetricsConfig,
    MQTTConfig,
    RoborockConfig,
)
from rockville.domain import Telemetry

if TYPE_CHECKING:
    from collections.abc import Sequence

    from rockville.domain import Command

SAMPLE_TELEMETRY = Telemetry(
    state="cleaning",
    battery=80,
    fan_speed="balanced",
    error_code=0,
    error_name="none",
    clean_area_m2=12.3,
    clean_time_s=845,
    dock_state="idle",
    main_brush_work_time=180_000,
    side_brush_work_time=72_000,
    filter_work_time=54_000,
    sensor_dirty_time=10_800,
)


def make_config(
    *,
    devices: Sequence[DeviceConfig] | None = None,
    metrics: MetricsConfig | None = None,
    persist: Path = Path("/persist"),
    password: str | None = "secret",
    poll_interval: float = 30.0,
    topic_prefix: str = "rockville",
) -> Config:
    """Build a `Config` with sensible defaults for tests."""
    if devices is None:
        devices = (DeviceConfig(name="vac", duid="duid-1", ip=None),)
    return Config(
        roborock=RoborockConfig(
            email="me@example.com", persist_path=persist, password=password
        ),
        mqtt=MQTTConfig(
            host="broker.local",
            port=1883,
            tls=False,
            topic_prefix=topic_prefix,
            username=None,
            password=None,
            display_url="mqtt://broker.local:1883",
        ),
        devices=tuple(devices),
        poll_interval=poll_interval,
        metrics=metrics,
    )


class FakeHandle:
    """A `VacuumHandle` test double."""

    def __init__(
        self,
        *,
        online: bool = True,
        local: bool = True,
        telemetry: Telemetry | None = None,
        refresh_error: Exception | None = None,
        execute_error: Exception | None = None,
        fan_ok: bool = True,
        fan_error: Exception | None = None,
    ) -> None:
        self._online = online
        self._local = local
        self._telemetry = telemetry if telemetry is not None else SAMPLE_TELEMETRY
        self._refresh_error = refresh_error
        self._execute_error = execute_error
        self._fan_ok = fan_ok
        self._fan_error = fan_error
        self.commands: list[Command] = []
        self.fan_speeds: list[str] = []
        self.refresh_count = 0

    @property
    def online(self) -> bool:
        return self._online

    @property
    def local(self) -> bool:
        return self._local

    async def refresh(self) -> Telemetry:
        self.refresh_count += 1
        if self._refresh_error is not None:
            raise self._refresh_error
        return self._telemetry

    async def execute(self, command: Command) -> None:
        if self._execute_error is not None:
            raise self._execute_error
        self.commands.append(command)

    async def set_fan_speed(self, name: str) -> bool:
        if self._fan_error is not None:
            raise self._fan_error
        self.fan_speeds.append(name)
        return self._fan_ok


class FakeBackend:
    """A `Backend` test double."""

    def __init__(
        self,
        handles: dict[str, FakeHandle] | None = None,
        *,
        authenticated: bool = True,
        start_error: Exception | None = None,
    ) -> None:
        self._handles = handles or {}
        self._authenticated = authenticated
        self._start_error = start_error
        self.started = False
        self.closed = False

    @property
    def authenticated(self) -> bool:
        return self._authenticated

    async def start(self) -> None:
        if self._start_error is not None:
            raise self._start_error
        self.started = True

    async def close(self) -> None:
        self.closed = True

    def handle(self, duid: str) -> FakeHandle | None:
        return self._handles.get(duid)


class FakeMessage:
    """A stand-in for an aiomqtt `Message`."""

    def __init__(self, topic: str, payload: bytes) -> None:
        self.topic = SimpleNamespace(value=topic)
        self.payload = payload


class _MessageIterator:
    def __init__(self, client: FakeMQTTClient) -> None:
        self._client = client
        self._index = 0

    def __aiter__(self) -> Self:
        return self

    async def __anext__(self) -> FakeMessage:
        client = self._client
        if self._index < len(client.queued):
            message = client.queued[self._index]
            self._index += 1
            return message
        if client.disconnect_error is not None:
            raise client.disconnect_error
        if client.block:
            await asyncio.Event().wait()  # park until the task is cancelled
        raise StopAsyncIteration


class FakeMQTTClient:
    """An aiomqtt `Client` test double usable as an async context manager."""

    def __init__(
        self,
        *args: object,
        queued: Sequence[FakeMessage] = (),
        disconnect_error: Exception | None = None,
        publish_error: Exception | None = None,
        block: bool = False,
        **kwargs: object,
    ) -> None:
        self.args = args
        self.kwargs = kwargs
        self.queued = list(queued)
        self.disconnect_error = disconnect_error
        self.publish_error = publish_error
        self.block = block
        self.entered = False
        self.exited = False
        self.published: list[tuple[str, object, bool]] = []
        self.subscriptions: list[str] = []

    async def __aenter__(self) -> Self:
        self.entered = True
        return self

    async def __aexit__(self, *_args: object) -> None:
        self.exited = True

    @property
    def messages(self) -> _MessageIterator:
        return _MessageIterator(self)

    async def subscribe(self, topic: str, **_kwargs: object) -> None:
        self.subscriptions.append(topic)

    async def publish(
        self,
        topic: str,
        payload: object = None,
        *,
        retain: bool = False,
        **_kwargs: object,
    ) -> None:
        if self.publish_error is not None:
            raise self.publish_error
        self.published.append((topic, payload, retain))
