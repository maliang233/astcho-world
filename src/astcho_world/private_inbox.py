"""Trusted QQ transport routing, independent of model-provided role claims."""

import time


class PrivateInbox:
    def __init__(self, kernel, admins, wake):
        self.kernel, self.admins, self.wake = kernel, frozenset(admins), wake

    async def receive(self, chat_id, message, **metadata):
        sender = str(message["user_id"])
        if chat_id != f"private:{sender}" or message.get("is_bot"):
            return
        mid = str(message["message_id"])
        await self.kernel.ingest(
            "qq",
            {
                "batch_id": f"private:{sender}:{mid}",
                "chat_id": chat_id,
                "messages": [message],
                "kind": "private",
            },
        )
        if not message.get("text", "").strip():
            return
        if sender in self.admins:
            request = {
                "id": f"qq:{chat_id}:{mid}",
                "chat_id": chat_id,
                "message_id": mid,
                "sender_id": sender,
                "nickname": message["nickname"],
                "text": message["text"],
                "received_at": time.time(),
                "authority": "configured_admin",
            }
            if self.kernel.store.enqueue_admin(request):
                self.wake()
            return
        # Match the protected private handler's command/empty-message exclusions.
        if message["text"].startswith(("/", ".")):
            return
        # The App creates this snapshot internally; the ordinary private text is
        # never supplied to the World model or its notification/observation stream.
        snapshot = await self.kernel.apps["qq"].request(
            "private_observation",
            {
                "chat_id": chat_id,
                "message_id": mid,
            },
            15,
        )
        await self.kernel.call(
            "qq", "participate", snapshot, operation_id=f"private-chat:{sender}:{mid}"
        )
