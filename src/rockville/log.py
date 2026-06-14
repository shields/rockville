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

"""Structured logging configuration.

right-answers prescribes structlog for services. Output is human-friendly
console rendering by default; set `LOG_FORMAT=json` for machine-readable logs.
"""

import logging
import sys
from typing import TextIO

import structlog

_DEFAULT_LEVEL = "INFO"


def configure(
    *,
    level: str = _DEFAULT_LEVEL,
    log_format: str = "console",
    stream: TextIO | None = None,
) -> None:
    """Configure structlog process-wide.

    `level` must be a standard uppercase level name (`DEBUG`, `INFO`, `WARNING`,
    `ERROR`, `CRITICAL`); a lowercase or unknown name raises `KeyError`.
    `log_format` is `"json"` for machine-readable output or anything else for
    console rendering. `stream` defaults to stderr so logs never pollute a
    command's stdout.
    """
    renderer: structlog.types.Processor = (
        structlog.processors.JSONRenderer()
        if log_format == "json"
        else structlog.dev.ConsoleRenderer(colors=False)
    )
    structlog.configure(
        processors=[
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            renderer,
        ],
        # `level` must be a canonical uppercase name; we deliberately do not
        # `.upper()` it. Per the project's fail-fast policy a misconfigured
        # LOG_LEVEL should crash loudly at startup rather than be silently
        # coerced. The requirement is documented (README + the docstring above).
        wrapper_class=structlog.make_filtering_bound_logger(
            logging.getLevelNamesMapping()[level],
        ),
        logger_factory=structlog.PrintLoggerFactory(file=stream or sys.stderr),
        cache_logger_on_first_use=False,
    )


def get_logger(name: str) -> structlog.types.FilteringBoundLogger:
    """Return a bound logger for `name`."""
    return structlog.get_logger(name)
