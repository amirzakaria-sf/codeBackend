from __future__ import annotations

from typing import Any

from ..models import OrchestrationRun, Project, UserNotification
from .realtime import broadcast_user_event


def create_user_notification(
    *,
    user,
    kind: str,
    title: str,
    message: str = "",
    project: Project | None = None,
    run: OrchestrationRun | None = None,
    payload: dict[str, Any] | None = None,
) -> UserNotification:
    notification = UserNotification.objects.create(
        user=user,
        project=project,
        run=run,
        kind=kind,
        title=title,
        message=message,
        payload=payload or {},
    )
    broadcast_user_event(
        user.id,
        {
            "kind": "notification_created",
            "notification_id": notification.id,
            "title": notification.title,
            "message": notification.message,
            "project_id": project.id if project else None,
            "run_id": run.id if run else None,
            "notification_kind": notification.kind,
            "created_at": notification.created_at.isoformat(),
        },
    )
    return notification
