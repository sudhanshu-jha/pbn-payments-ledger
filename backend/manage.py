#!/usr/bin/env python

import os
import sys

from decouple import config


if __name__ == "__main__":
    settings_module = config("DJANGO_SETTINGS_MODULE", default=None)

    if sys.argv[1] == "test":
        if settings_module:
            print(
                "Ignoring config('DJANGO_SETTINGS_MODULE') because it's test. "
                "Using 'pbn_payments.settings.test'"
            )
        # Force (not setdefault): inside docker compose the env_file exports
        # DJANGO_SETTINGS_MODULE=...local, which would otherwise win over the
        # test settings and make the suite behave differently in Docker.
        os.environ["DJANGO_SETTINGS_MODULE"] = "pbn_payments.settings.test"
    else:
        if settings_module is None:
            print(
                "Error: no DJANGO_SETTINGS_MODULE found. Will NOT start devserver. "
                "Remember to create .env file at project root. "
                "Check README for more info."
            )
            sys.exit(1)
        os.environ.setdefault("DJANGO_SETTINGS_MODULE", settings_module)

    from django.core.management import execute_from_command_line

    execute_from_command_line(sys.argv)
