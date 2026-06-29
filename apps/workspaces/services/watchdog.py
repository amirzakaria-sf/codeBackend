import logging
import os
import sys
import threading
import time

from django.conf import settings
from django.db import close_old_connections

from ..models import Project
from .daemon import daemon_health, is_daemon_running, start_opencode_daemon, stop_opencode_daemon
from .realtime import broadcast_project_event

logger = logging.getLogger(__name__)
_watchdog_started = False
_watchdog_lock = threading.Lock()


def start_watchdog_once() -> None:
    global _watchdog_started  # noqa: PLW0603

    if not settings.DAEMON_WATCHDOG_ENABLED:
        return
    if _should_skip_process():
        return

    with _watchdog_lock:
        if _watchdog_started:
            return

        thread = threading.Thread(target=_watchdog_loop, name="daemon-watchdog", daemon=True)
        thread.start()
        _watchdog_started = True
        logger.info("Daemon watchdog started.")


def _should_skip_process() -> bool:
    command = os.path.basename(sys.argv[0])
    if command == "celery":
        return True
    if command != "manage.py":
        return False

    blocked_commands = {
        "check",
        "makemigrations",
        "migrate",
        "collectstatic",
        "shell",
        "createsuperuser",
        "test",
    }
    if len(sys.argv) > 1 and sys.argv[1] in blocked_commands:
        return True

    if settings.DEBUG:
        return os.environ.get("RUN_MAIN") != "true"
    return False


def _watchdog_loop() -> None:
    interval = max(5, settings.DAEMON_WATCHDOG_INTERVAL_SECONDS)
    while True:
        close_old_connections()
        try:
            _heal_projects()
        except Exception as error:  # noqa: BLE001
            logger.exception("Watchdog cycle failed: %s", error)
        finally:
            close_old_connections()
            time.sleep(interval)


def _heal_projects() -> None:
    monitored_projects = Project.objects.exclude(daemon_pid__isnull=True).exclude(allocated_port__isnull=True)
    for project in monitored_projects:
        pid_alive = is_daemon_running(project.daemon_pid)
        health = daemon_health(project.allocated_port if pid_alive else None)
        is_healthy = pid_alive and bool(health.get("healthy"))
        if is_healthy:
            continue

        logger.warning(
            "Watchdog restarting unhealthy daemon for project=%s pid=%s port=%s",
            project.name,
            project.daemon_pid,
            project.allocated_port,
        )

        if project.daemon_pid:
            stop_opencode_daemon(
                project.daemon_pid,
                allocated_port=project.allocated_port,
                project_absolute_path=project.absolute_path,
            )

        process = start_opencode_daemon(project.absolute_path, project.allocated_port)
        old_pid = project.daemon_pid
        project.daemon_pid = process.pid
        project.save(update_fields=["daemon_pid", "updated_at"])

        broadcast_project_event(
            project.id,
            {
                "kind": "daemon_recovered",
                "project_id": project.id,
                "old_pid": old_pid,
                "new_pid": process.pid,
                "allocated_port": project.allocated_port,
            },
        )
