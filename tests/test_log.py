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

"""Tests for logging configuration."""

import io
import json

from rockville import log


def test_console_renderer_emits():
    stream = io.StringIO()
    log.configure(level="INFO", log_format="console", stream=stream)
    log.get_logger("test").info("hello", key="value")
    output = stream.getvalue()
    assert "hello" in output
    assert "value" in output


def test_json_renderer_emits():
    stream = io.StringIO()
    log.configure(level="DEBUG", log_format="json", stream=stream)
    log.get_logger("test").warning("careful", count=3)
    record = json.loads(stream.getvalue().splitlines()[-1])
    assert record["event"] == "careful"
    assert record["count"] == 3
    assert record["level"] == "warning"
