# AGENTS.md

This file provides guidance to AI coding agents working in this repository.

## Repository Structure

This is a monorepo containing:

- `web/` — Django web application (AI code review platform)
- `cli/` — Typer-based Python CLI (`codereview` command)

## Build/Lint/Test Commands

### Web (Django)

```bash
cd web

# Install dependencies
uv sync

# Run Django server
uv run python manage.py runserver

# Database migrations
uv run python manage.py migrate

# Linting & Formatting (Ruff)
uv run ruff check .          # Check for lint errors
uv run ruff check --fix .    # Fix auto-fixable issues
uv run ruff format .         # Format code

# Type checking (Pyright)
uv run pyright

# Frontend (Tailwind CSS)
pnpm install
pnpm run build:css           # Build CSS for production
pnpm run dev:css             # Watch mode for development
```

### CLI

```bash
cd cli

# Install dependencies
uv sync

# Run CLI locally
uv run codereview

# Install as tool
uv tool install --editable .

# Type checking (if pyright configured)
uv run pyright
```

## Code Style Guidelines

### Python (Web & CLI)

**Imports:**
- Use `from __future__ import annotations` at the top of every file
- Group imports: stdlib → third-party → local
- Use absolute imports, avoid relative imports

**Formatting:**
- Use Ruff for linting and formatting
- Line length: 88 characters (Black-compatible)
- Use double quotes for strings
- Trailing commas in multi-line collections

**Type Hints:**
- Use Python 3.12+ type hint syntax
- Use `|` for unions (e.g., `str | None` instead of `Optional[str]`)
- Use built-in generics (e.g., `list[str]` instead of `List[str]`)
- Type all function parameters and return values

**Error Handling:**
- Use specific exceptions, avoid bare `except:`
- Use `raise from` when re-raising exceptions
- Handle expected errors gracefully with user-friendly messages

**Documentation:**
- Use docstrings for modules, classes, and public functions
- Follow Google-style docstrings

### Django (Web)

**Models:**
- Use type hints with Django's generic field types
- Always define `__str__` method
- Use `related_name` on ForeignKey/OneToOneField/ManyToManyField

**Views:**
- Use function-based views or class-based views consistently
- Use Django HTMX for partial page updates

**Settings:**
- Use `os.getenv()` with defaults for environment variables
- Never commit secrets to the repository

## General Guidelines

- Always run linters before committing (`ruff check .`)
- Keep functions small and focused
- Prefer composition over inheritance
- Write tests for new features
- Use environment variables for configuration
- Never log or expose sensitive data
- Follow existing patterns in the codebase
