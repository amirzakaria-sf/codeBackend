import logging
import os
import sys
import threading
import time
from collections import defaultdict

from django.conf import settings
from django.db import close_old_connections
from django.utils import timezone

from ..models import OrchestrationRun, Project
from .daemon import daemon_runtime_status, start_opencode_daemon, stop_opencode_daemon
from .orchestration import expire_project_lock_if_stale, fail_active_runs_for_project, release_project_lock
from .realtime import broadcast_project_event

logger = logging.getLogger(__name__)
_watchdog_started = False
_watchdog_lock = threading.Lock()
_failure_counts: dict[int, int] = defaultdict(int)

ACTIVE_RUN_STATUSES = {
    OrchestrationRun.Status.PENDING_APPROVAL,
    OrchestrationRun.Status.QUEUED,
    OrchestrationRun.Status.PLANNING,
    OrchestrationRun.Status.BREAKING_DOWN,
    OrchestrationRun.Status.PLAN_READY,
    OrchestrationRun.Status.AWAITING_PLAN_APPROVAL,
    OrchestrationRun.Status.RUNNING,
    OrchestrationRun.Status.VERIFYING,
}


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
    failure_threshold = max(1, settings.DAEMON_WATCHDOG_CONSECUTIVE_FAILURE_THRESHOLD)
    monitored_projects = Project.objects.filter(
        daemon_desired_state=Project.DaemonDesiredState.RUNNING,
    ).exclude(allocated_port__isnull=True)
    for project in monitored_projects:
        runtime_status = daemon_runtime_status(project.daemon_pid, project.allocated_port)
        has_active_runs = project.runs.filter(status__in=ACTIVE_RUN_STATUSES, finished_at__isnull=True).exists()
        if not has_active_runs:
            expire_project_lock_if_stale(project, runtime_status=runtime_status)
        if runtime_status.get("reachable") or runtime_status.get("healthy"):
            project.daemon_last_heartbeat_at = timezone.now()
            project.save(update_fields=["daemon_last_heartbeat_at", "updated_at"])

        is_healthy = runtime_status.get("running") and runtime_status.get("reachable") and runtime_status.get("healthy")
        if is_healthy:
            _failure_counts.pop(project.id, None)
            continue

        if runtime_status.get("reachable") and (runtime_status.get("busy") or has_active_runs):
            logger.info(
                "Watchdog tolerated busy daemon for project=%s pid=%s port=%s state=%s active_runs=%s",
                project.name,
                project.daemon_pid,
                project.allocated_port,
                runtime_status.get("state"),
                has_active_runs,
            )
            _failure_counts.pop(project.id, None)
            continue

        failure_count = _failure_counts[project.id] + 1
        _failure_counts[project.id] = failure_count
        if failure_count < failure_threshold:
            logger.warning(
                "Watchdog observed unhealthy daemon for project=%s pid=%s port=%s state=%s failure=%s/%s",
                project.name,
                project.daemon_pid,
                project.allocated_port,
                runtime_status.get("state"),
                failure_count,
                failure_threshold,
            )
            continue

        logger.warning(
            "Watchdog restarting unhealthy daemon for project=%s pid=%s port=%s state=%s failure=%s/%s",
            project.name,
            project.daemon_pid,
            project.allocated_port,
            runtime_status.get("state"),
            failure_count,
            failure_threshold,
        )

        if project.daemon_pid or project.allocated_port:
            stop_opencode_daemon(
                project.daemon_pid,
                allocated_port=project.allocated_port,
                project_absolute_path=project.absolute_path,
            )
        try:
            process = start_opencode_daemon(project.absolute_path, project.allocated_port)
        except Exception as error:  # noqa: BLE001
            logger.exception("Watchdog failed to restart daemon for project=%s: %s", project.name, error)
            failed_run_ids = fail_active_runs_for_project(
                project,
                message=f"Daemon crashed and watchdog restart failed: {error}",
                activity_kind="daemon.restart_failed",
            )
            release_project_lock(project, reason="daemon_restart_failed")
            broadcast_project_event(
                project.id,
                {
                    "kind": "daemon_restart_failed",
                    "project_id": project.id,
                    "allocated_port": project.allocated_port,
                    "failed_run_ids": failed_run_ids,
                    "error": str(error),
                },
            )
            _failure_counts.pop(project.id, None)
            continue

        old_pid = project.daemon_pid
        project.daemon_pid = process.pid
        project.daemon_stop_requested_at = None
        project.daemon_last_heartbeat_at = timezone.now()
        project.save(update_fields=["daemon_pid", "daemon_stop_requested_at", "daemon_last_heartbeat_at", "updated_at"])
        _failure_counts.pop(project.id, None)

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
