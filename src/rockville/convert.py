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

"""Pure conversions between the domain model and MQTT payloads.

This is the testable heart of the bridge: every function here is pure, so the
round-trips (telemetry → MQTT payloads, MQTT command string → `Command`) are
exhaustively unit-tested without any I/O. Consumable replacement budgets are
imported from `python-roborock` so they never drift from the library.
"""

import json

from roborock.const import (
    FILTER_REPLACE_TIME,
    MAIN_BRUSH_REPLACE_TIME,
    SENSOR_DIRTY_REPLACE_TIME,
    SIDE_BRUSH_REPLACE_TIME,
)

from .domain import Command, Telemetry

_SECONDS_PER_HOUR = 3600
_FULL_PERCENT = 100

# (topic suffix, Telemetry field, replacement budget in seconds).
_CONSUMABLES: tuple[tuple[str, str, int], ...] = (
    ("main_brush", "main_brush_work_time", MAIN_BRUSH_REPLACE_TIME),
    ("side_brush", "side_brush_work_time", SIDE_BRUSH_REPLACE_TIME),
    ("filter", "filter_work_time", FILTER_REPLACE_TIME),
    ("sensor", "sensor_dirty_time", SENSOR_DIRTY_REPLACE_TIME),
)


def percent_remaining(work_time: int, replace_time: int) -> int:
    """Return the percentage of consumable life remaining, clamped to 0–100."""
    remaining = (replace_time - work_time) / replace_time * _FULL_PERCENT
    return max(0, min(_FULL_PERCENT, round(remaining)))


def hours_remaining(work_time: int, replace_time: int) -> float:
    """Return the hours of consumable life remaining, clamped at 0 and rounded."""
    remaining = max(0, replace_time - work_time)
    return round(remaining / _SECONDS_PER_HOUR, 1)


def consumable_payload(work_time: int, replace_time: int) -> str:
    """Render a consumable's remaining life as a JSON payload."""
    return _json(
        {
            "percent": percent_remaining(work_time, replace_time),
            "hours_left": hours_remaining(work_time, replace_time),
        },
    )


def error_payload(code: int, name: str | None) -> str:
    """Render an error code and its name as a JSON payload."""
    return _json({"code": code, "name": name or "unknown"})


def availability_payload(*, online: bool) -> str:
    """Return the retained availability payload."""
    return "online" if online else "offline"


def telemetry_payloads(telemetry: Telemetry) -> dict[str, str]:
    """Map a telemetry snapshot to `{topic suffix: payload}`.

    Only fields the device actually reported are included, so the bridge never
    publishes a topic for missing data.
    """
    payloads: dict[str, str] = {}
    if telemetry.state is not None:
        payloads["state"] = telemetry.state
    if telemetry.battery is not None:
        payloads["battery"] = str(telemetry.battery)
    if telemetry.fan_speed is not None:
        payloads["fan_speed"] = telemetry.fan_speed
    if telemetry.error_code is not None:
        payloads["error"] = error_payload(telemetry.error_code, telemetry.error_name)
    if telemetry.clean_area_m2 is not None:
        payloads["cleaning/area_m2"] = _number(telemetry.clean_area_m2)
    if telemetry.clean_time_s is not None:
        payloads["cleaning/time_s"] = str(telemetry.clean_time_s)
    if telemetry.dock_state is not None:
        payloads["dock"] = telemetry.dock_state
    for suffix, field_name, budget in _CONSUMABLES:
        work_time = getattr(telemetry, field_name)
        if work_time is not None:
            payloads[f"consumable/{suffix}"] = consumable_payload(work_time, budget)
    return payloads


def command_from_mqtt(payload: str) -> Command | None:
    """Parse an MQTT command payload into a `Command`, or `None` if unknown."""
    try:
        return Command(payload.strip().lower())
    except ValueError:
        return None


def _number(value: float) -> str:
    """Render a float without a trailing `.0` when it is integral."""
    return str(int(value)) if value.is_integer() else str(value)


def _json(payload: dict[str, object]) -> str:
    return json.dumps(payload, separators=(",", ":"), sort_keys=True)
