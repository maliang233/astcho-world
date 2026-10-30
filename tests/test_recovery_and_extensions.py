import asyncio
import json
import time
from pathlib import Path

import pytest

from astcho_world.apps.library import MANIFEST as LIBRARY
from astcho_world.apps.notes import MANIFEST as NOTES
from astcho_world.apps.qq import QQ
from astcho_world.kernel import Kernel
from astcho_world.runtime import Agent


def test_single_subject_owner_prevents_second_runtime_corrupting_operations(tmp_path):
    kernel = Kernel(tmp_path)
    try:
        with pytest.raises(RuntimeError, match="already has an owner"):
            Kernel(tmp_path)
    finally:
        asyncio.run(kernel.close())


def test_stop_and_reinstall_keep_app_data_without_affecting_other_apps(tmp_path):
    async def scenario():
        kernel = Kernel(tmp_path)
        kernel.register(NOTES)
        try:
            saved = await kernel.call("notes", "save", {"title": "保留", "text": "应用自己的数据"})
            await kernel.stop_app("notes")
            assert (await kernel.call("notes", "list", {})).status == "failed"
            await kernel.start_app("notes")
            await kernel.unregister("notes")
            assert kernel.catalog() == []
            kernel.register(NOTES)
            read = await kernel.call("notes", "read", {"id": saved.data["id"]})
            assert read.data["text"] == "应用自己的数据"
        finally:
            await kernel.close()

    asyncio.run(scenario())


def test_new_app_composes_with_notes_and_skill_without_qq_changes(tmp_path):
    async def scenario():
        library = tmp_path / "library"
        library.mkdir()
        (library / "example.md").write_text("一段主动读取的资料")
        kernel = Kernel(tmp_path / "world")
        kernel.register(NOTES)
        kernel.register(LIBRARY, config={"root": str(library)})
        try:
            read = await kernel.call("library", "read_document", {"name": "example.md"})
            assert read.status == "succeeded"
            note = await kernel.call("notes", "save", {"title": "摘记", "text": read.data["text"]})
            assert note.status == "succeeded"
            escape = await kernel.call("library", "read_document", {"name": "../private.md"})
            assert escape.status == "failed"
            agent = Agent(kernel, None, skills=Path(__file__).parents[1] / "examples" / "skills")
            names = await agent.execute("world_skills", {}, "skill-list")
            assert names["skills"] == ["notes.md"]
            content = await agent.execute("world_read_skill", {"name": "notes.md"}, "skill-read")
            assert "不要求写日记" in content["text"]
        finally:
            await kernel.close()

    asyncio.run(scenario())


def test_restart_resumes_recorded_tool_call_with_same_operation_id(tmp_path):
    class MustNotRun:
        async def decide(self, *args):
            raise AssertionError("Restart should settle pending calls, not ask for new ones")

    async def scenario():
        kernel = Kernel(tmp_path)
        kernel.register(NOTES)
        args = {"title": "已执行的笔记", "text": "进程在保存工具结果前中断"}
        await kernel.call("notes", "save", args, operation_id="agent:1:saved-call")
        kernel.store.set(
            "agent_history",
            [
                {"role": "user", "content": "一次唤醒"},
                {
                    "role": "assistant",
                    "tool_calls": [
                        {
                            "id": "saved-call",
                            "type": "function",
                            "function": {"name": "notes__save", "arguments": json.dumps(args)},
                        },
                        {
                            "id": "finish",
                            "type": "function",
                            "function": {"name": "world_finish", "arguments": "{}"},
                        },
                    ],
                },
            ],
        )
        kernel.store.set("expanded_apps", ["notes"])
        await kernel.close()
        kernel = Kernel(tmp_path)
        kernel.register(NOTES)
        try:
            assert (await Agent(kernel, MustNotRun()).run())["status"] == "idle"
            notes = await kernel.call("notes", "list", {})
            assert len(notes.data["notes"]) == 1
        finally:
            await kernel.close()

    asyncio.run(scenario())


def test_participation_uses_fixed_observation_and_cannot_replay_same_snapshot(tmp_path):
    class Backend:
        calls = []

        async def participate(self, snapshot, state):
            self.calls.append(snapshot)
            return {
                "status": "completed",
                "sent": [
                    {
                        "message_id": "sent-1",
                        "user_id": "10001",
                        "nickname": "星回",
                        "text": "原聊天结果",
                        "timestamp": time.time(),
                        "is_bot": True,
                    }
                ],
            }

    async def scenario():
        app = QQ(tmp_path, {"conversation": {"allow_send": True, "group_reply_mode": "legacy"}})
        app.conversation = Backend()

        def incoming(batch, mid):
            return {
                "batch_id": batch,
                "chat_id": "group:10003",
                "messages": [
                    {
                        "message_id": mid,
                        "user_id": "10002",
                        "nickname": "甲",
                        "text": "在吗",
                        "timestamp": time.time(),
                        "mentioned_bot": True,
                    }
                ],
            }

        app.ingest(incoming("one", "1"))
        snapshot = app.read({"chat_id": "group:10003"})
        app.ingest(incoming("two", "2"))
        params = {
            "tool": "participate",
            "arguments": {"observation_id": snapshot["observation_id"]},
            "operation_id": "first-action",
        }
        first = await app.dispatch("call", params)
        second = await app.dispatch("call", {**params, "operation_id": "new-action"})
        assert first == second
        assert first["sent_message_ids"] == ["sent-1"]
        assert len(app.conversation.calls) == 1
        assert [m["message_id"] for m in app.conversation.calls[0]["messages"]] == ["1"]

    asyncio.run(scenario())


def test_plain_model_text_does_not_send_and_a_new_wake_can_run(tmp_path):
    class Model:
        async def decide(self, messages, tools):
            return {"role": "assistant", "content": "我先休息一下"}

    async def scenario():
        kernel = Kernel(tmp_path)
        try:
            agent = Agent(kernel, Model())
            assert (await agent.run())["status"] == "idle"
            assert (await agent.run())["status"] == "idle"
            assert kernel.store.db.execute("SELECT count(*) FROM operations").fetchone()[0] == 0
        finally:
            await kernel.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("sample", [0, 0.01, 0.05, 0.1, 0.9])
def test_notification_sampling_matches_original_attention(tmp_path, monkeypatch, sample):
    from astcho.domain.models import ChatMessage
    from astcho.services.attention import AttentionService
    from astcho.services.schedule import ScheduleState

    monkeypatch.setattr("time.time", lambda: 1060)
    message = ChatMessage(
        message_id="1", user_id="10002", nickname="甲", text="普通消息", timestamp=1000
    )
    original = AttentionService("10001", bot_name="星回")
    original.add(message)
    expected = original.should_plan(
        message, ScheduleState(50, "calm", "default"), random_value=sample
    )
    app = QQ(tmp_path, {})
    result = app.ingest(
        {
            "batch_id": "batch",
            "chat_id": "group:test",
            "messages": [message.model_dump()],
            "random_value": sample,
        }
    )
    assert bool(result["notifications"]) is expected
    state = json.loads(app.db.execute("SELECT attention FROM chats").fetchone()[0])
    assert state["consecutive_no_reply"] == 0
