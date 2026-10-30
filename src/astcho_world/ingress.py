from __future__ import annotations

import asyncio
import random
import uuid


class QQIngress:
    """Transport-side per-chat batching, preserving the original 2–5 second window."""

    def __init__(self, kernel, *, schedule_provider=None):
        self.kernel = kernel
        self.schedule_provider = schedule_provider
        self.pending = {}
        self.tasks = {}
        self.private_handler = None
        self.private_tasks = set()
        self.private_locks = {}

    def receive(self, chat_id: str, message: dict, **metadata):
        if metadata.get("kind") == "private" and self.private_handler:
            task = asyncio.create_task(self._private(chat_id, message, metadata))
            self.private_tasks.add(task)
            task.add_done_callback(self.private_tasks.discard)
            return
        if chat_id in self.pending and self.pending[chat_id]["metadata"] != metadata:
            raise ValueError("Metadata must remain stable within a notification window")
        batch = self.pending.setdefault(chat_id, {"messages": [], "metadata": metadata})
        batch["messages"].append(message)
        if chat_id not in self.tasks:
            self.tasks[chat_id] = asyncio.create_task(self._flush_after_window(chat_id))

    async def _private(self, chat_id, message, metadata):
        lock = self.private_locks.setdefault(chat_id, asyncio.Lock())
        async with lock:
            try:
                await self.private_handler(chat_id, message, **metadata)
            except Exception as exc:
                self.kernel.store.event(
                    "private.failed",
                    "QQ 私聊处理失败",
                    app="qq",
                    data={"error_type": type(exc).__name__},
                )

    async def _flush_after_window(self, chat_id):
        try:
            await asyncio.sleep(random.uniform(2.0, 5.0))
            batch = self.pending.pop(chat_id)
            metadata = dict(batch["metadata"])
            if self.schedule_provider:
                metadata["schedule"] = self.schedule_provider()
            return await self.kernel.ingest(
                "qq",
                {
                    "batch_id": uuid.uuid4().hex,
                    "chat_id": chat_id,
                    "messages": batch["messages"],
                    **metadata,
                },
            )
        finally:
            self.tasks.pop(chat_id, None)
            if chat_id in self.pending:
                self.tasks[chat_id] = asyncio.create_task(self._flush_after_window(chat_id))

    async def drain(self):
        if self.private_tasks:
            await asyncio.gather(*list(self.private_tasks))
        while self.tasks:
            await asyncio.gather(*list(self.tasks.values()))
