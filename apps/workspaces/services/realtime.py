from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer


def broadcast_project_event(project_id: int, event: dict) -> None:
    channel_layer = get_channel_layer()
    if not channel_layer:
        return

    async_to_sync(channel_layer.group_send)(
        f"project_{project_id}",
        {
            "type": "workspace.event",
            "event": event,
        },
    )


def broadcast_user_event(user_id: int, event: dict) -> None:
    channel_layer = get_channel_layer()
    if not channel_layer:
        return

    async_to_sync(channel_layer.group_send)(
        f"user_{user_id}",
        {
            "type": "workspace.event",
            "event": event,
        },
    )
