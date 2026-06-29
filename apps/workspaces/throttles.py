from rest_framework.throttling import SimpleRateThrottle


class ProjectMutationThrottle(SimpleRateThrottle):
    scope = "project_mutation"

    def get_cache_key(self, request, view):
        user_id = getattr(request.user, "id", None)
        ident = f"user:{user_id}" if user_id else f"ip:{self.get_ident(request)}"
        return self.cache_format % {
            "scope": self.scope,
            "ident": ident,
        }


class ExecutePromptThrottle(SimpleRateThrottle):
    scope = "execute_prompt"

    def get_cache_key(self, request, view):
        user_id = getattr(request.user, "id", None)
        ident = f"user:{user_id}" if user_id else f"ip:{self.get_ident(request)}"
        return self.cache_format % {
            "scope": self.scope,
            "ident": ident,
        }
