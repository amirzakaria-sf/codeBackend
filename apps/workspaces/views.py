import re
import uuid
from pathlib import Path

from django.db import transaction
from django.shortcuts import get_object_or_404
from rest_framework import permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.response import Response

from opencode_client import OpenCodeClient, OpenCodeClientError

from django.db.models import Count, Sum
from django.utils import timezone

from .models import GitSyncJob, OrchestrationRun, Project, TaskQueue, TokenUsageEvent, UserNotification, WorkspaceTarget
from .serializers import (
    AuditLogSerializer,
    ExecuteIdeaSerializer,
    GitSyncJobSerializer,
    LockAcquireSerializer,
    OrchestrationRunSerializer,
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
    daemon_health,
    is_daemon_running,
    start_opencode_daemon,
    stop_opencode_daemon,
)
from .services.orchestration import (
    OrchestrationError,
    approve_pending_task,
    execute_or_queue_project_prompt,
    reject_pending_task,
)
from .services.github_sync import retry_sync_job
from .services.provisioning import (
    ProvisionWorkspaceRequest,
    ProvisioningError,
    WorkspaceTargetSpec,
    provision_project_structure,
)
from .services.realtime import broadcast_project_event
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


class ProjectViewSet(viewsets.ModelViewSet):
    queryset = Project.objects.all().select_related("locked_by").prefetch_related("targets")
    serializer_class = ProjectSerializer
    permission_classes = [permissions.IsAuthenticated]

    def _require_lock_owner_or_admin(self, user, project: Project):
        if user.is_staff or user.is_superuser:
            return
        if project.locked_by_id == user.id:
            return
        raise PermissionDenied("Only the project lock owner or an admin can perform this action.")

    @staticmethod
    def _build_opencode_client(project: Project) -> OpenCodeClient:
        if not project.allocated_port:
            raise OrchestrationError("Project does not have an allocated OpenCode port.")
        return OpenCodeClient(project.allocated_port)

    def _ensure_runtime_ready(self, project: Project, user) -> Project:
        if project.is_locked and project.locked_by_id and project.locked_by_id != user.id and not (user.is_staff or user.is_superuser):
            raise PermissionDenied("Workspace is locked by another user.")

        fields_to_update: list[str] = []
        if not project.is_locked or project.locked_by_id != user.id:
            project.is_locked = True
            project.locked_by = user
            fields_to_update.extend(["is_locked", "locked_by", "updated_at"])

        if not project.allocated_port:
            project.allocated_port = allocate_available_port()
            fields_to_update.extend(["allocated_port", "updated_at"])

        port = int(project.allocated_port)
        daemon_running = is_daemon_running(project.daemon_pid)
        health = daemon_health(port) if daemon_running else {"reachable": False, "healthy": False}

        if not daemon_running or not health.get("reachable") or not health.get("healthy"):
            if project.daemon_pid:
                stop_opencode_daemon(project.daemon_pid)
            process = start_opencode_daemon(project.name, port)
            project.daemon_pid = process.pid
            fields_to_update.extend(["daemon_pid", "updated_at"])

        if fields_to_update:
            unique_fields = []
            for field in fields_to_update:
                if field not in unique_fields:
                    unique_fields.append(field)
            project.save(update_fields=unique_fields)
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
        project = get_object_or_404(Project, pk=pk)
        serializer = LockAcquireSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        force = serializer.validated_data["force"]

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

        project.is_locked = True
        project.locked_by = request.user
        project.save(update_fields=["is_locked", "locked_by", "updated_at"])
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
        project = get_object_or_404(Project, pk=pk)

        if not project.is_locked:
            return Response(
                {
                    "detail": "Project is already unlocked.",
                    "project": ProjectSerializer(project).data,
                },
                status=status.HTTP_200_OK,
            )

        self._require_lock_owner_or_admin(request.user, project)
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
        project = get_object_or_404(Project, pk=pk)
        self._require_lock_owner_or_admin(request.user, project)
        serializer = StartDaemonSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        allocated_port = serializer.validated_data.get("allocated_port") or allocate_available_port()

        if project.daemon_pid and is_daemon_running(project.daemon_pid):
            return Response(
                {
                    "detail": "Daemon is already running for this project.",
                    "daemon_pid": project.daemon_pid,
                    "allocated_port": project.allocated_port,
                },
                status=status.HTTP_409_CONFLICT,
            )

        try:
            process = start_opencode_daemon(project.name, allocated_port)
        except (ProvisioningError, DaemonStartError) as error:
            return Response({"detail": str(error)}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as error:  # noqa: BLE001
            return Response(
                {"detail": f"Unexpected daemon error: {error}"},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

        project.allocated_port = allocated_port
        project.daemon_pid = process.pid
        project.save(update_fields=["allocated_port", "daemon_pid", "updated_at"])

        return Response(
            {
                "project_id": project.id,
                "name": project.name,
                "allocated_port": allocated_port,
                "daemon_pid": process.pid,
                "message": "OpenCode daemon started.",
            },
            status=status.HTTP_200_OK,
        )

    @action(detail=True, methods=["post"], url_path="stop-daemon", throttle_classes=[ProjectMutationThrottle])
    def stop_daemon(self, request, pk=None):
        project = get_object_or_404(Project, pk=pk)
        self._require_lock_owner_or_admin(request.user, project)
        if not project.daemon_pid:
            return Response({"detail": "No daemon PID recorded for this project."}, status=status.HTTP_400_BAD_REQUEST)

        stopped = stop_opencode_daemon(project.daemon_pid)
        if not stopped:
            return Response({"detail": "Failed to stop daemon process."}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

        project.daemon_pid = None
        project.save(update_fields=["daemon_pid", "updated_at"])
        return Response(
            {
                "project_id": project.id,
                "name": project.name,
                "message": "OpenCode daemon stopped.",
            },
            status=status.HTTP_200_OK,
        )

    @action(detail=True, methods=["post"], url_path="restart-daemon", throttle_classes=[ProjectMutationThrottle])
    def restart_daemon(self, request, pk=None):
        project = get_object_or_404(Project, pk=pk)
        self._require_lock_owner_or_admin(request.user, project)
        serializer = StartDaemonSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        if project.daemon_pid:
            stop_opencode_daemon(project.daemon_pid)

        allocated_port = serializer.validated_data.get("allocated_port") or project.allocated_port or allocate_available_port()
        process = start_opencode_daemon(project.name, allocated_port)

        project.allocated_port = allocated_port
        project.daemon_pid = process.pid
        project.save(update_fields=["allocated_port", "daemon_pid", "updated_at"])

        return Response(
            {
                "project_id": project.id,
                "name": project.name,
                "allocated_port": allocated_port,
                "daemon_pid": process.pid,
                "message": "OpenCode daemon restarted.",
            },
            status=status.HTTP_200_OK,
        )

    @action(detail=True, methods=["get"], url_path="daemon-status")
    def daemon_status(self, request, pk=None):
        project = get_object_or_404(Project, pk=pk)
        running = is_daemon_running(project.daemon_pid)
        health = daemon_health(project.allocated_port if running else None)
        return Response(
            {
                "project_id": project.id,
                "daemon_pid": project.daemon_pid,
                "allocated_port": project.allocated_port,
                "running": running,
                "health": health,
            },
            status=status.HTTP_200_OK,
        )

    @action(detail=True, methods=["get"], url_path="tasks")
    def tasks(self, request, pk=None):
        project = get_object_or_404(Project, pk=pk)
        serializer = TaskQueueSerializer(project.tasks.select_related("user", "run").all(), many=True)
        return Response(serializer.data)

    @action(detail=True, methods=["get"], url_path="audit")
    def audit(self, request, pk=None):
        project = get_object_or_404(Project, pk=pk)
        serializer = AuditLogSerializer(project.audit_logs.select_related("user").all(), many=True)
        return Response(serializer.data)

    @action(detail=True, methods=["post"], url_path="execute", throttle_classes=[ExecutePromptThrottle])
    def execute(self, request, pk=None):
        project = get_object_or_404(Project, pk=pk)
        serializer = ExecuteIdeaSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        try:
            project = self._ensure_runtime_ready(project, request.user)
            result = execute_or_queue_project_prompt(
                project=project,
                user=request.user,
                prompt=serializer.validated_data["prompt"],
            )
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
        project = get_object_or_404(Project, pk=pk)
        try:
            prepared = self._ensure_runtime_ready(project, request.user)
        except (ProvisioningError, DaemonStartError, PermissionDenied) as error:
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
        project = get_object_or_404(Project, pk=pk)
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
        project = get_object_or_404(Project, pk=pk)
        queryset = project.runs.select_related("user").prefetch_related("steps")
        serializer = OrchestrationRunSerializer(queryset, many=True)
        return Response(serializer.data)

    @action(detail=True, methods=["get"], url_path=r"runs/(?P<run_id>[^/.]+)")
    def run_detail(self, request, pk=None, run_id=None):
        project = get_object_or_404(Project, pk=pk)
        run = get_object_or_404(project.runs.select_related("user").prefetch_related("steps"), pk=run_id)
        serializer = OrchestrationRunSerializer(run)
        return Response(serializer.data)

    @action(detail=True, methods=["get"], url_path="sessions")
    def sessions(self, request, pk=None):
        project = get_object_or_404(Project, pk=pk)
        try:
            client = self._build_opencode_client(project)
            payload = client.sessions(project.absolute_path)
        except (OrchestrationError, OpenCodeClientError) as error:
            return Response({"detail": str(error)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(payload, status=status.HTTP_200_OK)

    @action(detail=True, methods=["get"], url_path=r"sessions/(?P<session_id>[^/.]+)")
    def session_detail(self, request, pk=None, session_id=None):
        project = get_object_or_404(Project, pk=pk)
        try:
            client = self._build_opencode_client(project)
            payload = client.session(session_id, project.absolute_path)
        except (OrchestrationError, OpenCodeClientError) as error:
            return Response({"detail": str(error)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(payload, status=status.HTTP_200_OK)

    @action(detail=True, methods=["get"], url_path=r"sessions/(?P<session_id>[^/.]+)/messages")
    def session_messages(self, request, pk=None, session_id=None):
        project = get_object_or_404(Project, pk=pk)
        try:
            client = self._build_opencode_client(project)
            payload = client.session_messages(session_id, project.absolute_path)
        except (OrchestrationError, OpenCodeClientError) as error:
            return Response({"detail": str(error)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(payload, status=status.HTTP_200_OK)

    @action(detail=True, methods=["get"], url_path=r"sessions/(?P<session_id>[^/.]+)/diff")
    def session_diff(self, request, pk=None, session_id=None):
        project = get_object_or_404(Project, pk=pk)
        try:
            client = self._build_opencode_client(project)
            payload = client.session_diff(session_id, project.absolute_path)
        except (OrchestrationError, OpenCodeClientError) as error:
            return Response({"detail": str(error)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(payload, status=status.HTTP_200_OK)

    @action(detail=True, methods=["get"], url_path="sessions-status")
    def sessions_status(self, request, pk=None):
        project = get_object_or_404(Project, pk=pk)
        try:
            client = self._build_opencode_client(project)
            payload = client.session_status(project.absolute_path)
        except (OrchestrationError, OpenCodeClientError) as error:
            return Response({"detail": str(error)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(payload, status=status.HTTP_200_OK)

    @action(detail=True, methods=["get"], url_path="sessions-active")
    def sessions_active(self, request, pk=None):
        project = get_object_or_404(Project, pk=pk)
        try:
            client = self._build_opencode_client(project)
            sessions_payload = client.sessions(project.absolute_path)
            status_payload = client.session_status(project.absolute_path)
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
        project = get_object_or_404(Project, pk=pk)
        queryset = project.git_sync_jobs.select_related("run", "user")
        serializer = GitSyncJobSerializer(queryset, many=True)
        return Response(serializer.data)

    @action(detail=True, methods=["post"], url_path=r"sync-jobs/(?P<job_id>[^/.]+)/retry", throttle_classes=[ProjectMutationThrottle])
    def retry_sync(self, request, pk=None, job_id=None):
        project = get_object_or_404(Project, pk=pk)
        self._require_lock_owner_or_admin(request.user, project)
        job = get_object_or_404(project.git_sync_jobs, pk=job_id)
        retried = retry_sync_job(job)
        return Response(GitSyncJobSerializer(retried).data, status=status.HTTP_200_OK)

    @action(detail=True, methods=["get"], url_path="usage-summary")
    def usage_summary(self, request, pk=None):
        project = get_object_or_404(Project, pk=pk)
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
        project = get_object_or_404(Project, pk=pk)
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
        projects = Project.objects.prefetch_related("targets")
        runs = OrchestrationRun.objects.select_related("project", "user")
        sync_jobs = GitSyncJob.objects.select_related("project", "run", "user")
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
                    "active_daemon_count": projects.exclude(daemon_pid__isnull=True).count(),
                    "run_count": runs.count(),
                    "active_run_count": runs.filter(
                        status__in=[
                            OrchestrationRun.Status.PENDING_APPROVAL,
                            OrchestrationRun.Status.QUEUED,
                            OrchestrationRun.Status.PLANNING,
                            OrchestrationRun.Status.BREAKING_DOWN,
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
                    "usage_event_count": TokenUsageEvent.objects.count(),
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
        project = get_object_or_404(Project, pk=pk)
        try:
            client = self._build_opencode_client(project)
            payload = client.session_todos(session_id, project.absolute_path)
        except (OrchestrationError, OpenCodeClientError) as error:
            return Response({"detail": str(error)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(payload, status=status.HTTP_200_OK)

    @action(detail=True, methods=["post"], url_path=r"sessions/(?P<session_id>[^/.]+)/interrupt", throttle_classes=[ProjectMutationThrottle])
    def session_interrupt(self, request, pk=None, session_id=None):
        project = get_object_or_404(Project, pk=pk)
        self._require_lock_owner_or_admin(request.user, project)
        try:
            client = self._build_opencode_client(project)
            payload = client.interrupt_session(session_id, project.absolute_path)
        except (OrchestrationError, OpenCodeClientError) as error:
            return Response({"detail": str(error)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(payload, status=status.HTTP_200_OK)

    @action(detail=True, methods=["post"], url_path=r"sessions/(?P<session_id>[^/.]+)/fork", throttle_classes=[ProjectMutationThrottle])
    def session_fork(self, request, pk=None, session_id=None):
        project = get_object_or_404(Project, pk=pk)
        self._require_lock_owner_or_admin(request.user, project)
        serializer = SessionForkSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            client = self._build_opencode_client(project)
            payload = client.fork_session(
                session_id,
                project.absolute_path,
                title=serializer.validated_data.get("title", ""),
                agent=serializer.validated_data.get("agent", ""),
            )
        except (OrchestrationError, OpenCodeClientError) as error:
            return Response({"detail": str(error)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(payload, status=status.HTTP_200_OK)

    @action(detail=True, methods=["post"], url_path=r"sessions/(?P<session_id>[^/.]+)/summarize", throttle_classes=[ProjectMutationThrottle])
    def session_summarize(self, request, pk=None, session_id=None):
        project = get_object_or_404(Project, pk=pk)
        self._require_lock_owner_or_admin(request.user, project)
        serializer = SessionSummarizeSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            client = self._build_opencode_client(project)
            payload = client.summarize_session(
                session_id,
                project.absolute_path,
                serializer.validated_data.get("prompt", ""),
            )
        except (OrchestrationError, OpenCodeClientError) as error:
            return Response({"detail": str(error)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(payload, status=status.HTTP_200_OK)

    @action(detail=False, methods=["get"], url_path="approval-inbox")
    def approval_inbox(self, request):
        pending = TaskQueue.objects.filter(status=TaskQueue.Status.PENDING_APPROVAL).select_related("project", "user")
        if not (request.user.is_staff or request.user.is_superuser):
            pending = pending.filter(project__locked_by=request.user)
        serializer = PendingApprovalTaskSerializer(pending.order_by("created_at"), many=True)
        return Response(serializer.data)

    @action(detail=True, methods=["post"], url_path="approve-task", throttle_classes=[ProjectMutationThrottle])
    def approve_task(self, request, pk=None):
        project = get_object_or_404(Project, pk=pk)
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
        project = get_object_or_404(Project, pk=pk)
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
