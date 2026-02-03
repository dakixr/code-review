# codereview (CLI)

Local code review wrapper around `opencode run` that **injects a system prompt** by writing a temporary `.opencode/agent/*.md` file in the target repo and calling `opencode run --agent <name> ...` (so it does not depend on your `~/.config/opencode/agent/*` setup).

## Install (uv)

From this folder:

```bash
uv sync
```

To run `codereview` from any repo without `uv --directory` changing your working directory, install it as a tool:

```bash
uv tool install --editable .
```

## Usage

Run from any git repo (or inside this repo):

```bash
uv run codereview
```

Suggested shell alias (keeps `codereview`'s python env here, but reviews your current repo):

```bash
alias codereview='uv -q --directory /Users/dakixr/dev/code-review/cli run codereview --repo "$PWD"'
```

Common options:

```bash
uv run codereview --scope all --style greptile --strictness 2
uv run codereview --scope staged
uv run codereview --no-include-untracked
uv run codereview --extra "Focus on security + API compatibility"
uv run codereview -m openai/gpt-5 --variant high
```

Dry-run (print the injected system prompt + the message it would send to opencode):

```bash
uv run codereview --dry-run
```
