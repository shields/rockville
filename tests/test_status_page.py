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

"""Tests for status-page rendering."""

from rockville import status_page
from rockville.status_page import DeviceStatus, StatusData


def make_status(*, devices, mqtt_connected=True, authenticated=True):
    return StatusData(
        bridge_name="rockville",
        version="1.2.3",
        mqtt_url="mqtt://broker:1883",
        mqtt_connected=mqtt_connected,
        roborock_authenticated=authenticated,
        devices=tuple(devices),
    )


def test_local_device_renders_state():
    device = DeviceStatus(
        name="vac",
        duid="duid-1",
        present=True,
        online=True,
        local=True,
        payloads={"state": "cleaning", "battery": "80"},
    )
    html = status_page.render_status_content(make_status(devices=[device]))
    assert "rockville" in html
    assert "version 1.2.3" in html
    assert "connected" in html
    assert "authenticated" in html
    assert ">local<" in html
    assert "cleaning" in html
    assert "battery" in html


def test_cloud_offline_and_absent_devices():
    cloud = DeviceStatus(
        name="a", duid="d-a", present=True, online=True, local=False, payloads={}
    )
    offline = DeviceStatus(
        name="b", duid="d-b", present=True, online=False, local=False, payloads={}
    )
    absent = DeviceStatus(
        name="c", duid="d-c", present=False, online=False, local=False, payloads={}
    )
    html = status_page.render_status_content(
        make_status(
            devices=[cloud, offline, absent], mqtt_connected=False, authenticated=False
        )
    )
    assert ">cloud<" in html
    assert ">offline<" in html
    assert ">not found<" in html
    assert "disconnected" in html
    assert "auth error" in html
    assert "No state received yet." in html


def test_full_page_includes_event_source_and_escapes():
    device = DeviceStatus(
        name="<vac>",
        duid="d&1",
        present=True,
        online=True,
        local=True,
        payloads={"note": "<b>hi</b>"},
    )
    page = status_page.render_status_page(make_status(devices=[device]))
    assert page.startswith("<!doctype html>")
    assert "EventSource('/events')" in page
    assert "&lt;vac&gt;" in page
    assert "d&amp;1" in page
    assert "&lt;b&gt;hi&lt;/b&gt;" in page
    assert "<b>hi</b>" not in page
