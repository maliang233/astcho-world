import asyncio
import json

from astcho_world.apps.qq import MANIFEST as QQ
from astcho_world.kernel import Kernel
from astcho_world.toolbox import WorldTools


def test_recall_cannot_access_unread_messages_admin_logs_or_its_own_nested_results(tmp_path):
    async def scenario():
        kernel = Kernel(tmp_path)
        kernel.register(QQ)
        tools = WorldTools(kernel)
        try:
            await kernel.ingest(
                "qq",
                {
                    "batch_id": "synthetic",
                    "chat_id": "group:test",
                    "messages": [
                        {
                            "message_id": "hidden",
                            "user_id": "2",
                            "nickname": "朋友",
                            "text": "未读秘密",
                            "timestamp": 1,
                        },
                        {
                            "message_id": "trigger",
                            "user_id": "2",
                            "nickname": "朋友",
                            "text": "触发通知",
                            "timestamp": 1,
                            "mentioned_bot": True,
                        },
                    ],
                },
            )
            # Notification creation is not delivery, and debug logs are not experience.
            kernel.store.event("debug.model_request", "管理员调试", data={"text": "私人调试信息"})
            kernel.store.event(
                "decision.result",
                "嵌套回看",
                data={"tool": "world_recall", "result": {"text": "递归回看结果"}},
            )
            assert not (await tools.execute("world_recall", {}, "list"))["items"]
            pending = kernel.store.pending_notification_ids()
            hidden_seq = kernel.store.db.execute(
                "SELECT seq FROM events WHERE kind='notification.created'"
            ).fetchone()[0]
            assert not (await tools.execute("world_recall", {"event_seq": hidden_seq}, "hidden"))[
                "items"
            ]
            assert kernel.store.pending_notification_ids() == pending
            notice = kernel.store.notifications()[0]
            seen = await tools.execute("world_recall", {"query": "触发通知"}, "list")
            assert seen["items"][0]["preview_only"]
            record = await tools.execute(
                "world_recall", {"event_seq": seen["items"][0]["seq"]}, "read"
            )
            assert "触发通知" in json.dumps(record, ensure_ascii=False)
            assert "未读秘密" not in json.dumps(record, ensure_ascii=False)
            await kernel.open_notification(notice["id"])
            seen = await tools.execute("world_recall", {"query": "未读秘密"}, "list")
            assert seen["items"]
            assert not (
                await tools.execute(
                    "world_recall",
                    {"before": seen["items"][-1]["seq"], "query": "未读秘密"},
                    "older",
                )
            )["items"]
        finally:
            await kernel.close()

    asyncio.run(scenario())
