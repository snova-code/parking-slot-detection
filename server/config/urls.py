# server/config/urls.py
"""Page routes and the API.

Every page is an anonymous shell: the templates hold no tenant data, so they
need no authentication. All data arrives through /api/v1, which enforces the
session, and the JavaScript redirects to /login/ on a 401.
"""
from django.urls import path
from django.views.generic import TemplateView

from config.api import api

urlpatterns = [
    path("api/v1/", api.urls),
    path("", TemplateView.as_view(template_name="lots.html"), name="lots"),
    path("login/", TemplateView.as_view(template_name="login.html"), name="login"),
    path("lots/<uuid:lot_id>/",
         TemplateView.as_view(template_name="lot.html"), name="lot"),
    path("lots/<uuid:lot_id>/cameras/<uuid:camera_id>/annotate/",
         TemplateView.as_view(template_name="annotate.html"), name="annotate"),
]