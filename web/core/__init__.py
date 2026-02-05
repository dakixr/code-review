from .celery import app as celery_app

try:
    import django_stubs_ext
except ImportError:
    django_stubs_ext = None
else:
    django_stubs_ext.monkeypatch()

__all__ = ("celery_app",)
