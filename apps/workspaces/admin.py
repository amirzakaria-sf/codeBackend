from django.contrib import admin

from .models import (
    AuditLog,
    GitSyncJob,
    OrchestrationRun,
    OrchestrationStep,
    Project,
    TaskQueue,
    TokenUsageEvent,
    UserNotification,
    WorkspaceTarget,
)


class WorkspaceTargetInline(admin.TabularInline):
    model = WorkspaceTarget
    extra = 0
    fields = (
        "name",
        "role",
        "source_type",
        "relative_path",
        "is_primary",
        "is_editable",
        "default_branch",
    )
    readonly_fields = ("relative_path",)


@admin.register(Project)
class ProjectAdmin(admin.ModelAdmin):
    list_display = (
        "name",
        "workspace_mode",
        "starter_template",
        "setup_status",
        "allocated_port",
        "is_locked",
        "locked_by",
        "daemon_pid",
        "created_at",
    )
    search_fields = ("name", "absolute_path")
    list_filter = ("workspace_mode", "setup_status", "is_locked", "created_at")
    inlines = (WorkspaceTargetInline,)


@admin.register(WorkspaceTarget)
class WorkspaceTargetAdmin(admin.ModelAdmin):
    list_display = (
        "name",
        "project",
        "role",
        "source_type",
        "is_primary",
        "is_editable",
        "default_branch",
        "created_at",
    )
    list_filter = ("role", "source_type", "is_primary", "is_editable", "created_at")
    search_fields = ("name", "project__name", "absolute_path", "remote_url")


@admin.register(TaskQueue)
class TaskQueueAdmin(admin.ModelAdmin):
    list_display = (
        "project",
        "run",
        "assigned_agent",
        "sequence_order",
        "status",
        "user",
        "created_at",
    )
    list_filter = ("status", "assigned_agent", "created_at")
    search_fields = ("project__name", "assigned_agent", "instruction_payload")


@admin.register(OrchestrationRun)
class OrchestrationRunAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "project",
        "user",
        "status",
        "progress_percent",
        "stuck_recovery_count",
        "completed_steps",
        "total_steps",
        "last_recovery_at",
        "started_at",
        "finished_at",
        "created_at",
    )
    list_filter = ("status", "project", "created_at")
    search_fields = ("project__name", "user__username", "prompt", "celery_task_id")


@admin.register(OrchestrationStep)
class OrchestrationStepAdmin(admin.ModelAdmin):
    list_display = (
        "run",
        "sequence_order",
        "assigned_agent",
        "status",
        "attempt_count",
        "created_at",
    )
    list_filter = ("status", "assigned_agent", "created_at")
    search_fields = ("run__project__name", "assigned_agent", "instruction_payload")


@admin.register(AuditLog)
class AuditLogAdmin(admin.ModelAdmin):
    list_display = ("project", "user", "timestamp")
    list_filter = ("timestamp",)
    search_fields = ("project__name", "user__username", "original_prompt")


@admin.register(TokenUsageEvent)
class TokenUsageEventAdmin(admin.ModelAdmin):
    list_display = ("run", "session_id", "endpoint", "prompt_tokens", "completion_tokens", "total_tokens", "created_at")
    list_filter = ("endpoint", "created_at")
    search_fields = ("run__project__name", "session_id")


@admin.register(GitSyncJob)
class GitSyncJobAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "project",
        "run",
        "status",
        "base_branch",
        "feature_branch",
        "pr_number",
        "attempts",
        "created_at",
    )
    list_filter = ("status", "base_branch", "created_at")
    search_fields = ("project__name", "feature_branch", "pr_url", "commit_sha")


@admin.register(UserNotification)
class UserNotificationAdmin(admin.ModelAdmin):
    list_display = ("id", "user", "kind", "title", "project", "run", "is_read", "created_at")
    list_filter = ("kind", "is_read", "created_at")
    search_fields = ("user__username", "title", "message", "project__name")
