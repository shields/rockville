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

# uv assembles a virtualenv with a managed (relocatable) CPython, which is then
# copied onto a distroless runtime. cc-debian13 provides glibc and libstdc++,
# which the native wheels (pycryptodome, aiohttp, paho-mqtt) need. Digests pin
# the exact images and are kept current by Renovate.
FROM ghcr.io/astral-sh/uv:0.13.8-trixie-slim@sha256:2e5596835973a75bed0d37a980625b5089e87ffbd1026f11012ca0fe254ccac5 AS build
ENV UV_PYTHON_INSTALL_DIR=/python \
    UV_PYTHON_PREFERENCE=only-managed \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy
WORKDIR /app
RUN uv python install 3.15.1
# Install dependencies first (without the project) so this layer is cached
# across source changes.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project --no-dev
COPY README.md ./
COPY src ./src
RUN uv sync --frozen --no-dev

FROM gcr.io/distroless/cc-debian13:nonroot@sha256:cc66d222f0a7ec996b78a99fe76a969bc80b539a5016ad866c14d1949bc6d0ab
ARG GIT_VERSION=unknown
ENV ROCKVILLE_VERSION=${GIT_VERSION} \
    CONFIG_PATH=/config/config.yaml \
    PERSIST_PATH=/persist \
    PATH=/app/.venv/bin:$PATH
COPY --from=build /python /python
COPY --from=build /app /app
WORKDIR /app
# distroless :nonroot is uid 65532; the persist volume is made group-writable
# for that uid via fsGroup in the Kubernetes manifest.
USER 65532:65532
ENTRYPOINT ["/app/.venv/bin/python", "-m", "rockville"]
CMD ["run"]
