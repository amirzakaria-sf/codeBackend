from urllib.parse import parse_qs

from channels.auth import AuthMiddlewareStack
from channels.db import database_sync_to_async
from django.contrib.auth.models import AnonymousUser
from rest_framework_simplejwt.authentication import JWTAuthentication
from rest_framework_simplejwt.exceptions import InvalidToken, TokenError


@database_sync_to_async
def get_user_from_token(raw_token: str):
    authenticator = JWTAuthentication()
    try:
        validated_token = authenticator.get_validated_token(raw_token)
        return authenticator.get_user(validated_token)
    except (InvalidToken, TokenError, Exception):
        return AnonymousUser()


class JwtAuthMiddleware:
    def __init__(self, inner):
        self.inner = inner

    async def __call__(self, scope, receive, send):
        scope["user"] = scope.get("user", AnonymousUser())
        raw_token = self._extract_token(scope)
        if raw_token:
            scope["user"] = await get_user_from_token(raw_token)
        return await self.inner(scope, receive, send)

    def _extract_token(self, scope) -> str | None:
        headers = dict(scope.get("headers", []))
        authorization = headers.get(b"authorization", b"").decode("utf-8")
        if authorization.lower().startswith("bearer "):
            return authorization.split(" ", 1)[1].strip()

        query_string = scope.get("query_string", b"").decode("utf-8")
        params = parse_qs(query_string)
        token_values = params.get("token") or params.get("access")
        if token_values:
            return token_values[0]
        return None


def JwtAuthMiddlewareStack(inner):
    return AuthMiddlewareStack(JwtAuthMiddleware(inner))
