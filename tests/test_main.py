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

"""Tests for the command-line entry point."""

import asyncio
from pathlib import Path

from click.testing import CliRunner
from fakes import make_config

from rockville import __main__ as entry
from rockville.errors import ConfigError


def test_read_version_from_env():
    assert entry.read_version({"ROCKVILLE_VERSION": "1.2.3"}, Path("/nope")) == "1.2.3"


def test_read_version_from_file(tmp_path: Path):
    path = tmp_path / "VERSION"
    path.write_text("9.9\n", encoding="utf-8")
    assert entry.read_version({}, path) == "9.9"


def test_read_version_missing_file(tmp_path: Path):
    assert entry.read_version({}, tmp_path / "absent") == "unknown"


def test_read_version_empty_file(tmp_path: Path):
    path = tmp_path / "VERSION"
    path.write_text("  \n", encoding="utf-8")
    assert entry.read_version({}, path) == "unknown"


def test_configure_logging_runs():
    entry._configure_logging({"LOG_LEVEL": "INFO", "LOG_FORMAT": "console"})


def test_load_uses_config_path_env(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        entry, "load_config", lambda path: captured.setdefault("path", path)
    )
    monkeypatch.setenv("CONFIG_PATH", "/custom/config.yaml")
    entry._load()
    assert captured["path"] == "/custom/config.yaml"


class _FakeBridge:
    def __init__(self):
        self.started = False
        self.stopped = False

    async def start(self):
        self.started = True

    async def shutdown(self):
        self.stopped = True


async def test_serve_runs_until_stop():
    bridge = _FakeBridge()
    stop = asyncio.Event()
    stop.set()
    await entry.serve(
        make_config(),
        make_backend=lambda _c: object(),
        make_bridge=lambda *_a, **_k: bridge,
        install_signals=False,
        stop=stop,
    )
    assert bridge.started is True
    assert bridge.stopped is True


async def test_serve_installs_signal_handlers():
    bridge = _FakeBridge()
    stop = asyncio.Event()
    stop.set()
    await entry.serve(
        make_config(),
        make_backend=lambda _c: object(),
        make_bridge=lambda *_a, **_k: bridge,
        install_signals=True,
        stop=stop,
    )
    assert bridge.stopped is True


def test_run_command(monkeypatch):
    monkeypatch.setattr(entry, "_configure_logging", lambda _env: None)
    monkeypatch.setattr(entry, "_load", make_config)
    ran = {}

    def fake_run(coro):
        coro.close()
        ran["ran"] = True

    monkeypatch.setattr(entry.asyncio, "run", fake_run)
    result = CliRunner().invoke(entry.cli, ["run"])
    assert result.exit_code == 0
    assert ran["ran"] is True


def test_default_command_runs(monkeypatch):
    monkeypatch.setattr(entry, "_configure_logging", lambda _env: None)
    monkeypatch.setattr(entry, "_load", make_config)
    ran = {}
    monkeypatch.setattr(
        entry.asyncio, "run", lambda coro: (coro.close(), ran.setdefault("ran", True))
    )
    result = CliRunner().invoke(entry.cli, [])
    assert result.exit_code == 0
    assert ran["ran"] is True


def test_login_command(monkeypatch):
    monkeypatch.setattr(entry, "_configure_logging", lambda _env: None)
    monkeypatch.setattr(entry, "_load", make_config)
    monkeypatch.setattr(entry.asyncio, "run", lambda coro: coro.close())
    result = CliRunner().invoke(entry.cli, ["login"])
    assert result.exit_code == 0
    assert "Saved credentials." in result.output


def test_run_reports_config_error_cleanly(monkeypatch):
    monkeypatch.setattr(entry, "_configure_logging", lambda _env: None)

    def boom():
        msg = "bad config: missing 'mqtt'"
        raise ConfigError(msg)

    monkeypatch.setattr(entry, "_load", boom)
    result = CliRunner().invoke(entry.cli, ["run"])
    assert result.exit_code == 1
    assert "bad config: missing 'mqtt'" in result.output
    assert "Traceback" not in result.output


def test_main_invokes_cli(monkeypatch):
    called = {}
    monkeypatch.setattr(entry, "cli", lambda: called.setdefault("called", True))
    entry.main()
    assert called["called"] is True
