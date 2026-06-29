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

    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="owned_projects",
    )
    path_owner_username = models.CharField(max_length=120, blank=True, default="")
    name = models.CharField(max_length=120)
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
        constraints = [
            models.UniqueConstraint(fields=["path_owner_username", "name"], name="unique_project_name_per_path_owner"),
        ]

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
    class ApprovalScope(models.TextChoices):
        NONE = "NONE", "No approval required"
        LOCK = "LOCK", "Lock approval"
        PLAN = "PLAN", "Plan approval"

    class ComplexityLevel(models.TextChoices):
        SIMPLE = "SIMPLE", "Simple"
        COMPLEX = "COMPLEX", "Complex"

    class Status(models.TextChoices):
        PENDING_APPROVAL = "PENDING_APPROVAL", "Pending approval"
        QUEUED = "QUEUED", "Queued"
        PLANNING = "PLANNING", "Planning"
        BREAKING_DOWN = "BREAKING_DOWN", "Breaking down"
        PLAN_READY = "PLAN_READY", "Plan ready"
        AWAITING_PLAN_APPROVAL = "AWAITING_PLAN_APPROVAL", "Awaiting plan approval"
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
    approval_scope = models.CharField(
        max_length=16,
        choices=ApprovalScope.choices,
        default=ApprovalScope.NONE,
    )
    complexity_level = models.CharField(
        max_length=16,
        choices=ComplexityLevel.choices,
        default=ComplexityLevel.SIMPLE,
    )
    plan_requires_approval = models.BooleanField(default=False)
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
    plan_approved_at = models.DateTimeField(null=True, blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"Run {self.id} :: {self.project.name} :: {self.status}"


class TaskQueue(models.Model):
    class ApprovalScope(models.TextChoices):
        NONE = "NONE", "No approval required"
        LOCK = "LOCK", "Lock approval"
        PLAN = "PLAN", "Plan approval"

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
    approval_scope = models.CharField(
        max_length=16,
        choices=ApprovalScope.choices,
        default=ApprovalScope.NONE,
    )
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


class OrchestrationPlanStep(models.Model):
    class Status(models.TextChoices):
        DRAFT = "DRAFT", "Draft"
        APPROVED = "APPROVED", "Approved"
        REPLACED = "REPLACED", "Replaced"

    run = models.ForeignKey(
        OrchestrationRun,
        on_delete=models.CASCADE,
        related_name="plan_steps",
    )
    sequence_order = models.PositiveIntegerField(default=1)
    assigned_agent = models.CharField(max_length=120)
    instruction_payload = models.TextField()
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.DRAFT)
    planner_notes = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["sequence_order", "created_at"]

    def __str__(self) -> str:
        return f"Run {self.run_id} :: Plan step {self.sequence_order} :: {self.status}"


class OrchestrationRunActivity(models.Model):
    class Level(models.TextChoices):
        INFO = "INFO", "Info"
        WARNING = "WARNING", "Warning"
        ERROR = "ERROR", "Error"

    run = models.ForeignKey(
        OrchestrationRun,
        on_delete=models.CASCADE,
        related_name="activities",
    )
    step = models.ForeignKey(
        OrchestrationStep,
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="activities",
    )
    task = models.ForeignKey(
        TaskQueue,
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="activities",
    )
    kind = models.CharField(max_length=64)
    level = models.CharField(max_length=16, choices=Level.choices, default=Level.INFO)
    session_id = models.CharField(max_length=255, blank=True, default="")
    attempt_count = models.PositiveIntegerField(default=0)
    message = models.TextField(blank=True, default="")
    payload = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at", "id"]

    def __str__(self) -> str:
        return f"Run {self.run_id} :: {self.kind} :: {self.level}"


class OrchestrationArtifact(models.Model):
    class ArtifactType(models.TextChoices):
        PLAN = "PLAN", "Plan"
        STEP_LIST = "STEP_LIST", "Step list"
        MESSAGE = "MESSAGE", "Message"
        DIFF = "DIFF", "Diff"
        SUPERVISOR_FEEDBACK = "SUPERVISOR_FEEDBACK", "Supervisor feedback"
        SESSION_MESSAGES = "SESSION_MESSAGES", "Session messages"
        SESSION_DIFF = "SESSION_DIFF", "Session diff"
        SESSION_STATUS = "SESSION_STATUS", "Session status"
        SHELL_OUTPUT = "SHELL_OUTPUT", "Shell output"
        USAGE = "USAGE", "Usage"
        ERROR = "ERROR", "Error"
        APPROVAL = "APPROVAL", "Approval"

    run = models.ForeignKey(
        OrchestrationRun,
        on_delete=models.CASCADE,
        related_name="artifacts",
    )
    step = models.ForeignKey(
        OrchestrationStep,
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="artifacts",
    )
    task = models.ForeignKey(
        TaskQueue,
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="artifacts",
    )
    artifact_type = models.CharField(max_length=32, choices=ArtifactType.choices)
    session_id = models.CharField(max_length=255, blank=True, default="")
    label = models.CharField(max_length=255, blank=True, default="")
    content = models.TextField(blank=True, default="")
    payload = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at", "id"]

    def __str__(self) -> str:
        return f"Run {self.run_id} :: {self.artifact_type}"


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
