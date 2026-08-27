.PHONY: sync lint test build smoke

sync:
	uv sync --locked --group dev

lint:
	uv run ruff format --check .
	uv run ruff check .
	uv run mypy src/oria

test:
	uv run pytest -m "not live and not enterprise and not performance"

build:
	uv build

smoke:
	uv run oria --version
