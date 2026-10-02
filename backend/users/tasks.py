from django.core import management

from pbn_payments import celery_app


@celery_app.task
def clearsessions():
    management.call_command("clearsessions")
