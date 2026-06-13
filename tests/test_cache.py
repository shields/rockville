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

"""Tests for the JSON-backed device cache."""

from pathlib import Path

import pytest
from roborock.data import HomeData, NetworkInfo
from roborock.data.v1.v1_code_mappings import RoborockStateCode
from roborock.devices.cache import CacheData, DeviceCacheData

from rockville import cache


def test_default_handles_enum_bytes_and_dataclass():
    assert cache._default(RoborockStateCode.idle) == RoborockStateCode.idle.value
    assert cache._default(b"abc") == "YWJj"
    assert cache._default(NetworkInfo(ip="1.2.3.4"))["ip"] == "1.2.3.4"


def test_default_rejects_unsupported():
    with pytest.raises(TypeError, match="not JSON serializable"):
        cache._default(object())


async def test_missing_file_returns_empty(tmp_path: Path):
    data = await cache.JsonCache(tmp_path / "absent.json").get()
    assert data.home_data is None


async def test_roundtrip_preserves_home_data_and_ip(tmp_path: Path):
    path = tmp_path / "cache.json"
    store = cache.JsonCache(path)
    data = CacheData()
    data.home_data = HomeData(id=7, name="home")
    data.device_info["duid-1"] = DeviceCacheData(
        network_info=NetworkInfo(ip="192.168.1.5")
    )
    await store.set(data)
    assert path.exists()

    loaded = await cache.JsonCache(path).get()
    assert loaded.home_data is not None
    assert loaded.home_data.id == 7
    assert loaded.device_info["duid-1"].network_info is not None
    assert loaded.device_info["duid-1"].network_info.ip == "192.168.1.5"


async def test_get_is_cached(tmp_path: Path):
    store = cache.JsonCache(tmp_path / "cache.json")
    first = await store.get()
    second = await store.get()
    assert first is second


async def test_corrupt_json_is_discarded(tmp_path: Path):
    path = tmp_path / "cache.json"
    path.write_text("{ this is not json", encoding="utf-8")
    data = await cache.JsonCache(path).get()
    assert data.home_data is None


async def test_non_object_json_is_discarded(tmp_path: Path):
    path = tmp_path / "cache.json"
    path.write_text("[]", encoding="utf-8")
    data = await cache.JsonCache(path).get()
    assert data.home_data is None
