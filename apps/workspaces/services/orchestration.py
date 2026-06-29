import json
import re
from dataclasses import dataclass
from typing import Any

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from opencode_client import OpenCodeClient, diff_to_text, extract_text_from_parts

from ..models import AuditLog, OrchestrationArtifact, OrchestrationPlanStep, OrchestrationRun, OrchestrationRunActivity, OrchestrationStep, Project, TaskQueue
from .daemon import daemon_health, is_daemon_running, start_opencode_daemon, stop_opencode_daemon
from .github_sync import enqueue_github_sync_for_run
from .notifications import create_user_notification
from .realtime import broadcast_project_event, broadcast_user_event
from .telemetry import record_artifact, record_run_activity, send_run_status_event, transition_run
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
            approval_scope=OrchestrationRun.ApprovalScope.LOCK,
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
            approval_scope=TaskQueue.ApprovalScope.LOCK,
            status=TaskQueue.Status.PENDING_APPROVAL,
        )
        record_run_activity(
            run=queued_run,
            kind="approval.lock_requested",
            message=f"Prompt from {user.username} is waiting for lock approval.",
            payload={"task_id": queued_task.id, "requested_by": user.username},
        )
        record_artifact(
            run=queued_run,
            task=queued_task,
            artifact_type=OrchestrationArtifact.ArtifactType.APPROVAL,
            label="lock-approval-request",
            content=prompt,
            payload={"requested_by": user.username, "scope": OrchestrationRun.ApprovalScope.LOCK},
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
        send_run_status_event(project.id, queued_run)
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
            approval_scope=OrchestrationRun.ApprovalScope.NONE,
            status=OrchestrationRun.Status.QUEUED,
            current_phase="Queued",
            progress_percent=5,
        )
        record_run_activity(
            run=run,
            kind="run.queued",
            message="Run queued for background orchestration.",
            payload={"requested_by": user.username},
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
    send_run_status_event(project.id, run)

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
    send_run_status_event(project.id, run)
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
        run.approval_scope = OrchestrationRun.ApprovalScope.LOCK
        run.save(update_fields=["user", "approval_scope", "updated_at"])

    transition_run(
        run=run,
        status=OrchestrationRun.Status.QUEUED,
        current_phase="Queued",
        progress_percent=max(run.progress_percent, 5),
        last_error="",
        activity_kind="approval.lock_approved",
        activity_message=f"Lock approval granted by {approver.username}.",
        activity_payload={"approver": approver.username, "requester": requester.username},
    )
    record_artifact(
        run=run,
        task=task,
        artifact_type=OrchestrationArtifact.ArtifactType.APPROVAL,
        label="lock-approval-approved",
        content=task.supervisor_feedback,
        payload={"approver": approver.username, "scope": TaskQueue.ApprovalScope.LOCK},
    )

    _broadcast_task_status(project, task)
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
    send_run_status_event(project.id, run)
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
            approval_scope=OrchestrationRun.ApprovalScope.LOCK,
        )
        task.run.refresh_from_db()
        transition_run(
            run=task.run,
            status=OrchestrationRun.Status.FAILED,
            current_phase="Rejected",
            last_error=task.supervisor_feedback,
            finished=True,
            activity_kind="approval.lock_rejected",
            activity_message=task.supervisor_feedback,
            activity_level=OrchestrationRunActivity.Level.WARNING,
            activity_payload={"approver": approver.username, "reason": rejection_reason},
        )
        record_artifact(
            run=task.run,
            task=task,
            artifact_type=OrchestrationArtifact.ArtifactType.APPROVAL,
            label="lock-approval-rejected",
            content=task.supervisor_feedback,
            payload={"approver": approver.username, "scope": TaskQueue.ApprovalScope.LOCK},
        )
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


def approve_plan_for_run(run: OrchestrationRun, approver) -> dict:
    if run.status != OrchestrationRun.Status.AWAITING_PLAN_APPROVAL:
        raise OrchestrationError("Run is not awaiting plan approval.")
    if not run.plan_requires_approval:
        raise OrchestrationError("Run does not require plan approval.")

    draft_steps = list(run.plan_steps.filter(status=OrchestrationPlanStep.Status.DRAFT).order_by("sequence_order", "id"))
    if not draft_steps:
        raise OrchestrationError("Run does not have any draft plan steps to approve.")

    OrchestrationPlanStep.objects.filter(run=run, status=OrchestrationPlanStep.Status.DRAFT).update(
        status=OrchestrationPlanStep.Status.APPROVED,
        updated_at=timezone.now(),
    )
    run.plan_requires_approval = False
    run.approval_scope = OrchestrationRun.ApprovalScope.NONE
    run.plan_approved_at = timezone.now()
    run.save(update_fields=["plan_requires_approval", "approval_scope", "plan_approved_at", "updated_at"])

    transition_run(
        run=run,
        status=OrchestrationRun.Status.QUEUED,
        current_phase="Plan approved and queued for execution",
        progress_percent=max(run.progress_percent, 25),
        activity_kind="plan.approved",
        activity_message=f"Plan approved by {approver.username}.",
        activity_payload={"approver": approver.username, "step_count": len(draft_steps)},
    )
    record_artifact(
        run=run,
        artifact_type=OrchestrationArtifact.ArtifactType.APPROVAL,
        label="plan-approved",
        content=f"Approved by {approver.username}",
        payload={"approver": approver.username, "scope": OrchestrationRun.ApprovalScope.PLAN},
    )

    task_result = _enqueue_run(run)
    run.celery_task_id = task_result.id
    run.save(update_fields=["celery_task_id", "updated_at"])
    send_run_status_event(run.project_id, run)
    return {
        "mode": "plan_approved",
        "run_id": run.id,
        "celery_task_id": task_result.id,
        "status": run.status,
    }


def request_replan_for_run(run: OrchestrationRun, requester, feedback: str = "") -> dict:
    if run.status not in {OrchestrationRun.Status.AWAITING_PLAN_APPROVAL, OrchestrationRun.Status.PLAN_READY}:
        raise OrchestrationError("Run is not currently in a replannable state.")

    cleaned_feedback = feedback.strip()
    if cleaned_feedback:
        run.last_error = cleaned_feedback
        run.save(update_fields=["last_error", "updated_at"])

    OrchestrationPlanStep.objects.filter(run=run, status__in=[OrchestrationPlanStep.Status.DRAFT, OrchestrationPlanStep.Status.APPROVED]).update(
        status=OrchestrationPlanStep.Status.REPLACED,
        updated_at=timezone.now(),
    )
    run.plan_requires_approval = False
    run.plan_approved_at = None
    run.approval_scope = OrchestrationRun.ApprovalScope.NONE
    run.save(update_fields=["plan_requires_approval", "plan_approved_at", "approval_scope", "updated_at"])

    transition_run(
        run=run,
        status=OrchestrationRun.Status.QUEUED,
        current_phase="Queued for replanning",
        progress_percent=10,
        last_error=cleaned_feedback,
        activity_kind="plan.replan_requested",
        activity_message=cleaned_feedback or f"Replan requested by {requester.username}.",
        activity_level=OrchestrationRunActivity.Level.WARNING,
        activity_payload={"requested_by": requester.username},
    )
    record_artifact(
        run=run,
        artifact_type=OrchestrationArtifact.ArtifactType.APPROVAL,
        label="plan-replan-request",
        content=cleaned_feedback,
        payload={"requested_by": requester.username, "scope": OrchestrationRun.ApprovalScope.PLAN},
    )

    task_result = _enqueue_run(run)
    run.celery_task_id = task_result.id
    run.save(update_fields=["celery_task_id", "updated_at"])
    send_run_status_event(run.project_id, run)
    return {
        "mode": "plan_replan_requested",
        "run_id": run.id,
        "celery_task_id": task_result.id,
        "status": run.status,
    }


def perform_orchestration_run(run_id: int) -> dict:
    run = OrchestrationRun.objects.select_related("project", "user").get(pk=run_id)
    if run.status == OrchestrationRun.Status.CANCELLED:
        record_run_activity(run=run, kind="run.cancelled", message="Run was cancelled before execution started.")
        return {"mode": "cancelled", "run_id": run.id}

    if not run.project.allocated_port:
        mark_run_failed(run, "Project does not have an allocated OpenCode port.")
        raise OrchestrationError("Project does not have an allocated OpenCode port.")

    daemon_running = is_daemon_running(run.project.daemon_pid)
    health = daemon_health(run.project.allocated_port) if daemon_running else {"reachable": False, "healthy": False}
    if not daemon_running or not health.get("reachable") or not health.get("healthy"):
        if run.project.daemon_pid:
            stop_opencode_daemon(
                run.project.daemon_pid,
                allocated_port=run.project.allocated_port,
                project_absolute_path=run.project.absolute_path,
            )
        process = start_opencode_daemon(run.project.absolute_path, int(run.project.allocated_port))
        run.project.daemon_pid = process.pid
        run.project.save(update_fields=["daemon_pid", "updated_at"])
        record_run_activity(
            run=run,
            kind="daemon.restarted",
            message="Daemon restarted before orchestration execution.",
            payload={"daemon_pid": process.pid, "allocated_port": run.project.allocated_port},
        )

    if not run.user_id:
        mark_run_failed(run, "Run has no associated requesting user.")
        raise OrchestrationError("Run has no associated requesting user.")

    transition_run(
        run=run,
        status=OrchestrationRun.Status.PLANNING,
        current_phase="Planning",
        progress_percent=max(run.progress_percent, 10),
        last_error="",
        started=True,
        activity_kind="run.planning_started",
        activity_message="Planning orchestration run.",
    )

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

    run.refresh_from_db(fields=["total_steps", "completed_steps", "failed_steps", "progress_percent", "current_phase", "status", "updated_at"])
    if run.status in {OrchestrationRun.Status.AWAITING_PLAN_APPROVAL, OrchestrationRun.Status.PLAN_READY}:
        return result
    outcome_error = _evaluate_run_outcome(run)
    if outcome_error:
        mark_run_failed(run, outcome_error)
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
        raise OrchestrationError(outcome_error)

    transition_run(
        run=run,
        status=OrchestrationRun.Status.COMPLETED,
        current_phase="Completed",
        progress_percent=100,
        finished=True,
        activity_kind="run.completed",
        activity_message=f"Completed {run.completed_steps} of {run.total_steps} steps.",
        activity_payload={"completed_steps": run.completed_steps, "failed_steps": run.failed_steps, "total_steps": run.total_steps},
    )

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
    allowed_worker_agents = _allowed_worker_agents()

    approved_plan_steps = list(
        run.plan_steps.filter(status=OrchestrationPlanStep.Status.APPROVED).order_by("sequence_order", "id"),
    )
    if approved_plan_steps and not run.plan_requires_approval:
        transition_run(
            run=run,
            status=OrchestrationRun.Status.RUNNING,
            current_phase=f"Running {len(approved_plan_steps)} approved plan steps",
            progress_percent=max(run.progress_percent, 25),
            activity_kind="plan.execution_resumed",
            activity_message="Executing previously approved plan.",
            activity_payload={"step_count": len(approved_plan_steps)},
        )
        return _execute_planned_steps(
            client=client,
            project=project,
            user=user,
            raw_prompt=raw_prompt,
            run=run,
            steps=[
                {
                    "sequence_order": plan_step.sequence_order,
                    "assigned_agent": plan_step.assigned_agent,
                    "instruction": plan_step.instruction_payload,
                }
                for plan_step in approved_plan_steps
            ],
            plan_origin="approved",
        )

    plan_agent = settings.PLAN_AGENT_NAME
    supervisor_agent = settings.SUPERVISOR_AGENT_NAME

    plan_session = client.create_session(project.absolute_path, f"Plan {project.name}", plan_agent)
    run.plan_session_id = plan_session["id"]
    run.active_session_id = plan_session["id"]
    run.save(update_fields=["plan_session_id", "active_session_id", "updated_at"])
    record_run_activity(
        run=run,
        kind="session.created",
        message="Planning session created.",
        session_id=run.plan_session_id,
        payload={"agent": plan_agent, "title": f"Plan {project.name}"},
    )
    planner_prompt = _build_planner_prompt(raw_prompt=raw_prompt, allowed_worker_agents=allowed_worker_agents)
    plan_response = client.prompt(run.plan_session_id, project.absolute_path, planner_prompt, plan_agent)
    record_artifact(
        run=run,
        artifact_type=OrchestrationArtifact.ArtifactType.MESSAGE,
        session_id=run.plan_session_id,
        label="plan-request",
        content=planner_prompt,
        payload={"agent": plan_agent, "requested_prompt": raw_prompt},
    )
    _capture_usage_with_fallback(
        client=client,
        run=run,
        session_id=run.plan_session_id,
        directory=project.absolute_path,
        endpoint="session.message",
        payload=plan_response,
        step=None,
        task=None,
    )
    client.wait_for_idle(run.plan_session_id, project.absolute_path)
    _store_session_messages_artifact(client, run=run, session_id=run.plan_session_id, directory=project.absolute_path, label="plan-session-messages")
    blueprint = _extract_response_text(
        response_payload=plan_response,
        client=client,
        session_id=run.plan_session_id,
        directory=project.absolute_path,
    )
    if not blueprint:
        raise OrchestrationError("Plan agent returned an empty blueprint.")
    record_artifact(
        run=run,
        artifact_type=OrchestrationArtifact.ArtifactType.PLAN,
        session_id=run.plan_session_id,
        label="blueprint",
        content=blueprint,
    )

    run.blueprint = blueprint
    run.save(update_fields=["blueprint", "updated_at"])
    transition_run(
        run=run,
        status=OrchestrationRun.Status.BREAKING_DOWN,
        current_phase="Breaking tasks down",
        progress_percent=max(run.progress_percent, 20),
        activity_kind="run.breakdown_started",
        activity_message="Supervisor is breaking the blueprint into executable steps.",
    )

    supervisor_session = client.create_session(
        project.absolute_path,
        f"Supervisor {project.name}",
        supervisor_agent,
    )
    run.supervisor_session_id = supervisor_session["id"]
    run.active_session_id = supervisor_session["id"]
    run.save(update_fields=["supervisor_session_id", "active_session_id", "updated_at"])
    record_run_activity(
        run=run,
        kind="session.created",
        message="Supervisor session created.",
        session_id=run.supervisor_session_id,
        payload={"agent": supervisor_agent, "title": f"Supervisor {project.name}"},
    )
    supervisor_prompt = (
        "Convert the following blueprint into a strict JSON array of steps. "
        "Each item must have keys: sequence_order, assigned_agent, instruction. "
        f"The assigned_agent value MUST be exactly one of: {', '.join(allowed_worker_agents)}. "
        "Return JSON only and do not include markdown fences or commentary.\n\n"
        f"Blueprint:\n{blueprint}"
    )
    supervisor_response = client.prompt(
        run.supervisor_session_id,
        project.absolute_path,
        supervisor_prompt,
        supervisor_agent,
    )
    record_artifact(
        run=run,
        artifact_type=OrchestrationArtifact.ArtifactType.MESSAGE,
        session_id=run.supervisor_session_id,
        label="supervisor-step-request",
        content=supervisor_prompt,
        payload={"agent": supervisor_agent},
    )
    _capture_usage_with_fallback(
        client=client,
        run=run,
        session_id=run.supervisor_session_id,
        directory=project.absolute_path,
        endpoint="session.message",
        payload=supervisor_response,
        step=None,
        task=None,
    )
    client.wait_for_idle(run.supervisor_session_id, project.absolute_path)
    _store_session_messages_artifact(client, run=run, session_id=run.supervisor_session_id, directory=project.absolute_path, label="supervisor-session-messages")
    supervisor_text = _extract_response_text(
        response_payload=supervisor_response,
        client=client,
        session_id=run.supervisor_session_id,
        directory=project.absolute_path,
    )
    steps = _parse_supervisor_steps(supervisor_text)
    if not steps:
        raise OrchestrationError("Supervisor did not return a valid step list.")
    steps = _coerce_step_agents(steps, allowed_worker_agents=allowed_worker_agents)
    record_artifact(
        run=run,
        artifact_type=OrchestrationArtifact.ArtifactType.STEP_LIST,
        session_id=run.supervisor_session_id,
        label="supervisor-steps",
        content=supervisor_text,
        payload={"steps": steps},
    )

    OrchestrationPlanStep.objects.filter(run=run, status__in=[OrchestrationPlanStep.Status.DRAFT, OrchestrationPlanStep.Status.APPROVED]).update(
        status=OrchestrationPlanStep.Status.REPLACED,
        updated_at=timezone.now(),
    )
    plan_step_rows = [
        OrchestrationPlanStep(
            run=run,
            sequence_order=step["sequence_order"],
            assigned_agent=step["assigned_agent"],
            instruction_payload=step["instruction"],
            status=OrchestrationPlanStep.Status.DRAFT,
        )
        for step in steps
    ]
    OrchestrationPlanStep.objects.bulk_create(plan_step_rows)

    complexity = _classify_plan(steps)
    run.total_steps = len(steps)
    run.completed_steps = 0
    run.failed_steps = 0
    run.complexity_level = complexity["level"]
    run.plan_requires_approval = complexity["requires_approval"]
    run.approval_scope = OrchestrationRun.ApprovalScope.PLAN if complexity["requires_approval"] else OrchestrationRun.ApprovalScope.NONE
    run.plan_approved_at = None if complexity["requires_approval"] else timezone.now()
    run.save(
        update_fields=[
            "total_steps",
            "completed_steps",
            "failed_steps",
            "complexity_level",
            "plan_requires_approval",
            "approval_scope",
            "plan_approved_at",
            "updated_at",
        ],
    )
    record_run_activity(
        run=run,
        kind="plan.generated",
        message=f"Generated {len(steps)} draft plan steps.",
        payload={"complexity": complexity},
    )
    record_artifact(
        run=run,
        artifact_type=OrchestrationArtifact.ArtifactType.APPROVAL,
        label="plan-complexity",
        payload=complexity,
    )

    if complexity["requires_approval"]:
        transition_run(
            run=run,
            status=OrchestrationRun.Status.AWAITING_PLAN_APPROVAL,
            current_phase="Awaiting plan approval",
            progress_percent=max(run.progress_percent, 25),
            activity_kind="plan.awaiting_approval",
            activity_message="Generated plan requires approval before execution.",
            activity_payload=complexity,
        )
        return {
            "mode": "awaiting_plan_approval",
            "run_id": run.id,
            "blueprint": blueprint,
            "steps": [
                {
                    "sequence_order": step["sequence_order"],
                    "assigned_agent": step["assigned_agent"],
                    "instruction": step["instruction"],
                }
                for step in steps
            ],
            "complexity": complexity,
        }

    OrchestrationPlanStep.objects.filter(run=run, status=OrchestrationPlanStep.Status.DRAFT).update(
        status=OrchestrationPlanStep.Status.APPROVED,
        updated_at=timezone.now(),
    )
    transition_run(
        run=run,
        status=OrchestrationRun.Status.RUNNING,
        current_phase=f"Running {len(steps)} auto-approved steps",
        progress_percent=max(run.progress_percent, 25),
        activity_kind="plan.auto_approved",
        activity_message="Plan was auto-approved and execution is starting.",
        activity_payload=complexity,
    )

    return _execute_planned_steps(
        client=client,
        project=project,
        user=user,
        raw_prompt=raw_prompt,
        run=run,
        steps=steps,
        plan_origin="auto_approved",
        blueprint=blueprint,
    )


def _execute_planned_steps(
    *,
    client: OpenCodeClient,
    project: Project,
    user,
    raw_prompt: str,
    run: OrchestrationRun,
    steps: list[dict],
    plan_origin: str,
    blueprint: str = "",
) -> dict:
    supervisor_agent = settings.SUPERVISOR_AGENT_NAME
    if not run.supervisor_session_id:
        supervisor_session = client.create_session(
            project.absolute_path,
            f"Supervisor {project.name}",
            supervisor_agent,
        )
        run.supervisor_session_id = supervisor_session["id"]
        run.active_session_id = supervisor_session["id"]
        run.save(update_fields=["supervisor_session_id", "active_session_id", "updated_at"])
        record_run_activity(
            run=run,
            kind="session.created",
            message="Supervisor session created for execution stage.",
            session_id=run.supervisor_session_id,
            payload={"agent": supervisor_agent},
        )

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
        record_run_activity(
            run=run,
            kind="run.progress",
            message=run.current_phase,
            payload={"completed_steps": run.completed_steps, "failed_steps": run.failed_steps, "total_steps": run.total_steps},
        )
        send_run_status_event(project.id, run)

    return {
        "mode": "executed",
        "run_id": run.id,
        "blueprint": blueprint,
        "plan_origin": plan_origin,
        "steps": results,
    }


def _classify_plan(steps: list[dict]) -> dict:
    reasons: list[str] = []
    assigned_agents = {str(step.get("assigned_agent", "")).strip() for step in steps if step.get("assigned_agent")}
    instructions = "\n".join(str(step.get("instruction", "")) for step in steps)
    lowered = instructions.lower()

    if len(steps) > 5:
        reasons.append("step_count_gt_5")
    if len(assigned_agents) > 1:
        reasons.append("multiple_specialized_agents")
    if any(keyword in lowered for keyword in ["migration", "database", "schema"]):
        reasons.append("database_change")
    if any(keyword in lowered for keyword in ["auth", "jwt", "permission", "security"]):
        reasons.append("auth_or_security_change")
    if any(keyword in lowered for keyword in ["deploy", "docker", "nginx", "infra", "daemon"]):
        reasons.append("infra_change")
    if any(keyword in lowered for keyword in ["delete all", "drop", "remove all", "wipe", "destroy"]):
        reasons.append("destructive_operation")
    if any(keyword in lowered for keyword in ["github repo", "create repo", "origin", "bootstrap git", "main branch", "dev branch"]):
        reasons.append("repo_bootstrap")

    requires_approval = bool(reasons)
    level = OrchestrationRun.ComplexityLevel.COMPLEX if requires_approval else OrchestrationRun.ComplexityLevel.SIMPLE
    return {
        "level": level,
        "requires_approval": requires_approval,
        "reasons": reasons,
        "step_count": len(steps),
        "agent_count": len(assigned_agents),
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
        approval_scope=TaskQueue.ApprovalScope.NONE,
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
    record_run_activity(
        run=run,
        step=step_record,
        task=task,
        kind="step.queued",
        message=f"Queued step {step['sequence_order']} for @{step['assigned_agent']}.",
        payload={"instruction": step["instruction"], "assigned_agent": step["assigned_agent"]},
    )
    _broadcast_task_status(project, task)
    send_run_status_event(project.id, run)

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
    record_run_activity(
        run=run,
        step=step_record,
        task=task,
        kind="session.created",
        message=f"Worker session created for step {step['sequence_order']}.",
        session_id=worker_session_id,
        payload={"agent": step["assigned_agent"]},
    )
    send_run_status_event(project.id, run)

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
        transition_run(
            run=run,
            status=OrchestrationRun.Status.RUNNING,
            current_phase=f"Running step {step['sequence_order']}",
            progress_percent=_progress_for_run(run, active_step=step["sequence_order"], verifying=False),
            activity_kind="step.running",
            activity_message=f"Running step {step['sequence_order']} attempt {attempt}.",
            activity_payload={"sequence_order": step["sequence_order"], "assigned_agent": step["assigned_agent"], "attempt": attempt},
        )
        _broadcast_task_status(project, task)

        instruction = step["instruction"]
        if feedback:
            instruction = f"{instruction}\n\nSupervisor feedback to fix before finishing:\n{feedback}"
        record_artifact(
            run=run,
            step=step_record,
            task=task,
            artifact_type=OrchestrationArtifact.ArtifactType.MESSAGE,
            session_id=worker_session_id,
            label=f"worker-instruction-attempt-{attempt}",
            content=instruction,
            payload={"attempt": attempt, "assigned_agent": step["assigned_agent"]},
        )

        worker_response = client.prompt(worker_session_id, project.absolute_path, instruction, step["assigned_agent"])
        _capture_usage_with_fallback(
            client=client,
            run=run,
            session_id=worker_session_id,
            directory=project.absolute_path,
            endpoint="session.message",
            payload=worker_response,
            step=step_record,
            task=task,
        )
        client.wait_for_idle(worker_session_id, project.absolute_path)
        _store_session_messages_artifact(
            client,
            run=run,
            step=step_record,
            task=task,
            session_id=worker_session_id,
            directory=project.absolute_path,
            label=f"worker-session-messages-attempt-{attempt}",
        )

        task.status = TaskQueue.Status.VERIFYING
        task.save(update_fields=["status", "updated_at"])
        step_record.status = TaskQueue.Status.VERIFYING
        step_record.save(update_fields=["status", "updated_at"])
        transition_run(
            run=run,
            status=OrchestrationRun.Status.VERIFYING,
            current_phase=f"Verifying step {step['sequence_order']}",
            progress_percent=_progress_for_run(run, active_step=step["sequence_order"], verifying=True),
            activity_kind="step.verifying",
            activity_message=f"Supervisor reviewing step {step['sequence_order']} attempt {attempt}.",
            activity_payload={"sequence_order": step["sequence_order"], "attempt": attempt},
        )
        _broadcast_task_status(project, task)

        generated_diff = diff_to_text(client.session_diff(worker_session_id, project.absolute_path))
        step_record.generated_diff = generated_diff
        step_record.save(update_fields=["generated_diff", "updated_at"])
        record_artifact(
            run=run,
            step=step_record,
            task=task,
            artifact_type=OrchestrationArtifact.ArtifactType.DIFF,
            session_id=worker_session_id,
            label=f"worker-diff-attempt-{attempt}",
            content=generated_diff,
        )
        validation_feedback = _validate_step_with_supervisor(
            client=client,
            supervisor_session_id=supervisor_session_id,
            supervisor_agent=supervisor_agent,
            project=project,
            step=step,
            generated_diff=generated_diff,
            run=run,
            step_record=step_record,
            task_record=task,
        )
        if validation_feedback == "APPROVED":
            shell_payload = client.run_shell_command(
                worker_session_id,
                project.absolute_path,
                settings.PROJECT_COMPILE_COMMAND,
            )
            record_artifact(
                run=run,
                step=step_record,
                task=task,
                artifact_type=OrchestrationArtifact.ArtifactType.SHELL_OUTPUT,
                session_id=worker_session_id,
                label="compile-command",
                content=settings.PROJECT_COMPILE_COMMAND,
                payload=shell_payload or {},
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
            record_run_activity(
                run=run,
                step=step_record,
                task=task,
                kind="step.completed",
                message=f"Step {step['sequence_order']} approved.",
                session_id=worker_session_id,
                attempt_count=attempt,
                payload={"assigned_agent": step["assigned_agent"]},
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
        record_artifact(
            run=run,
            step=step_record,
            task=task,
            artifact_type=OrchestrationArtifact.ArtifactType.SUPERVISOR_FEEDBACK,
            session_id=supervisor_session_id,
            label=f"supervisor-feedback-attempt-{attempt}",
            content=validation_feedback,
        )
        record_run_activity(
            run=run,
            step=step_record,
            task=task,
            kind="step.retry_requested",
            message=validation_feedback,
            level=OrchestrationRunActivity.Level.WARNING,
            session_id=supervisor_session_id,
            attempt_count=attempt,
            payload={"sequence_order": step["sequence_order"], "assigned_agent": step["assigned_agent"]},
        )

        if attempt == max_attempts:
            break

    task.status = TaskQueue.Status.FAILED
    task.supervisor_feedback = feedback or "Supervisor rejected the change."
    task.save(update_fields=["status", "supervisor_feedback", "updated_at"])
    step_record.status = TaskQueue.Status.FAILED
    step_record.supervisor_feedback = task.supervisor_feedback
    step_record.save(update_fields=["status", "supervisor_feedback", "updated_at"])
    record_run_activity(
        run=run,
        step=step_record,
        task=task,
        kind="step.failed",
        message=task.supervisor_feedback,
        level=OrchestrationRunActivity.Level.ERROR,
        session_id=worker_session_id,
        attempt_count=step_record.attempt_count,
        payload={"sequence_order": step["sequence_order"], "assigned_agent": step["assigned_agent"]},
    )
    record_artifact(
        run=run,
        step=step_record,
        task=task,
        artifact_type=OrchestrationArtifact.ArtifactType.ERROR,
        session_id=worker_session_id,
        label="step-failure",
        content=task.supervisor_feedback,
    )
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
    step_record: OrchestrationStep | None = None,
    task_record: TaskQueue | None = None,
) -> str:
    expected_artifact_guidance = (
        "If the instruction is primarily conversational/planning/diagnostic and does not require file edits, "
        "do not reject solely because the diff is empty. Approve when the required non-code artifact is present and adequate."
    )
    validation_prompt = (
        "Review the implementation diff for this step. If the diff completely satisfies the instruction, "
        "reply with APPROVED only. Otherwise explain what must be fixed.\n\n"
        f"Validation guidance:\n{expected_artifact_guidance}\n\n"
        f"Step instruction:\n{step['instruction']}\n\n"
        f"Generated diff:\n{generated_diff}"
    )
    response = client.prompt(supervisor_session_id, project.absolute_path, validation_prompt, supervisor_agent)
    _capture_usage_with_fallback(
        client=client,
        run=run,
        session_id=supervisor_session_id,
        directory=project.absolute_path,
        endpoint="session.message",
        payload=response,
        step=step_record,
        task=task_record,
    )
    client.wait_for_idle(supervisor_session_id, project.absolute_path)
    _store_session_messages_artifact(
        client,
        run=run,
        step=step_record,
        task=task_record,
        session_id=supervisor_session_id,
        directory=project.absolute_path,
        label="supervisor-validation-messages",
    )
    feedback = extract_text_from_parts(response.get("parts", [])) or "Supervisor returned no feedback."
    record_artifact(
        run=run,
        step=step_record,
        task=task_record,
        artifact_type=OrchestrationArtifact.ArtifactType.SUPERVISOR_FEEDBACK,
        session_id=supervisor_session_id,
        label="supervisor-validation-feedback",
        content=feedback,
    )
    return feedback


def _capture_usage_with_fallback(
    *,
    client: OpenCodeClient,
    run: OrchestrationRun,
    session_id: str,
    directory: str,
    endpoint: str,
    payload: object,
    step: OrchestrationStep | None,
    task: TaskQueue | None,
) -> None:
    usage_event = capture_usage_event(run=run, session_id=session_id, endpoint=endpoint, payload=payload)
    if usage_event is not None:
        record_artifact(
            run=run,
            step=step,
            task=task,
            artifact_type=OrchestrationArtifact.ArtifactType.USAGE,
            session_id=session_id,
            label=f"usage:{endpoint}",
            payload={
                "prompt_tokens": usage_event.prompt_tokens,
                "completion_tokens": usage_event.completion_tokens,
                "total_tokens": usage_event.total_tokens,
                "raw_usage": usage_event.raw_usage,
            },
        )
        return

    try:
        client.wait_for_idle(session_id, directory)
        messages_payload = client.session_messages(session_id, directory)
    except Exception as error:  # noqa: BLE001
        record_run_activity(
            run=run,
            step=step,
            task=task,
            kind="usage.capture_unavailable",
            message=f"Unable to load session messages for usage fallback: {error}",
            level=OrchestrationRunActivity.Level.WARNING,
            session_id=session_id,
            payload={"endpoint": endpoint},
        )
        return

    usage_event = capture_usage_event(run=run, session_id=session_id, endpoint=f"{endpoint}.fallback_messages", payload=messages_payload)
    if usage_event is not None:
        record_artifact(
            run=run,
            step=step,
            task=task,
            artifact_type=OrchestrationArtifact.ArtifactType.USAGE,
            session_id=session_id,
            label=f"usage:{endpoint}:fallback",
            payload={
                "prompt_tokens": usage_event.prompt_tokens,
                "completion_tokens": usage_event.completion_tokens,
                "total_tokens": usage_event.total_tokens,
                "raw_usage": usage_event.raw_usage,
            },
        )
        return

    record_run_activity(
        run=run,
        step=step,
        task=task,
        kind="usage.capture_unavailable",
        message="No usage payload was found in prompt response or session message fallback.",
        level=OrchestrationRunActivity.Level.WARNING,
        session_id=session_id,
        payload={"endpoint": endpoint},
    )


def _store_session_messages_artifact(
    client: OpenCodeClient,
    *,
    run: OrchestrationRun,
    session_id: str,
    directory: str,
    label: str,
    step: OrchestrationStep | None = None,
    task: TaskQueue | None = None,
) -> None:
    try:
        messages_payload = client.session_messages(session_id, directory)
    except Exception as error:  # noqa: BLE001
        record_run_activity(
            run=run,
            step=step,
            task=task,
            kind="session.messages_unavailable",
            message=f"Unable to persist session messages: {error}",
            level=OrchestrationRunActivity.Level.WARNING,
            session_id=session_id,
        )
        return

    record_artifact(
        run=run,
        step=step,
        task=task,
        artifact_type=OrchestrationArtifact.ArtifactType.SESSION_MESSAGES,
        session_id=session_id,
        label=label,
        payload={"items": messages_payload if isinstance(messages_payload, list) else [messages_payload]},
    )


def _evaluate_run_outcome(run: OrchestrationRun) -> str:
    if run.total_steps <= 0:
        return "Run finished without any executable steps."
    if run.completed_steps <= 0:
        return "Run failed because no orchestration steps completed successfully."
    if run.failed_steps > 0:
        return f"Run failed because {run.failed_steps} of {run.total_steps} steps failed verification or execution."
    if run.completed_steps != run.total_steps:
        return f"Run finished in an inconsistent state: completed {run.completed_steps} of {run.total_steps} steps."
    return ""


def _allowed_worker_agents() -> list[str]:
    raw = (getattr(settings, "ORCHESTRATION_ALLOWED_WORKER_AGENTS", "") or "").strip()
    if not raw:
        return ["build", "frontend-wizard", "db-expert", "code-reviewer", "explore", "scout", "general"]
    return [agent.strip() for agent in raw.split(",") if agent.strip()]


def _build_planner_prompt(*, raw_prompt: str, allowed_worker_agents: list[str]) -> str:
    return (
        "You are the planning agent for a multi-agent coding orchestrator. "
        "Create a complete implementation blueprint for the user request.\n\n"
        "Rules:\n"
        "1) Always return a non-empty blueprint in plain text.\n"
        "2) Include scope, architecture, ordered implementation phases, verification steps, and risk notes.\n"
        "3) Be explicit enough that a supervisor can split work into executable agent tasks.\n"
        f"4) Worker agents available in this system are: {', '.join(allowed_worker_agents)}.\n"
        "5) Do not return JSON in this step.\n\n"
        f"User request:\n{raw_prompt}"
    )


def _extract_text_from_message_item(item: Any) -> str:
    if not isinstance(item, dict):
        return ""

    parts = item.get("parts")
    if isinstance(parts, list):
        text = extract_text_from_parts(parts)
        if text:
            return text

    for key in ("text", "content", "message"):
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()

    content_value = item.get("content")
    if isinstance(content_value, list):
        for entry in content_value:
            if isinstance(entry, dict):
                text = str(entry.get("text") or "").strip()
                if text:
                    return text
            elif isinstance(entry, str) and entry.strip():
                return entry.strip()

    return ""


def _extract_response_text(*, response_payload: Any, client: OpenCodeClient, session_id: str, directory: str) -> str:
    if isinstance(response_payload, dict):
        parts = response_payload.get("parts")
        if isinstance(parts, list):
            text = extract_text_from_parts(parts)
            if text:
                return text

        for key in ("text", "content", "message"):
            value = response_payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()

    try:
        messages_payload = client.session_messages(session_id, directory)
    except Exception:  # noqa: BLE001
        return ""

    if not isinstance(messages_payload, list):
        return ""

    for item in reversed(messages_payload):
        text = _extract_text_from_message_item(item)
        if text:
            return text

    return ""


def _coerce_step_agents(steps: list[dict], *, allowed_worker_agents: list[str]) -> list[dict]:
    allowed = {agent.strip() for agent in allowed_worker_agents if agent.strip()}
    fallback_agent = (getattr(settings, "ORCHESTRATION_FALLBACK_WORKER_AGENT", "general") or "general").strip()
    if fallback_agent not in allowed:
        raise OrchestrationError("Configured ORCHESTRATION_FALLBACK_WORKER_AGENT is not present in ORCHESTRATION_ALLOWED_WORKER_AGENTS.")

    normalized: list[dict] = []
    for step in steps:
        assigned_agent = str(step.get("assigned_agent") or "").strip()
        if assigned_agent not in allowed:
            assigned_agent = fallback_agent
        normalized.append(
            {
                "sequence_order": step["sequence_order"],
                "assigned_agent": assigned_agent,
                "instruction": step["instruction"],
            },
        )
    return normalized


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
        try:
            normalized_order = int(sequence_order)
        except (TypeError, ValueError):
            normalized_order = index
        if normalized_order <= 0:
            normalized_order = index
        normalized_steps.append(
            {
                "sequence_order": normalized_order,
                "assigned_agent": str(assigned_agent).strip(),
                "instruction": str(instruction).strip(),
            },
        )
    return normalized_steps


def _enqueue_run(run: OrchestrationRun):
    from ..tasks import execute_orchestration_run_task

    return execute_orchestration_run_task.delay(run.id)


def mark_run_failed(run: OrchestrationRun, error_message: str) -> None:
    transition_run(
        run=run,
        status=OrchestrationRun.Status.FAILED,
        current_phase="Failed",
        last_error=error_message,
        progress_percent=min(run.progress_percent, 99),
        finished=True,
        activity_kind="run.failed",
        activity_message=error_message,
        activity_level=OrchestrationRunActivity.Level.ERROR,
    )
    record_artifact(
        run=run,
        artifact_type=OrchestrationArtifact.ArtifactType.ERROR,
        label="run-failure",
        content=error_message,
    )
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
