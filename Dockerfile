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
FROM ghcr.io/astral-sh/uv:0.13.7-trixie-slim@sha256:f6da342fe598dc462a7e6f61e7b3bf7fc84e381467efbdf29ccc1d32f2fc68a4 AS build
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

FROM gcr.io/distroless/cc-debian13:nonroot@sha256:08476c244888bfb425bae1ae4dd7c17f093e972aab8426883d97f3c6f280f7c1
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
