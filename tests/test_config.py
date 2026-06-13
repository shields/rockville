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

"""Tests for configuration loading and validation."""

from pathlib import Path

import pytest

from rockville import config
from rockville.errors import ConfigError


def valid_doc():
    return {
        "roborock": {"email": "me@example.com", "persist_path": "/persist"},
        "mqtt": {"url": "mqtt://broker.local:1883"},
        "devices": [{"name": "vac", "duid": "duid-1"}],
    }


def test_valid_minimal():
    cfg = config.validate(valid_doc(), env={})
    assert cfg.roborock.email == "me@example.com"
    assert cfg.roborock.persist_path == Path("/persist")
    assert cfg.roborock.password is None
    assert cfg.mqtt.host == "broker.local"
    assert cfg.mqtt.port == 1883
    assert cfg.mqtt.tls is False
    assert cfg.mqtt.topic_prefix == "rockville"
    assert cfg.mqtt.display_url == "mqtt://broker.local:1883"
    assert cfg.poll_interval == 30.0
    assert cfg.metrics is None
    assert cfg.devices[0].name == "vac"


def test_valid_full():
    doc = {
        "roborock": {
            "email": "me@example.com",
            "persist_path": "/persist",
            "password": "filepw",
        },
        "mqtt": {
            "url": "mqtts://broker.local",
            "username": "user",
            "topic_prefix": "robo",
        },
        "devices": [
            {"name": "up", "duid": "d-up", "ip": "192.168.1.5"},
            {"name": "down", "duid": "d-down"},
        ],
        "poll_interval": 15,
        "metrics": {"port": 9090, "bind": "127.0.0.1"},
    }
    cfg = config.validate(doc, env={})
    assert cfg.roborock.password == "filepw"
    assert cfg.mqtt.tls is True
    assert cfg.mqtt.port == 8883
    assert cfg.mqtt.username == "user"
    assert cfg.mqtt.topic_prefix == "robo"
    assert cfg.poll_interval == 15.0
    assert cfg.metrics is not None
    assert cfg.metrics.port == 9090
    assert cfg.metrics.bind == "127.0.0.1"
    assert cfg.devices[0].ip == "192.168.1.5"


def test_password_env_overrides_file():
    doc = valid_doc()
    doc["roborock"]["password"] = "filepw"
    cfg = config.validate(doc, env={"ROBOROCK_PASSWORD": "envpw"})
    assert cfg.roborock.password == "envpw"


def test_mqtt_password_from_env():
    cfg = config.validate(valid_doc(), env={"MQTT_PASSWORD": "envpw"})
    assert cfg.mqtt.password == "envpw"


def test_mqtt_credentials_from_url():
    doc = valid_doc()
    doc["mqtt"]["url"] = "mqtt://user:pass@broker.local:1884"
    cfg = config.validate(doc, env={})
    assert cfg.mqtt.username == "user"
    assert cfg.mqtt.password == "pass"
    assert cfg.mqtt.port == 1884
    assert cfg.mqtt.display_url == "mqtt://broker.local:1884"


def test_root_not_mapping():
    with pytest.raises(ConfigError, match="config must be a mapping"):
        config.validate([], env={})


def test_missing_roborock():
    doc = valid_doc()
    del doc["roborock"]
    with pytest.raises(ConfigError, match="missing required field: roborock"):
        config.validate(doc, env={})


def test_roborock_not_mapping():
    doc = valid_doc()
    doc["roborock"] = "nope"
    with pytest.raises(ConfigError, match="roborock must be a mapping"):
        config.validate(doc, env={})


def test_missing_email():
    doc = valid_doc()
    del doc["roborock"]["email"]
    with pytest.raises(ConfigError, match="roborock.email must be a non-empty string"):
        config.validate(doc, env={})


def test_empty_email():
    doc = valid_doc()
    doc["roborock"]["email"] = ""
    with pytest.raises(ConfigError, match="roborock.email"):
        config.validate(doc, env={})


def test_missing_mqtt():
    doc = valid_doc()
    del doc["mqtt"]
    with pytest.raises(ConfigError, match="missing required field: mqtt"):
        config.validate(doc, env={})


def test_bad_scheme():
    doc = valid_doc()
    doc["mqtt"]["url"] = "http://broker.local"
    with pytest.raises(ConfigError, match="scheme must be"):
        config.validate(doc, env={})


def test_missing_host():
    doc = valid_doc()
    doc["mqtt"]["url"] = "mqtt://"
    with pytest.raises(ConfigError, match="must include a host"):
        config.validate(doc, env={})


def test_topic_prefix_wildcard():
    doc = valid_doc()
    doc["mqtt"]["topic_prefix"] = "bad+prefix"
    with pytest.raises(ConfigError, match="wildcards or whitespace"):
        config.validate(doc, env={})


def test_topic_prefix_not_string():
    doc = valid_doc()
    doc["mqtt"]["topic_prefix"] = 5
    with pytest.raises(ConfigError, match="non-empty string when present"):
        config.validate(doc, env={})


def test_devices_not_list():
    doc = valid_doc()
    doc["devices"] = {}
    with pytest.raises(ConfigError, match="devices must be a non-empty list"):
        config.validate(doc, env={})


def test_devices_empty():
    doc = valid_doc()
    doc["devices"] = []
    with pytest.raises(ConfigError, match="non-empty list"):
        config.validate(doc, env={})


def test_device_not_mapping():
    doc = valid_doc()
    doc["devices"] = ["nope"]
    with pytest.raises(ConfigError, match=r"devices\[0\] must be a mapping"):
        config.validate(doc, env={})


def test_device_missing_name():
    doc = valid_doc()
    del doc["devices"][0]["name"]
    with pytest.raises(ConfigError, match=r"devices\[0\].name"):
        config.validate(doc, env={})


def test_device_name_wildcard():
    doc = valid_doc()
    doc["devices"][0]["name"] = "bad#name"
    with pytest.raises(ConfigError, match="wildcards or whitespace"):
        config.validate(doc, env={})


def test_device_name_slash():
    doc = valid_doc()
    doc["devices"][0]["name"] = "a/b"
    with pytest.raises(ConfigError, match="must not contain '/'"):
        config.validate(doc, env={})


def test_device_missing_duid():
    doc = valid_doc()
    del doc["devices"][0]["duid"]
    with pytest.raises(ConfigError, match=r"devices\[0\].duid"):
        config.validate(doc, env={})


def test_device_ip_empty():
    doc = valid_doc()
    doc["devices"][0]["ip"] = ""
    with pytest.raises(ConfigError, match="non-empty string when present"):
        config.validate(doc, env={})


def test_duplicate_name():
    doc = valid_doc()
    doc["devices"] = [
        {"name": "vac", "duid": "d-1"},
        {"name": "vac", "duid": "d-2"},
    ]
    with pytest.raises(ConfigError, match="duplicate device name"):
        config.validate(doc, env={})


def test_duplicate_duid():
    doc = valid_doc()
    doc["devices"] = [
        {"name": "a", "duid": "d-1"},
        {"name": "b", "duid": "d-1"},
    ]
    with pytest.raises(ConfigError, match="duplicate device duid"):
        config.validate(doc, env={})


def test_poll_interval_negative():
    doc = valid_doc()
    doc["poll_interval"] = -1
    with pytest.raises(ConfigError, match="positive number"):
        config.validate(doc, env={})


def test_poll_interval_bool():
    doc = valid_doc()
    doc["poll_interval"] = True
    with pytest.raises(ConfigError, match="positive number"):
        config.validate(doc, env={})


def test_metrics_not_mapping():
    doc = valid_doc()
    doc["metrics"] = "nope"
    with pytest.raises(ConfigError, match="metrics must be a mapping"):
        config.validate(doc, env={})


def test_metrics_port_out_of_range():
    doc = valid_doc()
    doc["metrics"] = {"port": 70000}
    with pytest.raises(ConfigError, match="between 1 and 65535"):
        config.validate(doc, env={})


def test_metrics_port_bool():
    doc = valid_doc()
    doc["metrics"] = {"port": True}
    with pytest.raises(ConfigError, match="between 1 and 65535"):
        config.validate(doc, env={})


def test_load_config_reads_file(tmp_path: Path):
    path = tmp_path / "config.yaml"
    path.write_text(
        "roborock:\n"
        "  email: me@example.com\n"
        "  persist_path: /persist\n"
        "mqtt:\n"
        "  url: mqtt://broker.local\n"
        "devices:\n"
        "  - name: vac\n"
        "    duid: duid-1\n",
        encoding="utf-8",
    )
    cfg = config.load_config(path, env={})
    assert cfg.devices[0].duid == "duid-1"
    assert cfg.mqtt.port == 1883
