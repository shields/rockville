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
import os
import tempfile
from collections.abc import Callable
from pathlib import Path

from roborock.data import UserData
from roborock.exceptions import RoborockException
from roborock.web_api import RoborockApiClient

from .errors import AuthError
from .log import get_logger

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
    """Persist `user_data` as owner-only JSON under `persist`.

    The write is atomic (temp file plus rename) so a crash mid-write cannot
    leave truncated credentials behind, and the file is created with mode 0600
    so the cloud session secret is not exposed to other users.
    """
    persist.mkdir(parents=True, exist_ok=True)
    path = user_data_path(persist)
    # mkstemp creates a unique file with O_EXCL at mode 0600, so another user
    # cannot pre-create it (symlink/TOCTOU) or read the secret mid-write.
    fd, tmp_str = tempfile.mkstemp(dir=persist, prefix=f"{path.name}.", suffix=".tmp")
    tmp = Path(tmp_str)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(user_data.as_dict(), fh, indent=2)
            fh.flush()
            os.fsync(fh.fileno())
        tmp.replace(path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


async def password_login(
    email: str,
    password: str,
    persist: Path,
    *,
    client_factory: ApiClientFactory = RoborockApiClient,
) -> UserData:
    """Log in unattended with the account password and persist the result.

    The cloud login is required, but persisting the session is best-effort: if
    the persist directory cannot be written (a read-only or full volume), the
    failure is logged loudly and the in-memory credentials are returned anyway.
    Raising instead would discard a *successful* login and crash the bridge,
    and because the crash restarts the container, every restart would re-run a
    real login — the exact rate-limit hammer the persisted session exists to
    avoid. Degrading to "re-login on the next start" is the lesser evil.
    """
    client = client_factory(email)
    try:
        user_data = await client.pass_login(password)
    except RoborockException as err:
        msg = f"password login failed for {email}: {err}"
        raise AuthError(msg) from err
    try:
        store_user_data(persist, user_data)
    except OSError as err:
        _log.error(
            "logged in but could not persist credentials; "
            "the next restart will need a fresh login",
            path=str(user_data_path(persist)),
            error=str(err),
        )
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
