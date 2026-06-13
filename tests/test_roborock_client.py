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

"""Tests for the python-roborock adapter."""

import asyncio
import contextlib
from pathlib import Path
from types import SimpleNamespace

import pytest
from fakes import make_config
from roborock.data import Reference, RRiot, UserData
from roborock.roborock_typing import RoborockCommand

from rockville import auth, roborock_client
from rockville.config import DeviceConfig
from rockville.domain import Command
from rockville.errors import AuthError
from rockville.roborock_client import DeviceManagerBackend, _DeviceHandle


def sample_user_data() -> UserData:
    return UserData(
        rriot=RRiot(u="u", s="s", h="h", k="k", r=Reference(a="https://api"))
    )


class FakeStatus:
    def __init__(self, *, error: bool = True):
        self.state_name = "cleaning"
        self.battery = 80
        self.fan_speed_name = "balanced"
        self.error_code = SimpleNamespace(value=0) if error else None
        self.error_code_name = "none" if error else None
        self.square_meter_clean_area = 12.3
        self.clean_time = 845
        self.dock_state = SimpleNamespace(value="idle")
        self.fan_speed_mapping = {101: "quiet", 102: "balanced"}
        self.refreshed = 0

    async def refresh(self) -> None:
        self.refreshed += 1


class FakeConsumables:
    def __init__(self):
        self.main_brush_work_time = 0
        self.side_brush_work_time = 0
        self.filter_work_time = 0
        self.sensor_dirty_time = 0
        self.refreshed = 0

    async def refresh(self) -> None:
        self.refreshed += 1


class FakeCommandTrait:
    def __init__(self):
        self.sent = []

    async def send(self, command, params=None):
        self.sent.append((command, params))


class FakeV1:
    def __init__(self, *, error: bool = True):
        self.status = FakeStatus(error=error)
        self.consumables = FakeConsumables()
        self.command = FakeCommandTrait()


class FakeDevice:
    def __init__(self, duid, *, connected=True, local=True, v1=None):
        self.duid = duid
        self.is_connected = connected
        self.is_local_connected = local
        self.v1_properties = v1


class FakeManager:
    def __init__(self, devices):
        self._devices = devices
        self.closed = False

    async def get_devices(self):
        return self._devices

    async def close(self):
        self.closed = True


# --- _DeviceHandle ---------------------------------------------------------


async def test_handle_refresh_maps_telemetry():
    v1 = FakeV1()
    handle = _DeviceHandle(FakeDevice("d", connected=True, local=True), v1)
    telemetry = await handle.refresh()
    assert v1.status.refreshed == 1
    assert v1.consumables.refreshed == 1
    assert telemetry.state == "cleaning"
    assert telemetry.battery == 80
    assert telemetry.fan_speed == "balanced"
    assert telemetry.error_code == 0
    assert telemetry.error_name == "none"
    assert telemetry.clean_area_m2 == 12.3
    assert telemetry.clean_time_s == 845
    assert telemetry.dock_state == "idle"
    assert telemetry.main_brush_work_time == 0
    assert handle.online is True
    assert handle.local is True


async def test_handle_refresh_with_no_error_code():
    handle = _DeviceHandle(FakeDevice("d"), FakeV1(error=False))
    telemetry = await handle.refresh()
    assert telemetry.error_code is None
    assert telemetry.error_name is None


async def test_handle_execute_maps_commands():
    v1 = FakeV1()
    handle = _DeviceHandle(FakeDevice("d"), v1)
    await handle.execute(Command.START)
    await handle.execute(Command.RETURN)
    assert v1.command.sent == [
        (RoborockCommand.APP_START, None),
        (RoborockCommand.APP_CHARGE, None),
    ]


async def test_handle_set_fan_speed_known():
    v1 = FakeV1()
    handle = _DeviceHandle(FakeDevice("d"), v1)
    assert await handle.set_fan_speed("balanced") is True
    assert v1.command.sent == [(RoborockCommand.SET_CUSTOM_MODE, [102])]


async def test_handle_set_fan_speed_unknown():
    v1 = FakeV1()
    handle = _DeviceHandle(FakeDevice("d"), v1)
    assert await handle.set_fan_speed("jet") is False
    assert v1.command.sent == []


# --- DeviceManagerBackend --------------------------------------------------


def _factory(manager_box: dict):
    async def factory(params, **kwargs):
        manager_box["params"] = params
        manager_box["kwargs"] = kwargs
        manager = manager_box["next"]()
        manager_box["last"] = manager
        return manager

    return factory


async def test_start_with_saved_credentials(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(auth, "load_user_data", lambda _p: sample_user_data())
    config = make_config(persist=tmp_path)
    box = {"next": lambda: FakeManager([FakeDevice("duid-1", v1=FakeV1())])}
    backend = DeviceManagerBackend(config, manager_factory=_factory(box))

    await backend.start()
    try:
        assert backend.authenticated is True
        assert backend.handle("duid-1") is not None
        assert backend.handle("missing") is None
        assert (
            box["kwargs"]["mqtt_session_unauthorized_hook"] == backend._on_unauthorized
        )
        assert box["kwargs"]["prefer_cache"] is True
    finally:
        await backend.close()
    assert box["last"].closed is True


async def test_start_with_password_login(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(auth, "load_user_data", lambda _p: None)
    calls = []

    async def fake_password_login(email, password, persist):
        calls.append((email, password, persist))
        return sample_user_data()

    monkeypatch.setattr(auth, "password_login", fake_password_login)
    config = make_config(persist=tmp_path, password="secret")
    box = {"next": lambda: FakeManager([FakeDevice("duid-1", v1=FakeV1())])}
    backend = DeviceManagerBackend(config, manager_factory=_factory(box))

    await backend.start()
    await backend.close()
    assert calls == [("me@example.com", "secret", tmp_path)]


async def test_start_without_credentials_raises(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(auth, "load_user_data", lambda _p: None)
    config = make_config(persist=tmp_path, password=None)
    backend = DeviceManagerBackend(config, manager_factory=_factory({"next": dict}))
    with pytest.raises(AuthError, match="rockville login"):
        await backend.start()


async def test_seed_static_ips(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(auth, "load_user_data", lambda _p: sample_user_data())
    config = make_config(
        persist=tmp_path,
        devices=(DeviceConfig(name="vac", duid="duid-1", ip="192.168.1.9"),),
    )
    box = {"next": lambda: FakeManager([FakeDevice("duid-1", v1=FakeV1())])}
    backend = DeviceManagerBackend(config, manager_factory=_factory(box))

    await backend.start()
    try:
        cache = box["kwargs"]["cache"]
        data = await cache.get()
        assert data.device_info["duid-1"].network_info.ip == "192.168.1.9"
    finally:
        await backend.close()


async def test_build_handles_skips_absent_and_non_v1(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(auth, "load_user_data", lambda _p: sample_user_data())
    config = make_config(
        persist=tmp_path,
        devices=(
            DeviceConfig(name="vac", duid="duid-1"),
            DeviceConfig(name="ghost", duid="duid-2"),
            DeviceConfig(name="dryer", duid="duid-3"),
        ),
    )
    devices = [FakeDevice("duid-1", v1=FakeV1()), FakeDevice("duid-3", v1=None)]
    box = {"next": lambda: FakeManager(devices)}
    backend = DeviceManagerBackend(config, manager_factory=_factory(box))

    await backend.start()
    try:
        assert backend.handle("duid-1") is not None
        assert backend.handle("duid-2") is None
        assert backend.handle("duid-3") is None
    finally:
        await backend.close()


def _idle_backend(
    tmp_path: Path, *, password: str | None = "secret"
) -> DeviceManagerBackend:
    config = make_config(persist=tmp_path, password=password)
    box = {"next": lambda: FakeManager([FakeDevice("duid-1", v1=FakeV1())])}
    backend = DeviceManagerBackend(config, manager_factory=_factory(box))
    backend._box = box  # type: ignore[attr-defined]
    return backend


async def test_on_unauthorized_schedules_reauth(tmp_path: Path):
    backend = _idle_backend(tmp_path)
    backend._loop = asyncio.get_running_loop()
    backend._authenticated = True
    backend._on_unauthorized()
    assert backend.authenticated is False
    await asyncio.sleep(0)
    assert backend._reauth.is_set()


def test_on_unauthorized_without_loop(tmp_path: Path):
    backend = _idle_backend(tmp_path)
    backend._loop = None
    backend._on_unauthorized()
    assert backend.authenticated is False
    assert not backend._reauth.is_set()


async def test_close_without_start_is_safe(tmp_path: Path):
    backend = _idle_backend(tmp_path)
    await backend.close()


async def test_reauthenticate_without_password(tmp_path: Path):
    backend = _idle_backend(tmp_path, password=None)
    assert await backend._reauthenticate() is False


async def test_reauthenticate_success(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(auth, "load_user_data", lambda _p: sample_user_data())
    backend = _idle_backend(tmp_path)
    await backend.start()
    try:
        first_manager = backend._box["last"]  # type: ignore[attr-defined]

        async def fake_password_login(*_args):
            return sample_user_data()

        monkeypatch.setattr(auth, "password_login", fake_password_login)
        backend._authenticated = False
        assert await backend._reauthenticate() is True
        assert backend.authenticated is True
        assert first_manager.closed is True
    finally:
        await backend.close()


async def test_reauthenticate_failure(monkeypatch, tmp_path: Path):
    backend = _idle_backend(tmp_path)

    async def fake_password_login(*_args):
        raise AuthError("nope")

    monkeypatch.setattr(auth, "password_login", fake_password_login)
    assert await backend._reauthenticate() is False


async def test_supervise_success_branch(tmp_path: Path):
    backend = _idle_backend(tmp_path)

    async def fake_reauth():
        await asyncio.sleep(0)
        return True

    backend._reauthenticate = fake_reauth  # type: ignore[method-assign]
    backend._reauth.set()
    task = asyncio.create_task(backend._supervise())
    await asyncio.sleep(0.02)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    assert not backend._reauth.is_set()


async def test_supervise_failure_backs_off(tmp_path: Path):
    backend = _idle_backend(tmp_path)
    seen: list[float] = []

    async def fake_reauth():
        await asyncio.sleep(0)
        return False

    async def fake_sleep(delay):
        seen.append(delay)
        await asyncio.sleep(0)

    backend._reauthenticate = fake_reauth  # type: ignore[method-assign]
    backend._sleep = fake_sleep  # type: ignore[method-assign]
    backend._reauth.set()
    task = asyncio.create_task(backend._supervise())
    await asyncio.sleep(0.02)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    assert seen
    assert seen[0] == roborock_client._REAUTH_MIN_BACKOFF
