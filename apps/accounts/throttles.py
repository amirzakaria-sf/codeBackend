from rest_framework.throttling import SimpleRateThrottle


class AuthIPThrottle(SimpleRateThrottle):
    scope = "auth_ip"

    def get_cache_key(self, request, view):
        return self.cache_format % {
            "scope": self.scope,
            "ident": self.get_ident(request),
        }


class AuthUserThrottle(SimpleRateThrottle):
    scope = "auth_user"

    def get_cache_key(self, request, view):
        username = (request.data.get("username") or "").strip().lower()
        ident = username or self.get_ident(request)
        return self.cache_format % {
            "scope": self.scope,
            "ident": ident,
        }


class AuthRefreshThrottle(SimpleRateThrottle):
    scope = "auth_refresh"

    def get_cache_key(self, request, view):
        return self.cache_format % {
            "scope": self.scope,
            "ident": self.get_ident(request),
        }


class AuthManageIPThrottle(SimpleRateThrottle):
    scope = "auth_manage_ip"

    def get_cache_key(self, request, view):
        return self.cache_format % {
            "scope": self.scope,
            "ident": self.get_ident(request),
        }
