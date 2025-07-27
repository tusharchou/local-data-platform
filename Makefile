# Makefile for the local-data-platform project

.PHONY: help all install reinstall lint test docs serve-docs clean generate-docs verify-pymdownx

# Default target to show help.
help:
	@echo "Usage: make [target]"
	@echo ""
	@echo "Targets:"
	@echo "  all           Run all quality checks (lint and test)."
	@echo "  help          Show this help message."
	@echo "  install       Install project dependencies using Poetry."
	@echo "  reinstall     Force a clean re-installation of all dependencies."
	@echo "  lint          Run flake8 linter on the project."
	@echo "  test          Run pytest tests."
	@echo "  generate-docs Generate dynamic documentation content (e.g., issue lists)."
	@echo "  docs          Build the MkDocs documentation."
	@echo "  serve-docs    Build and serve the documentation locally on http://localhost:8000."
	@echo "  clean         Remove temporary build files and caches."
	@echo "  verify-pymdownx  Verify a specific documentation dependency."

POETRY_RUN := poetry run

# ==============================================================================
# Development Setup
# ==============================================================================

install:
	@echo "--> Installing dependencies with Poetry..."
	poetry install --with dev,docs

reinstall:
	@echo "--> Removing existing virtual environment to ensure a clean state..."
	@poetry env remove $$(poetry env info --path) || echo "No virtualenv found to remove, continuing..."
	@echo "--> Reinstalling all dependencies from scratch..."
	@$(MAKE) install

# ==============================================================================
# Quality & Testing
# ==============================================================================

all: lint test
	@echo "--> All quality checks passed successfully."

lint:
	@echo "--> Linting with flake8..."
	$(POETRY_RUN) flake8 src/ tests/

test:
	@echo "--> Running tests with pytest..."
	$(POETRY_RUN) pytest tests/

# ==============================================================================
# Documentation
# ==============================================================================

generate-docs:
	@echo "--> Generating dynamic documentation content..."
	$(POETRY_RUN) python3 docs/scripts/generate_issue_list.py

docs:
	@$(MAKE) generate-docs
	@echo "--> Building documentation..."
	$(POETRY_RUN) mkdocs build --strict
 
serve-docs:
	@$(MAKE) generate-docs
	@echo "--> Serving documentation..."
	$(POETRY_RUN) mkdocs serve

# ==============================================================================
# Cleaning
# ==============================================================================

clean:
	@echo "--> Cleaning up build artifacts and caches..."
	@find . -type f -name "*.py[co]" -delete
	@find . -type d -name "__pycache__" -exec rm -rf {} +
	@rm -rf .pytest_cache .mypy_cache build dist *.egg-info site

# Verify if pymdownx.toc is importable within the Poetry environment
verify-pymdownx:
	@echo "--> Verifying pymdownx.toc installation..."
	$(POETRY_RUN) python3 -c "import pymdownx.toc" || echo "pymdownx.toc not found. Please run 'make install'."