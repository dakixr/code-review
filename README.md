## code-review

Monorepo for a local **AI code review** toolset.

### Packages

- `cli/` — Typer-based `codereview` CLI (review uncommitted changes, configurable strictness/style)
- `web/` — Placeholder web app (Django scaffold)

### Quick start (CLI)

```bash
cd cli
uv sync
uv run codereview
```

### Install as a tool (recommended)

```bash
cd cli
uv tool install --editable .
codereview
```

### Credits

Built on top of the OpenCode CLI (`opencode`).
