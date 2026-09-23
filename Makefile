.PHONY: venv test lint scan plan install

PY ?= .venv/bin/python

venv:
	python3 -m venv .venv && .venv/bin/pip install -q -e '.[dev]'

test:
	$(PY) -m pytest

install:
	pipx install .

# Offline demo against the sanitized fixture: no credentials, no network.
demo:
	$(PY) -c "import sys; sys.path[:0]=['src','tests']; \
	from conftest import load_fixture; import rewind.cli as c; \
	f=load_fixture('agent_session.json'); c._source=lambda r,a: f.source(); \
	c.main(['plan','--identity','perf-agent','--start',f.start_time.isoformat(), \
	'--end',f.end_time.isoformat(),'--region','us-west-1','--explain'], now=f.end_time)"
