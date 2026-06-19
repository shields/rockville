# rockville

A small, local-first bridge that exposes a Roborock vacuum on your own MQTT
broker — state, telemetry, and control — so it can be driven from Home Assistant
or any MQTT client. It is built for the Roborock Q5 Max+ and other V1-protocol
Roborock vacuums.

rockville is modeled on [hoboken](https://github.com/shields/hoboken) and
follows the conventions in
[right-answers](https://github.com/shields/right-answers).

## How it connects

rockville talks to the vacuum through the
[`python-roborock`](https://github.com/Python-roborock/python-roborock) library.
A one-time cloud login fetches the device list, each device's local key, and its
LAN address; everything is cached on disk as JSON. After that, routine status
polling and commands run **over the LAN**, so there are no cloud rate limits and
the bridge keeps working when Roborock's cloud is down.

This is local-_preferred_, not strictly cloud-free: `python-roborock` still
opens a best-effort connection to Roborock's own cloud MQTT for push updates.
rockville does not depend on it — control and status use the local connection —
but it is opened opportunistically. The vacuum's LAN address is discovered
through one cloud call and then cached (refreshed roughly every 12 hours), so a
DHCP reservation, or the optional static `ip` in the config, keeps the bridge
from needing the cloud to find the vacuum.

## Requirements

- Python 3.14, managed with [uv](https://docs.astral.sh/uv/).
- A Roborock cloud account and a V1-protocol vacuum (e.g. the Q5 Max+).
- Your own MQTT broker (e.g. Mosquitto).

## Quick start

```sh
uv sync
```

### Authenticate

The bridge authenticates unattended with your account password. Set it in the
environment and rockville logs in on startup:

```sh
export ROBOROCK_PASSWORD='your-password'
```

The first successful login is cached to the persist directory, so later starts
reuse the saved session instead of logging in again. If a login fails (for
example, Roborock's rate limit after too many attempts), the bridge stays up and
retries with exponential backoff rather than exiting — so a restart loop can't
keep hammering the login endpoint. A write failure to the persist directory
(read-only or full volume) is logged but is non-fatal too: the bridge keeps
running on the in-memory session rather than crashing into that same loop. Keep
the persist directory on durable, writable storage so the cached session
survives restarts.

If password login is unavailable for your account, use the interactive
email-code fallback once to cache credentials:

```sh
uv run rockville login
```

### Configure

Copy `config.example.yaml` to `config.yaml` and edit it. A static `ip` (with a
DHCP reservation) lets rockville skip the cloud address lookup.

Each vacuum is identified by its DUID. Find it in the Roborock app, or list
every device on the account with the `roborock` CLI bundled with
`python-roborock`:

```sh
# One-time login; omit --password to be prompted for an emailed code instead.
uv run --with pyshark,pyyaml roborock login --email you@example.com --password "$ROBOROCK_PASSWORD"

# Print a {name: duid} map of every device on the account.
uv run --with pyshark,pyyaml roborock list-devices
```

The `--with pyshark,pyyaml` is required until `python-roborock`
[PR#853](https://github.com/Python-roborock/python-roborock/pull/853) is merged.

### Run

```sh
CONFIG_PATH=config.yaml PERSIST_PATH=./persist uv run rockville run
```

### Logging

Logs go to stderr. Set `LOG_FORMAT=json` for machine-readable output (anything
else renders a human-friendly console format). `LOG_LEVEL` sets the threshold
and must be an **uppercase** standard level name — `DEBUG`, `INFO` (the
default), `WARNING`, `ERROR`, or `CRITICAL`.

## MQTT topics

All topics are published under `{topic_prefix}/{device}` (the prefix defaults to
`rockville` and `device` is the configured name). State topics are **retained**;
command topics are not.

### State (published)

| Topic                          | Example payload                     | Notes                                                  |
| ------------------------------ | ----------------------------------- | ------------------------------------------------------ |
| `…/availability`               | `online` / `offline`                | Per-device reachability.                               |
| `…/state`                      | `cleaning`                          | The vacuum's state name (e.g. `idle`, `charging`).     |
| `…/battery`                    | `80`                                | Battery percentage.                                    |
| `…/fan_speed`                  | `balanced`                          | Current fan-speed name.                                |
| `…/error`                      | `{"code":0,"name":"none"}`          | Decoded error code.                                    |
| `…/cleaning/area_m2`           | `12.3`                              | Area cleaned this run, in m².                          |
| `…/cleaning/time_s`            | `845`                               | Time cleaned this run, in seconds.                     |
| `…/dock`                       | `charging`                          | Synthesized dock state.                                |
| `…/connection`                 | `local` / `cloud` / `offline`       | How the vacuum is currently reachable.                 |
| `…/consumable/main_brush`      | `{"hours_left":248.0,"percent":83}` | Remaining life. Also `side_brush`, `filter`, `sensor`. |
| `{prefix}/bridge/availability` | `online` / `offline`                | Bridge liveness (the MQTT Last-Will topic).            |

### Commands (subscribed)

| Topic             | Payload                                  | Action              |
| ----------------- | ---------------------------------------- | ------------------- |
| `…/command/set`   | `start` `stop` `pause` `return` `locate` | Control the vacuum. |
| `…/fan_speed/set` | a fan-speed name (e.g. `balanced`)       | Set the fan speed.  |

Try it with the Mosquitto clients:

```sh
mosquitto_sub -t 'rockville/#' -v
mosquitto_pub -t 'rockville/downstairs/command/set' -m start
```

## Metrics and status page

When `metrics` is configured, rockville serves on that port:

- `/metrics` — Prometheus metrics (`rockville_*`).
- `/` — a live status page that updates over Server-Sent Events.
- `/healthz` — a liveness check.

## Deployment

### Docker

```sh
docker build -t rockville .
docker run --rm \
  -v "$PWD/config.yaml:/config/config.yaml:ro" \
  -v rockville-persist:/persist \
  -e ROBOROCK_PASSWORD -e MQTT_PASSWORD \
  rockville
```

The image is distroless and runs as a non-root user.

### Kubernetes

`k8s/deployment.yaml` is a starting point. It uses `hostNetwork` so the pod can
reach the vacuum on the LAN, a `PersistentVolumeClaim` for the cached
credentials and device cache, and a `Secret` for the Roborock and MQTT
passwords. Create the config and secret first:

```sh
kubectl create configmap rockville-config --from-file=config.yaml
kubectl create secret generic rockville-secrets \
  --from-literal=roborock-password=… --from-literal=mqtt-password=…
```

## Development

```sh
make lint   # ruff format check, ruff, ty (src), prettier
make test   # pytest with 100% coverage enforced
make run    # run the bridge locally
make image  # build the container image
```

The bridge core is tested without the network or a real broker: all
`python-roborock` access is behind an injected backend, and the MQTT client is
faked. The test suite holds 100% line and branch coverage.

## License

[Apache 2.0](LICENSE).
