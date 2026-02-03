# CodeReview AI Web

## Local Setup

Run commands from the `web/` directory.

1. Install deps with `uv`.
2. Install CSS deps with `pnpm install`.
3. Copy `.env.example` to `.env` and fill GitHub App settings.
4. Start the stack (Redis + web + worker):
   - `docker compose up -d --build`
5. Apply migrations:
   - `uv run python manage.py migrate`
6. Build Tailwind locally (optional if not using Docker):
   - `pnpm run build:css`

## GitHub App Webhooks

Expose the webhook endpoint at `/github/webhook` and set the secret in `GITHUB_WEBHOOK_SECRET`.
Events handled:
- `installation`
- `installation_repositories`
- `pull_request` (opened, reopened, synchronize)
- `issue_comment` (PR chat + feedback signals)
