import asyncio
import json

from astcho_world.dsh_runtime import DSHAgent
from astcho_world.kernel import Kernel
from tests.test_dsh_runtime import model_server as model_server_fixture

model_server = model_server_fixture


def test_real_sdk_activity_projection_replay_and_archive(tmp_path, model_server):
    script, requests, config = model_server

    script.extend(
        [
            "第一段原始全文SECRET-A",
            [("world_activity", {"resume_note": "合成的明确继续意图"})],
            [("world_finish", {"note": "合成活动自留信息"})],
            "形成另一段合成思考",
        ]
    )

    async def scenario():
        kernel = Kernel(tmp_path)
        agent = DSHAgent(kernel, config, activity_context=True, recent_activities=1)
        try:
            await agent.run()
            first = kernel.activities.finished_ids()[0]
            await agent.run()
            assert kernel.store.get("activity_archive_error") is None
            assert "SECRET-A" not in json.dumps(requests[1])
            assert "留下内部记录" in json.dumps(requests[1], ensure_ascii=False)
            index = kernel.activities.recent(1)[0]
            assert set(index) == {"type", "logical_activity_id", "time", "apps", "brief"}
            assert index["time"]["start"] <= index["time"]["end"]
            assert "references" in kernel.activities.recall(first)["summary"]
            session_id = agent.session_state["id"]
            await agent.close()
            agent = DSHAgent(kernel, config, activity_context=True, recent_activities=1)
            await agent.run()
            assert agent.session_state["id"] == session_id
            messages = requests[3]["messages"]
            summaries = [m for m in messages if "world.activity_summary" in str(m)]
            assert len(summaries) == 1
            assert "合成活动自留信息" in json.dumps(summaries, ensure_ascii=False)
            assert len(requests) == 4
            assert first not in json.dumps(summaries)
            assert "SECRET-A" in json.dumps(kernel.activities.recall(first, full=True))
            assert kernel.store.get("activity_archive_error") is None
        finally:
            await agent.close()
            await kernel.close()

    asyncio.run(scenario())


def test_eleven_finished_activities_keep_ten_and_failed_index_keeps_raw(tmp_path, model_server):
    script, requests, config = model_server
    script.extend([f"raw-secret-{n}" for n in range(11)])
    script.extend(["unarchived-SECRET", "end"])

    async def scenario():
        kernel = Kernel(tmp_path)
        agent = DSHAgent(kernel, config, activity_context=True)
        try:
            for _ in range(11):
                await agent.run()
            first = kernel.activities.finished_ids()[0]
            original_index = kernel.activities.index

            def fail_index(_):
                raise OSError("Synthetic archive/index disk failure")

            kernel.activities.index = fail_index
            await agent.run()
            snapshot = next(
                json.loads(m["content"])
                for m in requests[11]["messages"]
                if m["role"] == "user" and '"type":"world.runtime"' in m["content"]
            )
            assert len(snapshot["recent_activities"]) == 10
            assert first not in json.dumps(snapshot)
            assert "raw-secret-0" not in json.dumps(requests[11])
            assert "raw-secret-0" in json.dumps(kernel.activities.recall(first, full=True))
            assert "Synthetic archive/index disk failure" in kernel.store.get(
                "activity_archive_error"
            )
            kernel.activities.index = original_index
            await agent.run()
            assert "unarchived-SECRET" in json.dumps(requests[12])
            assert kernel.store.get("activity_archive_error") is None
            assert len(requests) == 13
            assert (
                kernel.store.db.execute(
                    "SELECT count(*) FROM usage_calls WHERE purpose='activity_summary'"
                ).fetchone()[0]
                == 0
            )
        finally:
            await agent.close()
            await kernel.close()

    asyncio.run(scenario())


def test_pause_restart_expiry_no_model_polling_and_budget_suspend(tmp_path, model_server):
    from astcho_world.apps.qq import MANIFEST

    script, requests, config = model_server
    script.extend(
        [
            [
                (
                    "world_pause",
                    {
                        "resume_note": "可留意后续",
                        "app_id": "qq",
                        "scope": "group:test",
                        "expires_in": 30,
                    },
                )
            ],
            [("world_apps", {})],
            [("world_finish", {})],
        ]
    )

    async def scenario():
        kernel = Kernel(tmp_path)
        kernel.register(MANIFEST, config={"default_group_muted": True})
        agent = DSHAgent(kernel, config)
        try:
            await agent.run()
            activity = kernel.activities.current()
            assert activity["state"] == "waiting"
            assert len(requests) == 1
            await kernel.ingest(
                "qq",
                {
                    "batch_id": "one",
                    "chat_id": "group:test",
                    "messages": [
                        {
                            "message_id": "1",
                            "user_id": "2",
                            "nickname": "合成",
                            "text": "有后续",
                            "timestamp": 1,
                        }
                    ],
                    "random_value": 1,
                },
            )
            assert kernel.store.pending_notification_ids()
            await agent.close()
            agent = DSHAgent(kernel, config)
            result = await agent.run(max_steps=1)
            assert result["status"] == "budget_exhausted"
            assert kernel.activities.current()["id"] == activity["id"]
            assert kernel.activities.current()["state"] == "suspended"
            await agent.run()
            assert kernel.activities.current() is None
            kernel.activities.begin("new-run")
            kernel.activities.pause(
                resume_note="短等", app_id="qq", scope="group:test", expires_in=1
            )
            assert kernel.activities.tick({"qq"}, now=10**12) == "expired"
            assert kernel.activities.current() is None
            assert len(requests) == 3
        finally:
            await agent.close()
            await kernel.close()

    asyncio.run(scenario())


def test_idle_schedule_clock_sleep_and_no_busy_backlog():
    from astcho_world.wake import IdleClock, interval

    assert interval(5) is None
    assert round(interval(85)) == 131
    assert round(interval(70)) == 163
    assert interval(75) == 150
    assert interval(50) == 240
    assert round(interval(20)) == 427
    clock = IdleClock(0, adaptive=True)
    assert not clock.state(100, 5)["ready"]
    assert not clock.state(500, 100, busy=True)["ready"]
    assert not clock.state(500, 100, waiting=True)["ready"]
    clock.completed(500)
    assert not clock.state(619, 100)["ready"]
    assert clock.state(620, 100)["ready"]


def test_real_sdk_legacy_migration_same_session_idempotent(tmp_path, model_server):
    script, requests, config = model_server

    script.extend(["legacy-raw-secret", [("world_finish", {})]])

    async def scenario():
        kernel = Kernel(tmp_path)
        agent = DSHAgent(kernel, config)
        original_wake = agent._wake

        def old_wake(reasons):
            value = original_wake(reasons)
            value.pop("logical_activity_id")
            return value

        agent._wake = old_wake
        try:
            await agent.run()
            session_id = agent.session_state["id"]
            await agent.close()
            # Emulate the old World index; the SDK log itself is never edited.
            kernel.store.db.execute("DELETE FROM activity_runs")
            kernel.store.db.execute("DELETE FROM life_activities")
            kernel.store.db.commit()
            agent = DSHAgent(kernel, config, activity_context=True)
            assert (await agent.migrate_legacy())["imported"] == 1
            assert (await agent.migrate_legacy())["imported"] == 0
            await agent.close()
            agent = DSHAgent(kernel, config, activity_context=True)
            await agent.run()
            assert agent.session_state["id"] == session_id
            assert "legacy-raw-secret" not in json.dumps(requests[1])
            assert len(requests) == 2
            legacy = kernel.activities.legacy_ids()[0]
            assert "legacy-raw-secret" in json.dumps(kernel.activities.recall(legacy, full=True))
        finally:
            await agent.close()
            await kernel.close()

    asyncio.run(scenario())


def test_qq_ambient_has_only_metadata_and_reading_notification_marks_older_read(tmp_path):
    from astcho_world.apps.qq import MANIFEST

    async def scenario():
        kernel = Kernel(tmp_path)
        kernel.register(MANIFEST, config={"default_group_muted": True})
        packet = {
            "batch_id": "first",
            "chat_id": "group:test",
            "title": "合成群",
            "messages": [
                {
                    "message_id": "1",
                    "user_id": "2",
                    "nickname": "合成",
                    "text": "UNREAD_SECRET",
                    "timestamp": 1,
                },
                {
                    "message_id": "2",
                    "user_id": "2",
                    "nickname": "合成",
                    "text": "实际提及",
                    "timestamp": 1,
                    "mentioned_bot": True,
                },
            ],
            "random_value": 1,
        }
        try:
            await kernel.ingest("qq", packet)
            projection = kernel.apps["qq"].ambient
            assert projection["data"]["chats"][0]["unread"] == 2
            assert projection["data"]["chats"][0]["muted"] == 1
            assert "UNREAD_SECRET" not in json.dumps(projection)
            delivered = kernel.store.notifications()
            assert len(delivered) == 1
            await kernel.open_notification(delivered[0]["id"])
            assert kernel.apps["qq"].ambient["data"]["chats"][0]["unread"] == 0
            await kernel.call("qq", "set_notifications", {"chat_id": "group:test", "muted": False})
            await kernel.apps["qq"].stop()
            state = await kernel.call("qq", "list_chats", {})
            assert state.data["chats"][0]["muted"] == 0
        finally:
            await kernel.close()

    asyncio.run(scenario())


def test_long_activity_native_compaction_can_still_archive_on_finish(tmp_path, model_server):
    from astcho_world.apps.notes import MANIFEST

    script, requests, config = model_server
    decisions = 0

    def respond():
        nonlocal decisions
        content = requests[-1]["messages"][-1]["content"]
        if "可用消息ID：" in content:
            ids = json.loads(content.split("可用消息ID：")[-1])
            return json.dumps(
                {
                    "facts": [{"text": "实际保存了长笔记", "references": ids[-1:]}],
                    "choices": [],
                    "unfinished": [],
                    "uncertainties": [],
                }
            )
        decisions += 1
        if decisions == 1:
            return [("world_tools", {"app_id": "notes"})]
        if decisions == 2:
            return {
                "content": [
                    ("notes__save", {"title": "合成笔记", "text": "长活动原文标记" * 3000})
                ],
                "usage": {"prompt_tokens": 80000, "completion_tokens": 10, "total_tokens": 80010},
            }
        return [("world_finish", {})]

    script.extend([respond] * 20)

    async def scenario():
        kernel = Kernel(tmp_path)
        kernel.register(MANIFEST)
        agent = DSHAgent(kernel, config, activity_context=True)
        try:
            assert (await agent.run(max_steps=2))["status"] == "budget_exhausted"
            activity_id = kernel.activities.current()["id"]
            await agent.run()
            assert kernel.activities.current() is None
            assert kernel.store.get("activity_archive_error") is None
            assert "长活动原文标记" * 100 in json.dumps(
                kernel.activities.recall(activity_id, full=True), ensure_ascii=False
            )
            await agent.run()
            last_decision = requests[-1]
            assert "长活动原文标记" * 100 not in json.dumps(last_decision, ensure_ascii=False)
        finally:
            await agent.close()
            await kernel.close()

    asyncio.run(scenario())


def test_archive_file_commit_before_index_can_rebuild_without_new_summary(tmp_path):
    kernel = Kernel(tmp_path)
    try:
        item = kernel.activities.begin("synthetic-run")
        token = kernel.store.activity.set("synthetic-run")
        kernel.store.event("activity.started", "合成实际记录")
        kernel.store.observe("synthetic_read", "synthetic-source", {"text": "实际读取的合成内容"})
        kernel.store.activity.reset(token)
        kernel.activities.settle("finished")
        events = kernel.activities.archive(item["id"])["events"]
        summary = {"brief": "有来源的合成简述", "references": [events[0]["seq"]]}
        kernel.activities.summary(item["id"], summary)
        item = kernel.activities.get(item["id"])
        item["archive_state"] = "pending"
        kernel.activities.save(item)
        assert kernel.activities.archive(item["id"])["summary"] == summary
        assert kernel.activities.recent(10)[0]["brief"] == summary["brief"]
    finally:
        asyncio.run(kernel.close())


def test_local_index_maintenance_reuses_original_multi_run_archive_without_model_calls(
    tmp_path, model_server
):
    import hashlib
    from datetime import datetime

    script, requests, config = model_server

    async def scenario():
        kernel = Kernel(tmp_path)
        item = kernel.activities.begin("first-run")
        token = kernel.store.activity.set("first-run")
        kernel.store.observe("notification", "seen", {"app": "qq", "message": "observed-first-run"})
        kernel.store.activity.reset(token)
        kernel.activities.settle("suspended")
        assert kernel.activities.begin("second-run")["id"] == item["id"]
        token = kernel.store.activity.set("second-run")
        kernel.store.event(
            "decision.tool",
            "实际工具选择",
            data={
                "tool": "notes__save",
                "arguments": {"text": "observed-second-run"},
            },
        )
        kernel.store.event(
            "model.finished",
            "合成模型输出",
            data={
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "actual-model-output"}],
                },
                "stream": {"chunks": "transport-only-marker"},
                "usage": {"inputTokens": 10},
            },
        )
        kernel.store.notify(
            "bilibili",
            {
                "id": "unread",
                "source": "unread",
                "message": "UNREAD_SECRET",
                "locator": {},
            },
        )
        kernel.store.activity.reset(token)
        kernel.activities.settle("finished")
        events = kernel.activities.archive(item["id"])["events"]
        old = {"brief": "old-summary-only-marker", "references": [events[0]["seq"]]}
        kernel.activities.summary(item["id"], old)
        path = tmp_path / "experience" / "activities" / item["id"] / "events.jsonl"
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        # Unknown writes remain independently authoritative even if absent from brief prose.
        kernel.store.db.execute(
            "INSERT INTO operations(id,app,tool,request,effect,status) "
            "VALUES('unknown-write','qq','share_link','{}','write','unknown')"
        )
        kernel.store.db.commit()
        agent = DSHAgent(kernel, config, activity_context=True)
        try:
            await agent.refresh_activity_summaries()
            assert kernel.activities.recall(item["id"])["summary"] == old
            index = kernel.activities.recent(10)[0]
            assert index["apps"] == ["notes", "qq"]
            assert (
                datetime.fromisoformat(index["time"]["start"]).timestamp() <= events[0]["created"]
            )
            assert (
                datetime.fromisoformat(index["time"]["end"]).timestamp() > events[-1]["created"] - 1
            )
            assert "references" not in index and "version" not in index
            assert hashlib.sha256(path.read_bytes()).hexdigest() == digest
            assert kernel.activities.recall(item["id"], full=True)["events"] == events
            assert kernel.activities.recall(item["id"])["summary"] == old
            session_id = agent.session_state["id"]
            await agent.close()
            agent = DSHAgent(kernel, config, activity_context=True)
            await agent.refresh_activity_summaries()
            assert agent.session_state["id"] == session_id
            assert len(requests) == 0  # Maintenance and restart never call the model.
            assert (
                kernel.store.db.execute(
                    "SELECT status FROM operations WHERE id='unknown-write'"
                ).fetchone()[0]
                == "unknown"
            )
        finally:
            await agent.close()
            await kernel.close()

    asyncio.run(scenario())
