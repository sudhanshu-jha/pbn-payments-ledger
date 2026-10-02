import logging
import os

from django.conf import settings


logger = logging.getLogger(__name__)


def _default_admin_environment_label():
    settings_module = getattr(settings, "SETTINGS_MODULE", "") or os.getenv(
        "DJANGO_SETTINGS_MODULE", ""
    )
    if settings_module:
        try:
            environment_label = (
                settings_module.rsplit(".", maxsplit=1)[-1].replace("_", " ").title()
            )
            if not environment_label:
                raise IndexError
            return environment_label
        except IndexError:
            logger.warning(
                "Could not determine admin environment label from SETTINGS_MODULE: %s",
                settings_module,
            )
            return "Unknown"

    return "Local"


def admin_environment_label(request):
    admin_environment = settings.ADMIN_ENVIRONMENT_LABEL.strip()
    return {
        "ADMIN_ENVIRONMENT_LABEL": admin_environment or _default_admin_environment_label(),
        "ADMIN_ENVIRONMENT_COLOR": settings.ADMIN_ENVIRONMENT_COLOR,
        "ADMIN_ENVIRONMENT_BACKGROUND_COLOR": settings.ADMIN_ENVIRONMENT_BACKGROUND_COLOR,
    }


def sentry_dsn(request):
    return {"SENTRY_DSN": settings.SENTRY_DSN}


def commit_sha(request):
    return {"COMMIT_SHA": settings.COMMIT_SHA}
