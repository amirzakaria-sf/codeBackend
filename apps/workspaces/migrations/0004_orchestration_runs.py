from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("workspaces", "0003_workspace_topology"),
    ]

    operations = [
        migrations.CreateModel(
            name="OrchestrationRun",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("prompt", models.TextField()),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("PENDING_APPROVAL", "Pending approval"),
                            ("QUEUED", "Queued"),
                            ("PLANNING", "Planning"),
                            ("BREAKING_DOWN", "Breaking down"),
                            ("RUNNING", "Running"),
                            ("VERIFYING", "Verifying"),
                            ("COMPLETED", "Completed"),
                            ("FAILED", "Failed"),
                            ("CANCELLED", "Cancelled"),
                        ],
                        default="QUEUED",
                        max_length=32,
                    ),
                ),
                ("current_phase", models.CharField(blank=True, default="Queued", max_length=120)),
                ("progress_percent", models.PositiveSmallIntegerField(default=0)),
                ("total_steps", models.PositiveIntegerField(default=0)),
                ("completed_steps", models.PositiveIntegerField(default=0)),
                ("failed_steps", models.PositiveIntegerField(default=0)),
                ("celery_task_id", models.CharField(blank=True, default="", max_length=255)),
                ("plan_session_id", models.CharField(blank=True, default="", max_length=255)),
                ("supervisor_session_id", models.CharField(blank=True, default="", max_length=255)),
                ("active_session_id", models.CharField(blank=True, default="", max_length=255)),
                ("blueprint", models.TextField(blank=True, default="")),
                ("last_error", models.TextField(blank=True, default="")),
                ("started_at", models.DateTimeField(blank=True, null=True)),
                ("finished_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "project",
                    models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="runs", to="workspaces.project"),
                ),
                (
                    "user",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="orchestration_runs",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={"ordering": ["-created_at"]},
        ),
        migrations.AddField(
            model_name="taskqueue",
            name="run",
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name="queue_entries", to="workspaces.orchestrationrun"),
        ),
        migrations.CreateModel(
            name="OrchestrationStep",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("sequence_order", models.PositiveIntegerField(default=1)),
                ("assigned_agent", models.CharField(max_length=120)),
                ("instruction_payload", models.TextField()),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("PENDING_APPROVAL", "Pending approval"),
                            ("QUEUED", "Queued"),
                            ("RUNNING", "Running"),
                            ("VERIFYING", "Verifying"),
                            ("COMPLETED", "Completed"),
                            ("FAILED", "Failed"),
                        ],
                        default="QUEUED",
                        max_length=32,
                    ),
                ),
                ("attempt_count", models.PositiveIntegerField(default=0)),
                ("worker_session_id", models.CharField(blank=True, default="", max_length=255)),
                ("generated_diff", models.TextField(blank=True, default="")),
                ("supervisor_feedback", models.TextField(blank=True, default="")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "run",
                    models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="steps", to="workspaces.orchestrationrun"),
                ),
                (
                    "task",
                    models.OneToOneField(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="run_step", to="workspaces.taskqueue"),
                ),
            ],
            options={"ordering": ["sequence_order", "created_at"]},
        ),
    ]
