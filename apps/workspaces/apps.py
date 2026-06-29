from django.apps import AppConfig


class WorkspacesConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.workspaces"

    def ready(self):
        from .services.watchdog import start_watchdog_once

        start_watchdog_once()
