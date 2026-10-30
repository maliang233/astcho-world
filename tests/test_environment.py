import asyncio
import json
import time
from pathlib import Path

import pytest

from astcho_world.apps.notes import MANIFEST as NOTES
from astcho_world.apps.qq import MANIFEST as QQ
from astcho_world.baseline import verify
from astcho_world.contracts import Manifest, Tool, object_schema
from astcho_world.kernel import Kernel
from astcho_world.runtime import Agent


def run(coroutine):
    return asyncio.run(coroutine)


def message(mid="1", text="你好", **changes):
    return {
        "message_id": mid,
        "user_id": "10002",
        "nickname": "测试群友",
        "text": text,
        "timestamp": time.time(),
        **changes,
    }


def packet(batch="b1", **changes):
    return {
        "batch_id": batch,
        "chat_id": "group:test",
        "title": "测试群",
        "messages": [message(mentioned_bot=True)],
        **changes,
    }


def test_legacy_source_and_tests_are_unchanged():
    root = Path(__file__).resolve().parents[1]
    assert verify(root) == []


def test_notes_operation_receipt_survives_restart(tmp_path):
    async def scenario():
        kernel = Kernel(tmp_path)
        kernel.register(NOTES)
        args = {"title": "随手记", "text": "今天看到的事情"}
        first = await kernel.call("notes", "save", args, operation_id="save-once")
        assert first.status == "succeeded"
        await kernel.close()
        kernel = Kernel(tmp_path)
        kernel.register(NOTES)
        try:
            repeated = await kernel.call("notes", "save", args, operation_id="save-once")
            assert first == repeated
            notes = await kernel.call("notes", "list", {})
            assert len(notes.data["notes"]) == 1
            with pytest.raises(ValueError):
                await kernel.call(
                    "notes", "save", {**args, "text": "changed"}, operation_id="save-once"
                )
        finally:
            await kernel.close()

    run(scenario())


def test_unread_does_not_enter_observations_and_mute_is_not_history_loss(tmp_path):
    async def scenario():
        kernel = Kernel(tmp_path)
        kernel.register(QQ)
        try:
            assert await kernel.ingest("qq", packet()) == 1
            assert kernel.store.db.execute("SELECT count(*) FROM observations").fetchone()[0] == 0
            notices = kernel.store.notifications()
            assert set(notices[0]) == {"id", "app", "source", "message"}
            assert "messages" not in json.dumps(notices)
            kernel.store.ignore(notices[0]["id"])
            muted = await kernel.call(
                "qq", "set_notifications", {"chat_id": "group:test", "muted": True}
            )
            assert muted.status == "succeeded"
            assert await kernel.ingest("qq", packet("b2", messages=[message("2", "未读秘密")])) == 0
            assert "未读秘密" not in json.dumps(
                [row[0] for row in kernel.store.db.execute("SELECT content FROM observations")],
                ensure_ascii=False,
            )
            assert (
                await kernel.ingest(
                    "qq",
                    packet(
                        "b3",
                        messages=[
                            message("3", "引用你", replied_to_bot=True),
                        ],
                    ),
                )
                == 1
            )
            assert await kernel.ingest("qq", packet("b3", messages=[])) == 1
            assert len(kernel.store.notifications()) == 1  # Duplicate event, not duplicate notice.
            records = await kernel.call("qq", "read_messages", {"chat_id": "group:test"})
            assert [m["message_id"] for m in records.data["messages"]] == ["1", "2", "3"]
            assert "未读秘密" in json.dumps(records.data, ensure_ascii=False)
        finally:
            await kernel.close()

    run(scenario())


def test_notification_anchor_does_not_drift_to_newest_message(tmp_path):
    async def scenario():
        kernel = Kernel(tmp_path)
        kernel.register(QQ)
        try:
            await kernel.ingest("qq", packet())
            notice = kernel.store.notifications()[0]
            await kernel.ingest("qq", packet("b2", messages=[message("2", "后来到达")]))
            opened = await kernel.open_notification(notice["id"])
            assert opened.data["anchor_message_id"] == "1"
            assert [m["message_id"] for m in opened.data["messages"]] == ["1"]
            older = await kernel.call(
                "qq",
                "read_messages",
                {
                    "chat_id": "group:test",
                    "before_sequence": 2,
                    "limit": 1,
                },
            )
            assert older.data["messages"][0]["message_id"] == "1"
        finally:
            await kernel.close()

    run(scenario())


def test_per_app_process_crash_and_timeout_do_not_block_notes(tmp_path):
    async def scenario():
        kernel = Kernel(tmp_path)
        broken = Manifest(
            app_id="broken",
            version="1",
            description="故障测试",
            entrypoint="astcho_world.testing:FaultApp",
            tools=[
                Tool(
                    name="hang",
                    description="block",
                    input_schema=object_schema({}),
                    timeout=0.25,
                    effect="write",
                ),
                Tool(
                    name="crash", description="exit", input_schema=object_schema({}), timeout=0.25
                ),
            ],
        )
        kernel.register(broken)
        kernel.register(NOTES)
        try:
            hung, good = await asyncio.gather(
                kernel.call("broken", "hang", {}, operation_id="uncertain-write"),
                kernel.call("notes", "save", {"title": "仍在工作", "text": "独立应用"}),
            )
            assert hung.status == "unknown"
            assert good.status == "succeeded"
            assert kernel.apps["broken"].process is None
            replay = await kernel.call("broken", "hang", {}, operation_id="uncertain-write")
            assert replay == hung
            assert kernel.apps["broken"].process is None  # No automatic replay.
            crash = await kernel.call("broken", "crash", {})
            assert crash.status == "failed"
            assert (await kernel.call("notes", "list", {})).status == "succeeded"
        finally:
            await kernel.close()

    run(scenario())


def test_schema_rejects_invalid_tool_arguments_before_launch(tmp_path):
    async def scenario():
        kernel = Kernel(tmp_path)
        kernel.register(NOTES)
        try:
            from jsonschema import ValidationError

            with pytest.raises(ValidationError):
                await kernel.call("notes", "save", {"title": "missing text"})
            assert kernel.apps["notes"].process is None
        finally:
            await kernel.close()

    run(scenario())


def test_reminder_survives_restart_without_creating_a_behavior_script(tmp_path):
    async def scenario():
        kernel = Kernel(tmp_path)
        due = time.time() + 60
        kernel.store.remind(due, "再看看自己刚才记的东西")
        await kernel.close()
        kernel = Kernel(tmp_path)
        try:
            kernel.store.tick(due + 1)
            kernel.store.tick(due + 2)
            notices = kernel.store.notifications()
            assert len(notices) == 1
            assert notices[0]["message"] == "再看看自己刚才记的东西"
        finally:
            await kernel.close()

    run(scenario())


def test_agent_can_ignore_and_finish_without_opening_qq(tmp_path):
    class Model:
        def __init__(self):
            self.step = 0

        async def decide(self, messages, tools):
            self.step += 1
            assert "未读秘密" not in json.dumps(messages, ensure_ascii=False)
            if self.step == 1:
                name, args = "world_notifications", {}
            elif self.step == 2:
                name, args = "world_ignore", {"id": "qq:group:test:1"}
            else:
                name, args = "world_finish", {}
            return {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": str(self.step),
                        "type": "function",
                        "function": {"name": name, "arguments": json.dumps(args)},
                    }
                ],
            }

    async def scenario():
        kernel = Kernel(tmp_path)
        kernel.register(QQ)
        try:
            await kernel.ingest(
                "qq",
                packet(
                    messages=[
                        message("hidden", "未读秘密"),
                        message(mentioned_bot=True),
                    ]
                ),
            )
            result = await Agent(kernel, Model()).run()
            assert result["status"] == "idle"
            assert kernel.store.db.execute("SELECT count(*) FROM operations").fetchone()[0] == 0
        finally:
            await kernel.close()

    run(scenario())
