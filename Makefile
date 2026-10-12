# Makefile for local-data-platform
#
# Every target runs tools through $(PY), the project virtualenv's Python by default.
# CI, which has no .venv, passes its own interpreter:  make lint PY=python LDP=ldp

PYTHON   ?= python3
VENV     ?= .venv
PY       ?= $(VENV)/bin/python
LDP      ?= $(VENV)/bin/ldp
DEMO_DIR ?= ldp_demo
LINT_PATHS := src tests examples samples scripts

# The taxi demo's config. The other demos reuse its data, so they run 'ldp demo' first when it is
# missing. That also keeps the order safe: 'ldp demo' refuses a non-empty folder it didn't create,
# so it must own $(DEMO_DIR) before the other demos add their subfolders to it.
DEMO_CONFIG  := $(DEMO_DIR)/rides.json
ROBOTICS_DIR ?= $(DEMO_DIR)/robotics
# Extra arguments for examples/robot_episodes/run.py, e.g. ROBOTICS_ARGS=--spark for its Scala Spark aggregate.
ROBOTICS_ARGS ?=

# The JVM demos and tests. scala-cli provisions its own JDK (Temurin 17) and the Maven jars on first use.
SCALA_CLI ?= $(firstword $(shell command -v scala-cli 2>/dev/null) $(wildcard $(HOME)/.local/bin/scala-cli) scala-cli)
SCALA_CLI_HINT := brew install Virtuslab/scala-cli/scala-cli, or see https://scala-cli.virtuslab.org/install

# demo-rest: the Iceberg REST catalog fixture (Apache Iceberg's RESTCatalogServer on a SQLite JdbcCatalog and
# a file:// warehouse under $(REST_DIR)), run with scala-cli, and a REST-backed copy of the demo config.
REST_FIXTURE ?= tools/rest_fixture
REST_HOST    ?= 127.0.0.1
REST_PORT    ?= 8181
REST_WAIT    ?= 600
REST_DIR     ?= $(DEMO_DIR)/rest
REST_CONFIG  ?= $(REST_DIR)/rides_rest.json

.PHONY: help all install lint test demo build smoke docs serve-docs generate-docs clean \
	demo-robotics demo-agent demo-spark demo-rest demo-all spark-test rest-test check-scala-cli

help:
	@echo "Usage: make [target]"
	@echo ""
	@echo "  install        Create $(VENV) if missing and install the package with the dev and docs extras"
	@echo "  lint           Run flake8 on $(LINT_PATHS)"
	@echo "  test           Run the pytest suite (offline; the Spark and REST tests skip)"
	@echo "  demo           Run the end-to-end taxi demo into ./$(DEMO_DIR)"
	@echo "  demo-robotics  Run the robot-episode example into ./$(ROBOTICS_DIR) (ROBOTICS_ARGS=--spark adds its"
	@echo "                 Scala Spark aggregate)"
	@echo "  demo-agent     Start the read-only MCP server on the demo table and run a scripted agent client"
	@echo "  demo-spark     Run the Scala Spark job on the demo table, then read its output back with pyiceberg"
	@echo "                 (needs scala-cli)"
	@echo "  demo-rest      Start the Iceberg REST catalog fixture ($(REST_FIXTURE)) on port $(REST_PORT), run a"
	@echo "                 REST-backed demo config against it, then stop it (needs scala-cli)"
	@echo "  demo-all       demo, demo-robotics and demo-agent, plus demo-spark and demo-rest when scala-cli is found"
	@echo "  spark-test     Run the opt-in Spark integration tests (LDP_RUN_SPARK=1 pytest -m spark)"
	@echo "  rest-test      Run the opt-in REST catalog integration tests (LDP_RUN_REST=1 pytest -m rest)"
	@echo "  build          Build the sdist and wheel into dist/"
	@echo "  smoke          Install the built wheel in a fresh venv, then run 'ldp --version' and 'ldp demo'"
	@echo "  docs           Build the docs with 'mkdocs build --strict'"
	@echo "  serve-docs     Serve the docs on http://127.0.0.1:8000"
	@echo "  generate-docs  Refresh docs/user_issues.md from the GitHub API (needs network)"
	@echo "  clean          Remove build, test, docs and demo artefacts"
	@echo "  all            lint, test, docs, build and smoke: what CI runs"

all: lint test docs build smoke
	@echo "--> All checks passed."

# ------------------------------------------------------------------------------
# Setup
# ------------------------------------------------------------------------------

install:
	@test -x $(VENV)/bin/python || $(PYTHON) -m venv $(VENV)
	$(VENV)/bin/python -m pip install -e ".[dev,docs]"

# ------------------------------------------------------------------------------
# Quality
# ------------------------------------------------------------------------------

lint:
	$(PY) -m flake8 $(LINT_PATHS)

test:
	$(PY) -m pytest tests

# The first run downloads a JDK, Spark and Iceberg (about 500 MB); see docs/spark.md.
spark-test: check-scala-cli
	LDP_RUN_SPARK=1 $(PY) -m pytest -m spark tests

rest-test: check-scala-cli
	LDP_RUN_REST=1 LDP_SCALA_CLI=$(SCALA_CLI) $(PY) -m pytest -m rest tests

# ------------------------------------------------------------------------------
# Demos
# ------------------------------------------------------------------------------

demo:
	$(LDP) demo --workdir $(DEMO_DIR)

$(DEMO_CONFIG):
	$(LDP) demo --workdir $(DEMO_DIR)

demo-robotics: $(DEMO_CONFIG)
	$(PY) examples/robot_episodes/run.py --workdir $(ROBOTICS_DIR) $(ROBOTICS_ARGS)

demo-agent: $(DEMO_CONFIG)
	$(PY) examples/agent_client.py --config $(DEMO_CONFIG)

# The Scala job reads demo.rides from the demo's SQLite catalog, prints revenue by city per day and the
# snapshot history, and writes demo.rides_by_city_day, which pyiceberg then reads back.
demo-spark: $(DEMO_CONFIG) check-scala-cli
	$(SCALA_CLI) run spark -q --suppress-outdated-dependency-warning -- \
		--catalog-name demo --catalog-db $(DEMO_DIR)/warehouse/demo_catalog.db --warehouse $(DEMO_DIR)/warehouse \
		--namespace demo --table rides --output-table rides_by_city_day
	$(PY) -c "import sys; from local_data_platform.format.iceberg import Iceberg; \
		t = Iceberg('rides_by_city_day', {'identifier': 'demo', 'warehouse_path': sys.argv[1]}); \
		print('--> pyiceberg reads', t.identifier, 'written by Spark:', t.row_count(), 'rows')" $(DEMO_DIR)/warehouse

# Starts the fixture in its own process session (so stopping it also stops the JVM that scala-cli
# launches), waits for the port, writes a copy of the demo config whose target catalog is the REST
# server, checks the catalog, runs the config twice (overwrite, so the second run leaves the row count
# unchanged), lists the snapshots, queries the table and stops the fixture, also when a step fails.
demo-rest: $(DEMO_CONFIG) check-scala-cli
	@set -e; \
	if [ ! -e "$(REST_FIXTURE)" ]; then echo "No Iceberg REST fixture at $(REST_FIXTURE)." >&2; exit 1; fi; \
	if $(PY) -c "import socket; socket.create_connection(('$(REST_HOST)', $(REST_PORT)), timeout=1).close()" \
		2>/dev/null; then \
		echo "Port $(REST_PORT) on $(REST_HOST) is already in use. Stop that server, or pass REST_PORT=<free port>." >&2; \
		exit 1; \
	fi; \
	mkdir -p "$(REST_DIR)/warehouse"; \
	log="$(REST_DIR)/rest_fixture.log"; \
	echo "--> Starting the Iceberg REST fixture $(REST_FIXTURE) on $(REST_HOST):$(REST_PORT) (log: $$log)"; \
	pid=$$($(PY) -c "import subprocess, sys; log = open(sys.argv[1], 'w'); \
		print(subprocess.Popen(sys.argv[2:], stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, \
		start_new_session=True).pid)" "$$log" $(SCALA_CLI) run $(REST_FIXTURE) -q --suppress-outdated-dependency-warning -- \
		--port $(REST_PORT) --warehouse "$(abspath $(REST_DIR))/warehouse"); \
	stop() { \
		echo "--> Stopping the REST fixture (process group $$pid)"; \
		$(PY) -c "import os, signal, sys; os.killpg(int(sys.argv[1]), signal.SIGTERM)" $$pid 2>/dev/null || true; \
		n=0; while kill -0 $$pid 2>/dev/null && [ $$n -lt 20 ]; do sleep 1; n=$$((n + 1)); done; \
		$(PY) -c "import os, signal, sys; os.killpg(int(sys.argv[1]), signal.SIGKILL)" $$pid 2>/dev/null || true; \
	}; \
	trap stop EXIT; \
	trap 'exit 130' INT TERM; \
	waited=0; \
	until $(PY) -c "import socket; socket.create_connection(('$(REST_HOST)', $(REST_PORT)), timeout=1).close()" \
		2>/dev/null; do \
		if ! kill -0 $$pid 2>/dev/null; then \
			echo "The REST fixture exited before opening port $(REST_PORT). Its log:" >&2; tail -n 40 "$$log" >&2; exit 1; \
		fi; \
		if [ $$waited -ge $(REST_WAIT) ]; then \
			echo "The REST fixture did not open port $(REST_PORT) within $(REST_WAIT)s. Its log:" >&2; \
			tail -n 40 "$$log" >&2; exit 1; \
		fi; \
		sleep 1; waited=$$((waited + 1)); \
	done; \
	echo "--> REST catalog is up on http://$(REST_HOST):$(REST_PORT) after $${waited}s"; \
	$(PY) -c "import json, sys; out, csv, uri, warehouse = sys.argv[1:5]; json.dump({ \
		'identifier': 'rest_demo_rides', 'who': 'analyst', 'what': 'rides', 'how': 'batch', 'metadata': { \
		'source': {'name': 'rides', 'format': 'CSV', 'path': csv}, \
		'target': {'name': 'rides', 'format': 'ICEBERG', 'write_mode': 'overwrite', 'catalog': { \
		'type': 'rest', 'name': 'ldp_rest_demo', 'uri': uri, 'warehouse': warehouse, 'namespace': 'demo'}}, \
		'quality': {'on_failure': 'fail', 'checks': [{'check': 'row_count', 'min': 1}, \
		{'check': 'unique', 'columns': ['ride_id']}]}}}, open(out, 'w'), indent=2)" \
		"$(REST_CONFIG)" "$(abspath $(DEMO_DIR))/data/rides.csv" "http://$(REST_HOST):$(REST_PORT)" \
		"file://$(abspath $(REST_DIR))/warehouse"; \
	echo "--> Wrote $(REST_CONFIG), the demo config with a REST catalog target"; \
	$(LDP) catalog test "$(REST_CONFIG)"; \
	$(LDP) run "$(REST_CONFIG)"; \
	$(LDP) run "$(REST_CONFIG)"; \
	$(LDP) snapshots "$(REST_CONFIG)"; \
	$(LDP) query "$(REST_CONFIG)" \
		"SELECT city, count(*) AS rides, round(sum(fare), 2) AS revenue FROM rides GROUP BY city ORDER BY city"; \
	echo "--> REST demo passed."

demo-all:
	$(MAKE) demo
	$(MAKE) demo-robotics demo-agent
	@if command -v "$(SCALA_CLI)" >/dev/null 2>&1; then \
		$(MAKE) demo-spark demo-rest; \
	else \
		echo "--> Skipping demo-spark and demo-rest: scala-cli not found. Install it: $(SCALA_CLI_HINT)"; \
	fi

check-scala-cli:
	@command -v "$(SCALA_CLI)" >/dev/null 2>&1 || \
		{ echo "scala-cli not found (looked for '$(SCALA_CLI)'). Install it: $(SCALA_CLI_HINT)" >&2; exit 1; }

# ------------------------------------------------------------------------------
# Packaging
# ------------------------------------------------------------------------------

build:
	rm -rf dist
	$(PY) -m build

# Installs the wheel from dist/ (with the duckdb extra the demo's SQL step uses) into
# a throwaway venv and runs the CLI from a temp folder, so nothing is imported from src/.
smoke:
	@set -e; \
	whl=$$(ls -t "$(CURDIR)"/dist/*.whl 2>/dev/null | head -n 1); \
	if [ -z "$$whl" ]; then echo "No wheel in dist/. Run 'make build' first." >&2; exit 1; fi; \
	tmp=$$(mktemp -d); \
	trap 'rm -rf "$$tmp"' EXIT; \
	echo "--> Smoke-testing $$whl in $$tmp"; \
	$(PYTHON) -m venv "$$tmp/venv"; \
	"$$tmp/venv/bin/python" -m pip install --quiet "$$whl[duckdb]"; \
	cd "$$tmp"; \
	"$$tmp/venv/bin/ldp" --version; \
	"$$tmp/venv/bin/ldp" demo --workdir "$$tmp/demo"; \
	echo "--> Smoke test passed."

# ------------------------------------------------------------------------------
# Documentation
# ------------------------------------------------------------------------------

docs:
	$(PY) -m mkdocs build --strict

serve-docs:
	$(PY) -m mkdocs serve

generate-docs:
	$(PY) scripts/generate_issue_list.py

# ------------------------------------------------------------------------------
# Cleaning
# ------------------------------------------------------------------------------

clean:
	rm -rf build dist site .pytest_cache $(DEMO_DIR) examples/*/warehouse examples/*/data/exports
	rm -rf spark/.scala-build spark/.bsp $(REST_FIXTURE)/.scala-build $(REST_FIXTURE)/.bsp \
		examples/*/spark/.scala-build examples/*/spark/.bsp
	find . -path ./$(VENV) -prune -o -name "*.egg-info" -type d -exec rm -rf {} +
	find . -path ./$(VENV) -prune -o -name "__pycache__" -type d -exec rm -rf {} +
