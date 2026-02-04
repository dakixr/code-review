#!/usr/bin/env bash
set -euo pipefail

if [[ "${RUN_DJANGO_COMMANDS:-false}" == "true" ]]; then
  echo "Running Django startup commands..."
  /opt/venv/bin/python - <<'PY'
import os
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "core.settings")
import django
django.setup()
from django.conf import settings

print("DJANGO_DEBUG =", settings.DEBUG)
print("settings file =", settings.SETTINGS_MODULE, getattr(__import__('core.settings', fromlist=['__file__']), '__file__', 'unknown'))
print("STATIC_URL =", settings.STATIC_URL)
print("STATIC_ROOT =", settings.STATIC_ROOT)
print("STATICFILES_DIRS =", getattr(settings, "STATICFILES_DIRS", None))
PY

  /opt/venv/bin/python manage.py migrate --noinput
  mkdir -p /app/static
  /opt/venv/bin/python manage.py collectstatic --noinput

  /opt/venv/bin/python - <<'PY'
import os
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "core.settings")
import django
django.setup()
from django.templatetags.static import static
from django.conf import settings
print("After collectstatic:")
print("DJANGO_DEBUG =", settings.DEBUG)
print("static('css/output.css') =", static("css/output.css"))
PY
fi

exec "$@"
