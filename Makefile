.PHONY: setup run test build
setup:
	python3 -m venv .venv
	.venv/bin/pip install -r requirements.lock
run:
	.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000
test:
	.venv/bin/python -m pytest -q
build:
	.venv/bin/python scripts/build.py
