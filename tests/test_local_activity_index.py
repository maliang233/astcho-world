import asyncio
import hashlib
import json

import pytest

from astcho_world.apps.notes import MANIFEST as NOTES
from astcho_world.dsh_runtime import DSHAgent
from astcho_world.kernel import Kernel
from tests.test_dsh_runtime import model_server as model_server_fixture

model_server = model_server_fixture


def test_real_sdk_optional_finish_note_and_normal_actions_never_call_a_summary_model(
    tmp_path, model_server
):
    script, requests, config = model_server
    script.extend(
        [
            [("world_tools", {"app_id": "notes"})],
            [("notes__save", {"title": "合成笔记", "text": "完整原文保留标记" * 500})],
            [("world_finish", {"note": "记下了今天想看的资料"})],
            [("world_finish", {})],
            [("world_finish", {"note": "刚才想到的一件事，下次有需要再回看"})],
        ]
    )

    async def scenario():
        kernel = Kernel(tmp_path)
        kernel.register(NOTES)
        agent = DSHAgent(kernel, config, activity_context=True)
        try:
            await agent.run()
            first = kernel.activities.finished_ids()[0]
            brief = kernel.activities.recent(10)[0]["brief"]
            assert "自留：记下了今天想看的资料" in brief
            assert "notes__save×1" in brief and "succeeded" in brief
            assert "完整原文保留标记" not in brief
            await agent.run()
            assert len(kernel.activities.recent(10)) == 1  # Empty still costs no index slot.
            session = agent.session_state["id"]
            await agent.close()
            agent = DSHAgent(kernel, config, activity_context=True)
            await agent.run()
            assert agent.session_state["id"] == session
            assert len(requests) == 5
            assert len(kernel.activities.recent(10)) == 2  # A voluntary note alone is retained.
            assert "完整原文保留标记" not in json.dumps(requests[3:], ensure_ascii=False)
            assert "完整原文保留标记" in json.dumps(
                kernel.activities.recall(first, full=True), ensure_ascii=False
            )
            assert (
                kernel.store.db.execute(
                    "SELECT count(*) FROM usage_calls WHERE purpose='activity_summary'"
                ).fetchone()[0]
                == 0
            )
            assert kernel.store.get("activity_archive_error") is None
            # The note never sends a message, creates a reminder or performs an extra action.
            assert kernel.store.db.execute("SELECT tool FROM operations").fetchall()[0][0] == "save"
            assert kernel.store.db.execute("SELECT count(*) FROM operations").fetchone()[0] == 1
            assert kernel.store.db.execute("SELECT count(*) FROM reminders").fetchone()[0] == 0
        finally:
            await agent.close()
            await kernel.close()

    asyncio.run(scenario())


def test_local_facts_keep_unknown_and_missing_receipts_separate_from_a_subject_note(tmp_path):
    kernel = Kernel(tmp_path)
    item = kernel.activities.begin("first")

    def event(run, kind, data):
        token = kernel.store.activity.set(run)
        try:
            kernel.store.event(kind, "合成经历", data=data)
        finally:
            kernel.store.activity.reset(token)

    event("first", "decision.tool", {"tool": "future__act", "arguments": {}, "call_id": "same"})
    event(
        "first",
        "decision.result",
        {"tool": "future__act", "call_id": "same", "result": {"status": "unknown"}},
    )
    kernel.activities.settle("suspended")
    assert kernel.activities.begin("second")["id"] == item["id"]
    event("second", "decision.tool", {"tool": "future__act", "arguments": {}, "call_id": "same"})
    event(
        "second",
        "decision.result",
        {"tool": "future__act", "call_id": "same", "result": {"status": "succeeded"}},
    )
    event("second", "decision.tool", {"tool": "future__other", "arguments": {}, "call_id": "lost"})
    event(
        "second",
        "decision.tool",
        {"tool": "world_finish", "arguments": {"note": "我觉得事情都做好了"}, "call_id": "finish"},
    )
    kernel.store.notify(
        "qq", {"id": "unread", "source": "群", "message": "UNREAD_SECRET", "locator": {}}
    )
    kernel.store.db.execute(
        "INSERT INTO operations(id,app,tool,request,effect,status) "
        "VALUES('uncertain','future','act','{}','write','unknown')"
    )
    kernel.store.db.commit()
    kernel.activities.settle("finished")
    try:
        index = kernel.activities.index(item["id"])
        assert "自留：我觉得事情都做好了；实际记录：" in index["brief"]
        assert "unknown×1" in index["brief"] and "succeeded×1" in index["brief"]
        assert "未记录关联回执" in index["brief"]
        assert "UNREAD_SECRET" not in json.dumps(index)
        assert kernel.store.pending_notification_ids() == {"qq:unread"}
        events = kernel.activities.recall(item["id"], full=True)["events"]
        assert set(index["references"]) <= {e["seq"] for e in events}
        assert (
            kernel.store.db.execute(
                "SELECT status FROM operations WHERE id='uncertain'"
            ).fetchone()[0]
            == "unknown"
        )
        assert kernel.activities.index(item["id"]) == index
    finally:
        asyncio.run(kernel.close())


def test_invalid_cached_summary_keeps_original_archive_until_explicit_repair(tmp_path):
    kernel = Kernel(tmp_path)
    item = kernel.activities.begin("run")
    token = kernel.store.activity.set("run")
    kernel.store.observe("synthetic_read", "source", {"text": "完整经历"})
    kernel.store.activity.reset(token)
    kernel.activities.settle("finished")
    kernel.activities.archive(item["id"])
    folder = tmp_path / "experience/activities" / item["id"]
    before = hashlib.sha256((folder / "events.jsonl").read_bytes()).hexdigest()
    corrupt = '{"version":2,"brief":"非法旧索引","references":[-1]}'
    (folder / "summary.json").write_text(corrupt)
    try:
        with pytest.raises(ValueError, match="Invalid activity summary"):
            kernel.activities.index(item["id"])
        assert (folder / "summary.json").read_text() == corrupt
        assert hashlib.sha256((folder / "events.jsonl").read_bytes()).hexdigest() == before
        assert kernel.activities.recent(10) == []
    finally:
        asyncio.run(kernel.close())
