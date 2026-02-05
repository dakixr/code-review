# code-review

Monorepo for an **AI code review** toolset:

- `cli/`: `codereview` — a local, repo-agnostic wrapper around OpenCode (`opencode run`)
- `web/`: CodeReview AI Web — a Django control plane for a GitHub-native PR reviewer

If you only want local reviews, start with `cli/`. If you want the GitHub App + background jobs, use `web/`.

## Repo layout

- `cli/` — Typer-based Python CLI (`codereview` command). See `cli/README.md`.
- `web/` — Django 5.x app (HTMX + htpy) + Celery worker + Tailwind. See `web/README.md`.

## Quick start (CLI)

Run from this repo:

```bash
cd cli
uv sync
uv run codereview --help
uv run codereview
```

Install as a tool (recommended, so you can run `codereview` from any repo):

```bash
cd cli
uv tool install --editable .
codereview --help
```

More details and examples: `cli/README.md`.

## Quick start (Web)

Local dev (Redis + Django + Celery worker + Tailwind watcher) is easiest via Docker.

```bash
cd web
cp .env.example .env
docker compose -f docker-compose.local.yml up -d --build
```

Note: `docker-entrypoint.sh` runs `migrate` + `collectstatic` automatically when
`RUN_DJANGO_COMMANDS=true` (the default in the provided compose files).

Open the app at `http://127.0.0.1:7999`.

More details (architecture, GitHub App manifest flow, production notes): `web/README.md`.

## Dev commands

CLI:

```bash
cd cli
uv sync
uv run codereview --help
```

Web:

```bash
cd web
uv sync
uv run ruff check .
uv run ruff format .
uv run pyright
```

## Credits

Built on top of the OpenCode CLI (`opencode`).
