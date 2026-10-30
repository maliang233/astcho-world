import asyncio

from astcho_world.kernel import Kernel
from astcho_world.toolbox import WorldTools


def test_only_new_by_default_old_is_explicit_and_queries_do_not_duplicate_observation(tmp_path):
    async def scenario():
        kernel = Kernel(tmp_path)
        tools = WorldTools(kernel)
        try:
            kernel.store.notify(
                "qq",
                {
                    "id": "one",
                    "source": "合成群",
                    "message": "合成单条通知",
                    "locator": {"chat_id": "group:test"},
                },
            )
            first = await tools.execute("world_notifications", {}, "first")
            assert len(first["notifications"]) == 1
            assert (await tools.execute("world_notifications", {}, "second"))["notifications"] == []
            history = await tools.execute("world_notifications", {"include_delivered": True}, "old")
            assert history == first
            assert (
                kernel.store.db.execute(
                    "SELECT count(*) FROM observations WHERE kind='notification'"
                ).fetchone()[0]
                == 1
            )
            assert kernel.store.locator("qq:one", "qq") == {"chat_id": "group:test"}
            kernel.store.ignore("qq:one")
            assert kernel.store.notifications(include_delivered=True) == []
        finally:
            await kernel.close()

    asyncio.run(scenario())


def test_delivery_limit_keeps_remaining_notifications_pending(tmp_path):
    kernel = Kernel(tmp_path)
    try:
        for n in range(5):
            kernel.store.notify(
                "notes",
                {
                    "id": str(n),
                    "source": "合成来源",
                    "message": "合成消息",
                    "locator": {},
                },
            )
        first = kernel.store.notifications(limit=2)
        assert len(first) == 2 and len(kernel.store.pending_notification_ids()) == 3
        second = kernel.store.notifications()
        assert len(second) == 3
        assert not set(n["id"] for n in first) & set(n["id"] for n in second)
        assert kernel.store.notifications() == []
    finally:
        asyncio.run(kernel.close())
