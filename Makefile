.PHONY: venv test lint scan plan install

PY ?= .venv/bin/python

venv:
	python3 -m venv .venv && .venv/bin/pip install -q -e '.[dev]'

test:
	$(PY) -m pytest

install:
	pipx install .

# Offline demo against the sanitized fixture: no credentials, no network.
# The script lives in tools/ because a one-liner here was silently patching an attribute
# that no longer existed, so the "offline" demo was quietly calling real AWS.
demo:
	$(PY) tools/demo.py
