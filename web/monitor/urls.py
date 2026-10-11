from django.urls import path

from . import views

urlpatterns = [
    path("", views.dashboard),
    path("files/", views.files),
    path("checkpoints/", views.checkpoints),
    path("api/state", views.api_state),
    path("api/command", views.api_command),
    path("api/command/<int:command_id>", views.api_command_status),
    path("chaos/", views.chaos),
]