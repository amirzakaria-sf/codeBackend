from __future__ import annotations

from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from opencode_client import OpenCodeClient, OpenCodeClientError

from ..models import OrchestrationRun, Project, TaskQueue
from .daemon import daemon_health, is_daemon_running, start_opencode_daemon, stop_opencode_daemon
from .orchestration import mark_run_failed
from .realtime import broadcast_project_event
from .telemetry import record_run_activity, send_run_status_event


ACTIVE_RUN_STATUSES = {
    OrchestrationRun.Status.QUEUED,
    OrchestrationRun.Status.PLANNING,
    OrchestrationRun.Status.BREAKING_DOWN,
    OrchestrationRun.Status.RUNNING,
    OrchestrationRun.Status.VERIFYING,
}


def scan_and_enqueue_stuck_runs() -> list[int]:
    threshold_seconds = max(30, settings.STUCK_RUN_THRESHOLD_SECONDS)
    cutoff = timezone.now() - timedelta(seconds=threshold_seconds)
    candidates = OrchestrationRun.objects.filter(
        status__in=ACTIVE_RUN_STATUSES,
        finished_at__isnull=True,
        updated_at__lt=cutoff,
    ).select_related("project", "user")

    enqueued: list[int] = []
    from ..tasks import recover_stuck_run_task

    for run in candidates:
        task_result = recover_stuck_run_task.delay(run.id)
        run.celery_task_id = task_result.id
        run.current_phase = "Recovery scheduled"
        run.save(update_fields=["celery_task_id", "current_phase", "updated_at"])
        send_run_status_event(run.project.id, run)
        enqueued.append(run.id)

    return enqueued


def recover_stuck_run(run_id: int) -> dict:
    run = OrchestrationRun.objects.select_related("project", "user").filter(pk=run_id).first()
    if not run:
        return {"mode": "missing", "run_id": run_id}
    if run.status in {OrchestrationRun.Status.COMPLETED, OrchestrationRun.Status.FAILED, OrchestrationRun.Status.CANCELLED}:
        return {"mode": "finished", "run_id": run.id, "status": run.status}

    max_attempts = max(1, settings.STUCK_RUN_MAX_RECOVERY_ATTEMPTS)
    if run.stuck_recovery_count >= max_attempts:
        mark_run_failed(run, "Run exceeded maximum automated recovery attempts.")
        _unlock_project(run.project)
        return {"mode": "failed", "run_id": run.id, "reason": "max_attempts_exceeded"}

    attempt_number = run.stuck_recovery_count + 1
    run.stuck_recovery_count = attempt_number
    run.last_recovery_at = timezone.now()
    run.last_recovery_error = ""
    run.current_phase = f"Recovering stuck run (attempt {attempt_number}/{max_attempts})"
    run.save(update_fields=["stuck_recovery_count", "last_recovery_at", "last_recovery_error", "current_phase", "updated_at"])
    record_run_activity(
        run=run,
        kind="recovery.started",
        message=run.current_phase,
        payload={"attempt": attempt_number, "max_attempts": max_attempts},
    )
    send_run_status_event(run.project.id, run)

    try:
        if attempt_number == 1:
            _attempt_interrupt(run)
            run.current_phase = f"Recovery attempt {attempt_number}: interrupt sent"
        elif attempt_number == 2:
            _attempt_continue(run)
            run.current_phase = f"Recovery attempt {attempt_number}: continue sent"
        else:
            _attempt_daemon_restart(run)
        run.current_phase = f"Recovery attempt {attempt_number}: daemon restarted"

        run.save(update_fields=["current_phase", "updated_at"])
        record_run_activity(
            run=run,
            kind="recovery.progress",
            message=run.current_phase,
            payload={"attempt": attempt_number},
        )
        send_run_status_event(run.project.id, run)
        return {"mode": "recovery_attempted", "run_id": run.id, "attempt": attempt_number}
    except Exception as error:  # noqa: BLE001
        run.last_recovery_error = str(error)
        run.current_phase = f"Recovery attempt {attempt_number} failed"
        run.save(update_fields=["last_recovery_error", "current_phase", "updated_at"])
        record_run_activity(
            run=run,
            kind="recovery.failed",
            message=run.current_phase,
            level="WARNING",
            payload={"attempt": attempt_number, "error": str(error)},
        )
        send_run_status_event(run.project.id, run)
        if run.stuck_recovery_count >= max_attempts:
            mark_run_failed(run, f"Recovery attempts exhausted: {error}")
            _unlock_project(run.project)
            return {"mode": "failed", "run_id": run.id, "error": str(error)}
        return {"mode": "recovery_failed", "run_id": run.id, "attempt": attempt_number, "error": str(error)}


def _attempt_interrupt(run: OrchestrationRun) -> None:
    session_id = _resolve_active_session(run)
    if not session_id:
        raise OpenCodeClientError("No active session available for interrupt recovery.")
    client = OpenCodeClient(run.project.allocated_port)
    client.abort_session(session_id, run.project.absolute_path)


def _attempt_continue(run: OrchestrationRun) -> None:
    session_id = _resolve_active_session(run)
    if not session_id:
        raise OpenCodeClientError("No active session available for continue recovery.")

    client = OpenCodeClient(run.project.allocated_port)
    agent = _resolve_recovery_agent(run)
    client.prompt(session_id, run.project.absolute_path, "continue", agent)


def _attempt_daemon_restart(run: OrchestrationRun) -> None:
    project = run.project
    if not project.allocated_port:
        raise OpenCodeClientError("Project does not have an allocated daemon port.")

    if project.daemon_pid and is_daemon_running(project.daemon_pid):
        stop_opencode_daemon(project.daemon_pid)

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

    health = daemon_health(project.allocated_port)
    if not health.get("healthy"):
        raise OpenCodeClientError("Daemon restart completed but health check is still failing.")


def _resolve_active_session(run: OrchestrationRun) -> str:
    for candidate in (run.active_session_id, run.supervisor_session_id, run.plan_session_id):
        if candidate:
            return candidate
    return ""


def _resolve_recovery_agent(run: OrchestrationRun) -> str:
    if run.status == OrchestrationRun.Status.PLANNING:
        return settings.PLAN_AGENT_NAME
    if run.status in {OrchestrationRun.Status.BREAKING_DOWN, OrchestrationRun.Status.VERIFYING}:
        return settings.SUPERVISOR_AGENT_NAME

    active_step = run.steps.filter(status__in=[TaskQueue.Status.RUNNING, TaskQueue.Status.VERIFYING]).order_by("sequence_order").first()
    if active_step:
        return active_step.assigned_agent
    return settings.SUPERVISOR_AGENT_NAME


def _unlock_project(project: Project) -> None:
    with transaction.atomic():
        project.is_locked = False
        project.locked_by = None
        project.save(update_fields=["is_locked", "locked_by", "updated_at"])

    broadcast_project_event(
        project.id,
        {
            "kind": "lock_status_changed",
            "project_id": project.id,
            "is_locked": False,
            "locked_by": None,
        },
    )
