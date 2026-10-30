import asyncio
import json

import pytest

from astcho_world.apps.qq import MANIFEST as QQ
from astcho_world.dsh_runtime import DSHAgent
from astcho_world.kernel import Kernel
from tests.test_dsh_runtime import model_server as model_server_fixture

model_server = model_server_fixture


def test_real_sdk_empty_wakes_have_no_summary_call_or_recent_entry_even_after_restart(
    tmp_path, model_server
):
    script, requests, config = model_server
    script.extend([[("world_finish", {})]] * 5)

    async def scenario():
        kernel = Kernel(tmp_path)
        agent = DSHAgent(kernel, config, activity_context=True)
        try:
            for _ in range(4):
                assert (await agent.run())["status"] == "idle"
            ids = kernel.activities.finished_ids()
            assert len(ids) == 4 and kernel.activities.recent(10) == []
            for identifier in ids:
                archive = kernel.activities.recall(identifier, full=True)
                assert archive["activity"]["archive_state"] == "empty"
                assert "summary" not in archive
                assert any(e["kind"] == "decision.result" for e in archive["events"])
            session = agent.session_state["id"]
            await agent.close()
            agent = DSHAgent(kernel, config, activity_context=True)
            await agent.run()
            assert agent.session_state["id"] == session
            assert len(requests) == 5
            assert (
                kernel.store.db.execute(
                    "SELECT count(*) FROM usage_calls WHERE purpose='activity_summary'"
                ).fetchone()[0]
                == 0
            )
            assert kernel.store.get("activity_archive_error") is None
            assert kernel.activities.recent(10) == []
        finally:
            await agent.close()
            await kernel.close()

    asyncio.run(scenario())


def test_status_only_and_ignored_notifications_skip_but_reading_without_reply_has_local_index(
    tmp_path, model_server
):
    script, requests, config = model_server
    script.extend(
        [
            [("world_apps", {})],
            [("world_notifications", {})],
            [("world_finish", {})],
            [("world_tools", {"app_id": "qq"})],
            [("qq__list_chats", {})],
            [("world_finish", {})],
            [("qq__read_messages", {"chat_id": "group:test"})],
            [("world_finish", {})],
        ]
    )

    async def scenario():
        kernel = Kernel(tmp_path)
        kernel.register(QQ)
        await kernel.ingest(
            "qq",
            {
                "batch_id": "new",
                "chat_id": "group:test",
                "messages": [
                    {
                        "message_id": "1",
                        "user_id": "2",
                        "nickname": "合成朋友",
                        "text": "实际阅读的合成消息",
                        "timestamp": 1,
                        "mentioned_bot": True,
                    }
                ],
            },
        )
        agent = DSHAgent(kernel, config, activity_context=True)
        try:
            await agent.run()
            assert kernel.activities.recent(10) == []
            # The wake already delivered it. A default query must not repeat it.
            tool_result = json.loads(
                next(
                    m["content"]
                    for m in requests[2]["messages"]
                    if m["role"] == "tool" and '"notifications"' in m["content"]
                )
            )
            assert tool_result["notifications"] == []
            await agent.run()
            assert kernel.activities.recent(10) == []
            await agent.run()
            assert len(kernel.activities.recent(10)) == 1
            assert "qq__read_messages" in kernel.activities.recent(10)[0]["brief"]
            assert len(requests) == 8 and kernel.store.get("activity_archive_error") is None
        finally:
            await agent.close()
            await kernel.close()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "tool,args,result,observation,text,empty",
    [
        ("world_finish", {}, {"status": "idle"}, None, "", True),
        ("world_apps", {}, {"apps": []}, None, "", True),
        ("world_finish", {}, {"status": "unknown"}, None, "", False),
        ("world_apps", {}, {"error": "failed"}, None, "", False),
        ("world_activity", {"resume_note": "明确的待续意图"}, {}, None, "", False),
        ("world_notifications", {"include_delivered": True}, {}, None, "", False),
        ("notes__read", {}, {"status": "succeeded"}, None, "", False),
        ("world_finish", {}, {}, "admin_request", "", False),
        ("world_finish", {}, {}, "notification", "", True),
        ("world_finish", {}, {}, None, "新形成的明确思考", False),
    ],
)
def test_conservative_activity_evidence_policy(
    tmp_path, tool, args, result, observation, text, empty
):
    kernel = Kernel(tmp_path)
    item = kernel.activities.begin("run")
    token = kernel.store.activity.set("run")
    try:
        kernel.store.event(
            "decision.tool",
            "合成选择",
            data={
                "tool": tool,
                "arguments": args,
                "call_id": "call",
            },
        )
        kernel.store.event(
            "decision.result",
            "合成回执",
            data={
                "tool": tool,
                "call_id": "call",
                "result": result,
            },
        )
        if observation:
            kernel.store.observe(observation, "source", {"text": "已送达的合成内容"})
        kernel.store.event(
            "model.finished",
            "合成模型输出",
            data={
                "message": {
                    "content": [
                        {"type": "reasoning", "text": "内部选择过程"},
                        {"type": "text", "text": text},
                    ],
                }
            },
        )
        kernel.activities.settle("finished")
        policy = kernel.activities.summary_policy(item["id"])
        assert (policy["kind"] == "empty") is empty
    finally:
        kernel.store.activity.reset(token)
        asyncio.run(kernel.close())


def test_old_empty_index_is_excluded_but_original_summary_and_log_remain(tmp_path):
    kernel = Kernel(tmp_path)
    item = kernel.activities.begin("old-run")
    token = kernel.store.activity.set("old-run")
    kernel.store.event("activity.started", "合成旧空唤醒")
    kernel.store.activity.reset(token)
    kernel.activities.settle("finished")
    archive = kernel.activities.archive(item["id"])
    directory = tmp_path / "experience" / "activities" / item["id"]
    original = json.dumps(
        {"version": 2, "brief": "旧空活动简述", "references": [archive["events"][0]["seq"]]}
    )
    (directory / "summary.json").write_text(original)
    raw = (directory / "events.jsonl").read_bytes()
    old = kernel.activities.get(item["id"])
    old.pop("summary_policy")
    old["archive_state"] = "ready"
    kernel.activities.save(old)
    asyncio.run(kernel.close())
    kernel = Kernel(tmp_path)
    try:
        assert kernel.activities.recent(10) == []
        assert (directory / "summary.json").read_text() == original
        assert (directory / "events.jsonl").read_bytes() == raw
        assert kernel.activities.recall(item["id"], full=True)["events"]
    finally:
        asyncio.run(kernel.close())
