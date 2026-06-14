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
import json
import os
import tempfile
from enum import Enum
from pathlib import Path

from roborock.data import RoborockBase
from roborock.devices.cache import Cache, CacheData

from .log import get_logger

_log = get_logger(__name__)


def _default(obj: object) -> object:
    """Serialize values `json` cannot handle (enums and nested dataclasses)."""
    if isinstance(obj, RoborockBase):
        return obj.as_dict()
    if isinstance(obj, Enum):
        return obj.value
    # We deliberately don't serialize bytes: the only bytes field is the
    # deprecated, never-written home_map_content, and base64-encoding it was
    # lossy (RoborockBase.from_dict can't decode it back). Fail loudly instead.
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
        # Write atomically (temp file plus rename) so a crash mid-write cannot
        # leave a truncated cache that would then be discarded on next start.
        # mkstemp creates the temp with O_EXCL, so a planted symlink with the
        # predictable name cannot redirect the write to another file.
        directory = self._path.parent
        directory.mkdir(parents=True, exist_ok=True)
        fd, tmp_str = tempfile.mkstemp(
            dir=directory, prefix=f"{self._path.name}.", suffix=".tmp"
        )
        tmp = Path(tmp_str)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(value.as_dict(), fh, default=_default, indent=2)
                fh.flush()
                os.fsync(fh.fileno())
            tmp.replace(self._path)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
