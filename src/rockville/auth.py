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

"""Roborock cloud authentication and credential persistence.

The bridge prefers unattended password login (`pass_login`) so it can bootstrap
and re-authenticate on its own. The interactive email-code flow is a fallback
for accounts where password login is blocked. Credentials are persisted as
JSON via `UserData.as_dict()` — no pickle.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import TYPE_CHECKING

from roborock.data import UserData
from roborock.exceptions import RoborockException
from roborock.web_api import RoborockApiClient

from .errors import AuthError
from .log import get_logger

if TYPE_CHECKING:
    from pathlib import Path

_log = get_logger(__name__)

USER_DATA_FILE = "user_data.json"
CACHE_FILE = "cache.json"

ApiClientFactory = Callable[[str], RoborockApiClient]


def user_data_path(persist: Path) -> Path:
    """Return the path of the saved-credentials file under `persist`."""
    return persist / USER_DATA_FILE


def cache_path(persist: Path) -> Path:
    """Return the path of the device cache file under `persist`."""
    return persist / CACHE_FILE


def load_user_data(persist: Path) -> UserData | None:
    """Load saved credentials, or `None` if absent or unreadable."""
    path = user_data_path(persist)
    if not path.exists():
        return None
    try:
        data = UserData.from_dict(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError, TypeError) as err:
        _log.warning(
            "ignoring unreadable saved credentials", path=str(path), error=str(err)
        )
        return None
    if not isinstance(data, UserData):
        return None
    return data


def store_user_data(persist: Path, user_data: UserData) -> None:
    """Persist `user_data` as JSON under `persist`."""
    persist.mkdir(parents=True, exist_ok=True)
    user_data_path(persist).write_text(
        json.dumps(user_data.as_dict(), indent=2),
        encoding="utf-8",
    )


async def password_login(
    email: str,
    password: str,
    persist: Path,
    *,
    client_factory: ApiClientFactory = RoborockApiClient,
) -> UserData:
    """Log in unattended with the account password and persist the result."""
    client = client_factory(email)
    try:
        user_data = await client.pass_login(password)
    except RoborockException as err:
        msg = f"password login failed for {email}: {err}"
        raise AuthError(msg) from err
    store_user_data(persist, user_data)
    return user_data


async def code_login(
    email: str,
    persist: Path,
    *,
    prompt: Callable[[str], str] = input,
    client_factory: ApiClientFactory = RoborockApiClient,
) -> UserData:
    """Log in interactively via an emailed code and persist the result."""
    client = client_factory(email)
    try:
        await client.request_code()
        code = prompt("Enter the code emailed to you: ")
        user_data = await client.code_login(code)
    except RoborockException as err:
        msg = f"code login failed for {email}: {err}"
        raise AuthError(msg) from err
    store_user_data(persist, user_data)
    return user_data
