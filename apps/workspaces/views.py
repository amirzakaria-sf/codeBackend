import re
import uuid
from pathlib import Path

from celery.result import AsyncResult
from django.db import transaction
from django.db.models import Count, Q, Sum
from django.shortcuts import get_object_or_404
from rest_framework import mixins, permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.response import Response

from opencode_client import OpenCodeClient, OpenCodeClientError
from django.utils import timezone

from .models import GitSyncJob, OrchestrationPlanStep, OrchestrationRun, Project, TaskQueue, TokenUsageEvent, UserNotification, WorkspaceTarget
from .serializers import (
    AuditLogSerializer,
    ExecuteIdeaSerializer,
    GitSyncJobSerializer,
    LockAcquireSerializer,
    OrchestrationArtifactSerializer,
    OrchestrationPlanStepSerializer,
    OrchestrationRunActivitySerializer,
    OrchestrationRunSerializer,
    PlanApprovalSerializer,
    PlanReplanSerializer,
    PlanStepUpdateSerializer,
    PendingApprovalDecisionSerializer,
    PendingApprovalTaskSerializer,
    ProjectSerializer,
    ProvisionProjectSerializer,
    SessionForkSerializer,
    SessionSummarizeSerializer,
    StartDaemonSerializer,
    TaskQueueSerializer,
    TokenUsageEventSerializer,
    UserNotificationSerializer,
)
from .services.daemon import (
    DaemonStartError,
    allocate_available_port,
    daemon_directory_for_project,
    daemon_runtime_status,
    project_opencode_config_summary,
    start_opencode_daemon,
    stop_opencode_daemon,
)
from .services.orchestration import (
    OrchestrationError,
    approve_plan_for_run,
    approve_pending_task,
    cancel_active_runs_for_project,
    expire_project_lock_if_stale,
    execute_or_queue_project_prompt,
    fail_active_runs_for_project,
    mark_project_locked,
    release_project_lock,
    request_replan_for_run,
    reject_pending_task,
)
from .services.github_sync import retry_sync_job
from .services.provisioning import (
    ProvisionWorkspaceRequest,
    ProvisioningError,
    WorkspaceTargetSpec,
    normalize_path_owner_username,
    provision_project_structure,
)
from .services.realtime import broadcast_project_event
from .services.telemetry import send_run_status_event
from .throttles import ExecutePromptThrottle, ProjectMutationThrottle


ALLOWED_REFERENCE_EXTENSIONS = {
    ".pdf",
    ".md",
    ".txt",
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".webp",
    ".svg",
    ".py",
    ".js",
    ".jsx",
    ".ts",
    ".tsx",
    ".json",
    ".yaml",
    ".yml",
    ".html",
    ".css",
    ".scss",
    ".java",
    ".go",
    ".rs",
    ".c",
    ".cpp",
    ".h",
    ".hpp",
    ".sh",
    ".sql",
    ".xml",
    ".toml",
    ".ini",
}
MAX_REFERENCE_UPLOAD_BYTES = 25 * 1024 * 1024

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


class ProjectViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet):
    queryset = Project.objects.all().select_related("locked_by", "owner").prefetch_related("targets")
    serializer_class = ProjectSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        queryset = Project.objects.all().select_related("locked_by", "owner").prefetch_related("targets")
        user = getattr(self.request, "user", None)
        if not user or user.is_anonymous:
            return queryset.none()
        if user.is_staff or user.is_superuser:
            return queryset
        return queryset.filter(owner=user)

    def get_serializer_context(self):
        context = super().get_serializer_context()
        context["request"] = self.request
        return context

    def _require_lock_owner_or_admin(self, user, project: Project):
        if user.is_staff or user.is_superuser:
            return
        if project.locked_by_id == user.id:
            return
        raise PermissionDenied("Only the project lock owner or an admin can perform this action.")

    def _require_plan_approver(self, user, project: Project, run: OrchestrationRun):
        if user.is_staff or user.is_superuser:
            return
        if run.user_id == user.id:
            return
        if project.locked_by_id == user.id:
            return
        raise PermissionDenied("Only the run requester, lock owner, or an admin can approve or replan this draft plan.")

    @staticmethod
    def _build_opencode_client(project: Project) -> OpenCodeClient:
        if not project.allocated_port:
            raise OrchestrationError("Project does not have an allocated OpenCode port.")
        return OpenCodeClient(project.allocated_port)

    @staticmethod
    def _daemon_directory(project: Project) -> str:
        return daemon_directory_for_project(project.absolute_path)

    @staticmethod
    def _dedupe_update_fields(fields: list[str]) -> list[str]:
        unique_fields: list[str] = []
        for field in fields:
            if field not in unique_fields:
                unique_fields.append(field)
        return unique_fields

    @staticmethod
    def _mark_daemon_running_intent(project: Project, *, allocated_port: int | None = None, daemon_pid: int | None = None) -> None:
        project.daemon_desired_state = Project.DaemonDesiredState.RUNNING
        project.daemon_stop_requested_at = None
        project.daemon_last_heartbeat_at = timezone.now()
        if allocated_port is not None:
            project.allocated_port = allocated_port
        if daemon_pid is not None:
            project.daemon_pid = daemon_pid

    @staticmethod
    def _mark_daemon_stopped_intent(project: Project) -> None:
        project.daemon_desired_state = Project.DaemonDesiredState.STOPPED
        project.daemon_stop_requested_at = timezone.now()

    @staticmethod
    def _refresh_daemon_heartbeat(project: Project, runtime_status: dict) -> bool:
        if runtime_status.get("reachable") or runtime_status.get("healthy"):
            project.daemon_last_heartbeat_at = timezone.now()
            return True
        return False

    @staticmethod
    def _daemon_config_diagnostics(project: Project) -> dict:
        try:
            summary = project_opencode_config_summary(project.absolute_path)
            return {
                "loaded": True,
                **summary,
            }
        except DaemonStartError as error:
            return {
                "loaded": False,
                "error": str(error),
            }

    @staticmethod
    def _cancel_active_runs_after_daemon_stop(project: Project) -> list[int]:
        cancelled_run_ids = cancel_active_runs_for_project(
            project,
            reason=OrchestrationRun.CancellationReason.MANUAL_DAEMON_STOP,
            message="Run cancelled manually when daemon was stopped.",
            activity_kind="run.cancelled.manual_daemon_stop",
        )
        release_project_lock(project, reason="manual_daemon_stop")
        return cancelled_run_ids

    def _ensure_runtime_ready(self, project: Project, user) -> Project:
        if project.is_locked and project.locked_by_id and project.locked_by_id != user.id and not (user.is_staff or user.is_superuser):
            stale_runtime_status = daemon_runtime_status(project.daemon_pid, project.allocated_port)
            expire_project_lock_if_stale(project, runtime_status=stale_runtime_status)
            project.refresh_from_db()
            if project.is_locked and project.locked_by_id and project.locked_by_id != user.id:
                raise PermissionDenied("Workspace is locked by another user.")

        fields_to_update: list[str] = []
        lock_was_owned_by_user = project.is_locked and project.locked_by_id == user.id
        mark_project_locked(project, user=user)

        if not project.allocated_port:
            project.allocated_port = allocate_available_port()
            fields_to_update.extend(["allocated_port", "updated_at"])

        if project.daemon_desired_state != Project.DaemonDesiredState.RUNNING:
            self._mark_daemon_running_intent(project)
            fields_to_update.extend(["daemon_desired_state", "daemon_stop_requested_at", "daemon_last_heartbeat_at", "updated_at"])

        port = int(project.allocated_port)
        runtime_status = daemon_runtime_status(project.daemon_pid, port)

        if runtime_status.get("reachable") or runtime_status.get("healthy"):
            if self._refresh_daemon_heartbeat(project, runtime_status):
                fields_to_update.extend(["daemon_last_heartbeat_at", "updated_at"])

        if not runtime_status.get("running") or not runtime_status.get("reachable") or not runtime_status.get("healthy"):
            try:
                if project.daemon_pid:
                    stop_opencode_daemon(
                        project.daemon_pid,
                        allocated_port=project.allocated_port,
                        project_absolute_path=project.absolute_path,
                    )
                process = start_opencode_daemon(project.absolute_path, port)
            except Exception:
                release_project_lock(project, reason="runtime_prepare_failed")
                raise
            self._mark_daemon_running_intent(project, daemon_pid=process.pid)
            fields_to_update.extend(["daemon_pid", "daemon_desired_state", "daemon_stop_requested_at", "daemon_last_heartbeat_at", "updated_at"])

        if fields_to_update:
            project.save(update_fields=self._dedupe_update_fields(fields_to_update))

        if not lock_was_owned_by_user:
            broadcast_project_event(
                project.id,
                {
                    "kind": "lock_status_changed",
                    "project_id": project.id,
                    "is_locked": project.is_locked,
                    "locked_by": project.locked_by.username if project.locked_by else None,
                },
            )

        return project

    @staticmethod
    def _sanitize_reference_filename(filename: str) -> str:
        base_name = Path(filename).name
        cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", base_name).strip("-._")
        return cleaned or "reference-upload"

    @staticmethod
    def _doc_references_root(project: Project) -> Path:
        project_root = Path(project.absolute_path).expanduser().resolve()
        if not project_root.exists() or not project_root.is_dir():
            raise ProvisioningError("Project workspace path does not exist on disk.")
        references_root = (project_root / "doc-references").resolve()
        if project_root not in references_root.parents:
            raise ProvisioningError("Invalid doc-references path.")
        references_root.mkdir(parents=True, exist_ok=True)
        return references_root

    @action(detail=True, methods=["post"], url_path="lock-acquire", throttle_classes=[ProjectMutationThrottle])
    def lock_acquire(self, request, pk=None):
        project = self.get_object()
        serializer = LockAcquireSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        force = serializer.validated_data["force"]

        if project.is_locked:
            if project.locked_by_id != request.user.id:
                expire_project_lock_if_stale(project, runtime_status=daemon_runtime_status(project.daemon_pid, project.allocated_port))
                project.refresh_from_db()
        if project.is_locked:
            if project.locked_by_id == request.user.id:
                return Response(
                    {
                        "detail": "Lock already held by current user.",
                        "project": ProjectSerializer(project).data,
                    },
                    status=status.HTTP_200_OK,
                )

            if not (request.user.is_staff or request.user.is_superuser) or not force:
                return Response(
                    {
                        "detail": "Project is locked by another user. Only admins can force lock takeover.",
                        "locked_by": project.locked_by.username if project.locked_by else None,
                    },
                    status=status.HTTP_409_CONFLICT,
                )

        mark_project_locked(project, user=request.user)
        broadcast_project_event(
            project.id,
            {
                "kind": "lock_status_changed",
                "project_id": project.id,
                "is_locked": True,
                "locked_by": request.user.username,
            },
        )

        return Response(
            {
                "detail": "Lock acquired.",
                "project": ProjectSerializer(project).data,
            },
            status=status.HTTP_200_OK,
        )

    @action(detail=True, methods=["post"], url_path="lock-release", throttle_classes=[ProjectMutationThrottle])
    def lock_release(self, request, pk=None):
        project = self.get_object()

        if not project.is_locked:
            return Response(
                {
                    "detail": "Project is already unlocked.",
                    "project": ProjectSerializer(project).data,
                },
                status=status.HTTP_200_OK,
            )

        self._require_lock_owner_or_admin(request.user, project)
        release_project_lock(project, reason="manual_lock_release")

        return Response(
            {
                "detail": "Lock released.",
                "project": ProjectSerializer(project).data,
            },
            status=status.HTTP_200_OK,
        )

    @action(detail=False, methods=["post"], url_path="provision", throttle_classes=[ProjectMutationThrottle])
    def provision(self, request):
        serializer = ProvisionProjectSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        project_name = serializer.validated_data["name"]
        normalized_owner_username = normalize_path_owner_username(request.user.username)

        if Project.objects.filter(path_owner_username=normalized_owner_username, name=project_name).exists():
            return Response(
                {"detail": f"Project '{project_name}' already exists for this user."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            with transaction.atomic():
                custom_targets = tuple(
                    WorkspaceTargetSpec(
                        name=target["name"],
                        role=target["role"],
                        source_type=target["source_type"],
                        relative_path=target.get("relative_path", ""),
                        remote_url=target.get("remote_url", ""),
                        default_branch=target.get("default_branch", ""),
                        is_primary=target.get("is_primary", False),
                        is_editable=target.get("is_editable", True),
                    )
                    for target in serializer.validated_data.get("targets", [])
                )
                provision_request = ProvisionWorkspaceRequest(
                    project_name=project_name,
                    path_owner_username=request.user.username,
                    workspace_mode=serializer.validated_data["workspace_mode"],
                    starter_template=serializer.validated_data.get("starter_template", Project.StarterTemplate.FULLSTACK),
                    bootstrap_enabled=serializer.validated_data.get("bootstrap_enabled", False),
                    clone_remote_url=serializer.validated_data.get("clone_remote_url", ""),
                    clone_branch=serializer.validated_data.get("clone_branch", ""),
                    clone_target_name=serializer.validated_data.get("clone_target_name", ""),
                    clone_target_role=serializer.validated_data.get("clone_target_role", WorkspaceTarget.Role.CUSTOM),
                    custom_targets=custom_targets,
                )
                provisioned_workspace = provision_project_structure(provision_request)
                project = Project.objects.create(
                    owner=request.user,
                    path_owner_username=provisioned_workspace.path_owner_username,
                    name=project_name,
                    absolute_path=str(provisioned_workspace.project_root),
                    workspace_mode=provisioned_workspace.workspace_mode,
                    starter_template=provisioned_workspace.starter_template,
                    setup_status=provisioned_workspace.setup_status,
                    bootstrap_enabled=provisioned_workspace.bootstrap_enabled,
                )
                WorkspaceTarget.objects.bulk_create(
                    [
                        WorkspaceTarget(
                            project=project,
                            name=target.name,
                            relative_path=target.relative_path,
                            absolute_path=str((provisioned_workspace.project_root / target.relative_path).resolve()),
                            role=target.role,
                            source_type=target.source_type,
                            remote_url=target.remote_url,
                            default_branch=target.default_branch,
                            is_primary=target.is_primary,
                            is_editable=target.is_editable,
                        )
                        for target in provisioned_workspace.targets
                    ],
                )
                project.refresh_from_db()
        except ProvisioningError as error:
            return Response({"detail": str(error)}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as error:  # noqa: BLE001
            return Response(
                {"detail": f"Unexpected provisioning error: {error}"},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

        return Response(ProjectSerializer(project).data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["post"], url_path="start-daemon", throttle_classes=[ProjectMutationThrottle])
    def start_daemon(self, request, pk=None):
        project = self.get_object()
        self._require_lock_owner_or_admin(request.user, project)
        serializer = StartDaemonSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        allocated_port = serializer.validated_data.get("allocated_port") or project.allocated_port or allocate_available_port()
        runtime_status = daemon_runtime_status(project.daemon_pid, project.allocated_port or allocated_port)

        if runtime_status.get("running"):
            self._mark_daemon_running_intent(project, allocated_port=project.allocated_port or allocated_port)
            project.save(update_fields=["allocated_port", "daemon_desired_state", "daemon_stop_requested_at", "daemon_last_heartbeat_at", "updated_at"])
            return Response(
                {
                    "detail": "Daemon is already running for this project.",
                    "daemon_pid": project.daemon_pid,
                    "allocated_port": project.allocated_port,
                    "daemon_desired_state": project.daemon_desired_state,
                    "config": self._daemon_config_diagnostics(project),
                },
                status=status.HTTP_200_OK,
            )

        try:
            process = start_opencode_daemon(project.absolute_path, allocated_port)
        except (ProvisioningError, DaemonStartError) as error:
            return Response({"detail": str(error)}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as error:  # noqa: BLE001
            return Response(
                {"detail": f"Unexpected daemon error: {error}"},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

        self._mark_daemon_running_intent(project, allocated_port=allocated_port, daemon_pid=process.pid)
        project.save(
            update_fields=[
                "allocated_port",
                "daemon_pid",
                "daemon_desired_state",
                "daemon_stop_requested_at",
                "daemon_last_heartbeat_at",
                "updated_at",
            ],
        )

        return Response(
            {
                "project_id": project.id,
                "name": project.name,
                "allocated_port": allocated_port,
                "daemon_pid": process.pid,
                "daemon_desired_state": project.daemon_desired_state,
                "config": self._daemon_config_diagnostics(project),
                "message": "OpenCode daemon started.",
            },
            status=status.HTTP_200_OK,
        )

    @action(detail=True, methods=["post"], url_path="stop-daemon", throttle_classes=[ProjectMutationThrottle])
    def stop_daemon(self, request, pk=None):
        project = self.get_object()
        self._require_lock_owner_or_admin(request.user, project)
        broadcast_project_event(
            project.id,
            {
                "kind": "daemon_stop_requested",
                "project_id": project.id,
                "daemon_pid": project.daemon_pid,
                "allocated_port": project.allocated_port,
                "requested_by": request.user.username,
            },
        )
        if not project.daemon_pid and not project.allocated_port:
            project.daemon_desired_state = Project.DaemonDesiredState.STOPPED
            project.daemon_stop_requested_at = timezone.now()
            project.save(update_fields=["daemon_desired_state", "daemon_stop_requested_at", "updated_at"])
            cancelled_run_ids = self._cancel_active_runs_after_daemon_stop(project)
            broadcast_project_event(
                project.id,
                {
                    "kind": "daemon_stopped",
                    "project_id": project.id,
                    "daemon_pid": None,
                    "allocated_port": project.allocated_port,
                    "cancelled_run_ids": cancelled_run_ids,
                    "requested_by": request.user.username,
                },
            )
            return Response(
                {
                    "project_id": project.id,
                    "name": project.name,
                    "daemon_desired_state": project.daemon_desired_state,
                    "message": "Daemon is already stopped.",
                },
                status=status.HTTP_200_OK,
            )

        self._mark_daemon_stopped_intent(project)
        project.save(update_fields=["daemon_desired_state", "daemon_stop_requested_at", "updated_at"])

        stopped = stop_opencode_daemon(
            project.daemon_pid,
            allocated_port=project.allocated_port,
            project_absolute_path=project.absolute_path,
        )
        runtime_status = daemon_runtime_status(project.daemon_pid, project.allocated_port)
        if not stopped and runtime_status.get("running"):
            self._mark_daemon_running_intent(project, allocated_port=project.allocated_port, daemon_pid=project.daemon_pid)
            project.save(
                update_fields=[
                    "allocated_port",
                    "daemon_pid",
                    "daemon_desired_state",
                    "daemon_stop_requested_at",
                    "daemon_last_heartbeat_at",
                    "updated_at",
                ],
            )
            broadcast_project_event(
                project.id,
                {
                    "kind": "daemon_stop_failed",
                    "project_id": project.id,
                    "daemon_pid": project.daemon_pid,
                    "allocated_port": project.allocated_port,
                    "requested_by": request.user.username,
                },
            )
            return Response({"detail": "Failed to stop daemon process."}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

        project.daemon_pid = None
        project.save(update_fields=["daemon_pid", "daemon_desired_state", "daemon_stop_requested_at", "updated_at"])
        cancelled_run_ids = self._cancel_active_runs_after_daemon_stop(project)
        broadcast_project_event(
            project.id,
            {
                "kind": "daemon_stopped",
                "project_id": project.id,
                "daemon_pid": None,
                "allocated_port": project.allocated_port,
                "cancelled_run_ids": cancelled_run_ids,
                "requested_by": request.user.username,
            },
        )
        return Response(
            {
                "project_id": project.id,
                "name": project.name,
                "daemon_desired_state": project.daemon_desired_state,
                "config": self._daemon_config_diagnostics(project),
                "message": "OpenCode daemon stopped.",
            },
            status=status.HTTP_200_OK,
        )

    @action(detail=True, methods=["post"], url_path="restart-daemon", throttle_classes=[ProjectMutationThrottle])
    def restart_daemon(self, request, pk=None):
        project = self.get_object()
        self._require_lock_owner_or_admin(request.user, project)
        serializer = StartDaemonSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        allocated_port = serializer.validated_data.get("allocated_port") or project.allocated_port or allocate_available_port()
        try:
            if project.daemon_pid or project.allocated_port:
                stopped = stop_opencode_daemon(
                    project.daemon_pid,
                    allocated_port=project.allocated_port,
                    project_absolute_path=project.absolute_path,
                )
                if not stopped and daemon_runtime_status(project.daemon_pid, project.allocated_port).get("running"):
                    release_project_lock(project, reason="daemon_restart_stop_failed")
                    broadcast_project_event(
                        project.id,
                        {
                            "kind": "daemon_stop_failed",
                            "project_id": project.id,
                            "daemon_pid": project.daemon_pid,
                            "allocated_port": project.allocated_port,
                            "requested_by": request.user.username,
                        },
                    )
                    return Response({"detail": "Failed to stop the existing daemon before restart."}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

            process = start_opencode_daemon(project.absolute_path, allocated_port)
        except (ProvisioningError, DaemonStartError) as error:
            fail_active_runs_for_project(project, message=f"Daemon restart failed: {error}", activity_kind="daemon.restart_failed")
            release_project_lock(project, reason="daemon_restart_failed")
            return Response({"detail": str(error)}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as error:  # noqa: BLE001
            fail_active_runs_for_project(project, message=f"Daemon restart failed: {error}", activity_kind="daemon.restart_failed")
            release_project_lock(project, reason="daemon_restart_failed")
            return Response({"detail": f"Unexpected daemon restart error: {error}"}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

        self._mark_daemon_running_intent(project, allocated_port=allocated_port, daemon_pid=process.pid)
        project.save(
            update_fields=[
                "allocated_port",
                "daemon_pid",
                "daemon_desired_state",
                "daemon_stop_requested_at",
                "daemon_last_heartbeat_at",
                "updated_at",
            ],
        )

        return Response(
            {
                "project_id": project.id,
                "name": project.name,
                "allocated_port": allocated_port,
                "daemon_pid": process.pid,
                "daemon_desired_state": project.daemon_desired_state,
                "config": self._daemon_config_diagnostics(project),
                "message": "OpenCode daemon restarted.",
            },
            status=status.HTTP_200_OK,
        )

    @action(detail=True, methods=["get"], url_path="daemon-status")
    def daemon_status(self, request, pk=None):
        project = self.get_object()
        runtime_status = daemon_runtime_status(project.daemon_pid, project.allocated_port)
        if runtime_status.get("reachable") or runtime_status.get("healthy"):
            project.daemon_last_heartbeat_at = timezone.now()
            project.save(update_fields=["daemon_last_heartbeat_at", "updated_at"])
        return Response(
            {
                "project_id": project.id,
                "daemon_pid": project.daemon_pid,
                "allocated_port": project.allocated_port,
                "daemon_desired_state": project.daemon_desired_state,
                "daemon_last_heartbeat_at": project.daemon_last_heartbeat_at,
                "daemon_stop_requested_at": project.daemon_stop_requested_at,
                "running": runtime_status.get("running", False),
                "pid_alive": runtime_status.get("pid_alive", False),
                "port_reachable": runtime_status.get("port_reachable", False),
                "health": {
                    "reachable": runtime_status.get("reachable", False),
                    "healthy": runtime_status.get("healthy", False),
                    "busy": runtime_status.get("busy", False),
                    "state": runtime_status.get("state", "unreachable"),
                    "version": runtime_status.get("version"),
                },
                "config": self._daemon_config_diagnostics(project),
            },
            status=status.HTTP_200_OK,
        )

    @action(detail=True, methods=["get"], url_path="tasks")
    def tasks(self, request, pk=None):
        project = self.get_object()
        serializer = TaskQueueSerializer(project.tasks.select_related("user", "run").all(), many=True)
        return Response(serializer.data)

    @action(detail=True, methods=["get"], url_path="audit")
    def audit(self, request, pk=None):
        project = self.get_object()
        serializer = AuditLogSerializer(project.audit_logs.select_related("user").all(), many=True)
        return Response(serializer.data)

    @action(detail=True, methods=["post"], url_path="execute", throttle_classes=[ExecutePromptThrottle])
    def execute(self, request, pk=None):
        project = self.get_object()
        serializer = ExecuteIdeaSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        try:
            project = self._ensure_runtime_ready(project, request.user)
            result = execute_or_queue_project_prompt(
                project=project,
                user=request.user,
                prompt=serializer.validated_data["prompt"],
            )
        except PermissionDenied as error:
            return Response({"detail": str(error)}, status=status.HTTP_403_FORBIDDEN)
        except (ProvisioningError, DaemonStartError, OrchestrationError) as error:
            return Response({"detail": str(error)}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as error:  # noqa: BLE001
            return Response(
                {"detail": f"Unexpected orchestration error: {error}"},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

        return Response(result, status=status.HTTP_200_OK)

    @action(detail=True, methods=["post"], url_path="prepare-workspace", throttle_classes=[ProjectMutationThrottle])
    def prepare_workspace(self, request, pk=None):
        project = self.get_object()
        try:
            prepared = self._ensure_runtime_ready(project, request.user)
        except PermissionDenied as error:
            return Response({"detail": str(error)}, status=status.HTTP_403_FORBIDDEN)
        except (ProvisioningError, DaemonStartError) as error:
            return Response({"detail": str(error)}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as error:  # noqa: BLE001
            return Response({"detail": f"Unable to prepare workspace runtime: {error}"}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

        return Response(
            {
                "detail": "Workspace runtime is ready.",
                "project": ProjectSerializer(prepared).data,
            },
            status=status.HTTP_200_OK,
        )

    @action(
        detail=True,
        methods=["post"],
        url_path="references/upload",
        throttle_classes=[ProjectMutationThrottle],
        parser_classes=[MultiPartParser, FormParser],
    )
    def upload_reference(self, request, pk=None):
        project = self.get_object()
        self._require_lock_owner_or_admin(request.user, project)
        upload = request.FILES.get("file")
        if upload is None:
            return Response({"detail": "Missing file in request payload."}, status=status.HTTP_400_BAD_REQUEST)

        if upload.size > MAX_REFERENCE_UPLOAD_BYTES:
            return Response(
                {"detail": f"File exceeds maximum upload size of {MAX_REFERENCE_UPLOAD_BYTES // (1024 * 1024)} MB."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        safe_filename = self._sanitize_reference_filename(getattr(upload, "name", "reference-upload"))
        extension = Path(safe_filename).suffix.lower()
        if extension not in ALLOWED_REFERENCE_EXTENSIONS:
            return Response(
                {"detail": f"Unsupported file type '{extension or 'unknown'}'."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            references_root = self._doc_references_root(project)
        except ProvisioningError as error:
            return Response({"detail": str(error)}, status=status.HTTP_400_BAD_REQUEST)

        timestamp = timezone.now().strftime("%Y%m%d-%H%M%S")
        destination_name = f"{timestamp}-{uuid.uuid4().hex[:8]}-{safe_filename}"
        destination = (references_root / destination_name).resolve()
        project_root = Path(project.absolute_path).expanduser().resolve()
        if project_root not in destination.parents:
            return Response({"detail": "Resolved upload path escapes project root."}, status=status.HTTP_400_BAD_REQUEST)

        with destination.open("wb") as destination_file:
            for chunk in upload.chunks():
                destination_file.write(chunk)

        relative_path = destination.relative_to(project_root).as_posix()
        return Response(
            {
                "filename": destination_name,
                "relative_path": relative_path,
                "absolute_path": str(destination),
                "size_bytes": upload.size,
                "content_type": getattr(upload, "content_type", "application/octet-stream"),
            },
            status=status.HTTP_201_CREATED,
        )

    @action(detail=True, methods=["get"], url_path="runs")
    def runs(self, request, pk=None):
        project = self.get_object()
        queryset = project.runs.select_related("user").prefetch_related("steps", "plan_steps")
        serializer = OrchestrationRunSerializer(queryset, many=True)
        return Response(serializer.data)

    @action(detail=True, methods=["get"], url_path=r"runs/(?P<run_id>[^/.]+)")
    def run_detail(self, request, pk=None, run_id=None):
        project = self.get_object()
        run = get_object_or_404(
            project.runs.select_related("user").prefetch_related("steps", "plan_steps", "activities", "artifacts"),
            pk=run_id,
        )
        serializer = OrchestrationRunSerializer(run)
        return Response(serializer.data)

    @action(detail=True, methods=["get"], url_path=r"runs/(?P<run_id>[^/.]+)/activities")
    def run_activities(self, request, pk=None, run_id=None):
        project = self.get_object()
        run = get_object_or_404(project.runs, pk=run_id)
        serializer = OrchestrationRunActivitySerializer(run.activities.select_related("step", "task"), many=True)
        return Response(serializer.data, status=status.HTTP_200_OK)

    @action(detail=True, methods=["get"], url_path=r"runs/(?P<run_id>[^/.]+)/artifacts")
    def run_artifacts(self, request, pk=None, run_id=None):
        project = self.get_object()
        run = get_object_or_404(project.runs, pk=run_id)
        serializer = OrchestrationArtifactSerializer(run.artifacts.select_related("step", "task"), many=True)
        return Response(serializer.data, status=status.HTTP_200_OK)

    @action(detail=True, methods=["get"], url_path=r"runs/(?P<run_id>[^/.]+)/plan")
    def run_plan(self, request, pk=None, run_id=None):
        project = self.get_object()
        run = get_object_or_404(project.runs, pk=run_id)
        serializer = OrchestrationPlanStepSerializer(run.plan_steps.order_by("sequence_order", "id"), many=True)
        return Response(
            {
                "run_id": run.id,
                "status": run.status,
                "approval_scope": run.approval_scope,
                "complexity_level": run.complexity_level,
                "plan_requires_approval": run.plan_requires_approval,
                "plan_approved_at": run.plan_approved_at,
                "steps": serializer.data,
            },
            status=status.HTTP_200_OK,
        )

    @action(detail=True, methods=["patch"], url_path=r"runs/(?P<run_id>[^/.]+)/plan/(?P<step_id>[^/.]+)", throttle_classes=[ProjectMutationThrottle])
    def run_plan_step_update(self, request, pk=None, run_id=None, step_id=None):
        project = self.get_object()
        run = get_object_or_404(project.runs, pk=run_id)
        self._require_plan_approver(request.user, project, run)
        if run.status != OrchestrationRun.Status.AWAITING_PLAN_APPROVAL:
            return Response({"detail": "Plan can only be edited while awaiting plan approval."}, status=status.HTTP_400_BAD_REQUEST)

        serializer = PlanStepUpdateSerializer(data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        plan_step = get_object_or_404(run.plan_steps.filter(status=OrchestrationPlanStep.Status.DRAFT), pk=step_id)
        update_fields = ["updated_at"]
        for field in ("assigned_agent", "instruction_payload", "sequence_order"):
            if field in serializer.validated_data:
                setattr(plan_step, field, serializer.validated_data[field])
                update_fields.append(field)
        plan_step.save(update_fields=update_fields)
        return Response(OrchestrationPlanStepSerializer(plan_step).data, status=status.HTTP_200_OK)

    @action(detail=True, methods=["post"], url_path=r"runs/(?P<run_id>[^/.]+)/approve-plan", throttle_classes=[ProjectMutationThrottle])
    def approve_plan(self, request, pk=None, run_id=None):
        project = self.get_object()
        run = get_object_or_404(project.runs, pk=run_id)
        self._require_plan_approver(request.user, project, run)
        serializer = PlanApprovalSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        if not serializer.validated_data.get("approved", True):
            return Response({"detail": "Use the replan endpoint to request plan changes instead of approve=false."}, status=status.HTTP_400_BAD_REQUEST)
        try:
            result = approve_plan_for_run(run, request.user)
        except OrchestrationError as error:
            return Response({"detail": str(error)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(result, status=status.HTTP_200_OK)

    @action(detail=True, methods=["post"], url_path=r"runs/(?P<run_id>[^/.]+)/replan", throttle_classes=[ProjectMutationThrottle])
    def replan_run(self, request, pk=None, run_id=None):
        project = self.get_object()
        run = get_object_or_404(project.runs, pk=run_id)
        self._require_plan_approver(request.user, project, run)
        serializer = PlanReplanSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            result = request_replan_for_run(run, request.user, serializer.validated_data.get("feedback", ""))
        except OrchestrationError as error:
            return Response({"detail": str(error)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(result, status=status.HTTP_200_OK)

    @action(detail=True, methods=["get"], url_path="sessions")
    def sessions(self, request, pk=None):
        project = self.get_object()
        try:
            client = self._build_opencode_client(project)
            payload = client.sessions(self._daemon_directory(project))
        except (OrchestrationError, OpenCodeClientError) as error:
            return Response({"detail": str(error)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(payload, status=status.HTTP_200_OK)

    @action(detail=True, methods=["get"], url_path=r"sessions/(?P<session_id>[^/.]+)")
    def session_detail(self, request, pk=None, session_id=None):
        project = self.get_object()
        try:
            client = self._build_opencode_client(project)
            payload = client.session(session_id, self._daemon_directory(project))
        except (OrchestrationError, OpenCodeClientError) as error:
            return Response({"detail": str(error)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(payload, status=status.HTTP_200_OK)

    @action(detail=True, methods=["get"], url_path=r"sessions/(?P<session_id>[^/.]+)/messages")
    def session_messages(self, request, pk=None, session_id=None):
        project = self.get_object()
        try:
            client = self._build_opencode_client(project)
            payload = client.session_messages(session_id, self._daemon_directory(project))
        except (OrchestrationError, OpenCodeClientError) as error:
            return Response({"detail": str(error)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(payload, status=status.HTTP_200_OK)

    @action(detail=True, methods=["get"], url_path=r"sessions/(?P<session_id>[^/.]+)/diff")
    def session_diff(self, request, pk=None, session_id=None):
        project = self.get_object()
        try:
            client = self._build_opencode_client(project)
            payload = client.session_diff(session_id, self._daemon_directory(project))
        except (OrchestrationError, OpenCodeClientError) as error:
            return Response({"detail": str(error)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(payload, status=status.HTTP_200_OK)

    @action(detail=True, methods=["get"], url_path="sessions-status")
    def sessions_status(self, request, pk=None):
        project = self.get_object()
        try:
            client = self._build_opencode_client(project)
            payload = client.session_status(self._daemon_directory(project))
        except (OrchestrationError, OpenCodeClientError) as error:
            return Response({"detail": str(error)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(payload, status=status.HTTP_200_OK)

    @action(detail=True, methods=["get"], url_path="sessions-active")
    def sessions_active(self, request, pk=None):
        project = self.get_object()
        try:
            client = self._build_opencode_client(project)
            daemon_directory = self._daemon_directory(project)
            sessions_payload = client.sessions(daemon_directory)
            status_payload = client.session_status(daemon_directory)
        except (OrchestrationError, OpenCodeClientError) as error:
            return Response({"detail": str(error)}, status=status.HTTP_400_BAD_REQUEST)

        active_session_ids = {
            session_id
            for session_id, session_state in (status_payload or {}).items()
            if isinstance(session_state, dict) and session_state.get("type") != "idle"
        }

        if isinstance(sessions_payload, list):
            active_sessions = [
                session_item
                for session_item in sessions_payload
                if isinstance(session_item, dict) and session_item.get("id") in active_session_ids
            ]
        elif isinstance(sessions_payload, dict):
            active_sessions = {
                session_id: session_item
                for session_id, session_item in sessions_payload.items()
                if session_id in active_session_ids
            }
        else:
            active_sessions = []

        return Response(
            {
                "sessions": active_sessions,
                "status": status_payload,
            },
            status=status.HTTP_200_OK,
        )

    @action(detail=True, methods=["get"], url_path="sync-jobs")
    def sync_jobs(self, request, pk=None):
        project = self.get_object()
        queryset = project.git_sync_jobs.select_related("run", "user")
        serializer = GitSyncJobSerializer(queryset, many=True)
        return Response(serializer.data)

    @action(detail=True, methods=["post"], url_path=r"sync-jobs/(?P<job_id>[^/.]+)/retry", throttle_classes=[ProjectMutationThrottle])
    def retry_sync(self, request, pk=None, job_id=None):
        project = self.get_object()
        self._require_lock_owner_or_admin(request.user, project)
        job = get_object_or_404(project.git_sync_jobs, pk=job_id)
        retried = retry_sync_job(job)
        return Response(GitSyncJobSerializer(retried).data, status=status.HTTP_200_OK)

    @action(detail=True, methods=["get"], url_path="usage-summary")
    def usage_summary(self, request, pk=None):
        project = self.get_object()
        runs = project.runs.all()
        usage_events = TokenUsageEvent.objects.filter(run__project=project)
        return Response(
            {
                "project_id": project.id,
                "run_count": runs.count(),
                "tracked_run_count": runs.filter(total_tokens__gt=0).count(),
                "prompt_tokens": sum(run.prompt_tokens for run in runs),
                "completion_tokens": sum(run.completion_tokens for run in runs),
                "total_tokens": sum(run.total_tokens for run in runs),
                "usage_event_count": usage_events.count(),
            },
            status=status.HTTP_200_OK,
        )

    @action(detail=True, methods=["get"], url_path=r"runs/(?P<run_id>[^/.]+)/usage-events")
    def run_usage_events(self, request, pk=None, run_id=None):
        project = self.get_object()
        run = get_object_or_404(project.runs, pk=run_id)
        serializer = TokenUsageEventSerializer(run.usage_events.all(), many=True)
        return Response(serializer.data, status=status.HTTP_200_OK)

    @action(detail=False, methods=["get"], url_path="notifications")
    def notifications(self, request):
        queryset = UserNotification.objects.filter(user=request.user).select_related("project", "run")
        serializer = UserNotificationSerializer(queryset[:100], many=True)
        unread_count = queryset.filter(is_read=False).count()
        return Response({"unread_count": unread_count, "items": serializer.data}, status=status.HTTP_200_OK)

    @action(detail=False, methods=["post"], url_path=r"notifications/(?P<notification_id>[^/.]+)/mark-read", throttle_classes=[ProjectMutationThrottle])
    def notification_mark_read(self, request, notification_id=None):
        notification = get_object_or_404(UserNotification, pk=notification_id, user=request.user)
        if not notification.is_read:
            notification.is_read = True
            notification.read_at = timezone.now()
            notification.save(update_fields=["is_read", "read_at", "updated_at"])
        return Response(UserNotificationSerializer(notification).data, status=status.HTTP_200_OK)

    @action(detail=False, methods=["post"], url_path="notifications/mark-all-read", throttle_classes=[ProjectMutationThrottle])
    def notifications_mark_all_read(self, request):
        updated = UserNotification.objects.filter(user=request.user, is_read=False).update(is_read=True, read_at=timezone.now(), updated_at=timezone.now())
        return Response({"updated": updated}, status=status.HTTP_200_OK)

    @action(detail=False, methods=["get"], url_path="operations")
    def operations(self, request):
        projects = self.get_queryset().prefetch_related("targets")
        runs = OrchestrationRun.objects.select_related("project", "user").filter(project__in=projects)
        sync_jobs = GitSyncJob.objects.select_related("project", "run", "user").filter(project__in=projects)
        notifications = UserNotification.objects.filter(user=request.user).select_related("project", "run")

        usage_totals = runs.aggregate(
            prompt_tokens=Sum("prompt_tokens"),
            completion_tokens=Sum("completion_tokens"),
            total_tokens=Sum("total_tokens"),
        )

        return Response(
            {
                "summary": {
                    "project_count": projects.count(),
                    "locked_project_count": projects.filter(is_locked=True).count(),
                    "active_daemon_count": projects.filter(daemon_desired_state=Project.DaemonDesiredState.RUNNING).count(),
                    "run_count": runs.count(),
                    "active_run_count": runs.filter(
                        status__in=[
                            OrchestrationRun.Status.PENDING_APPROVAL,
                            OrchestrationRun.Status.QUEUED,
                            OrchestrationRun.Status.PLANNING,
                            OrchestrationRun.Status.BREAKING_DOWN,
                            OrchestrationRun.Status.PLAN_READY,
                            OrchestrationRun.Status.AWAITING_PLAN_APPROVAL,
                            OrchestrationRun.Status.RUNNING,
                            OrchestrationRun.Status.VERIFYING,
                        ],
                    ).count(),
                    "failed_run_count": runs.filter(status=OrchestrationRun.Status.FAILED).count(),
                    "sync_job_count": sync_jobs.count(),
                    "failed_sync_job_count": sync_jobs.filter(status=GitSyncJob.Status.FAILED).count(),
                    "unread_notification_count": notifications.filter(is_read=False).count(),
                    "prompt_tokens": usage_totals["prompt_tokens"] or 0,
                    "completion_tokens": usage_totals["completion_tokens"] or 0,
                    "total_tokens": usage_totals["total_tokens"] or 0,
                    "usage_event_count": TokenUsageEvent.objects.filter(run__project__in=projects).count(),
                },
                "run_status_counts": dict(runs.values_list("status").annotate(count=Count("id"))),
                "sync_status_counts": dict(sync_jobs.values_list("status").annotate(count=Count("id"))),
                "projects": ProjectSerializer(projects[:50], many=True).data,
                "recent_runs": OrchestrationRunSerializer(runs[:25], many=True).data,
                "recent_sync_jobs": GitSyncJobSerializer(sync_jobs[:25], many=True).data,
                "recent_notifications": UserNotificationSerializer(notifications[:25], many=True).data,
            },
            status=status.HTTP_200_OK,
        )

    @action(detail=True, methods=["get"], url_path=r"sessions/(?P<session_id>[^/.]+)/todos")
    def session_todos(self, request, pk=None, session_id=None):
        project = self.get_object()
        try:
            client = self._build_opencode_client(project)
            payload = client.session_todos(session_id, self._daemon_directory(project))
        except (OrchestrationError, OpenCodeClientError) as error:
            return Response({"detail": str(error)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(payload, status=status.HTTP_200_OK)

    @action(detail=True, methods=["post"], url_path=r"sessions/(?P<session_id>[^/.]+)/interrupt", throttle_classes=[ProjectMutationThrottle])
    def session_interrupt(self, request, pk=None, session_id=None):
        project = self.get_object()
        self._require_lock_owner_or_admin(request.user, project)
        try:
            client = self._build_opencode_client(project)
            payload = client.interrupt_session(session_id, self._daemon_directory(project))
        except (OrchestrationError, OpenCodeClientError) as error:
            return Response({"detail": str(error)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(payload, status=status.HTTP_200_OK)

    @action(detail=True, methods=["post"], url_path=r"sessions/(?P<session_id>[^/.]+)/fork", throttle_classes=[ProjectMutationThrottle])
    def session_fork(self, request, pk=None, session_id=None):
        project = self.get_object()
        self._require_lock_owner_or_admin(request.user, project)
        serializer = SessionForkSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            client = self._build_opencode_client(project)
            payload = client.fork_session(
                session_id,
                self._daemon_directory(project),
                title=serializer.validated_data.get("title", ""),
                agent=serializer.validated_data.get("agent", ""),
            )
        except (OrchestrationError, OpenCodeClientError) as error:
            return Response({"detail": str(error)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(payload, status=status.HTTP_200_OK)

    @action(detail=True, methods=["post"], url_path=r"sessions/(?P<session_id>[^/.]+)/summarize", throttle_classes=[ProjectMutationThrottle])
    def session_summarize(self, request, pk=None, session_id=None):
        project = self.get_object()
        self._require_lock_owner_or_admin(request.user, project)
        serializer = SessionSummarizeSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            client = self._build_opencode_client(project)
            payload = client.summarize_session(
                session_id,
                self._daemon_directory(project),
                serializer.validated_data.get("prompt", ""),
            )
        except (OrchestrationError, OpenCodeClientError) as error:
            return Response({"detail": str(error)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(payload, status=status.HTTP_200_OK)

    @action(detail=False, methods=["get"], url_path="approval-inbox")
    def approval_inbox(self, request):
        pending = TaskQueue.objects.filter(status=TaskQueue.Status.PENDING_APPROVAL).select_related("project", "user")
        if not (request.user.is_staff or request.user.is_superuser):
            pending = pending.filter(Q(project__owner=request.user) | Q(project__locked_by=request.user))
        serializer = PendingApprovalTaskSerializer(pending.order_by("created_at"), many=True)
        return Response(serializer.data)

    @action(detail=True, methods=["post"], url_path="approve-task", throttle_classes=[ProjectMutationThrottle])
    def approve_task(self, request, pk=None):
        project = self.get_object()
        self._require_lock_owner_or_admin(request.user, project)
        serializer = PendingApprovalDecisionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        task = get_object_or_404(project.tasks, pk=serializer.validated_data["task_id"])
        try:
            result = approve_pending_task(project=project, task=task, approver=request.user)
        except OrchestrationError as error:
            return Response({"detail": str(error)}, status=status.HTTP_400_BAD_REQUEST)

        return Response(result, status=status.HTTP_200_OK)

    @action(detail=True, methods=["post"], url_path="reject-task", throttle_classes=[ProjectMutationThrottle])
    def reject_task(self, request, pk=None):
        project = self.get_object()
        self._require_lock_owner_or_admin(request.user, project)
        serializer = PendingApprovalDecisionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        task = get_object_or_404(project.tasks, pk=serializer.validated_data["task_id"])
        try:
            result = reject_pending_task(
                project=project,
                task=task,
                approver=request.user,
                reason=serializer.validated_data.get("reason", ""),
            )
        except OrchestrationError as error:
            return Response({"detail": str(error)}, status=status.HTTP_400_BAD_REQUEST)

        return Response(result, status=status.HTTP_200_OK)
