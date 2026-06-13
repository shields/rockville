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

"""Command-line entry point.

`rockville run` (the default) runs the bridge; `rockville login` performs the
one-time interactive email-code authentication as a fallback for accounts where
unattended password login is unavailable.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
from pathlib import Path
from typing import TYPE_CHECKING

import click

from . import auth, log
from .bridge import Bridge
from .config import load_config
from .roborock_client import DeviceManagerBackend

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from .config import Config
    from .roborock_client import Backend

_DEFAULT_CONFIG_PATH = "/config/config.yaml"
_CONFIG_PATH_ENV = "CONFIG_PATH"
_VERSION_FILE = Path(__file__).resolve().parents[2] / "VERSION"


def read_version(env: Mapping[str, str], version_file: Path) -> str:
    """Resolve the running version from the environment or a VERSION file."""
    override = env.get("ROCKVILLE_VERSION")
    if override:
        return override
    try:
        text = version_file.read_text(encoding="utf-8").strip()
    except OSError:
        return "unknown"
    return text or "unknown"


def _configure_logging(env: Mapping[str, str]) -> None:
    log.configure(
        level=env.get("LOG_LEVEL", "INFO"), log_format=env.get("LOG_FORMAT", "console")
    )


def _load() -> Config:
    return load_config(os.environ.get(_CONFIG_PATH_ENV, _DEFAULT_CONFIG_PATH))


async def serve(
    config: Config,
    *,
    make_backend: Callable[[Config], Backend] = DeviceManagerBackend,
    make_bridge: Callable[..., Bridge] = Bridge,
    version: str = "unknown",
    install_signals: bool = True,
    stop: asyncio.Event | None = None,
) -> None:
    """Run the bridge until a stop signal arrives, then shut it down cleanly."""
    backend = make_backend(config)
    bridge = make_bridge(config, backend, version=version)
    stop_event = stop if stop is not None else asyncio.Event()
    if install_signals:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError):
                loop.add_signal_handler(sig, stop_event.set)
    await bridge.start()
    try:
        await stop_event.wait()
    finally:
        await bridge.shutdown()


@click.group(invoke_without_command=True)
@click.pass_context
def cli(ctx: click.Context) -> None:
    """rockville — a local-first Roborock-to-MQTT bridge."""
    if ctx.invoked_subcommand is None:
        ctx.invoke(run)


@cli.command()
def run() -> None:
    """Run the bridge (the default command)."""
    _configure_logging(os.environ)
    config = _load()
    asyncio.run(serve(config, version=read_version(os.environ, _VERSION_FILE)))


@cli.command()
def login() -> None:
    """Authenticate with the Roborock cloud via an emailed code."""
    _configure_logging(os.environ)
    config = _load()
    asyncio.run(auth.code_login(config.roborock.email, config.roborock.persist_path))
    click.echo("Saved credentials.")


def main() -> None:
    """Console-script entry point."""
    cli()


if __name__ == "__main__":  # pragma: no cover
    main()
