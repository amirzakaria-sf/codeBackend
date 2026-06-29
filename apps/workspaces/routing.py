from django.urls import path

from .consumers import WorkspaceEventConsumer

websocket_urlpatterns = [
    path("ws/projects/<int:project_id>/", WorkspaceEventConsumer.as_asgi()),
]
