# tiffin — Windows-native. There is no `make` on the box; the targets document the commands
# and run anywhere make exists. On the box use the venv executables directly:
#   .venv\Scripts\ruff.exe check . && .venv\Scripts\ruff.exe format --check .
#   .venv\Scripts\pyright.exe
#   .venv\Scripts\python.exe -m pytest --cov=tiffin --cov-fail-under=70
export UV_CACHE_DIR ?= .uv-cache
PY := .venv/Scripts/python.exe
RUFF := .venv/Scripts/ruff.exe
PYRIGHT := .venv/Scripts/pyright.exe

.PHONY: sync check fmt lint types test cov e2e run doctor

sync:
	uv sync

check: lint types test

lint:
	$(RUFF) check .
	$(RUFF) format --check .

fmt:
	$(RUFF) check --fix .
	$(RUFF) format .

types:
	$(PYRIGHT)

test:
	$(PY) -m pytest

cov:
	$(PY) -m pytest --cov=tiffin --cov-report=term-missing --cov-fail-under=70

run:
	.venv/Scripts/tiffin.exe run

doctor:
	.venv/Scripts/tiffin.exe doctor
