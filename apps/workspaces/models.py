from django.conf import settings
from django.db import models


class Project(models.Model):
    class WorkspaceMode(models.TextChoices):
        STARTER = "STARTER", "Starter workspace"
        ACTIVE_CLONE = "ACTIVE_CLONE", "Active clone"
        REFERENCE_CLONE = "REFERENCE_CLONE", "Reference clone"
        CUSTOM = "CUSTOM", "Custom workspace"

    class StarterTemplate(models.TextChoices):
        FULLSTACK = "FULLSTACK", "Full-stack"
        FRONTEND = "FRONTEND", "Frontend"
        BACKEND = "BACKEND", "Backend"
        MOBILE_BACKEND = "MOBILE_BACKEND", "Mobile + backend"
        DESKTOP_BACKEND = "DESKTOP_BACKEND", "Desktop + backend"

    class SetupStatus(models.TextChoices):
        READY = "READY", "Ready"
        DRAFT = "DRAFT", "Draft / setup pending"

    name = models.CharField(max_length=120, unique=True)
    absolute_path = models.CharField(max_length=1024)
    workspace_mode = models.CharField(
        max_length=32,
        choices=WorkspaceMode.choices,
        default=WorkspaceMode.STARTER,
    )
    starter_template = models.CharField(
        max_length=32,
        choices=StarterTemplate.choices,
        blank=True,
        default=StarterTemplate.FULLSTACK,
    )
    setup_status = models.CharField(
        max_length=16,
        choices=SetupStatus.choices,
        default=SetupStatus.READY,
    )
    bootstrap_enabled = models.BooleanField(default=False)
    allocated_port = models.IntegerField(null=True, blank=True)
    is_locked = models.BooleanField(default=False)
    locked_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="locked_projects",
    )
    daemon_pid = models.IntegerField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return self.name


class WorkspaceTarget(models.Model):
    class Role(models.TextChoices):
        FRONTEND = "FRONTEND", "Frontend"
        BACKEND = "BACKEND", "Backend"
        MOBILE = "MOBILE", "Mobile"
        DESKTOP = "DESKTOP", "Desktop"
        SHARED = "SHARED", "Shared"
        INFRA = "INFRA", "Infrastructure"
        DOCS = "DOCS", "Documents"
        REFERENCE = "REFERENCE", "Reference"
        CUSTOM = "CUSTOM", "Custom"

    class SourceType(models.TextChoices):
        SCAFFOLD = "SCAFFOLD", "Scaffolded"
        EMPTY_DIR = "EMPTY_DIR", "Empty directory"
        GIT_CLONE = "GIT_CLONE", "Git clone"

    project = models.ForeignKey(
        Project,
        on_delete=models.CASCADE,
        related_name="targets",
    )
    name = models.CharField(max_length=120)
    relative_path = models.CharField(max_length=1024)
    absolute_path = models.CharField(max_length=1024)
    role = models.CharField(
        max_length=32,
        choices=Role.choices,
        default=Role.CUSTOM,
    )
    source_type = models.CharField(
        max_length=32,
        choices=SourceType.choices,
        default=SourceType.EMPTY_DIR,
    )
    remote_url = models.CharField(max_length=2048, blank=True, default="")
    default_branch = models.CharField(max_length=255, blank=True, default="")
    is_primary = models.BooleanField(default=False)
    is_editable = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-is_primary", "name", "created_at"]
        constraints = [
            models.UniqueConstraint(fields=["project", "name"], name="unique_workspace_target_name_per_project"),
            models.UniqueConstraint(fields=["project", "relative_path"], name="unique_workspace_target_path_per_project"),
        ]

    def __str__(self) -> str:
        return f"{self.project.name} :: {self.name}"


class OrchestrationRun(models.Model):
    class Status(models.TextChoices):
        PENDING_APPROVAL = "PENDING_APPROVAL", "Pending approval"
        QUEUED = "QUEUED", "Queued"
        PLANNING = "PLANNING", "Planning"
        BREAKING_DOWN = "BREAKING_DOWN", "Breaking down"
        RUNNING = "RUNNING", "Running"
        VERIFYING = "VERIFYING", "Verifying"
        COMPLETED = "COMPLETED", "Completed"
        FAILED = "FAILED", "Failed"
        CANCELLED = "CANCELLED", "Cancelled"

    project = models.ForeignKey(
        Project,
        on_delete=models.CASCADE,
        related_name="runs",
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="orchestration_runs",
    )
    prompt = models.TextField()
    status = models.CharField(
        max_length=32,
        choices=Status.choices,
        default=Status.QUEUED,
    )
    current_phase = models.CharField(max_length=120, blank=True, default="Queued")
    progress_percent = models.PositiveSmallIntegerField(default=0)
    total_steps = models.PositiveIntegerField(default=0)
    completed_steps = models.PositiveIntegerField(default=0)
    failed_steps = models.PositiveIntegerField(default=0)
    celery_task_id = models.CharField(max_length=255, blank=True, default="")
    plan_session_id = models.CharField(max_length=255, blank=True, default="")
    supervisor_session_id = models.CharField(max_length=255, blank=True, default="")
    active_session_id = models.CharField(max_length=255, blank=True, default="")
    blueprint = models.TextField(blank=True, default="")
    last_error = models.TextField(blank=True, default="")
    prompt_tokens = models.PositiveIntegerField(default=0)
    completion_tokens = models.PositiveIntegerField(default=0)
    total_tokens = models.PositiveIntegerField(default=0)
    stuck_recovery_count = models.PositiveIntegerField(default=0)
    last_recovery_at = models.DateTimeField(null=True, blank=True)
    last_recovery_error = models.TextField(blank=True, default="")
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"Run {self.id} :: {self.project.name} :: {self.status}"


class TaskQueue(models.Model):
    class Status(models.TextChoices):
        PENDING_APPROVAL = "PENDING_APPROVAL", "Pending approval"
        QUEUED = "QUEUED", "Queued"
        RUNNING = "RUNNING", "Running"
        VERIFYING = "VERIFYING", "Verifying"
        COMPLETED = "COMPLETED", "Completed"
        FAILED = "FAILED", "Failed"

    project = models.ForeignKey(
        Project,
        on_delete=models.CASCADE,
        related_name="tasks",
    )
    run = models.ForeignKey(
        OrchestrationRun,
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="queue_entries",
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="orchestration_tasks",
    )
    assigned_agent = models.CharField(max_length=120)
    instruction_payload = models.TextField()
    sequence_order = models.PositiveIntegerField(default=1)
    status = models.CharField(
        max_length=32,
        choices=Status.choices,
        default=Status.QUEUED,
    )
    supervisor_feedback = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["sequence_order", "created_at"]

    def __str__(self) -> str:
        return f"{self.project.name} :: {self.assigned_agent} :: {self.status}"


class OrchestrationStep(models.Model):
    run = models.ForeignKey(
        OrchestrationRun,
        on_delete=models.CASCADE,
        related_name="steps",
    )
    task = models.OneToOneField(
        TaskQueue,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="run_step",
    )
    sequence_order = models.PositiveIntegerField(default=1)
    assigned_agent = models.CharField(max_length=120)
    instruction_payload = models.TextField()
    status = models.CharField(
        max_length=32,
        choices=TaskQueue.Status.choices,
        default=TaskQueue.Status.QUEUED,
    )
    attempt_count = models.PositiveIntegerField(default=0)
    worker_session_id = models.CharField(max_length=255, blank=True, default="")
    generated_diff = models.TextField(blank=True, default="")
    supervisor_feedback = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["sequence_order", "created_at"]

    def __str__(self) -> str:
        return f"Run {self.run_id} :: Step {self.sequence_order} :: {self.status}"


class AuditLog(models.Model):
    project = models.ForeignKey(
        Project,
        on_delete=models.CASCADE,
        related_name="audit_logs",
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="audit_logs",
    )
    timestamp = models.DateTimeField(auto_now_add=True)
    original_prompt = models.TextField()
    generated_diff = models.TextField(blank=True, default="")

    class Meta:
        ordering = ["-timestamp"]

    def __str__(self) -> str:
        return f"{self.project.name} @ {self.timestamp.isoformat()}"


class TokenUsageEvent(models.Model):
    run = models.ForeignKey(
        OrchestrationRun,
        on_delete=models.CASCADE,
        related_name="usage_events",
    )
    session_id = models.CharField(max_length=255, blank=True, default="")
    endpoint = models.CharField(max_length=255, blank=True, default="")
    prompt_tokens = models.PositiveIntegerField(default=0)
    completion_tokens = models.PositiveIntegerField(default=0)
    total_tokens = models.PositiveIntegerField(default=0)
    raw_usage = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"Run {self.run_id} usage @ {self.created_at.isoformat()}"


class GitSyncJob(models.Model):
    class Status(models.TextChoices):
        QUEUED = "QUEUED", "Queued"
        RUNNING = "RUNNING", "Running"
        COMPLETED = "COMPLETED", "Completed"
        FAILED = "FAILED", "Failed"
        SKIPPED = "SKIPPED", "Skipped"

    project = models.ForeignKey(
        Project,
        on_delete=models.CASCADE,
        related_name="git_sync_jobs",
    )
    run = models.OneToOneField(
        OrchestrationRun,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="git_sync_job",
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="git_sync_jobs",
    )
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.QUEUED)
    base_branch = models.CharField(max_length=255, default="dev")
    feature_branch = models.CharField(max_length=255, blank=True, default="")
    commit_sha = models.CharField(max_length=80, blank=True, default="")
    pr_number = models.PositiveIntegerField(null=True, blank=True)
    pr_url = models.URLField(blank=True, default="")
    attempts = models.PositiveIntegerField(default=0)
    last_error = models.TextField(blank=True, default="")
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"Sync {self.id} :: {self.project.name} :: {self.status}"


class UserNotification(models.Model):
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="foundry_notifications",
    )
    project = models.ForeignKey(
        Project,
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="notifications",
    )
    run = models.ForeignKey(
        OrchestrationRun,
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="notifications",
    )
    kind = models.CharField(max_length=64, default="info")
    title = models.CharField(max_length=255)
    message = models.TextField(blank=True, default="")
    payload = models.JSONField(default=dict, blank=True)
    is_read = models.BooleanField(default=False)
    read_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"Notification {self.id} :: {self.user_id} :: {self.kind}"
