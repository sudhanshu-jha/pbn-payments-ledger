from django.urls import re_path

from .views import TokenizeView


app_name = "mock_processor"
urlpatterns = [
    re_path(r"^processor/tokenize/?$", TokenizeView.as_view(), name="tokenize"),
]
