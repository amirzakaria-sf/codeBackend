from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("workspaces", "0004_orchestration_runs"),
    ]

    operations = [
        migrations.AddField(
            model_name="orchestrationrun",
            name="stuck_recovery_count",
            field=models.PositiveIntegerField(default=0),
        ),
        migrations.AddField(
            model_name="orchestrationrun",
            name="last_recovery_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="orchestrationrun",
            name="last_recovery_error",
            field=models.TextField(blank=True, default=""),
        ),
    ]
