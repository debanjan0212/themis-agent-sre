.PHONY: install demo chaos serve test lint scan bench ecosystem render lock image check

install:            ## install Themis and the dev tools
	pip install -e ".[dev,postgres]"

demo:               ## run Themis and a ten-agent world on this machine. No cloud, no key, no Docker
	themis demo

chaos:              ## the same, and inject the four faults one after another so you can watch the fleet respond
	themis demo --chaos

serve:              ## run the API and evaluator only (configure with THEMIS_* variables, see docs/CONFIGURATION.md)
	themis serve

test:               ## unit, property, API, control-loop and full-system tests (about a minute)
	pytest -q

lint:
	ruff check . && ruff format --check .

scan:               ## static analysis and the dependency audit that CI runs
	bandit -q -ll -r themis
	pip-audit -r requirements.lock --require-hashes --disable-pip --progress-spinner off

bench:              ## run the four chaos experiments under load and rewrite docs/RESULTS.md from what was measured
	themis bench --out docs

ecosystem:          ## clone agent-harness-sdk, Aegis War Room, Argus and IntentOps and test Themis against them
	./scripts/ecosystem.sh

render:             ## regenerate deploy/kubernetes from deploy/intent.yaml (needs IntentOps: `make ecosystem` installs it)
	.siblings/venv/bin/python -m intentops check deploy/intent.yaml
	rm -rf .render deploy/kubernetes && .siblings/venv/bin/python -m intentops render deploy/intent.yaml --format manifests -o .render
	mkdir -p deploy/kubernetes && cp .render/kind/themis/*.yaml deploy/kubernetes/ && rm -rf .render

lock:               ## refresh the hashed lock file after changing dependencies in pyproject.toml
	uv pip compile pyproject.toml --extra postgres --universal --generate-hashes -o requirements.lock

image:              ## build the container image
	docker build -t themis-agent-sre:dev .

check: lint test scan   ## everything CI runs except the ecosystem job. Do this before every push
