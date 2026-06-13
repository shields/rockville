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

"""JSON-backed implementation of python-roborock's `Cache` protocol.

python-roborock ships a pickle-backed `FileCache`, but pickle is fragile across
library upgrades and opaque to inspect. Since `CacheData` (and everything it
contains) subclasses `RoborockBase`, which provides `as_dict()`/`from_dict()`,
we persist the cache as human-readable JSON instead. A corrupt or
incompatible file is discarded and rebuilt from the cloud rather than crashing.
"""

from __future__ import annotations

import asyncio
import base64
import json
from enum import Enum
from typing import TYPE_CHECKING

from roborock.data import RoborockBase
from roborock.devices.cache import Cache, CacheData

from .log import get_logger

if TYPE_CHECKING:
    from pathlib import Path

_log = get_logger(__name__)


def _default(obj: object) -> object:
    """Serialize values `json` cannot handle (enums, bytes, nested dataclasses)."""
    if isinstance(obj, RoborockBase):
        return obj.as_dict()
    if isinstance(obj, Enum):
        return obj.value
    if isinstance(obj, bytes):
        return base64.b64encode(obj).decode("ascii")
    msg = f"Object of type {type(obj).__name__} is not JSON serializable"
    raise TypeError(msg)


class JsonCache(Cache):
    """A `Cache` that persists `CacheData` as JSON, loaded lazily and cached."""

    def __init__(self, path: Path) -> None:
        """Back the cache with the file at `path`."""
        self._path = path
        self._data: CacheData | None = None

    async def get(self) -> CacheData:
        """Return the cached data, loading it from disk on first access."""
        if self._data is None:
            self._data = await asyncio.to_thread(self._load)
        return self._data

    async def set(self, value: CacheData) -> None:
        """Store `value` in memory and write it through to disk."""
        self._data = value
        await asyncio.to_thread(self._store, value)

    def _load(self) -> CacheData:
        if not self._path.exists():
            return CacheData()
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            data = CacheData.from_dict(raw)
        except (OSError, ValueError, TypeError) as err:
            _log.warning(
                "discarding unreadable cache; will rebuild from cloud",
                path=str(self._path),
                error=str(err),
            )
            return CacheData()
        if not isinstance(data, CacheData):
            _log.warning(
                "cache did not decode to CacheData; rebuilding", path=str(self._path)
            )
            return CacheData()
        return data

    def _store(self, value: CacheData) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(
            json.dumps(value.as_dict(), default=_default, indent=2),
            encoding="utf-8",
        )
