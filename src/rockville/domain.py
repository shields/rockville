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

"""Domain types shared across the bridge.

These types decouple the rest of the bridge from `python-roborock`: the
Roborock adapter (`roborock_client`) translates library objects into a
`Telemetry` snapshot and translates a `Command` into a `RoborockCommand`, while
`convert` and `bridge` depend only on these plain types.
"""

from dataclasses import dataclass
from enum import StrEnum


class Command(StrEnum):
    """A control command the bridge accepts over MQTT.

    The string value is the exact MQTT command payload.
    """

    START = "start"
    STOP = "stop"
    PAUSE = "pause"
    RETURN = "return"
    LOCATE = "locate"


@dataclass(frozen=True, slots=True)
class Telemetry:
    """A flat snapshot of a vacuum's reported state.

    Every field is optional: a freshly discovered device reports values
    incrementally, and unsupported fields stay `None` so the bridge never
    publishes a topic for data the device does not provide.
    """

    state: str | None = None
    battery: int | None = None
    fan_speed: str | None = None
    error_code: int | None = None
    error_name: str | None = None
    clean_area_m2: float | None = None
    clean_time_s: int | None = None
    dock_state: str | None = None
    main_brush_work_time: int | None = None
    side_brush_work_time: int | None = None
    filter_work_time: int | None = None
    sensor_dirty_time: int | None = None
