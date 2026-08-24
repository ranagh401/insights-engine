# Variables
PYTHON := python3
VENV := .venv
VENV_BIN := $(VENV)/bin
VENV_SCRIPTS := $(VENV)/Scripts
UV := uv
UV_CACHE_DIR := .uv-cache
CORE_PACKAGE := ../microservice-shared/python-packages/platform_core
SDK_PACKAGE := ../microservice-shared/python-packages/platform_sdk

export UV_CACHE_DIR

# Detect OS and set appropriate paths
ifeq ($(OS),Windows_NT)
    PYTHON := python
    VENV_BIN := $(VENV_SCRIPTS)
    RM_CMD := rmdir /s /q
    RM_FILE := del /f /q
    PYTHON_PATH := $(VENV_SCRIPTS)/python.exe
    UV := uv.exe
    PYTEST := $(VENV_SCRIPTS)/pytest.exe
    COVERAGE := $(VENV_SCRIPTS)/coverage.exe
    BLACK := $(VENV_SCRIPTS)/black.exe
    ISORT := $(VENV_SCRIPTS)/isort.exe
    ALEMBIC := $(VENV_SCRIPTS)/alembic.exe
    RUFF := $(VENV_SCRIPTS)/ruff.exe
    UVICORN := $(VENV_SCRIPTS)/uvicorn.exe
else
    PYTHON_PATH := $(VENV_BIN)/python
    PYTEST := $(VENV_BIN)/pytest
    COVERAGE := $(VENV_BIN)/coverage
    BLACK := $(VENV_BIN)/black
    ISORT := $(VENV_BIN)/isort
    ALEMBIC := $(VENV_BIN)/alembic
    RUFF := $(VENV_BIN)/ruff
    UVICORN := $(VENV_BIN)/uvicorn
    RM_CMD := rm -rf
    RM_FILE := rm -f
endif

# Default target
.DEFAULT_GOAL := help

# Help command
help:
	@echo "Available commands:"
	@echo "  make install    - Create venv and install all local packages in editable mode"
	@echo "  make run        - Run the insights service"
	@echo "  make dev-server - Run the service with hot reload"
	@echo "  make test       - Run tests"
	@echo "  make coverage   - Run tests with coverage report"
	@echo "  make clean      - Remove virtual environment and cache files"
	@echo "  make lint       - Run linting checks"
	@echo "  make format     - Format code using black, isort, and ruff"
	@echo "  make docker-build - Build Docker image"
	@echo "  make docker-run   - Run Docker container"
	@echo ""
	@echo "Migration commands:"
	@echo "  make migrate              - Apply all database migrations"
	@echo "  make migration-new m=\"...\" - Create a new migration file"
	@echo "  make migration-downgrade  - Downgrade by one revision"
	@echo "  make migration-current    - Show current revision"
	@echo "  make migration-history    - Show migration history"

# Check if virtual environment exists
check-venv:
ifeq ($(OS),Windows_NT)
	@if exist "$(VENV)" ( \
		echo Virtual environment exists. Updating... \
	) else ( \
		echo Creating new virtual environment... && \
		$(PYTHON) -m venv $(VENV) \
	)
else
	@if [ -d "$(VENV)" ]; then \
		echo "Virtual environment exists. Updating..."; \
	else \
		echo "Creating new virtual environment..."; \
		$(PYTHON) -m venv $(VENV); \
	fi
endif

# Create virtual environment and install dependencies
install: check-venv
	@echo "Installing/Updating dependencies using uv..."
	$(UV) pip install --upgrade pip
	@echo "Installing the insights portal and shared packages in editable mode..."
	$(UV) pip install -e . -e $(CORE_PACKAGE) -e $(SDK_PACKAGE)
	@echo "Installation complete!"

# Run the application
run:
	@echo "Starting insights service..."
	$(PYTHON_PATH) main.py

# Run the service with hot reload for development
dev-server:
	@echo "Starting insights service with hot reload..."
	$(UVICORN) --app-dir src insights_engine.app:app --host 0.0.0.0 --port 8009 --reload

# Run tests
test:
	@echo "Running tests..."
	$(PYTEST) tests/ -v

# Run tests with coverage
coverage:
	@echo "Running tests with coverage..."
	$(COVERAGE) run -m pytest tests/
	$(COVERAGE) report
	$(COVERAGE) html

# Clean up
clean:
	@echo "Cleaning up..."
ifeq ($(OS),Windows_NT)
	@if exist $(VENV) $(RM_CMD) $(VENV)
	@if exist __pycache__ $(RM_CMD) __pycache__
	@if exist .pytest_cache $(RM_CMD) .pytest_cache
	@if exist .coverage $(RM_FILE) .coverage
	@if exist htmlcov $(RM_CMD) htmlcov
	@if exist build $(RM_CMD) build
	@if exist dist $(RM_CMD) dist
	@if exist *.egg-info $(RM_CMD) *.egg-info
	@for /d /r . %%d in (__pycache__) do @if exist "%%d" $(RM_CMD) "%%d"
	@for /r . %%f in (*.pyc) do @if exist "%%f" $(RM_FILE) "%%f"
else
	$(RM_CMD) $(VENV)
	$(RM_CMD) __pycache__
	$(RM_CMD) .pytest_cache
	$(RM_FILE) .coverage
	$(RM_CMD) htmlcov
	$(RM_CMD) build
	$(RM_CMD) dist
	$(RM_CMD) *.egg-info
	find . -type d -name "__pycache__" -exec $(RM_CMD) {} +
	find . -type f -name "*.pyc" -delete
endif

# Run linting
lint:
	@echo "Running linting checks..."
	$(RUFF) check src/ tests/
	$(BLACK) --check src/ tests/

# Format code
format:
	@echo "Formatting code..."
	$(BLACK) src/ tests/
	$(RUFF) format src/ tests/

# Build Docker image
docker-build:
	@echo "Building Docker image..."
	docker build -t insights:latest .

# Run Docker container
docker-run:
	@echo "Running Docker container..."
	docker run -p 8009:8009 -p 50055:50055 --env-file .env insights:latest

# Migration commands
migrate:
	@echo "Applying database migrations..."
	$(ALEMBIC) upgrade head
	@echo "Migrations applied successfully!"

migration-new:
ifndef m
	$(error m is not set. Usage: make migration-new m="your message")
endif
	@echo "Creating new migration: $(m)"
	$(ALEMBIC) revision --autogenerate -m "$(m)"

migration-downgrade:
	@echo "Downgrading by one revision..."
	$(ALEMBIC) downgrade -1

migration-current:
	@echo "Current revision:"
	$(ALEMBIC) current

migration-history:
	@echo "Migration history:"
	$(ALEMBIC) history --verbose

.PHONY: help check-venv install run dev-server test coverage clean lint format docker-build docker-run migrate migration-new migration-downgrade migration-current migration-history
