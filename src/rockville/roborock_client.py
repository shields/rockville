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

"""Adapter over python-roborock — the only module that imports the library.

`bridge` depends on the `Backend` and `VacuumHandle` protocols defined here, so
the bridge and its tests never touch python-roborock. `DeviceManagerBackend`
owns the account-level `DeviceManager`, prefers the local LAN connection,
translates library traits into a `Telemetry` snapshot and a `Command` into a
`RoborockCommand`, and re-authenticates unattended (via `pass_login`) when the
cloud reports the session as unauthorized.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Protocol

from roborock.data import NetworkInfo
from roborock.devices.cache import DeviceCacheData
from roborock.devices.device_manager import UserParams, create_device_manager
from roborock.exceptions import RoborockException
from roborock.roborock_typing import RoborockCommand

from . import auth
from .cache import JsonCache
from .domain import Command, Telemetry
from .errors import AuthError
from .log import get_logger

if TYPE_CHECKING:
    from roborock.data import UserData
    from roborock.devices.device import RoborockDevice
    from roborock.devices.device_manager import DeviceManager
    from roborock.devices.traits.v1 import PropertiesApi

    from .config import Config

_log = get_logger(__name__)

_COMMAND_MAP: dict[Command, RoborockCommand] = {
    Command.START: RoborockCommand.APP_START,
    Command.STOP: RoborockCommand.APP_STOP,
    Command.PAUSE: RoborockCommand.APP_PAUSE,
    Command.RETURN: RoborockCommand.APP_CHARGE,
    Command.LOCATE: RoborockCommand.FIND_ME,
}

_REAUTH_MIN_BACKOFF = 5.0
_REAUTH_MAX_BACKOFF = 300.0
_REAUTH_BACKOFF_FACTOR = 2.0

ManagerFactory = Callable[..., Awaitable["DeviceManager"]]


class VacuumHandle(Protocol):
    """Per-device operations the bridge needs."""

    @property
    def online(self) -> bool:
        """Whether the device currently has any connection (local or cloud)."""
        ...

    @property
    def local(self) -> bool:
        """Whether the device is reachable over the LAN."""
        ...

    async def refresh(self) -> Telemetry:
        """Poll the device and return a fresh telemetry snapshot."""
        ...

    async def execute(self, command: Command) -> None:
        """Send a control command to the device."""
        ...

    async def set_fan_speed(self, name: str) -> bool:
        """Set the fan speed by name; return `False` if the name is unknown."""
        ...


class Backend(Protocol):
    """Account-level connection managing one or more vacuums."""

    @property
    def authenticated(self) -> bool:
        """Whether the cloud session is currently authenticated."""
        ...

    async def start(self) -> None:
        """Authenticate, connect, and discover devices."""
        ...

    async def close(self) -> None:
        """Tear down all connections."""
        ...

    def handle(self, duid: str) -> VacuumHandle | None:
        """Return the handle for a configured device, or `None` if absent."""
        ...


class _DeviceHandle:
    """A `VacuumHandle` backed by a python-roborock V1 device."""

    def __init__(self, device: RoborockDevice, v1: PropertiesApi) -> None:
        self._device = device
        self._v1 = v1

    @property
    def online(self) -> bool:
        return self._device.is_connected

    @property
    def local(self) -> bool:
        return self._device.is_local_connected

    async def refresh(self) -> Telemetry:
        await self._v1.status.refresh()
        await self._v1.consumables.refresh()
        status = self._v1.status
        consumables = self._v1.consumables
        return Telemetry(
            state=status.state_name,
            battery=status.battery,
            fan_speed=status.fan_speed_name,
            error_code=None if status.error_code is None else status.error_code.value,
            error_name=status.error_code_name,
            clean_area_m2=status.square_meter_clean_area,
            clean_time_s=status.clean_time,
            dock_state=status.dock_state.value,
            main_brush_work_time=consumables.main_brush_work_time,
            side_brush_work_time=consumables.side_brush_work_time,
            filter_work_time=consumables.filter_work_time,
            sensor_dirty_time=consumables.sensor_dirty_time,
        )

    async def execute(self, command: Command) -> None:
        await self._v1.command.send(_COMMAND_MAP[command])

    async def set_fan_speed(self, name: str) -> bool:
        code = self._fan_code(name)
        if code is None:
            return False
        await self._v1.command.send(RoborockCommand.SET_CUSTOM_MODE, params=[code])
        return True

    def _fan_code(self, name: str) -> int | None:
        for code, value in self._v1.status.fan_speed_mapping.items():
            if value == name:
                return code
        return None


class DeviceManagerBackend:
    """A `Backend` over python-roborock's account-level `DeviceManager`."""

    def __init__(
        self,
        config: Config,
        *,
        manager_factory: ManagerFactory = create_device_manager,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        """Create a backend for `config`; `manager_factory`/`sleep` aid testing."""
        self._config = config
        self._manager_factory = manager_factory
        self._sleep = sleep
        self._cache = JsonCache(auth.cache_path(config.roborock.persist_path))
        self._manager: DeviceManager | None = None
        self._handles: dict[str, _DeviceHandle] = {}
        self._authenticated = False
        self._reauth = asyncio.Event()
        self._supervisor: asyncio.Task[None] | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    @property
    def authenticated(self) -> bool:
        return self._authenticated

    def handle(self, duid: str) -> VacuumHandle | None:
        return self._handles.get(duid)

    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        await self._seed_static_ips()
        # A failed initial authentication must not crash the process. A crash
        # restarts the container, and an unattended restart loop re-attempts the
        # login on every boot until Roborock rate-limits the account (code 9002).
        # Instead, come up unauthenticated and let the supervisor retry on the
        # same exponential backoff it uses for an expired session: login attempts
        # stay spaced out, and the bridge keeps serving health and metrics while
        # publishing the devices as offline until a login succeeds.
        try:
            user_data = await self._resolve_user_data()
            await self._build_manager(user_data)
        except (AuthError, RoborockException) as err:
            _log.warning(
                "initial roborock authentication failed; retrying in background",
                error=str(err),
            )
            self._authenticated = False
        else:
            self._authenticated = True
        # Clear any stale signal a previously cancelled supervisor may have left
        # set; then arm the supervisor to retry immediately if we did not come up
        # authenticated.
        self._reauth.clear()
        if not self._authenticated:
            self._reauth.set()
        self._supervisor = asyncio.create_task(self._supervise())

    async def close(self) -> None:
        if self._supervisor is not None:
            self._supervisor.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._supervisor
            self._supervisor = None
        await self._close_manager()

    async def _resolve_user_data(self) -> UserData:
        existing = auth.load_user_data(self._config.roborock.persist_path)
        if existing is not None:
            return existing
        return await self._login()

    async def _login(self) -> UserData:
        roborock = self._config.roborock
        if not roborock.password:
            msg = (
                "no saved credentials and no ROBOROCK_PASSWORD set; "
                "run `rockville login`"
            )
            raise AuthError(msg)
        return await auth.password_login(
            roborock.email, roborock.password, roborock.persist_path
        )

    async def _seed_static_ips(self) -> None:
        configured = {
            device.duid: device.ip for device in self._config.devices if device.ip
        }
        if not configured:
            return
        cache_data = await self._cache.get()
        for duid, ip in configured.items():
            entry = cache_data.device_info.get(duid) or DeviceCacheData()
            entry.network_info = NetworkInfo(ip=ip)
            cache_data.device_info[duid] = entry
        await self._cache.set(cache_data)

    async def _build_manager(self, user_data: UserData) -> None:
        params = UserParams(username=self._config.roborock.email, user_data=user_data)
        self._manager = await self._manager_factory(
            params,
            cache=self._cache,
            prefer_cache=True,
            mqtt_session_unauthorized_hook=self._on_unauthorized,
        )
        await self._build_handles()

    async def _build_handles(self) -> None:
        manager = self._manager
        if manager is None:  # pragma: no cover - manager is always set before this call
            return
        devices = {device.duid: device for device in await manager.get_devices()}
        handles: dict[str, _DeviceHandle] = {}
        for device_config in self._config.devices:
            device = devices.get(device_config.duid)
            if device is None:
                _log.warning(
                    "configured device not found on account",
                    device=device_config.name,
                    duid=device_config.duid,
                )
                continue
            v1 = device.v1_properties
            if v1 is None:
                _log.warning(
                    "device is not a V1 vacuum; skipping",
                    device=device_config.name,
                    duid=device_config.duid,
                )
                continue
            handles[device_config.duid] = _DeviceHandle(device, v1)
        self._handles = handles

    def _on_unauthorized(self) -> None:
        _log.warning("roborock authorization failed; scheduling re-login")
        self._authenticated = False
        loop = self._loop
        if loop is not None:
            loop.call_soon_threadsafe(self._reauth.set)

    async def _supervise(self) -> None:
        backoff = _REAUTH_MIN_BACKOFF
        while True:
            await self._reauth.wait()
            self._reauth.clear()
            if await self._reauthenticate():
                backoff = _REAUTH_MIN_BACKOFF
            else:
                await self._sleep(backoff)
                backoff = min(backoff * _REAUTH_BACKOFF_FACTOR, _REAUTH_MAX_BACKOFF)
                self._reauth.set()

    async def _reauthenticate(self) -> bool:
        roborock = self._config.roborock
        if not roborock.password:
            _log.error(
                "auth expired and no ROBOROCK_PASSWORD set; run `rockville login`"
            )
            return False
        try:
            user_data = await auth.password_login(
                roborock.email, roborock.password, roborock.persist_path
            )
            await self._close_manager()
            await self._build_manager(user_data)
        except (AuthError, RoborockException) as err:
            _log.warning("re-login failed; will retry", error=str(err))
            return False
        else:
            self._authenticated = True
            _log.info("re-authenticated with roborock cloud")
            return True

    async def _close_manager(self) -> None:
        if self._manager is not None:
            await self._manager.close()
            self._manager = None
