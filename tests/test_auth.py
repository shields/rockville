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

"""Tests for Roborock authentication and credential persistence."""

import stat
from pathlib import Path

import pytest
from roborock.data import Reference, RRiot, UserData
from roborock.exceptions import RoborockException

from rockville import auth
from rockville.errors import AuthError


def sample_user_data() -> UserData:
    return UserData(
        rriot=RRiot(u="u", s="s", h="h", k="k", r=Reference(a="https://api"))
    )


class FakeApiClient:
    def __init__(self, *, user_data=None, code_error=None, login_error=None):
        self._user_data = user_data
        self._code_error = code_error
        self._login_error = login_error
        self.requested = False

    async def pass_login(self, password: str) -> UserData:
        assert password
        if self._login_error is not None:
            raise self._login_error
        return self._user_data

    async def request_code(self) -> None:
        self.requested = True
        if self._code_error is not None:
            raise self._code_error

    async def code_login(self, code: int | str) -> UserData:
        assert code
        return self._user_data


def test_paths(tmp_path: Path):
    assert auth.user_data_path(tmp_path).name == "user_data.json"
    assert auth.cache_path(tmp_path).name == "cache.json"


def test_load_missing_returns_none(tmp_path: Path):
    assert auth.load_user_data(tmp_path) is None


def test_store_then_load_roundtrip(tmp_path: Path):
    auth.store_user_data(tmp_path, sample_user_data())
    loaded = auth.load_user_data(tmp_path)
    assert loaded is not None
    assert loaded.rriot.u == "u"
    assert loaded.rriot.r.a == "https://api"


def test_store_user_data_is_owner_only(tmp_path: Path):
    auth.store_user_data(tmp_path, sample_user_data())
    mode = stat.S_IMODE(auth.user_data_path(tmp_path).stat().st_mode)
    assert mode == 0o600


def test_store_user_data_overwrite_keeps_owner_only(tmp_path: Path):
    # A pre-existing file with loose permissions must be tightened on rewrite.
    path = auth.user_data_path(tmp_path)
    path.write_text("{}", encoding="utf-8")
    path.chmod(0o644)
    auth.store_user_data(tmp_path, sample_user_data())
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert auth.load_user_data(tmp_path) is not None


def test_store_user_data_leaves_no_temp_file(tmp_path: Path):
    auth.store_user_data(tmp_path, sample_user_data())
    assert [p.name for p in tmp_path.iterdir()] == [auth.USER_DATA_FILE]


def test_store_user_data_failure_cleans_up_temp(tmp_path: Path):
    class Exploding:
        def as_dict(self) -> dict:
            raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        auth.store_user_data(tmp_path, Exploding())  # type: ignore[arg-type]
    # No partial credentials and no leftover temp file remain.
    assert list(tmp_path.iterdir()) == []


def test_load_corrupt_returns_none(tmp_path: Path):
    auth.user_data_path(tmp_path).write_text("{ broken", encoding="utf-8")
    assert auth.load_user_data(tmp_path) is None


def test_load_non_object_returns_none(tmp_path: Path):
    auth.user_data_path(tmp_path).write_text("[]", encoding="utf-8")
    assert auth.load_user_data(tmp_path) is None


async def test_password_login_success(tmp_path: Path):
    user_data = sample_user_data()
    result = await auth.password_login(
        "me@example.com",
        "secret",
        tmp_path,
        client_factory=lambda _email: FakeApiClient(user_data=user_data),
    )
    assert result.rriot.u == "u"
    assert auth.user_data_path(tmp_path).exists()


async def test_password_login_failure_raises_auth_error(tmp_path: Path):
    with pytest.raises(AuthError, match="password login failed"):
        await auth.password_login(
            "me@example.com",
            "secret",
            tmp_path,
            client_factory=lambda _email: FakeApiClient(
                login_error=RoborockException("nope")
            ),
        )


async def test_password_login_tolerates_store_failure(tmp_path: Path, monkeypatch):
    # A successful login whose credential write fails (read-only/full persist
    # volume) must still return the in-memory credentials rather than raising:
    # discarding a successful login and crashing would re-run login on every
    # restart — the rate-limit hammer the persisted session exists to avoid.
    def boom(*_args, **_kwargs):
        raise PermissionError("read-only")

    monkeypatch.setattr(auth, "store_user_data", boom)
    result = await auth.password_login(
        "me@example.com",
        "secret",
        tmp_path,
        client_factory=lambda _email: FakeApiClient(user_data=sample_user_data()),
    )
    assert result.rriot.u == "u"
    assert not auth.user_data_path(tmp_path).exists()


async def test_code_login_success(tmp_path: Path):
    user_data = sample_user_data()
    result = await auth.code_login(
        "me@example.com",
        tmp_path,
        prompt=lambda _msg: "123456",
        client_factory=lambda _email: FakeApiClient(user_data=user_data),
    )
    assert result.rriot.u == "u"
    assert auth.user_data_path(tmp_path).exists()


async def test_code_login_failure_raises_auth_error(tmp_path: Path):
    with pytest.raises(AuthError, match="code login failed"):
        await auth.code_login(
            "me@example.com",
            tmp_path,
            prompt=lambda _msg: "123456",
            client_factory=lambda _email: FakeApiClient(
                code_error=RoborockException("nope")
            ),
        )
