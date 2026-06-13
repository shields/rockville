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

# Prettier formats the non-Python files (Markdown, YAML, JSON5). It runs via
# npx so local and CI use the same version without a committed Node project.
PRETTIER = npx --yes prettier@3 --no-error-on-unmatched-pattern
DOCS = "**/*.md" "**/*.yaml" "**/*.yml" "**/*.json5"
IMAGE ?= rockville:dev

.PHONY: lint fmt test run image

lint:
	uv run ruff format --check .
	uv run ruff check .
	uv run ty check src
	$(PRETTIER) --check $(DOCS)

fmt:
	uv run ruff format .
	uv run ruff check --fix .
	$(PRETTIER) --write $(DOCS)

test:
	uv run coverage run -m pytest
	uv run coverage report --fail-under=100

run:
	uv run rockville run

image:
	docker build -t $(IMAGE) .
