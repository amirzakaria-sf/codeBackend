import os

from channels.routing import ProtocolTypeRouter, URLRouter
from channels.security.websocket import AllowedHostsOriginValidator
from django.core.asgi import get_asgi_application

"""ASGI config for the Foundry-AI orchestrator backend."""

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'core.settings')

django_asgi_application = get_asgi_application()

from apps.accounts.middleware import JwtAuthMiddlewareStack
from .routing import websocket_urlpatterns

application = ProtocolTypeRouter(
    {
        'http': django_asgi_application,
        'websocket': AllowedHostsOriginValidator(
            JwtAuthMiddlewareStack(
                URLRouter(websocket_urlpatterns),
            ),
        ),
    },
)
