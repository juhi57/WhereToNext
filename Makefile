# Developer shortcuts. `make help` lists everything.
.DEFAULT_GOAL := help
PY ?= python
VENV := .venv
BIN := $(VENV)/bin
ifeq ($(OS),Windows_NT)
	BIN := $(VENV)/Scripts
endif

.PHONY: help install dev run test cov lint fmt typecheck check clean docker docker-run demo

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

install: ## Create the virtualenv and install the package
	$(PY) -m venv $(VENV)
	$(BIN)/python -m pip install --upgrade pip
	$(BIN)/python -m pip install -e .

dev: ## Install with development dependencies
	$(PY) -m venv $(VENV)
	$(BIN)/python -m pip install --upgrade pip
	$(BIN)/python -m pip install -e ".[dev]"
	$(BIN)/python -m pre_commit install || true

run: ## Run the development server on :8000
	$(BIN)/python -m uvicorn app.main:app --reload --port 8000

test: ## Run the test suite
	$(BIN)/python -m pytest

cov: ## Run tests with a coverage report
	$(BIN)/python -m pytest --cov=app --cov-report=term-missing --cov-report=html

lint: ## Check formatting and lint rules
	$(BIN)/python -m ruff check app tests
	$(BIN)/python -m ruff format --check app tests

fmt: ## Auto-format and auto-fix
	$(BIN)/python -m ruff format app tests
	$(BIN)/python -m ruff check app tests --fix

typecheck: ## Run mypy
	$(BIN)/python -m mypy app

check: lint typecheck test ## Everything CI runs

demo: ## Generate a sample itinerary PDF with no API key
	$(BIN)/python -m app.cli plan --from Mumbai --to "Bali, Indonesia" \
		--start 2026-12-13 --days 6 --budget 180000 \
		--food vegetarian --interests beaches,food,culture \
		--pdf docs/sample-itinerary.pdf

docker: ## Build the Docker image
	docker build -t wheretonext:latest .

docker-run: ## Run the container on :8000
	docker run --rm -p 8000:8000 -e ANTHROPIC_API_KEY wheretonext:latest

clean: ## Remove caches and build artefacts
	rm -rf .pytest_cache .mypy_cache .ruff_cache htmlcov .coverage build dist *.egg-info
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
