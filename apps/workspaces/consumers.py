from channels.generic.websocket import AsyncJsonWebsocketConsumer


class WorkspaceEventConsumer(AsyncJsonWebsocketConsumer):
    async def connect(self):
        user = self.scope.get("user")
        if not user or user.is_anonymous:
            await self.close(code=4401)
            return

        self.project_id = self.scope["url_route"]["kwargs"]["project_id"]
        self.project_group_name = f"project_{self.project_id}"
        self.user_group_name = f"user_{user.id}"

        await self.channel_layer.group_add(self.project_group_name, self.channel_name)
        await self.channel_layer.group_add(self.user_group_name, self.channel_name)
        await self.accept()

    async def disconnect(self, close_code):
        if hasattr(self, "project_group_name"):
            await self.channel_layer.group_discard(self.project_group_name, self.channel_name)
        if hasattr(self, "user_group_name"):
            await self.channel_layer.group_discard(self.user_group_name, self.channel_name)

    async def workspace_event(self, event):
        await self.send_json(event["event"])
