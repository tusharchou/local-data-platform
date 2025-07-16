# Makefile for the local-data-platform project

.PHONY: help install lint test docs serve-docs clean

# Default target to show help.
help:
	@echo "Usage: make [target]"
	@echo ""
	@echo "Targets:"
	@echo "  help          Show this help message."
	@echo "  install       Install project dependencies using Poetry."
	@echo "  reinstall     Force a clean re-installation of all dependencies."
	@echo "  lint          Run flake8 linter on the project."
	@echo "  test          Run pytest tests."
	@echo "  docs          Build the MkDocs documentation."
	@echo "  serve-docs    Build and serve the documentation locally on http://localhost:8000."
	@echo "  clean         Remove temporary build files and caches."

# ==============================================================================
# Development Setup
# ==============================================================================

install:
	@echo "--> Installing dependencies with Poetry..."
	poetry install --with dev,docs


POETRY_RUN := poetry run

reinstall:
	@echo "--> Removing existing virtual environment to ensure a clean state..."
	@poetry env remove $$(poetry env list --full-path | grep 'Activated' | cut -d' ' -f1) || echo "Could not remove the active virtualenv. It may not exist, or you may have a permissions issue. Please check permissions for /Users/tushar/Library/Caches/pypoetry/virtualenvs/"
	@echo "--> Reinstalling all dependencies from scratch..."
	@$(MAKE) install

# ==============================================================================
# Quality & Testing



# ==============================================================================
# Quality & Testing
# ==============================================================================

lint:
	@echo "--> Linting with flake8..."
	poetry run flake8 src/ tests/

test:
	@echo "--> Running tests with pytest..."
	poetry run pytest tests/

# ==============================================================================
# Documentation
# ==============================================================================

docs:
	@echo "--> Building documentation..."
	poetry run mkdocs build --strict
 
serve-docs:
	@echo "--> Serving documentation..."
	poetry run mkdocs serve

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
  
generate-docs:
	@echo "Generating User issues tab"
	$(POETRY_RUN) python3 docs/scripts/generate_issue_list.py
	@echo "--> Verifying pymdownx.toc installation..."
	$(POETRY_RUN) python3 -c "import pymdownx.toc" || echo "pymdownx.toc not found. Please run 'make install'."