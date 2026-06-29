from __future__ import annotations

from typing import Any

from django.utils import timezone

from ..models import OrchestrationArtifact, OrchestrationRun, OrchestrationRunActivity, OrchestrationStep, TaskQueue
from .realtime import broadcast_project_event


def record_run_activity(
    *,
    run: OrchestrationRun,
    kind: str,
    message: str = "",
    level: str = OrchestrationRunActivity.Level.INFO,
    payload: dict[str, Any] | None = None,
    step: OrchestrationStep | None = None,
    task: TaskQueue | None = None,
    session_id: str = "",
    attempt_count: int = 0,
) -> OrchestrationRunActivity:
    activity = OrchestrationRunActivity.objects.create(
        run=run,
        step=step,
        task=task,
        kind=kind,
        level=level,
        session_id=session_id,
        attempt_count=max(attempt_count, 0),
        message=message,
        payload=payload or {},
    )
    return activity


def record_artifact(
    *,
    run: OrchestrationRun,
    artifact_type: str,
    content: str = "",
    payload: dict[str, Any] | list[Any] | None = None,
    label: str = "",
    step: OrchestrationStep | None = None,
    task: TaskQueue | None = None,
    session_id: str = "",
) -> OrchestrationArtifact:
    normalized_payload: dict[str, Any]
    if isinstance(payload, dict):
        normalized_payload = payload
    elif isinstance(payload, list):
        normalized_payload = {"items": payload}
    else:
        normalized_payload = {}

    artifact = OrchestrationArtifact.objects.create(
        run=run,
        step=step,
        task=task,
        artifact_type=artifact_type,
        session_id=session_id,
        label=label,
        content=content,
        payload=normalized_payload,
    )
    return artifact


def transition_run(
    *,
    run: OrchestrationRun,
    status: str,
    current_phase: str,
    progress_percent: int | None = None,
    last_error: str | None = None,
    started: bool = False,
    finished: bool = False,
    activity_kind: str = "run.transition",
    activity_message: str = "",
    activity_level: str = OrchestrationRunActivity.Level.INFO,
    activity_payload: dict[str, Any] | None = None,
) -> OrchestrationRun:
    run.status = status
    run.current_phase = current_phase
    if progress_percent is not None:
        run.progress_percent = progress_percent
    if last_error is not None:
        run.last_error = last_error
    if started and not run.started_at:
        run.started_at = timezone.now()
    if finished:
        run.finished_at = timezone.now()

    update_fields = ["status", "current_phase", "updated_at"]
    if progress_percent is not None:
        update_fields.append("progress_percent")
    if last_error is not None:
        update_fields.append("last_error")
    if started and run.started_at:
        update_fields.append("started_at")
    if finished and run.finished_at:
        update_fields.append("finished_at")
    run.save(update_fields=update_fields)

    record_run_activity(
        run=run,
        kind=activity_kind,
        message=activity_message or current_phase,
        level=activity_level,
        payload=activity_payload or {"status": status, "phase": current_phase, "progress_percent": run.progress_percent},
    )
    send_run_status_event(run.project_id, run)
    return run


def send_run_status_event(project_id: int, run: OrchestrationRun) -> None:
    broadcast_project_event(
        project_id,
        {
            "kind": "orchestration_run_updated",
            "project_id": project_id,
            "run_id": run.id,
            "status": run.status,
            "current_phase": run.current_phase,
            "progress_percent": run.progress_percent,
            "total_steps": run.total_steps,
            "completed_steps": run.completed_steps,
            "failed_steps": run.failed_steps,
            "last_error": run.last_error,
            "prompt_tokens": run.prompt_tokens,
            "completion_tokens": run.completion_tokens,
            "total_tokens": run.total_tokens,
            "stuck_recovery_count": run.stuck_recovery_count,
            "last_recovery_error": run.last_recovery_error,
        },
    )
