from .base import *


SECRET_KEY = "test"  # nosec

STATIC_ROOT = base_dir_join("staticfiles")
STATIC_URL = "/static/"

MEDIA_ROOT = base_dir_join("mediafiles")
MEDIA_URL = "/media/"

STORAGES = {
    "default": {
        "BACKEND": "django.core.files.storage.FileSystemStorage",
    },
    "staticfiles": {
        "BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage",
    },
}

# Speed up password hashing
PASSWORD_HASHERS = [
    "django.contrib.auth.hashers.MD5PasswordHasher",
]

# Celery
CELERY_TASK_ALWAYS_EAGER = True
CELERY_TASK_EAGER_PROPAGATES = True

# Deterministic webhook secret for the test-suite (never used outside tests)
PROCESSOR_WEBHOOK_SECRET = "test-webhook-secret"  # noqa: S105

# Capture everything at DEBUG with the sensitive-data scrubber attached, exactly as
# in local/production, so the "nothing sensitive in logs" test exercises real config.
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "filters": {
        "sensitive_data": {"()": "payments.logging_filters.SensitiveDataFilter"},
    },
    "handlers": {
        "null": {"class": "logging.NullHandler", "filters": ["sensitive_data"]},
    },
    "loggers": {
        "": {"handlers": ["null"], "level": "DEBUG"},
    },
}
