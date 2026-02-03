#!/usr/bin/env bash
set -euo pipefail

if [[ "${RUN_DJANGO_COMMANDS:-false}" == "true" ]]; then
  /opt/venv/bin/python manage.py migrate --noinput
  /opt/venv/bin/python manage.py collectstatic --noinput
fi

exec "$@"

