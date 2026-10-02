from django.urls import re_path

from . import views


app_name = "payments"

UUID = r"(?P<payment_id>[0-9a-f-]{36})"

urlpatterns = [
    re_path(r"^api/payments/?$", views.PaymentListCreateView.as_view(), name="payment-list"),
    re_path(rf"^api/payments/{UUID}/?$", views.PaymentDetailView.as_view(), name="payment-detail"),
    re_path(
        rf"^api/payments/{UUID}/replay/?$", views.PaymentReplayView.as_view(), name="payment-replay"
    ),
    re_path(r"^webhooks/processor/?$", views.processor_webhook, name="processor-webhook"),
    re_path(r"^healthz/?$", views.healthz, name="healthz"),
]
