from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("workspaces", "0002_taskqueue_auditlog"),
    ]

    operations = [
        migrations.AddField(
            model_name="project",
            name="bootstrap_enabled",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="project",
            name="setup_status",
            field=models.CharField(
                choices=[("READY", "Ready"), ("DRAFT", "Draft / setup pending")],
                default="READY",
                max_length=16,
            ),
        ),
        migrations.AddField(
            model_name="project",
            name="starter_template",
            field=models.CharField(
                blank=True,
                choices=[
                    ("FULLSTACK", "Full-stack"),
                    ("FRONTEND", "Frontend"),
                    ("BACKEND", "Backend"),
                    ("MOBILE_BACKEND", "Mobile + backend"),
                    ("DESKTOP_BACKEND", "Desktop + backend"),
                ],
                default="FULLSTACK",
                max_length=32,
            ),
        ),
        migrations.AddField(
            model_name="project",
            name="workspace_mode",
            field=models.CharField(
                choices=[
                    ("STARTER", "Starter workspace"),
                    ("ACTIVE_CLONE", "Active clone"),
                    ("REFERENCE_CLONE", "Reference clone"),
                    ("CUSTOM", "Custom workspace"),
                ],
                default="STARTER",
                max_length=32,
            ),
        ),
        migrations.CreateModel(
            name="WorkspaceTarget",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("name", models.CharField(max_length=120)),
                ("relative_path", models.CharField(max_length=1024)),
                ("absolute_path", models.CharField(max_length=1024)),
                (
                    "role",
                    models.CharField(
                        choices=[
                            ("FRONTEND", "Frontend"),
                            ("BACKEND", "Backend"),
                            ("MOBILE", "Mobile"),
                            ("DESKTOP", "Desktop"),
                            ("SHARED", "Shared"),
                            ("INFRA", "Infrastructure"),
                            ("DOCS", "Documents"),
                            ("REFERENCE", "Reference"),
                            ("CUSTOM", "Custom"),
                        ],
                        default="CUSTOM",
                        max_length=32,
                    ),
                ),
                (
                    "source_type",
                    models.CharField(
                        choices=[
                            ("SCAFFOLD", "Scaffolded"),
                            ("EMPTY_DIR", "Empty directory"),
                            ("GIT_CLONE", "Git clone"),
                        ],
                        default="EMPTY_DIR",
                        max_length=32,
                    ),
                ),
                ("remote_url", models.CharField(blank=True, default="", max_length=2048)),
                ("default_branch", models.CharField(blank=True, default="", max_length=255)),
                ("is_primary", models.BooleanField(default=False)),
                ("is_editable", models.BooleanField(default=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "project",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="targets",
                        to="workspaces.project",
                    ),
                ),
            ],
            options={
                "ordering": ["-is_primary", "name", "created_at"],
            },
        ),
        migrations.AddConstraint(
            model_name="workspacetarget",
            constraint=models.UniqueConstraint(fields=("project", "name"), name="unique_workspace_target_name_per_project"),
        ),
        migrations.AddConstraint(
            model_name="workspacetarget",
            constraint=models.UniqueConstraint(fields=("project", "relative_path"), name="unique_workspace_target_path_per_project"),
        ),
    ]
