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

"""Prometheus metrics and the HTTP server for metrics and the status page.

The server exposes `/metrics` (Prometheus), `/healthz`, `/` (the status page),
and `/events` (Server-Sent Events that push status updates as the bridge's
state changes).
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass
from typing import TYPE_CHECKING

from aiohttp import web
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Gauge,
    GCCollector,
    PlatformCollector,
    ProcessCollector,
    generate_latest,
)

from .log import get_logger
from .status_page import StatusData, render_status_content, render_status_page

if TYPE_CHECKING:
    from collections.abc import Callable

_log = get_logger(__name__)

_SSE_HEADERS = {
    "Content-Type": "text/event-stream",
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
}

# Idle SSE connections get a comment heartbeat on this interval so a silently
# dropped client surfaces (its next write fails) within one interval instead of
# leaking its queue and handler task until the next state change.
_HEARTBEAT_INTERVAL = 60.0
_HEARTBEAT = b": ping\n\n"


@dataclass(frozen=True, slots=True)
class Metrics:
    """The bridge's Prometheus metrics."""

    roborock_connected: Gauge
    roborock_local: Gauge
    mqtt_connected: Gauge
    messages_published: Counter
    messages_received: Counter
    poll_errors: Counter
    last_poll: Gauge
    commands: Counter
    auth_error: Gauge
    battery: Gauge
    consumable_work_time: Gauge
    consumable_life: Gauge
    clean_area: Gauge
    clean_time: Gauge
    error_code: Gauge
    state_info: Gauge
    dock_state_info: Gauge


def create_metrics(registry: CollectorRegistry) -> Metrics:
    """Create the bridge metrics and register process/platform collectors."""
    ProcessCollector(registry=registry)
    PlatformCollector(registry=registry)
    GCCollector(registry=registry)
    return Metrics(
        roborock_connected=Gauge(
            "rockville_roborock_connected",
            "Whether each device has any Roborock connection (1) or not (0).",
            ["device"],
            registry=registry,
        ),
        roborock_local=Gauge(
            "rockville_roborock_local",
            "Whether each device is reachable over the LAN (1) or not (0).",
            ["device"],
            registry=registry,
        ),
        mqtt_connected=Gauge(
            "rockville_mqtt_connected",
            "Whether the bridge's MQTT connection is up (1) or down (0).",
            registry=registry,
        ),
        messages_published=Counter(
            "rockville_mqtt_messages_published_total",
            "Total MQTT messages published by the bridge.",
            registry=registry,
        ),
        messages_received=Counter(
            "rockville_mqtt_messages_received_total",
            "Total command MQTT messages received, by device.",
            ["device"],
            registry=registry,
        ),
        poll_errors=Counter(
            "rockville_poll_errors_total",
            "Total polling errors, by device.",
            ["device"],
            registry=registry,
        ),
        last_poll=Gauge(
            "rockville_last_poll_timestamp_seconds",
            "Unix timestamp of the last successful poll, by device.",
            ["device"],
            registry=registry,
        ),
        commands=Counter(
            "rockville_commands_total",
            "Total commands dispatched to devices, by device and command.",
            ["device", "command"],
            registry=registry,
        ),
        auth_error=Gauge(
            "rockville_auth_error",
            "Whether the Roborock session is in an auth-error state (1) or not (0).",
            registry=registry,
        ),
        battery=Gauge(
            "rockville_battery_percent",
            "Last reported battery percentage, by device.",
            ["device"],
            registry=registry,
        ),
        consumable_work_time=Gauge(
            "rockville_consumable_work_time_seconds",
            "Cumulative work time of each consumable since its last reset, by device.",
            ["device", "consumable"],
            registry=registry,
        ),
        consumable_life=Gauge(
            "rockville_consumable_life_seconds",
            "Rated replacement lifetime (the budget) of each consumable, by device.",
            ["device", "consumable"],
            registry=registry,
        ),
        clean_area=Gauge(
            "rockville_clean_area_square_meters",
            "Area cleaned on the current or most recent run, by device.",
            ["device"],
            registry=registry,
        ),
        clean_time=Gauge(
            "rockville_clean_time_seconds",
            "Time spent cleaning on the current or most recent run, by device.",
            ["device"],
            registry=registry,
        ),
        error_code=Gauge(
            "rockville_error_code",
            "Last reported error code, where 0 is no error, by device.",
            ["device"],
            registry=registry,
        ),
        state_info=Gauge(
            "rockville_state_info",
            "Current vacuum state as a label; the value is always 1, by device.",
            ["device", "state"],
            registry=registry,
        ),
        dock_state_info=Gauge(
            "rockville_dock_state_info",
            "Current dock state as a label; the value is always 1, by device.",
            ["device", "dock_state"],
            registry=registry,
        ),
    )


class StatusServer:
    """An aiohttp server for metrics, health checks, and the status page."""

    def __init__(
        self,
        registry: CollectorRegistry,
        get_status: Callable[[], StatusData],
    ) -> None:
        """Serve `registry` metrics and the status page rendered from `get_status`."""
        self._registry = registry
        self._get_status = get_status
        self._heartbeat_s = _HEARTBEAT_INTERVAL
        self._clients: set[asyncio.Queue[str]] = set()
        self._runner: web.AppRunner | None = None
        self.app = web.Application()
        self.app.add_routes(
            [
                web.get("/", self._index),
                web.get("/events", self._events),
                web.get("/metrics", self._metrics),
                web.get("/healthz", self._healthz),
            ],
        )

    async def start(self, host: str, port: int) -> None:
        """Start serving the metrics and status endpoints on `host:port`."""
        runner = web.AppRunner(self.app)
        await runner.setup()
        site = web.TCPSite(runner, host, port)
        await site.start()
        self._runner = runner
        _log.info("status server listening", host=host, port=port)

    async def close(self) -> None:
        """Stop the server and disconnect any SSE clients."""
        for queue in list(self._clients):
            queue.put_nowait(_CLOSE)
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None

    def notify(self) -> None:
        """Push the current status to all connected SSE clients."""
        if not self._clients:
            return
        content = render_status_content(self._get_status())
        for queue in self._clients:
            queue.put_nowait(content)

    async def _index(self, _request: web.Request) -> web.Response:
        return web.Response(
            text=render_status_page(self._get_status()), content_type="text/html"
        )

    async def _metrics(self, _request: web.Request) -> web.Response:
        # CONTENT_TYPE_LATEST already includes a charset, which aiohttp rejects in
        # the content_type argument, so set it as a raw header instead.
        return web.Response(
            body=generate_latest(self._registry),
            headers={"Content-Type": CONTENT_TYPE_LATEST},
        )

    async def _healthz(self, _request: web.Request) -> web.Response:
        return web.Response(text="ok")

    async def _events(self, request: web.Request) -> web.StreamResponse:
        response = web.StreamResponse(headers=_SSE_HEADERS)
        await response.prepare(request)
        queue: asyncio.Queue[str] = asyncio.Queue()
        self._clients.add(queue)
        # A write to a dropped client raises ConnectionError (reset, broken pipe,
        # aborted); swallow it and let the finally clause reap the queue. A clean
        # disconnect cancels the handler instead — let that CancelledError
        # propagate so aiohttp finalizes the request normally.
        try:
            with contextlib.suppress(ConnectionError):
                await response.write(_sse(render_status_content(self._get_status())))
                while True:
                    try:
                        content = await asyncio.wait_for(queue.get(), self._heartbeat_s)
                    except TimeoutError:
                        # A write to a silently dropped client raises here, so
                        # the finally clause reaps it within one heartbeat.
                        await response.write(_HEARTBEAT)
                        continue
                    if content is _CLOSE:
                        break
                    await response.write(_sse(content))
        finally:
            self._clients.discard(queue)
        return response


_CLOSE = "\x00close"


def _sse(content: str) -> bytes:
    lines = "".join(f"data: {line}\n" for line in content.split("\n"))
    return f"{lines}\n".encode()
