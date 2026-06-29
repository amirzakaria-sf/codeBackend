import json
import re
from dataclasses import dataclass

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from opencode_client import OpenCodeClient, diff_to_text, extract_text_from_parts

from ..models import AuditLog, OrchestrationRun, OrchestrationStep, Project, TaskQueue
from .daemon import daemon_health, is_daemon_running, start_opencode_daemon, stop_opencode_daemon
from .github_sync import enqueue_github_sync_for_run
from .notifications import create_user_notification
from .realtime import broadcast_project_event, broadcast_user_event
from .usage import capture_usage_event


class OrchestrationError(Exception):
    """Raised when the orchestration loop fails."""


@dataclass
class StepResult:
    task: TaskQueue
    step: OrchestrationStep
    approved: bool
    feedback: str
    generated_diff: str


def execute_or_queue_project_prompt(project: Project, user, prompt: str) -> dict:
    if not project.allocated_port:
        raise OrchestrationError("Project does not have an allocated OpenCode port.")

    if project.is_locked and project.locked_by_id and project.locked_by_id != user.id:
        queued_run = OrchestrationRun.objects.create(
            project=project,
            user=user,
            prompt=prompt,
            status=OrchestrationRun.Status.PENDING_APPROVAL,
            current_phase="Waiting for approval",
            progress_percent=0,
        )
        queued_task = TaskQueue.objects.create(
            project=project,
            run=queued_run,
            user=user,
            assigned_agent="supervisor",
            instruction_payload=prompt,
            sequence_order=1,
            status=TaskQueue.Status.PENDING_APPROVAL,
        )
        broadcast_project_event(
            project.id,
            {
                "kind": "approval_requested",
                "project_id": project.id,
                "task_id": queued_task.id,
                "run_id": queued_run.id,
                "requested_by": user.username,
            },
        )
        if project.locked_by_id:
            broadcast_user_event(
                project.locked_by_id,
                {
                    "kind": "approval_requested",
                    "project_id": project.id,
                    "task_id": queued_task.id,
                    "run_id": queued_run.id,
                    "requested_by": user.username,
                },
            )
            create_user_notification(
                user=project.locked_by,
                kind="approval_requested",
                title=f"Approval requested for {project.name}",
                message=f"{user.username} submitted a prompt while the workspace was locked.",
                project=project,
                run=queued_run,
                payload={"task_id": queued_task.id, "run_id": queued_run.id},
            )
        send_run_status_event(project, queued_run)
        return {
            "mode": "queued_for_approval",
            "task_id": queued_task.id,
            "run_id": queued_run.id,
            "status": queued_task.status,
        }

    with transaction.atomic():
        project.is_locked = True
        project.locked_by = user
        project.save(update_fields=["is_locked", "locked_by", "updated_at"])
        run = OrchestrationRun.objects.create(
            project=project,
            user=user,
            prompt=prompt,
            status=OrchestrationRun.Status.QUEUED,
            current_phase="Queued",
            progress_percent=5,
        )

    broadcast_project_event(
        project.id,
        {
            "kind": "lock_status_changed",
            "project_id": project.id,
            "is_locked": True,
            "locked_by": user.username,
        },
    )
    send_run_status_event(project, run)

    try:
        task_result = _enqueue_run(run)
    except Exception as error:  # noqa: BLE001
        project.is_locked = False
        project.locked_by = None
        project.save(update_fields=["is_locked", "locked_by", "updated_at"])
        mark_run_failed(run, f"Failed to enqueue background run: {error}")
        broadcast_project_event(
            project.id,
            {
                "kind": "lock_status_changed",
                "project_id": project.id,
                "is_locked": False,
                "locked_by": None,
            },
        )
        raise OrchestrationError(str(error)) from error
    run.celery_task_id = task_result.id
    run.save(update_fields=["celery_task_id", "updated_at"])
    send_run_status_event(project, run)
    return {
        "mode": "queued",
        "run_id": run.id,
        "status": run.status,
        "progress_percent": run.progress_percent,
    }


def approve_pending_task(project: Project, task: TaskQueue, approver) -> dict:
    if task.project_id != project.id:
        raise OrchestrationError("Task does not belong to this project.")
    if task.status != TaskQueue.Status.PENDING_APPROVAL:
        raise OrchestrationError("Task is not pending approval.")
    if project.locked_by_id and project.locked_by_id != approver.id and not approver.is_staff:
        raise OrchestrationError("Only the project lock owner can approve this queued task.")

    run = task.run
    if not run:
        raise OrchestrationError("Pending approval task is missing its orchestration run.")

    requester = task.user or approver
    with transaction.atomic():
        project.is_locked = True
        project.locked_by = requester
        project.save(update_fields=["is_locked", "locked_by", "updated_at"])
        task.status = TaskQueue.Status.QUEUED
        task.supervisor_feedback = f"Approved by {approver.username}."
        task.save(update_fields=["status", "supervisor_feedback", "updated_at"])
        run.user = requester
        run.status = OrchestrationRun.Status.QUEUED
        run.current_phase = "Queued"
        run.progress_percent = max(run.progress_percent, 5)
        run.last_error = ""
        run.save(update_fields=["user", "status", "current_phase", "progress_percent", "last_error", "updated_at"])

    _broadcast_task_status(project, task)
    send_run_status_event(project, run)
    broadcast_project_event(
        project.id,
        {
            "kind": "lock_status_changed",
            "project_id": project.id,
            "is_locked": True,
            "locked_by": requester.username,
        },
    )

    try:
        task_result = _enqueue_run(run)
    except Exception as error:  # noqa: BLE001
        project.is_locked = False
        project.locked_by = None
        project.save(update_fields=["is_locked", "locked_by", "updated_at"])
        mark_run_failed(run, f"Failed to enqueue approved run: {error}")
        broadcast_project_event(
            project.id,
            {
                "kind": "lock_status_changed",
                "project_id": project.id,
                "is_locked": False,
                "locked_by": None,
            },
        )
        raise OrchestrationError(str(error)) from error
    run.celery_task_id = task_result.id
    run.save(update_fields=["celery_task_id", "updated_at"])
    send_run_status_event(project, run)
    return {
        "mode": "approved_and_enqueued",
        "approved_task_id": task.id,
        "run_id": run.id,
        "celery_task_id": task_result.id,
        "status": run.status,
    }


def reject_pending_task(project: Project, task: TaskQueue, approver, reason: str = "") -> dict:
    if task.project_id != project.id:
        raise OrchestrationError("Task does not belong to this project.")
    if task.status != TaskQueue.Status.PENDING_APPROVAL:
        raise OrchestrationError("Task is not pending approval.")
    if project.locked_by_id and project.locked_by_id != approver.id and not approver.is_staff:
        raise OrchestrationError("Only the project lock owner can reject this queued task.")

    rejection_reason = reason.strip() or "No reason provided."
    task.status = TaskQueue.Status.FAILED
    task.supervisor_feedback = f"Rejected by {approver.username}: {rejection_reason}"
    task.save(update_fields=["status", "supervisor_feedback", "updated_at"])
    if task.run_id:
        OrchestrationRun.objects.filter(pk=task.run_id).update(
            status=OrchestrationRun.Status.FAILED,
            current_phase="Rejected",
            last_error=task.supervisor_feedback,
            finished_at=timezone.now(),
            updated_at=timezone.now(),
        )
        task.run.refresh_from_db()
        send_run_status_event(project, task.run)
    _broadcast_task_status(project, task)

    if task.user_id:
        broadcast_user_event(
            task.user_id,
            {
                "kind": "approval_requested",
                "project_id": project.id,
                "task_id": task.id,
                "run_id": task.run_id,
                "requested_by": f"Rejected by {approver.username}",
            },
        )
        create_user_notification(
            user=task.user,
            kind="approval_rejected",
            title=f"Task rejected on {project.name}",
            message=task.supervisor_feedback,
            project=project,
            run=task.run,
            payload={"task_id": task.id, "run_id": task.run_id},
        )

    return {
        "mode": "rejected",
        "task_id": task.id,
        "run_id": task.run_id,
        "status": task.status,
        "reason": task.supervisor_feedback,
    }


def perform_orchestration_run(run_id: int) -> dict:
    run = OrchestrationRun.objects.select_related("project", "user").get(pk=run_id)
    if run.status == OrchestrationRun.Status.CANCELLED:
        return {"mode": "cancelled", "run_id": run.id}

    if not run.project.allocated_port:
        mark_run_failed(run, "Project does not have an allocated OpenCode port.")
        raise OrchestrationError("Project does not have an allocated OpenCode port.")

    daemon_running = is_daemon_running(run.project.daemon_pid)
    health = daemon_health(run.project.allocated_port) if daemon_running else {"reachable": False, "healthy": False}
    if not daemon_running or not health.get("reachable") or not health.get("healthy"):
        if run.project.daemon_pid:
            stop_opencode_daemon(run.project.daemon_pid)
        process = start_opencode_daemon(run.project.name, int(run.project.allocated_port))
        run.project.daemon_pid = process.pid
        run.project.save(update_fields=["daemon_pid", "updated_at"])

    if not run.user_id:
        mark_run_failed(run, "Run has no associated requesting user.")
        raise OrchestrationError("Run has no associated requesting user.")

    run.status = OrchestrationRun.Status.PLANNING
    run.current_phase = "Planning"
    run.progress_percent = max(run.progress_percent, 10)
    run.started_at = run.started_at or timezone.now()
    run.last_error = ""
    run.save(update_fields=["status", "current_phase", "progress_percent", "started_at", "last_error", "updated_at"])
    send_run_status_event(run.project, run)

    try:
        result = run_orchestration_cycle(project=run.project, user=run.user, raw_prompt=run.prompt, run=run)
    except Exception as error:  # noqa: BLE001
        mark_run_failed(run, str(error))
        run.project.is_locked = False
        run.project.locked_by = None
        run.project.save(update_fields=["is_locked", "locked_by", "updated_at"])
        broadcast_project_event(
            run.project.id,
            {
                "kind": "lock_status_changed",
                "project_id": run.project.id,
                "is_locked": False,
                "locked_by": None,
            },
        )
        raise

    run.status = OrchestrationRun.Status.COMPLETED
    run.current_phase = "Completed"
    run.progress_percent = 100
    run.finished_at = timezone.now()
    run.save(update_fields=["status", "current_phase", "progress_percent", "finished_at", "updated_at"])
    send_run_status_event(run.project, run)

    run.project.is_locked = False
    run.project.locked_by = None
    run.project.save(update_fields=["is_locked", "locked_by", "updated_at"])
    broadcast_project_event(
        run.project.id,
        {
            "kind": "lock_status_changed",
            "project_id": run.project.id,
            "is_locked": False,
            "locked_by": None,
        },
    )

    if run.user_id:
        create_user_notification(
            user=run.user,
            kind="run_completed",
            title=f"Run #{run.id} completed",
            message=f"Project {run.project.name} completed successfully.",
            project=run.project,
            run=run,
            payload={"run_id": run.id},
        )

    enqueue_github_sync_for_run(run)
    return result


def run_orchestration_cycle(project: Project, user, raw_prompt: str, run: OrchestrationRun) -> dict:
    client = OpenCodeClient(project.allocated_port)

    plan_agent = settings.PLAN_AGENT_NAME
    supervisor_agent = settings.SUPERVISOR_AGENT_NAME

    plan_session = client.create_session(project.absolute_path, f"Plan {project.name}", plan_agent)
    run.plan_session_id = plan_session["id"]
    run.active_session_id = plan_session["id"]
    run.save(update_fields=["plan_session_id", "active_session_id", "updated_at"])
    send_run_status_event(project, run)
    plan_response = client.prompt(run.plan_session_id, project.absolute_path, raw_prompt, plan_agent)
    capture_usage_event(run=run, session_id=run.plan_session_id, endpoint="session.message", payload=plan_response)
    client.wait_for_idle(run.plan_session_id, project.absolute_path)
    blueprint = extract_text_from_parts(plan_response.get("parts", []))
    if not blueprint:
        raise OrchestrationError("Plan agent returned an empty blueprint.")

    run.blueprint = blueprint
    run.status = OrchestrationRun.Status.BREAKING_DOWN
    run.current_phase = "Breaking tasks down"
    run.progress_percent = max(run.progress_percent, 20)
    run.save(update_fields=["blueprint", "status", "current_phase", "progress_percent", "updated_at"])
    send_run_status_event(project, run)

    supervisor_session = client.create_session(
        project.absolute_path,
        f"Supervisor {project.name}",
        supervisor_agent,
    )
    run.supervisor_session_id = supervisor_session["id"]
    run.active_session_id = supervisor_session["id"]
    run.save(update_fields=["supervisor_session_id", "active_session_id", "updated_at"])
    send_run_status_event(project, run)
    supervisor_prompt = (
        "Convert the following blueprint into a strict JSON array of steps. "
        "Each item must have keys: sequence_order, assigned_agent, instruction.\n\n"
        f"Blueprint:\n{blueprint}"
    )
    supervisor_response = client.prompt(
        run.supervisor_session_id,
        project.absolute_path,
        supervisor_prompt,
        supervisor_agent,
    )
    capture_usage_event(run=run, session_id=run.supervisor_session_id, endpoint="session.message", payload=supervisor_response)
    client.wait_for_idle(run.supervisor_session_id, project.absolute_path)
    supervisor_text = extract_text_from_parts(supervisor_response.get("parts", []))
    steps = _parse_supervisor_steps(supervisor_text)
    if not steps:
        raise OrchestrationError("Supervisor did not return a valid step list.")

    run.total_steps = len(steps)
    run.completed_steps = 0
    run.failed_steps = 0
    run.status = OrchestrationRun.Status.RUNNING
    run.current_phase = f"Running {len(steps)} steps"
    run.progress_percent = max(run.progress_percent, 25)
    run.save(update_fields=["total_steps", "completed_steps", "failed_steps", "status", "current_phase", "progress_percent", "updated_at"])
    send_run_status_event(project, run)

    results: list[dict] = []
    for step in steps:
        result = _execute_step(
            client=client,
            project=project,
            user=user,
            supervisor_session_id=run.supervisor_session_id,
            supervisor_agent=supervisor_agent,
            original_prompt=raw_prompt,
            step=step,
            run=run,
        )
        results.append(
            {
                "task_id": result.task.id,
                "step_id": result.step.id,
                "approved": result.approved,
                "status": result.task.status,
                "feedback": result.feedback,
            },
        )
        run.completed_steps = run.steps.filter(status=TaskQueue.Status.COMPLETED).count()
        run.failed_steps = run.steps.filter(status=TaskQueue.Status.FAILED).count()
        run.current_phase = f"Completed {run.completed_steps} of {run.total_steps} steps"
        run.progress_percent = _progress_for_run(run)
        run.save(update_fields=["completed_steps", "failed_steps", "current_phase", "progress_percent", "updated_at"])
        send_run_status_event(project, run)

    return {
        "mode": "executed",
        "run_id": run.id,
        "blueprint": blueprint,
        "steps": results,
    }


def _execute_step(
    client: OpenCodeClient,
    project: Project,
    user,
    supervisor_session_id: str,
    supervisor_agent: str,
    original_prompt: str,
    step: dict,
    run: OrchestrationRun,
) -> StepResult:
    task = TaskQueue.objects.create(
        project=project,
        run=run,
        user=user,
        assigned_agent=step["assigned_agent"],
        instruction_payload=step["instruction"],
        sequence_order=step["sequence_order"],
        status=TaskQueue.Status.QUEUED,
    )
    step_record = OrchestrationStep.objects.create(
        run=run,
        task=task,
        sequence_order=step["sequence_order"],
        assigned_agent=step["assigned_agent"],
        instruction_payload=step["instruction"],
        status=TaskQueue.Status.QUEUED,
    )
    _broadcast_task_status(project, task)
    send_run_status_event(project, run)

    worker_session = client.create_session(
        project.absolute_path,
        f"{step['assigned_agent']} step {step['sequence_order']}",
        step["assigned_agent"],
    )
    worker_session_id = worker_session["id"]
    step_record.worker_session_id = worker_session_id
    step_record.save(update_fields=["worker_session_id", "updated_at"])
    run.active_session_id = worker_session_id
    run.save(update_fields=["active_session_id", "updated_at"])
    send_run_status_event(project, run)

    max_attempts = 3
    feedback = ""
    generated_diff = ""

    for attempt in range(1, max_attempts + 1):
        task.status = TaskQueue.Status.RUNNING
        task.supervisor_feedback = feedback
        task.save(update_fields=["status", "supervisor_feedback", "updated_at"])
        step_record.status = TaskQueue.Status.RUNNING
        step_record.attempt_count = attempt
        step_record.supervisor_feedback = feedback
        step_record.save(update_fields=["status", "attempt_count", "supervisor_feedback", "updated_at"])
        run.status = OrchestrationRun.Status.RUNNING
        run.current_phase = f"Running step {step['sequence_order']}"
        run.progress_percent = _progress_for_run(run, active_step=step["sequence_order"], verifying=False)
        run.save(update_fields=["status", "current_phase", "progress_percent", "updated_at"])
        _broadcast_task_status(project, task)
        send_run_status_event(project, run)

        instruction = step["instruction"]
        if feedback:
            instruction = f"{instruction}\n\nSupervisor feedback to fix before finishing:\n{feedback}"

        worker_response = client.prompt(worker_session_id, project.absolute_path, instruction, step["assigned_agent"])
        capture_usage_event(run=run, session_id=worker_session_id, endpoint="session.message", payload=worker_response)
        client.wait_for_idle(worker_session_id, project.absolute_path)

        task.status = TaskQueue.Status.VERIFYING
        task.save(update_fields=["status", "updated_at"])
        step_record.status = TaskQueue.Status.VERIFYING
        step_record.save(update_fields=["status", "updated_at"])
        run.status = OrchestrationRun.Status.VERIFYING
        run.current_phase = f"Verifying step {step['sequence_order']}"
        run.progress_percent = _progress_for_run(run, active_step=step["sequence_order"], verifying=True)
        run.save(update_fields=["status", "current_phase", "progress_percent", "updated_at"])
        _broadcast_task_status(project, task)
        send_run_status_event(project, run)

        generated_diff = diff_to_text(client.session_diff(worker_session_id, project.absolute_path))
        step_record.generated_diff = generated_diff
        step_record.save(update_fields=["generated_diff", "updated_at"])
        validation_feedback = _validate_step_with_supervisor(
            client=client,
            supervisor_session_id=supervisor_session_id,
            supervisor_agent=supervisor_agent,
            project=project,
            step=step,
            generated_diff=generated_diff,
            run=run,
        )
        if validation_feedback == "APPROVED":
            client.run_shell_command(
                worker_session_id,
                project.absolute_path,
                settings.PROJECT_COMPILE_COMMAND,
            )
            task.status = TaskQueue.Status.COMPLETED
            task.supervisor_feedback = "APPROVED"
            task.save(update_fields=["status", "supervisor_feedback", "updated_at"])
            step_record.status = TaskQueue.Status.COMPLETED
            step_record.supervisor_feedback = "APPROVED"
            step_record.save(update_fields=["status", "supervisor_feedback", "updated_at"])
            AuditLog.objects.create(
                project=project,
                user=user,
                original_prompt=original_prompt,
                generated_diff=generated_diff,
            )
            _broadcast_task_status(project, task)
            return StepResult(
                task=task,
                step=step_record,
                approved=True,
                feedback="APPROVED",
                generated_diff=generated_diff,
            )

        feedback = validation_feedback
        task.supervisor_feedback = validation_feedback
        task.save(update_fields=["supervisor_feedback", "updated_at"])
        step_record.supervisor_feedback = validation_feedback
        step_record.save(update_fields=["supervisor_feedback", "updated_at"])

        if attempt == max_attempts:
            break

    task.status = TaskQueue.Status.FAILED
    task.supervisor_feedback = feedback or "Supervisor rejected the change."
    task.save(update_fields=["status", "supervisor_feedback", "updated_at"])
    step_record.status = TaskQueue.Status.FAILED
    step_record.supervisor_feedback = task.supervisor_feedback
    step_record.save(update_fields=["status", "supervisor_feedback", "updated_at"])
    _broadcast_task_status(project, task)
    return StepResult(task=task, step=step_record, approved=False, feedback=task.supervisor_feedback, generated_diff=generated_diff)


def _validate_step_with_supervisor(
    client: OpenCodeClient,
    supervisor_session_id: str,
    supervisor_agent: str,
    project: Project,
    step: dict,
    generated_diff: str,
    run: OrchestrationRun,
) -> str:
    validation_prompt = (
        "Review the implementation diff for this step. If the diff completely satisfies the instruction, "
        "reply with APPROVED only. Otherwise explain what must be fixed.\n\n"
        f"Step instruction:\n{step['instruction']}\n\n"
        f"Generated diff:\n{generated_diff}"
    )
    response = client.prompt(supervisor_session_id, project.absolute_path, validation_prompt, supervisor_agent)
    capture_usage_event(run=run, session_id=supervisor_session_id, endpoint="session.message", payload=response)
    client.wait_for_idle(supervisor_session_id, project.absolute_path)
    return extract_text_from_parts(response.get("parts", [])) or "Supervisor returned no feedback."


def _parse_supervisor_steps(supervisor_text: str) -> list[dict]:
    try:
        return _normalize_steps(json.loads(supervisor_text))
    except json.JSONDecodeError:
        pass

    match = re.search(r"(\[.*\])", supervisor_text, re.DOTALL)
    if not match:
        return []

    try:
        return _normalize_steps(json.loads(match.group(1)))
    except json.JSONDecodeError:
        return []


def _normalize_steps(payload: object) -> list[dict]:
    if not isinstance(payload, list):
        return []

    normalized_steps = []
    for index, item in enumerate(payload, start=1):
        if not isinstance(item, dict):
            continue
        assigned_agent = item.get("assigned_agent") or item.get("target")
        instruction = item.get("instruction") or item.get("prompt")
        sequence_order = item.get("sequence_order") or item.get("step") or index
        if not assigned_agent or not instruction:
            continue
        normalized_steps.append(
            {
                "sequence_order": int(sequence_order),
                "assigned_agent": str(assigned_agent),
                "instruction": str(instruction),
            },
        )
    return normalized_steps


def _enqueue_run(run: OrchestrationRun):
    from ..tasks import execute_orchestration_run_task

    return execute_orchestration_run_task.delay(run.id)


def mark_run_failed(run: OrchestrationRun, error_message: str) -> None:
    run.status = OrchestrationRun.Status.FAILED
    run.current_phase = "Failed"
    run.last_error = error_message
    run.finished_at = timezone.now()
    run.progress_percent = min(run.progress_percent, 99)
    run.save(update_fields=["status", "current_phase", "last_error", "finished_at", "progress_percent", "updated_at"])
    send_run_status_event(run.project, run)
    if run.user_id:
        create_user_notification(
            user=run.user,
            kind="run_failed",
            title=f"Run #{run.id} failed",
            message=error_message,
            project=run.project,
            run=run,
            payload={"run_id": run.id},
        )


def _progress_for_run(run: OrchestrationRun, active_step: int | None = None, verifying: bool = False) -> int:
    if run.total_steps <= 0:
        return run.progress_percent
    base = 25
    span = 65
    completed = run.completed_steps
    progress = base + int((completed / run.total_steps) * span)
    if active_step and completed < run.total_steps:
        increment = max(1, span // max(run.total_steps, 1))
        progress += increment if verifying else increment // 2
    return min(progress, 95 if verifying else 90 if active_step else 100)


def _broadcast_task_status(project: Project, task: TaskQueue) -> None:
    broadcast_project_event(
        project.id,
        {
            "kind": "task_status_changed",
            "project_id": project.id,
            "task_id": task.id,
            "run_id": task.run_id,
            "sequence_order": task.sequence_order,
            "assigned_agent": task.assigned_agent,
            "status": task.status,
            "supervisor_feedback": task.supervisor_feedback,
        },
    )


def send_run_status_event(project: Project, run: OrchestrationRun) -> None:
    broadcast_project_event(
        project.id,
        {
            "kind": "orchestration_run_updated",
            "project_id": project.id,
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
