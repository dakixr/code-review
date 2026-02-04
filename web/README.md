# CodeReview AI Web

## Vision

Build a **GitHub-native AI code reviewer** that feels like Greptile/CodeRabbit:

1. User installs a GitHub App on an org/repo.
2. On PR open/synchronize, the bot immediately comments with **👁** (“working”), then edits/posts the full review.
3. Users “chat” with the bot **in GitHub comments** (not in this web UI).
4. The system learns preferences from explicit feedback (e.g. `/ai like`, `/ai dislike`, `/ai ignore`) and from accumulated rule configuration.

This web app is the control plane:
- Install/status dashboard (installations, repos).
- Global + per-repo rule sets (prompt/instructions + structured rules).
- Operational visibility (review runs, failures) and future: token usage, audit logs, rate limits.

## Architecture (MVP)
- Django 5.x + htpy (no Jinja templates) + HTMX.
- Reusable UI primitives in `web/components/ui/` + Tailwind build via `pnpm`/`@tailwindcss/cli`.
- Celery for background jobs (webhook ingestion → review tasks).
- Redis as Celery broker.
- WhiteNoise + `collectstatic` for production static serving.
- OpenCode installed in the image with default model `zai/glm-4.7` (see `web/opencode.json`).

## Local Setup

Run commands from the `web/` directory.

1. Install deps with `uv`.
2. Install CSS deps with `pnpm install`.
3. Copy `.env.example` to `.env` and fill core settings (Django + Celery).
4. Start the local stack (Redis + web + worker + Tailwind watcher):
   - `docker compose -f docker-compose.local.yml up -d --build`
5. Apply migrations:
   - `uv run python manage.py migrate`
6. Build Tailwind locally (optional if not using Docker):
   - `pnpm run build:css`

## Production Notes (Coolify)

- Run `migrate` + `collectstatic` at startup by setting `RUN_DJANGO_COMMANDS=true`.
- **Persist the SQLite database** by mounting a persistent volume at `/data` and setting:
  - `DJANGO_DB_PATH=/data/db.sqlite3`
- Required env:
  - `DJANGO_SECRET_KEY`
  - `DJANGO_DEBUG=false`
  - `DJANGO_ALLOWED_HOSTS=code-review.dakixr.dev`
  - `DJANGO_CSRF_TRUSTED_ORIGINS=https://code-review.dakixr.dev`
  - `CELERY_BROKER_URL=redis://redis:6379/0`
  - `CELERY_RESULT_BACKEND=django-db`
  - (Optional legacy single-app mode) `GITHUB_APP_ID`, `GITHUB_APP_PRIVATE_KEY_PATH`, `GITHUB_WEBHOOK_SECRET`, `GITHUB_APP_SLUG`

## GitHub App Webhooks

Each user-created GitHub App gets its own webhook URL and secret:

- Webhook URL: `/github/webhook/<app_uuid>`
- Secret: stored on the `GithubApp` record (returned by the manifest conversion step)

Events handled:
- `installation`
- `installation_repositories`
- `pull_request` (opened, reopened, synchronize)
- `issue_comment` (PR chat + feedback signals)

## Creating The GitHub App

This project uses the **GitHub App Manifest** flow (Coolify-style):

1. Create an account in this web UI.
2. Go to `Account` → `GitHub App` → `Create GitHub App`.
3. GitHub opens a “Create GitHub App” screen pre-filled with the manifest:
   - webhook URL points at this server (scoped to your app UUID)
   - required permissions + events are pre-selected
4. After GitHub creates the app, install it on an org/repo.

API keys (e.g. `zai` for GLM) are stored **per user in the database** and will be injected into background review jobs as needed (not committed as env vars).

Hi! hi again!