UV_CACHE_DIR ?= /tmp/aimos-uv-cache
export UV_CACHE_DIR
.PHONY: setup build run demo test acceptance index

setup:
	uv sync --python 3.12
	cd frontend && npm ci

build:
	cd frontend && npm run build

run: setup build
	uv run uvicorn door.main:app --host 127.0.0.1 --port 8000

# The real app and console on built-in fixtures: no keys, no network, no licensed data.
demo: setup build
	uv run python scripts/run_fixture_server.py --port 8766

test:
	uv run pytest tests/unit -q
	npm --prefix frontend test

acceptance:
	uv run python scripts/run_acceptance.py

index:
	uv run python scripts/build_index.py
