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

"""The live status page: pure rendering of the bridge's state to HTML.

`StatusData` is a snapshot the bridge builds on demand; the render functions
here are pure so they unit-test without a socket. `render_status_content`
produces the inner HTML pushed over Server-Sent Events; `render_status_page`
wraps it in a full document with a reconnecting `EventSource` client.
"""

import html
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class DeviceStatus:
    """A single device's state for display on the status page."""

    name: str
    duid: str
    present: bool
    online: bool
    local: bool
    payloads: dict[str, str]


@dataclass(frozen=True, slots=True)
class StatusData:
    """A full snapshot of the bridge's state for display."""

    bridge_name: str
    version: str
    mqtt_url: str
    mqtt_connected: bool
    roborock_authenticated: bool
    devices: tuple[DeviceStatus, ...]


def _badge(*, ok: bool, yes: str, no: str) -> str:
    cls = "ok" if ok else "bad"
    return f'<span class="badge {cls}">{html.escape(yes if ok else no)}</span>'


def _device_html(device: DeviceStatus) -> str:
    if not device.present:
        conn = '<span class="badge bad">not found</span>'
    elif device.local:
        conn = '<span class="badge ok">local</span>'
    elif device.online:
        conn = '<span class="badge warn">cloud</span>'
    else:
        conn = '<span class="badge bad">offline</span>'
    if device.payloads:
        rows = "".join(
            f"<tr><td class=key>{html.escape(key)}</td>"
            f"<td>{html.escape(value)}</td></tr>"
            for key, value in sorted(device.payloads.items())
        )
        table = f"<table>{rows}</table>"
    else:
        table = '<p class="muted">No state received yet.</p>'
    return (
        f'<section class="device"><h2>{html.escape(device.name)} {conn}</h2>'
        f'<p class="muted">{html.escape(device.duid)}</p>{table}</section>'
    )


def render_status_content(data: StatusData) -> str:
    """Render the inner status HTML pushed over SSE and embedded in the page."""
    devices = "".join(_device_html(device) for device in data.devices)
    mqtt_badge = _badge(ok=data.mqtt_connected, yes="connected", no="disconnected")
    auth_badge = _badge(
        ok=data.roborock_authenticated, yes="authenticated", no="auth error"
    )
    return (
        f"<h1>{html.escape(data.bridge_name)}</h1>"
        f'<p class="muted">version {html.escape(data.version)}</p>'
        '<div class="status">'
        f"<div>MQTT {html.escape(data.mqtt_url)} {mqtt_badge}</div>"
        f"<div>Roborock {auth_badge}</div>"
        "</div>"
        f"{devices}"
    )


def render_status_page(data: StatusData) -> str:
    """Render the full HTML document with a reconnecting SSE client."""
    return (
        "<!doctype html>"
        "<html lang=en><head><meta charset=utf-8>"
        '<meta name=viewport content="width=device-width, initial-scale=1">'
        f"<title>{html.escape(data.bridge_name)}</title><style>{_STYLE}</style></head>"
        f"<body><div id=content>{render_status_content(data)}</div>"
        f"<script>{_SCRIPT}</script></body></html>"
    )


_STYLE = (
    "body{font-family:system-ui,sans-serif;max-width:48rem;margin:0 auto;"
    "padding:1rem;background:#f5f5f5;color:#222}"
    "h1{margin-bottom:0}h2{margin:.2rem 0;font-size:1.1rem}"
    ".muted{color:#888;font-size:.85rem;margin:.1rem 0}"
    ".status{display:flex;gap:1.5rem;margin:1rem 0;padding:.75rem;"
    "background:#fff;border-radius:8px}"
    ".device{background:#fff;border-radius:8px;padding:.75rem;margin:.75rem 0}"
    "table{border-collapse:collapse;font-family:monospace;font-size:.9rem}"
    "td{padding:.1rem .6rem;border-bottom:1px solid #eee}.key{color:#888}"
    ".badge{font-size:.75rem;padding:.05rem .4rem;border-radius:4px;color:#fff}"
    ".badge.ok{background:#2a2}.badge.warn{background:#c80}.badge.bad{background:#c22}"
)

_SCRIPT = (
    "var src=new EventSource('/events');"
    "src.onmessage=function(e){document.getElementById('content').innerHTML=e.data};"
)
