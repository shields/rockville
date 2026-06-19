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

"""Tests for the pure MQTT conversion functions."""

import json

import pytest

from rockville import convert
from rockville.domain import Command, Telemetry


def test_percent_remaining_full_and_empty():
    assert convert.percent_remaining(0, 1000) == 100
    assert convert.percent_remaining(1000, 1000) == 0
    assert convert.percent_remaining(500, 1000) == 50


def test_percent_remaining_clamps_when_overused():
    assert convert.percent_remaining(1500, 1000) == 0


def test_consumable_payload_is_sorted_json():
    payload = convert.consumable_payload(0, 360_000)
    assert json.loads(payload) == {"life_s": 360_000, "percent": 100, "work_time_s": 0}
    assert payload == '{"life_s":360000,"percent":100,"work_time_s":0}'


def test_consumable_metrics_yields_work_time_and_life():
    telemetry = Telemetry(
        main_brush_work_time=180_000,
        side_brush_work_time=72_000,
        filter_work_time=54_000,
        sensor_dirty_time=10_800,
    )
    assert list(convert.consumable_metrics(telemetry)) == [
        ("main_brush", 180_000, 1_080_000),
        ("side_brush", 72_000, 720_000),
        ("filter", 54_000, 540_000),
        ("sensor", 10_800, 108_000),
    ]


def test_consumable_metrics_omits_missing_consumables():
    telemetry = Telemetry(filter_work_time=0)
    assert list(convert.consumable_metrics(telemetry)) == [("filter", 0, 540_000)]
    assert list(convert.consumable_metrics(Telemetry())) == []


def test_error_payload_uses_unknown_for_missing_name():
    assert json.loads(convert.error_payload(5, None)) == {"code": 5, "name": "unknown"}
    assert json.loads(convert.error_payload(0, "none")) == {"code": 0, "name": "none"}


@pytest.mark.parametrize(("online", "expected"), [(True, "online"), (False, "offline")])
def test_availability_payload(online, expected):
    assert convert.availability_payload(online=online) == expected


def test_telemetry_payloads_full():
    telemetry = Telemetry(
        state="cleaning",
        battery=80,
        fan_speed="balanced",
        error_code=0,
        error_name="none",
        clean_area_m2=12.3,
        clean_time_s=845,
        dock_state="idle",
        main_brush_work_time=0,
        side_brush_work_time=0,
        filter_work_time=0,
        sensor_dirty_time=0,
    )
    payloads = convert.telemetry_payloads(telemetry)
    assert payloads["state"] == "cleaning"
    assert payloads["battery"] == "80"
    assert payloads["fan_speed"] == "balanced"
    assert json.loads(payloads["error"]) == {"code": 0, "name": "none"}
    assert payloads["cleaning/area_m2"] == "12.3"
    assert payloads["cleaning/time_s"] == "845"
    assert payloads["dock"] == "idle"
    assert json.loads(payloads["consumable/main_brush"]) == {
        "life_s": 1_080_000,
        "percent": 100,
        "work_time_s": 0,
    }
    assert set(payloads) >= {
        "consumable/main_brush",
        "consumable/side_brush",
        "consumable/filter",
        "consumable/sensor",
    }


def test_telemetry_payloads_omits_missing_fields():
    assert convert.telemetry_payloads(Telemetry()) == {}


def test_telemetry_payloads_integral_area_has_no_decimal():
    payloads = convert.telemetry_payloads(Telemetry(clean_area_m2=12.0))
    assert payloads["cleaning/area_m2"] == "12"


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ("start", Command.START),
        ("STOP", Command.STOP),
        (" pause ", Command.PAUSE),
        ("return", Command.RETURN),
        ("locate", Command.LOCATE),
    ],
)
def test_command_from_mqtt_known(payload, expected):
    assert convert.command_from_mqtt(payload) is expected


def test_command_from_mqtt_unknown():
    assert convert.command_from_mqtt("explode") is None
