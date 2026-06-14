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

"""Configuration loading and strict validation.

The config is YAML. Validation is deliberately strict and message-rich so a
misconfiguration fails fast at startup with a precise explanation rather than
misbehaving later. Secrets (the Roborock account password and the MQTT
password) are read from the environment so they stay out of the config file.
"""

import os
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlparse

import yaml

from .errors import ConfigError

ROBOROCK_PASSWORD_ENV = "ROBOROCK_PASSWORD"  # noqa: S105 — env var name, not a secret
MQTT_PASSWORD_ENV = "MQTT_PASSWORD"  # noqa: S105 — env var name, not a secret

_MIN_PORT = 1
_MAX_PORT = 65535
_DEFAULT_MQTT_PORT = 1883
_DEFAULT_MQTTS_PORT = 8883
_DEFAULT_TOPIC_PREFIX = "rockville"
_DEFAULT_POLL_INTERVAL = 30.0
_FORBIDDEN_TOPIC_CHARS = ("+", "#")


@dataclass(frozen=True, slots=True)
class DeviceConfig:
    """A single vacuum to bridge, identified by its Roborock DUID."""

    name: str
    duid: str
    ip: str | None = None


@dataclass(frozen=True, slots=True)
class MQTTConfig:
    """Connection parameters for the bridge's own MQTT broker."""

    host: str
    port: int
    tls: bool
    topic_prefix: str
    username: str | None = None
    password: str | None = None
    display_url: str = ""


@dataclass(frozen=True, slots=True)
class RoborockConfig:
    """Roborock cloud account and on-disk persistence location."""

    email: str
    persist_path: Path
    password: str | None = None


@dataclass(frozen=True, slots=True)
class MetricsConfig:
    """Where to serve Prometheus metrics and the status page."""

    port: int
    bind: str | None = None


@dataclass(frozen=True, slots=True)
class Config:
    """The fully validated bridge configuration."""

    roborock: RoborockConfig
    mqtt: MQTTConfig
    devices: tuple[DeviceConfig, ...]
    poll_interval: float = _DEFAULT_POLL_INTERVAL
    metrics: MetricsConfig | None = None


def load_config(path: str | Path, *, env: Mapping[str, str] | None = None) -> Config:
    """Load and validate the YAML config at `path`."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as err:
        msg = f"cannot read config file {path}: {err}"
        raise ConfigError(msg) from err
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as err:
        msg = f"cannot parse config file {path}: {err}"
        raise ConfigError(msg) from err
    return validate(data, env=os.environ if env is None else env)


def validate(data: object, *, env: Mapping[str, str]) -> Config:
    """Validate a parsed config document into a `Config`."""
    root = _as_mapping(data, "config")
    return Config(
        roborock=_roborock(_as_mapping(_require(root, "roborock"), "roborock"), env),
        mqtt=_mqtt(_as_mapping(_require(root, "mqtt"), "mqtt"), env),
        devices=_devices(root.get("devices")),
        poll_interval=_poll_interval(root.get("poll_interval")),
        metrics=_metrics(root.get("metrics")),
    )


def _roborock(data: Mapping[str, Any], env: Mapping[str, str]) -> RoborockConfig:
    persist = _require_str(data, "roborock.persist_path")
    password = env.get(ROBOROCK_PASSWORD_ENV) or _optional_str(
        data, "roborock.password"
    )
    return RoborockConfig(
        email=_require_str(data, "roborock.email"),
        persist_path=Path(persist),
        password=password,
    )


def _mqtt(data: Mapping[str, Any], env: Mapping[str, str]) -> MQTTConfig:
    url = _require_str(data, "mqtt.url")
    parsed = urlparse(url)
    if parsed.scheme not in ("mqtt", "mqtts"):
        msg = f"mqtt.url scheme must be mqtt:// or mqtts://, got {parsed.scheme!r}"
        raise ConfigError(msg)
    host = parsed.hostname
    if not host:
        msg = "mqtt.url must include a host"
        raise ConfigError(msg)
    tls = parsed.scheme == "mqtts"
    port = parsed.port or (_DEFAULT_MQTTS_PORT if tls else _DEFAULT_MQTT_PORT)
    topic_prefix = _optional_str(data, "mqtt.topic_prefix") or _DEFAULT_TOPIC_PREFIX
    _check_topic_segment(topic_prefix, "mqtt.topic_prefix")
    username = _optional_str(data, "mqtt.username") or parsed.username
    password = (
        env.get(MQTT_PASSWORD_ENV)
        or _optional_str(data, "mqtt.password")
        or parsed.password
    )
    return MQTTConfig(
        host=host,
        port=port,
        tls=tls,
        topic_prefix=topic_prefix,
        username=username,
        password=password,
        display_url=f"{parsed.scheme}://{host}:{port}",
    )


def _devices(value: object) -> tuple[DeviceConfig, ...]:
    if not isinstance(value, list) or not value:
        msg = "devices must be a non-empty list"
        raise ConfigError(msg)
    devices = tuple(_device(item, index) for index, item in enumerate(value))
    _check_unique(d.name for d in devices)
    _check_unique((d.duid for d in devices), field="duid")
    return devices


def _device(value: object, index: int) -> DeviceConfig:
    data = _as_mapping(value, f"devices[{index}]")
    name = _require_str(data, f"devices[{index}].name")
    _check_topic_segment(name, f"devices[{index}].name")
    if "/" in name:
        msg = f"devices[{index}].name must not contain '/'"
        raise ConfigError(msg)
    return DeviceConfig(
        name=name,
        duid=_require_str(data, f"devices[{index}].duid"),
        ip=_optional_str(data, f"devices[{index}].ip"),
    )


def _poll_interval(value: object) -> float:
    if value is None:
        return _DEFAULT_POLL_INTERVAL
    if not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0:
        msg = "poll_interval must be a positive number"
        raise ConfigError(msg)
    return float(value)


def _metrics(value: object) -> MetricsConfig | None:
    if value is None:
        return None
    data = _as_mapping(value, "metrics")
    port = data.get("port")
    if (
        not isinstance(port, int)
        or isinstance(port, bool)
        or not _MIN_PORT <= port <= _MAX_PORT
    ):
        msg = f"metrics.port must be an integer between {_MIN_PORT} and {_MAX_PORT}"
        raise ConfigError(msg)
    return MetricsConfig(port=port, bind=_optional_str(data, "metrics.bind"))


def _check_unique(values: Iterable[str], *, field: str = "name") -> None:
    seen: set[str] = set()
    for value in values:
        if value in seen:
            msg = f"duplicate device {field} {value!r}"
            raise ConfigError(msg)
        seen.add(value)


def _check_topic_segment(value: str, field: str) -> None:
    if any(char in value for char in _FORBIDDEN_TOPIC_CHARS) or any(
        c.isspace() for c in value
    ):
        msg = f"{field} must not contain MQTT wildcards or whitespace"
        raise ConfigError(msg)


def _as_mapping(value: object, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        msg = f"{field} must be a mapping"
        raise ConfigError(msg)
    return cast("Mapping[str, Any]", value)


def _require(data: Mapping[str, Any], key: str) -> Any:
    if key not in data:
        msg = f"missing required field: {key}"
        raise ConfigError(msg)
    return data[key]


def _key(field: str) -> str:
    """Return the bare key for a dotted `field` path (e.g. `mqtt.url` -> `url`)."""
    return field.rsplit(".", 1)[-1]


def _require_str(data: Mapping[str, Any], field: str) -> str:
    value = data.get(_key(field))
    if not isinstance(value, str) or not value:
        msg = f"{field} must be a non-empty string"
        raise ConfigError(msg)
    return value


def _optional_str(data: Mapping[str, Any], field: str) -> str | None:
    value = data.get(_key(field))
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        msg = f"{field} must be a non-empty string when present"
        raise ConfigError(msg)
    return value
