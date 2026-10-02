from django.apps import AppConfig


class MockProcessorConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "mock_processor"
    verbose_name = "Mock payment processor (external system)"
