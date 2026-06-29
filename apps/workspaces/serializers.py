from django.conf import settings
from rest_framework import serializers

from .models import (
    AuditLog,
    GitSyncJob,
    OrchestrationArtifact,
    OrchestrationPlanStep,
    OrchestrationRun,
    OrchestrationRunActivity,
    OrchestrationStep,
    Project,
    TaskQueue,
    TokenUsageEvent,
    UserNotification,
    WorkspaceTarget,
)
from .services.provisioning import host_path_from_runtime_path


class WorkspaceTargetSerializer(serializers.ModelSerializer):
    class Meta:
        model = WorkspaceTarget
        fields = (
            "id",
            "name",
            "relative_path",
            "absolute_path",
            "role",
            "source_type",
            "remote_url",
            "default_branch",
            "is_primary",
            "is_editable",
            "created_at",
            "updated_at",
        )
        read_only_fields = fields


class ProjectSerializer(serializers.ModelSerializer):
    owner_username = serializers.CharField(source="owner.username", read_only=True, default=None)
    locked_by_username = serializers.CharField(source="locked_by.username", read_only=True, default=None)
    runtime_path = serializers.CharField(source="absolute_path", read_only=True)
    host_path = serializers.SerializerMethodField()
    storage_mode = serializers.SerializerMethodField()
    targets = WorkspaceTargetSerializer(many=True, read_only=True)

    def get_host_path(self, obj: Project) -> str | None:
        try:
            return host_path_from_runtime_path(obj.absolute_path) or None
        except Exception:  # noqa: BLE001
            return None

    def get_storage_mode(self, obj: Project) -> str:
        configured = (getattr(settings, "MANAGED_PROJECTS_STORAGE_MODE", "") or "").strip()
        return configured or "unknown"

    class Meta:
        model = Project
        fields = (
            "id",
            "owner",
            "owner_username",
            "path_owner_username",
            "name",
            "absolute_path",
            "runtime_path",
            "host_path",
            "storage_mode",
            "workspace_mode",
            "starter_template",
            "setup_status",
            "bootstrap_enabled",
            "allocated_port",
            "is_locked",
            "locked_by",
            "locked_by_username",
            "daemon_pid",
            "created_at",
            "updated_at",
            "targets",
        )
        read_only_fields = (
            "id",
            "owner",
            "owner_username",
            "path_owner_username",
            "name",
            "absolute_path",
            "runtime_path",
            "host_path",
            "storage_mode",
            "workspace_mode",
            "starter_template",
            "setup_status",
            "bootstrap_enabled",
            "allocated_port",
            "is_locked",
            "locked_by",
            "locked_by_username",
            "daemon_pid",
            "created_at",
            "updated_at",
            "targets",
        )


class WorkspaceTargetInputSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=120)
    role = serializers.ChoiceField(choices=WorkspaceTarget.Role.choices)
    source_type = serializers.ChoiceField(choices=WorkspaceTarget.SourceType.choices)
    relative_path = serializers.CharField(max_length=1024, required=False, allow_blank=True)
    remote_url = serializers.CharField(max_length=2048, required=False, allow_blank=True)
    default_branch = serializers.CharField(max_length=255, required=False, allow_blank=True)
    is_primary = serializers.BooleanField(required=False, default=False)
    is_editable = serializers.BooleanField(required=False, default=True)

    def validate(self, attrs):
        source_type = attrs["source_type"]
        remote_url = (attrs.get("remote_url") or "").strip()
        if source_type == WorkspaceTarget.SourceType.GIT_CLONE and not remote_url:
            raise serializers.ValidationError({"remote_url": "A remote URL is required for git-clone targets."})
        if source_type != WorkspaceTarget.SourceType.GIT_CLONE:
            attrs["remote_url"] = ""
            attrs["default_branch"] = (attrs.get("default_branch") or "").strip()
        return attrs


class ProvisionProjectSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=120)
    workspace_mode = serializers.ChoiceField(
        choices=Project.WorkspaceMode.choices,
        required=False,
        default=Project.WorkspaceMode.STARTER,
    )
    starter_template = serializers.ChoiceField(
        choices=Project.StarterTemplate.choices,
        required=False,
        default=Project.StarterTemplate.FULLSTACK,
    )
    bootstrap_enabled = serializers.BooleanField(required=False, default=False)
    clone_remote_url = serializers.CharField(max_length=2048, required=False, allow_blank=True)
    clone_branch = serializers.CharField(max_length=255, required=False, allow_blank=True)
    clone_target_name = serializers.CharField(max_length=120, required=False, allow_blank=True)
    clone_target_role = serializers.ChoiceField(
        choices=WorkspaceTarget.Role.choices,
        required=False,
        default=WorkspaceTarget.Role.CUSTOM,
    )
    targets = WorkspaceTargetInputSerializer(many=True, required=False)

    def validate(self, attrs):
        workspace_mode = attrs["workspace_mode"]
        clone_remote_url = (attrs.get("clone_remote_url") or "").strip()
        targets = attrs.get("targets") or []

        if workspace_mode in {Project.WorkspaceMode.ACTIVE_CLONE, Project.WorkspaceMode.REFERENCE_CLONE} and not clone_remote_url:
            raise serializers.ValidationError({"clone_remote_url": "A remote URL is required for clone-based workspaces."})

        if workspace_mode == Project.WorkspaceMode.CUSTOM and not targets:
            raise serializers.ValidationError({"targets": "At least one target is required for custom workspaces."})

        if workspace_mode != Project.WorkspaceMode.CUSTOM:
            attrs["targets"] = []

        if workspace_mode == Project.WorkspaceMode.STARTER:
            attrs["clone_remote_url"] = ""
            attrs["clone_branch"] = ""
            attrs["clone_target_name"] = ""

        if workspace_mode in {Project.WorkspaceMode.ACTIVE_CLONE, Project.WorkspaceMode.REFERENCE_CLONE}:
            attrs["starter_template"] = ""

        primary_targets = [target for target in targets if target.get("is_primary")]
        if len(primary_targets) > 1:
            raise serializers.ValidationError({"targets": "Only one target can be marked as primary."})

        return attrs


class StartDaemonSerializer(serializers.Serializer):
    allocated_port = serializers.IntegerField(min_value=1, max_value=65535, required=False)


class ExecuteIdeaSerializer(serializers.Serializer):
    prompt = serializers.CharField()


class PendingApprovalDecisionSerializer(serializers.Serializer):
    task_id = serializers.IntegerField(min_value=1)
    reason = serializers.CharField(required=False, allow_blank=True)


class SessionForkSerializer(serializers.Serializer):
    title = serializers.CharField(required=False, allow_blank=True, max_length=255)
    agent = serializers.CharField(required=False, allow_blank=True, max_length=120)


class SessionSummarizeSerializer(serializers.Serializer):
    prompt = serializers.CharField(required=False, allow_blank=True)


class LockAcquireSerializer(serializers.Serializer):
    force = serializers.BooleanField(required=False, default=False)


class PlanStepUpdateSerializer(serializers.Serializer):
    assigned_agent = serializers.CharField(required=False, allow_blank=False, max_length=120)
    instruction_payload = serializers.CharField(required=False, allow_blank=False)
    sequence_order = serializers.IntegerField(required=False, min_value=1)


class PlanApprovalSerializer(serializers.Serializer):
    approved = serializers.BooleanField(required=False, default=True)


class PlanReplanSerializer(serializers.Serializer):
    feedback = serializers.CharField(required=False, allow_blank=True)


class PendingApprovalTaskSerializer(serializers.ModelSerializer):
    project_name = serializers.CharField(source="project.name", read_only=True)
    requested_by = serializers.CharField(source="user.username", read_only=True, default="unknown")

    class Meta:
        model = TaskQueue
        fields = (
            "id",
            "project",
            "project_name",
            "requested_by",
            "run",
            "instruction_payload",
            "sequence_order",
            "approval_scope",
            "status",
            "created_at",
        )
        read_only_fields = fields


class TaskQueueSerializer(serializers.ModelSerializer):
    class Meta:
        model = TaskQueue
        fields = (
            "id",
            "project",
            "run",
            "user",
            "assigned_agent",
            "instruction_payload",
            "sequence_order",
            "approval_scope",
            "status",
            "supervisor_feedback",
            "created_at",
            "updated_at",
        )
        read_only_fields = fields


class OrchestrationStepSerializer(serializers.ModelSerializer):
    class Meta:
        model = OrchestrationStep
        fields = (
            "id",
            "run",
            "task",
            "sequence_order",
            "assigned_agent",
            "instruction_payload",
            "status",
            "attempt_count",
            "worker_session_id",
            "generated_diff",
            "supervisor_feedback",
            "created_at",
            "updated_at",
        )
        read_only_fields = fields


class OrchestrationPlanStepSerializer(serializers.ModelSerializer):
    class Meta:
        model = OrchestrationPlanStep
        fields = (
            "id",
            "run",
            "sequence_order",
            "assigned_agent",
            "instruction_payload",
            "status",
            "planner_notes",
            "created_at",
            "updated_at",
        )
        read_only_fields = fields


class OrchestrationRunActivitySerializer(serializers.ModelSerializer):
    class Meta:
        model = OrchestrationRunActivity
        fields = (
            "id",
            "run",
            "step",
            "task",
            "kind",
            "level",
            "session_id",
            "attempt_count",
            "message",
            "payload",
            "created_at",
        )
        read_only_fields = fields


class OrchestrationArtifactSerializer(serializers.ModelSerializer):
    class Meta:
        model = OrchestrationArtifact
        fields = (
            "id",
            "run",
            "step",
            "task",
            "artifact_type",
            "session_id",
            "label",
            "content",
            "payload",
            "created_at",
        )
        read_only_fields = fields


class OrchestrationRunSerializer(serializers.ModelSerializer):
    user_username = serializers.CharField(source="user.username", read_only=True, default=None)
    steps = OrchestrationStepSerializer(many=True, read_only=True)
    plan_steps = OrchestrationPlanStepSerializer(many=True, read_only=True)

    class Meta:
        model = OrchestrationRun
        fields = (
            "id",
            "project",
            "user",
            "user_username",
            "prompt",
            "approval_scope",
            "complexity_level",
            "plan_requires_approval",
            "status",
            "current_phase",
            "progress_percent",
            "total_steps",
            "completed_steps",
            "failed_steps",
            "celery_task_id",
            "plan_session_id",
            "supervisor_session_id",
            "active_session_id",
            "blueprint",
            "last_error",
            "prompt_tokens",
            "completion_tokens",
            "total_tokens",
            "stuck_recovery_count",
            "last_recovery_at",
            "last_recovery_error",
            "plan_approved_at",
            "started_at",
            "finished_at",
            "created_at",
            "updated_at",
            "plan_steps",
            "steps",
        )
        read_only_fields = fields


class AuditLogSerializer(serializers.ModelSerializer):
    class Meta:
        model = AuditLog
        fields = (
            "id",
            "project",
            "user",
            "timestamp",
            "original_prompt",
            "generated_diff",
        )
        read_only_fields = fields


class TokenUsageEventSerializer(serializers.ModelSerializer):
    class Meta:
        model = TokenUsageEvent
        fields = (
            "id",
            "run",
            "session_id",
            "endpoint",
            "prompt_tokens",
            "completion_tokens",
            "total_tokens",
            "raw_usage",
            "created_at",
        )
        read_only_fields = fields


class GitSyncJobSerializer(serializers.ModelSerializer):
    user_username = serializers.CharField(source="user.username", read_only=True, default=None)

    class Meta:
        model = GitSyncJob
        fields = (
            "id",
            "project",
            "run",
            "user",
            "user_username",
            "status",
            "base_branch",
            "feature_branch",
            "commit_sha",
            "pr_number",
            "pr_url",
            "attempts",
            "last_error",
            "started_at",
            "finished_at",
            "created_at",
            "updated_at",
        )
        read_only_fields = fields


class UserNotificationSerializer(serializers.ModelSerializer):
    project_name = serializers.CharField(source="project.name", read_only=True, default=None)

    class Meta:
        model = UserNotification
        fields = (
            "id",
            "user",
            "project",
            "project_name",
            "run",
            "kind",
            "title",
            "message",
            "payload",
            "is_read",
            "read_at",
            "created_at",
            "updated_at",
        )
        read_only_fields = fields
