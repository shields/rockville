# rockville project instructions

## Project overview

A local-first bridge that exposes a Roborock vacuum (built for the Q5 Max+ and
other V1-protocol models) on the user's own MQTT broker. It talks to the vacuum
through [`python-roborock`](https://github.com/Python-roborock/python-roborock),
polls status and telemetry over the LAN, publishes changed state to retained
generic topics, and dispatches control commands back to the vacuum. Modeled on
`../hoboken`; follows `../right-answers`.

## Toolchain

- **Python 3.14**, managed with **uv** (`uv sync`, `uv.lock`).
- **Ruff** for lint + format (`.ruff.toml`, `select = ["ALL"]`).
- **ty** for type checking — run against `src` only (`ty check src`); the tests
  use loose test doubles that intentionally don't satisfy the strict protocols.
- **pytest** + **coverage**, 100% line **and** branch coverage enforced.
- **Prettier** (via `npx`) formats the non-Python files (Markdown, YAML, JSON5).
- Distroless container: `gcr.io/distroless/cc-debian13:nonroot` (native wheels —
  pycryptodome, aiohttp, paho — need glibc + libstdc++), digest-pinned.

## Commands

```sh
make lint    # ruff format --check, ruff check, ty check src, prettier --check
make fmt     # ruff format, ruff check --fix, prettier --write
make test    # coverage run -m pytest; coverage report --fail-under=100
make run     # uv run rockville run
make image   # docker build
```

Note: `test_metrics.py` and some `test_bridge.py` cases bind loopback sockets
(aiohttp `TestServer`, real Mosquitto smokes), so they need real network access
— a restrictive sandbox that blocks `bind()` will fail them.

## Module architecture

Each module is independently testable. All `python-roborock` and MQTT access is
behind injected seams, so the bridge is tested without the library or a broker.

- `config.py` — YAML load + strict, message-rich validation; frozen dataclasses.
- `domain.py` — `Command` (StrEnum) and `Telemetry` (the flat device snapshot).
- `convert.py` — **pure** functions: `Telemetry` → MQTT payloads, consumable
  percent/hours math (budgets imported from `roborock.const`),
  `command_from_mqtt`.
- `cache.py` — `JsonCache`, a JSON-backed implementation of python-roborock's
  `Cache` protocol.
- `auth.py` — `pass_login` (unattended) + `code_login` (interactive); JSON
  persistence of `UserData`.
- `roborock_client.py` — **the only module that imports python-roborock.**
  Defines the `Backend`/`VacuumHandle` protocols the bridge depends on;
  `DeviceManagerBackend` owns the account-level `DeviceManager`, seeds static
  IPs, maps library traits → `Telemetry` and `Command` → `RoborockCommand`, and
  re-authenticates unattended via a background supervisor.
- `bridge.py` — async core: per-device poll loop, MQTT reconnect loop with
  jittered backoff, command routing, metrics, status server.
- `metrics.py` — `prometheus-client` + aiohttp server (`/metrics`, `/healthz`,
  `/readyz`, `/`, `/events` SSE).
- `status_page.py` — pure HTML rendering of `StatusData`.
- `__main__.py` — `click` CLI (`run`, `login`), `serve()`, signal handling.

## python-roborock notes (verified against 5.14.x source)

These are non-obvious facts that shaped the design; re-verify against the
installed source before relying on them after a library upgrade.

- **Only the high-level API exists.** The old low-level `RoborockLocalClientV1`
  / `RoborockMQTTClientV1` / `DeviceData` clients were removed. Use
  `create_device_manager(...)` →
  `device.v1_properties.{status,consumables,command}`.
- **"Local-preferred", not cloud-free.** `create_device_manager` always opens a
  best-effort cloud MQTT socket (for DPS push). Control + status RPCs prefer the
  LAN and degrade gracefully when the cloud is down, but the cloud socket is
  opened opportunistically; you cannot disable it without patching the library.
- **No pickle.** `Cache` is a 2-method `Protocol` (`async get/set`) and
  `CacheData`/`UserData`/`HomeData` subclass `RoborockBase`
  (`as_dict`/`from_dict`), so we persist JSON (`JsonCache`) instead of the
  library's pickle `FileCache`.
- **Unattended auth** via `RoborockApiClient.pass_login(password)`. `code_login`
  (email code) is the interactive fallback. Re-auth is driven by the
  `mqtt_session_unauthorized_hook`.
- **Poll, don't rely on push.** V1 DPS push decodes only on the cloud message
  path, so for genuine local liveness the bridge polls `status.refresh()` /
  `consumables.refresh()` on an interval. Push was dropped from v1.
- **LAN IP discovery is a cloud call** (`GET_NETWORK_INFO`), cached ~12 h. A
  DHCP reservation or the optional static `ip` in config avoids depending on it.
- Status fields used: `state_name`, `battery`, `fan_power`, `fan_speed_name`,
  `fan_speed_mapping` (`dict[int, str]`), `error_code` (`.value` /
  `error_code_name`), `square_meter_clean_area`, `clean_time`,
  `dock_state.value`. Consumables: `*_work_time`. Commands:
  `command.send(RoborockCommand.X, params=...)`.

## Key design decisions

- **Injected seams**: `bridge.py` depends only on the `Backend`/`VacuumHandle`
  protocols and an injected `mqtt_factory`/`sleep`/`clock`/`rand`, so it is
  fully tested with fakes (`tests/fakes.py`). Library-shaped fakes for the
  adapter live in `test_roborock_client.py`.
- **Graceful offline via cancellation**: on shutdown the bridge cancels the MQTT
  task, which catches `CancelledError` and publishes retained `offline` from
  inside its own still-open connection before re-raising. `shutdown()` never
  touches `self._client` directly — that would race the loop closing it.
- **Last-Will**: `{prefix}/bridge/availability` is the LWT (`offline`,
  retained), set `online` on connect; per-device `…/availability` tracks
  reachability.
- **State publishing is change-only**: a per-device cache dedups; the full cache
  is re-published (retained) on every reconnect.
- **No config reload**: restart the process for config changes.

## Conventions

- Every source file starts with the Apache-2.0 header
  (`Copyright © 2026 Michael Shields`).
- **Exhaustive enums**: state mapping uses the library enum's `.name`; never add
  a silent catch-all that would hide an unmapped value.
- 100% line + branch coverage is required; use `# pragma: no cover` only for
  genuinely unreachable defensive code, and `log()` anything intentionally
  bounded.
- Secrets (`ROBOROCK_PASSWORD`, `MQTT_PASSWORD`) come from the environment,
  never the config file in production.
- Plain imperative commit messages (no Conventional Commits); never bypass git
  hooks.

## MQTT topics

See `README.md` for the full topic table. State is retained; commands
(`…/command/set`, `…/fan_speed/set`) are not.
