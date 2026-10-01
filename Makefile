.PHONY: setup run test build
PYTHON ?= python3
setup:
	"$(PYTHON)" -c 'import sys; sys.version_info >= (3, 12) or sys.exit("Python 3.12+ required. Run make setup PYTHON=/path/to/python3.12-or-newer")'
	"$(PYTHON)" -m venv .venv
	.venv/bin/python -c 'import sys; sys.version_info >= (3, 12) or sys.exit("Existing .venv uses old Python. Rename .venv, then run make setup again.")'
	.venv/bin/python -m pip install --upgrade pip
	.venv/bin/python -m pip install -r requirements.lock
run:
	.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000
test:
	.venv/bin/python -m pytest -q
build:
	.venv/bin/python scripts/build.py
