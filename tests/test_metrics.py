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

"""Tests for Prometheus metrics and the status HTTP server."""

from aiohttp.test_utils import TestClient, TestServer
from prometheus_client import CollectorRegistry

from rockville import metrics
from rockville.status_page import DeviceStatus, StatusData

_OK = 200


def status() -> StatusData:
    return StatusData(
        bridge_name="rockville",
        version="1.0",
        mqtt_url="mqtt://broker:1883",
        mqtt_connected=True,
        roborock_authenticated=True,
        devices=(
            DeviceStatus(
                name="vac",
                duid="d",
                present=True,
                online=True,
                local=True,
                payloads={"state": "cleaning"},
            ),
        ),
    )


async def _read_event(resp) -> str:
    lines = []
    while True:
        raw = await resp.content.readline()
        if not raw or raw in (b"\n", b"\r\n"):
            break
        lines.append(raw.decode())
    return "".join(lines)


def test_create_metrics_registers_families():
    registry = CollectorRegistry()
    created = metrics.create_metrics(registry)
    created.mqtt_connected.set(1)
    created.messages_received.labels(device="vac").inc()
    created.commands.labels(device="vac", command="start").inc()
    created.battery.labels(device="vac").set(80)
    names = {family.name for family in registry.collect()}
    assert "rockville_mqtt_connected" in names
    assert "rockville_commands" in names


async def test_health_index_and_metrics_routes():
    registry = CollectorRegistry()
    metrics.create_metrics(registry)
    server = metrics.StatusServer(registry, status)
    async with TestClient(TestServer(server.app)) as client:
        health = await client.get("/healthz")
        assert health.status == _OK
        assert await health.text() == "ok"

        index = await client.get("/")
        assert index.status == _OK
        assert "rockville" in await index.text()

        scrape = await client.get("/metrics")
        assert scrape.status == _OK
        assert "rockville_" in await scrape.text()


async def test_notify_without_clients_is_noop():
    server = metrics.StatusServer(CollectorRegistry(), status)
    server.notify()


async def test_events_streams_initial_and_pushed_updates():
    server = metrics.StatusServer(CollectorRegistry(), status)
    async with (
        TestClient(TestServer(server.app)) as client,
        client.get("/events") as resp,
    ):
        assert resp.status == _OK
        assert "rockville" in await _read_event(resp)
        server.notify()
        assert "rockville" in await _read_event(resp)


async def test_events_sends_heartbeat_when_idle():
    server = metrics.StatusServer(CollectorRegistry(), status)
    server._heartbeat_s = 0.01
    async with (
        TestClient(TestServer(server.app)) as client,
        client.get("/events") as resp,
    ):
        await _read_event(resp)  # initial snapshot
        # No notify(); the next chunk is the idle heartbeat comment.
        assert ": ping" in await _read_event(resp)


async def test_events_ends_when_server_closes():
    server = metrics.StatusServer(CollectorRegistry(), status)
    async with (
        TestClient(TestServer(server.app)) as client,
        client.get("/events") as resp,
    ):
        await _read_event(resp)
        await server.close()
        assert await resp.content.read() == b""


async def test_start_and_close_lifecycle():
    server = metrics.StatusServer(CollectorRegistry(), status)
    await server.start("127.0.0.1", 0)
    await server.close()
