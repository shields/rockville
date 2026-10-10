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
FROM ghcr.io/astral-sh/uv:trixie-slim@sha256:11bee95ce27982f00acb0c85aa1b5ee6203a9bb958b7def6f27ead851d454e8d AS build
ENV UV_PYTHON_INSTALL_DIR=/python \
    UV_PYTHON_PREFERENCE=only-managed \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy
WORKDIR /app
RUN uv python install 3.14
# Install dependencies first (without the project) so this layer is cached
# across source changes.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project --no-dev
COPY README.md ./
COPY src ./src
RUN uv sync --frozen --no-dev

FROM gcr.io/distroless/cc-debian13:nonroot@sha256:e792ab3d241a468a4fd7519ddbbebe66b49b5f365771716ea688ad40b6c6f1c2
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
