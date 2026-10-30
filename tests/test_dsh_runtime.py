"""Real installed Python SDK/native runtime against a localhost synthetic model."""

import asyncio
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from astcho_world.apps.notes import MANIFEST as NOTES
from astcho_world.apps.qq import MANIFEST as QQ
from astcho_world.contracts import Manifest, Tool, object_schema
from astcho_world.dsh_runtime import DSHAgent, DSHModelConfig
from astcho_world.kernel import Kernel
from astcho_world.speech import outbound_speech_guidance


@pytest.fixture
def model_server():
    pytest.importorskip("deepseek_harness")
    script, requests, errors = [], [], []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            try:
                data = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                requests.append({"path": self.path, **data})
                index = len(requests)
                item = script[index - 1]
                if callable(item):
                    item = item()
                usage = {"prompt_tokens": 20, "completion_tokens": 10, "total_tokens": 30}
                finish_override = None
                if isinstance(item, dict):
                    finish_override = item.get("finish_reason")
                    usage = item.get("usage", usage)
                    item = item["content"]
                delta = {"role": "assistant"}
                if isinstance(item, str):
                    delta["content"] = item
                    finish = "stop"
                else:
                    delta["tool_calls"] = [
                        {
                            "index": n,
                            "id": f"call-{index}-{n}",
                            "type": "function",
                            "function": {"name": name, "arguments": json.dumps(args)},
                        }
                        for n, (name, args) in enumerate(item)
                    ]
                    finish = "tool_calls"
                chunk = {
                    "id": f"synthetic-{index}",
                    "object": "chat.completion.chunk",
                    "created": 1,
                    "model": "world-fixture",
                    "choices": [
                        {"index": 0, "delta": delta, "finish_reason": finish_override or finish}
                    ],
                    "usage": usage,
                }
                body = ("data: " + json.dumps(chunk) + "\n\ndata: [DONE]\n\n").encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass  # Runtime cancellation may close a synthetic model stream.
            except Exception as exc:
                errors.append(str(exc))
                self.send_error(500)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield (
        script,
        requests,
        DSHModelConfig(
            "world-fixture", "synthetic-model-key", f"http://127.0.0.1:{server.server_port}/v1"
        ),
    )
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)
    assert errors == []


def test_real_sdk_discovers_tools_saves_notes_finishes_and_resumes(tmp_path, model_server):
    script, requests, config = model_server
    script.extend(
        [
            [("world_tools", {"app_id": "notes"})],
            [("notes__save", {"title": "自己的笔记", "text": "通过 DSH 保存"})],
            [("world_finish", {})],
            "本轮不行动",
            [("notes__list", {})],
            [("world_finish", {})],
        ]
    )

    async def scenario():
        kernel = Kernel(tmp_path)
        kernel.register(NOTES)
        profile = tmp_path / "profile.json"
        original = '{\r\n  "name": "星回", "text": "逐字保留。"\r\n}\r\n'
        profile.write_bytes(original.encode())
        agent = DSHAgent(kernel, config, profile_path=profile)
        try:
            first = await agent.run()
            assert first["runner"] == "dsh"
            assert first["status"] == "idle"
            assert first["steps"] == 3
            second = await agent.run()
            assert second["session_id"] == first["session_id"]
            assert second["steps"] == 1  # Plain completion must not create another model step.
            notes = await kernel.call("notes", "list", {})
            assert len(notes.data["notes"]) == 1
            assert {t["function"]["name"] for t in requests[0]["tools"]} == {
                "world_apps",
                "world_tools",
                "world_notifications",
                "world_ignore",
                "world_open",
                "web_fetch",
                "web_search",
                "skill",
                "schedule_create",
                "schedule_list",
                "schedule_update",
                "schedule_delete",
                "world_finish",
                "world_recall",
                "world_activity",
                "world_pause",
            }
            assert "notes__save" in {t["function"]["name"] for t in requests[1]["tools"]}
            assert all(r["path"] == "/v1/chat/completions" for r in requests)
            assert "helpful software engineer" not in json.dumps(requests)
            assert requests[0]["messages"][0]["role"] == "system"
            # Native plugins append their own tool guidance after World's exact
            # environment/persona block. The configured persona remains verbatim.
            assert agent.system_prompt.endswith(original)
            assert requests[0]["messages"][0]["content"].startswith(agent.system_prompt)
            # The same speaking rules reach the real SDK even with no QQ App,
            # chat observation or public-send tool installed.
            assert all(
                outbound_speech_guidance() in request["messages"][0]["content"]
                for request in requests
            )
            assert (
                kernel.store.db.execute(
                    "SELECT count(DISTINCT activity_id) FROM events WHERE kind='model.finished'"
                ).fetchone()[0]
                == 2
            )
        finally:
            await agent.close()
            await kernel.close()
        kernel = Kernel(tmp_path)
        kernel.register(NOTES)
        resumed = DSHAgent(kernel, config, profile_path=profile)
        resumed.system_prompt += "\n合成系统规则更新"
        try:
            result = await resumed.run()
            assert result["session_id"] == first["session_id"]
            assert "通过 DSH 保存" in json.dumps(requests[-1], ensure_ascii=False)
            assert outbound_speech_guidance() in requests[-1]["messages"][0]["content"]
            assert "合成系统规则更新" in requests[-1]["messages"][0]["content"]
            assert len((await kernel.call("notes", "list", {})).data["notes"]) == 1
        finally:
            await resumed.close()
            await kernel.close()

    asyncio.run(scenario())
    assert len(requests) == 6


def test_real_sdk_unknown_receipt_survives_resume_without_replaying(tmp_path, model_server):
    script, requests, config = model_server
    script.extend(
        [
            [("world_tools", {"app_id": "broken"})],
            [("broken__hang", {})],
            [("world_finish", {})],
            [("world_finish", {})],
        ]
    )
    broken = Manifest(
        app_id="broken",
        version="1",
        description="合成故障应用",
        entrypoint="astcho_world.testing:FaultApp",
        tools=[
            Tool(
                name="hang",
                description="超时写入",
                input_schema=object_schema({}),
                timeout=0.1,
                effect="write",
            )
        ],
    )

    async def scenario():
        for _ in range(2):
            kernel = Kernel(tmp_path)
            kernel.register(broken)
            agent = DSHAgent(kernel, config)
            try:
                assert (await agent.run())["status"] == "idle"
                rows = kernel.store.db.execute("SELECT status FROM operations").fetchall()
                assert [row[0] for row in rows] == ["unknown"]
                assert kernel.apps["broken"].process is None
            finally:
                await agent.close()
                await kernel.close()
        results = [
            json.loads(m["content"]) for m in requests[-1]["messages"] if m["role"] == "tool"
        ]
        assert any(value.get("status") == "unknown" for value in results)

    asyncio.run(scenario())
    assert len(requests) == 4


def test_host_defaults_to_real_sdk_and_reminder_wakes_it(tmp_path, model_server):
    from fastapi.testclient import TestClient

    from astcho_world.host import create_app

    script, requests, config = model_server
    script.append([("world_finish", {})])
    apps = tmp_path / "apps.json"
    apps.write_text('{"apps":[]}')
    data = tmp_path / "data"

    async def seed():
        kernel = Kernel(data)
        kernel.store.remind(time.time() + 0.1, "合成提醒，不规定活动")
        await kernel.close()

    asyncio.run(seed())
    app = create_app(
        config_path=apps,
        data_dir=data,
        model=config,
        bot_id="",
        ingress_token="",
        qq_transport="reverse_ws",
    )
    with TestClient(app) as client:

        async def completed():
            async with asyncio.timeout(10):
                while not app.state.kernel.store.get("environment_activity", {}).get("finished"):
                    await asyncio.sleep(0.02)
            return app.state.kernel.store.get("environment_activity")

        result = client.portal.call(completed)
        assert result["runner"] == "dsh"
        assert result["status"] == "idle"
    assert len(requests) == 1
    wakes = [
        json.loads(m["content"])
        for m in requests[0]["messages"]
        if m["role"] == "user" and m["content"].startswith("{")
    ]
    assert {"kind": "reminder"} in next(w for w in wakes if w["type"] == "world.wake")["reasons"]


def test_sdk_cancellation_closes_runtime_and_authenticated_bridge(tmp_path, model_server):
    script, requests, config = model_server
    entered, release = threading.Event(), threading.Event()

    def blocked_model():
        entered.set()
        release.wait(10)
        return "结束合成请求"

    script.append(blocked_model)

    async def scenario():
        kernel = Kernel(tmp_path)
        agent = DSHAgent(kernel, config)
        task = asyncio.create_task(agent.run())
        try:
            assert await asyncio.to_thread(entered.wait, 10)
            harness = agent.harness
            process = harness.client._proc
            socket = str(agent.directory.name) + "/world.sock"
            reader, writer = await asyncio.open_unix_connection(socket)
            writer.write(b'{"token":"incorrect","method":"describe"}\n')
            await writer.drain()
            assert "Invalid World bridge token" in (await reader.readline()).decode()
            writer.close()
            await writer.wait_closed()
            task.cancel()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 10)
            assert process.poll() is not None
            assert agent.harness is None
            assert agent.server is None
            assert agent.bridge_tasks == set()
            assert kernel.store.get("environment_activity")["status"] == "interrupted"
        finally:
            release.set()
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            await agent.close()
            await kernel.close()

    asyncio.run(scenario())
    assert len(requests) == 1


def test_real_sdk_budget_and_finish_batch_never_execute_later_actions(tmp_path, model_server):
    script, requests, config = model_server
    script.extend(
        [
            [("world_apps", {})],
            [("world_tools", {"app_id": "notes"})],
            [
                ("world_finish", {}),
                ("notes__save", {"title": "不能保存", "text": "活动已经结束"}),
                (
                    "schedule_create",
                    {"title": "不能设置", "prompt": "活动已经结束", "after_seconds": 60},
                ),
            ],
        ]
    )

    async def scenario():
        kernel = Kernel(tmp_path)
        kernel.register(NOTES)
        agent = DSHAgent(kernel, config)
        try:
            result = await agent.run(max_steps=1)
            assert result["status"] == "budget_exhausted"
            assert len(requests) == 1
            assert (await agent.run())["status"] == "idle"
            assert kernel.store.db.execute("SELECT count(*) FROM operations").fetchone()[0] == 0
        finally:
            await agent.close()
            await kernel.close()

    asyncio.run(scenario())


def test_real_sdk_does_not_read_unopened_qq_context(tmp_path, model_server):
    script, requests, config = model_server
    script.extend([[("world_ignore", {"id": "qq:group:test:trigger"})], [("world_finish", {})]])

    async def scenario():
        kernel = Kernel(tmp_path)
        kernel.register(QQ)
        agent = DSHAgent(kernel, config)
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
                            "text": "绝对不能旁路读到的内容",
                            "timestamp": 1,
                        },
                        {
                            "message_id": "trigger",
                            "user_id": "2",
                            "nickname": "朋友",
                            "text": "@星回 在吗",
                            "timestamp": 1,
                            "mentioned_bot": True,
                        },
                    ],
                },
            )
            assert (await agent.run(wake_reasons=[{"kind": "notification"}]))["status"] == "idle"
            text = json.dumps(requests, ensure_ascii=False)
            assert "绝对不能旁路读到的内容" not in text
            assert "@星回 在吗" in text
            assert kernel.store.db.execute("SELECT count(*) FROM operations").fetchone()[0] == 0
        finally:
            await agent.close()
            await kernel.close()

    asyncio.run(scenario())


def test_runtime_updates_preserve_prefix_and_requests_are_recorded(tmp_path, model_server):
    script, requests, config = model_server
    script.extend([[("world_apps", {})], [("world_finish", {})], [("world_finish", {})]])

    async def scenario():
        kernel = Kernel(tmp_path)
        agent = DSHAgent(kernel, config)
        try:
            await agent.run()
            await agent.close()
            agent = DSHAgent(kernel, config)
            await agent.run()
            all_snapshots = []
            for request in requests:
                snapshots = []
                for message in request["messages"]:
                    try:
                        item = json.loads(message["content"])
                    except (ValueError, TypeError):
                        continue
                    if item.get("type") == "world.runtime":
                        snapshots.append(item)
                all_snapshots.append(snapshots)
                assert snapshots[-1]["activity"]["remaining_including_current"] > 0
            assert [len(items) for items in all_snapshots] == [1, 2, 3]
            assert all_snapshots[0][0]["runtime_state"]["kind"] == "snapshot"
            assert all_snapshots[1][-1]["runtime_state"]["kind"] == "update"
            assert "apps" not in all_snapshots[1][-1]  # Unchanged state is not repeated.
            assert all_snapshots[2][-1]["runtime_state"]["kind"] == "snapshot"
            assert requests[1]["messages"][:len(requests[0]["messages"])] == requests[0]["messages"]
            assert requests[2]["messages"][:len(requests[1]["messages"])] == requests[1]["messages"]
            recorded = [
                json.loads(row[0])
                for row in kernel.store.db.execute(
                    "SELECT data FROM events WHERE kind='debug.model_request' ORDER BY seq"
                )
            ]
            from astcho_world.context_log import expand_input

            assert len(recorded) == len(requests)
            expanded = expand_input(kernel.store, recorded[0])
            assert (
                expanded["messages"][0]["content"][0]["text"]
                == requests[0]["messages"][0]["content"]
            )
            assert all(r["messages"] and r["tools"] for r in recorded)
            assert (
                kernel.store.db.execute(
                    "SELECT count(*) FROM events WHERE kind='debug.model_usage'"
                ).fetchone()[0]
                == 3
            )
            events = [
                json.loads(row[0])
                for row in kernel.store.db.execute(
                    "SELECT data FROM events WHERE kind='debug.dsh_event'"
                )
            ]
            assert not any(
                e.get("type") == "developer/message"
                and e.get("surfaceOp", {}).get("op") == "replace"
                for e in events
                if isinstance(e.get("surfaceOp"), dict)
            )
        finally:
            await agent.close()
            await kernel.close()

    asyncio.run(scenario())


def test_real_compaction_preserves_experience_and_exact_unknown_receipts(tmp_path, model_server):
    script, requests, config = model_server

    def summary():
        instruction = requests[-1]["messages"][-1]["content"]
        ids = json.loads(instruction.split("可用消息ID：")[-1])
        return json.dumps(
            {
                "facts": [{"text": "观察过一段合成内容。", "references": ids[-1:]}],
                "choices": [],
                "unfinished": [],
                "uncertainties": [],
            },
            ensure_ascii=False,
        )

    script.extend(["经历过的合成内容。" * 1600, summary, [("world_finish", {})]])

    async def scenario():
        kernel = Kernel(tmp_path)
        kernel.register(QQ)
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
                        "text": "压缩也不能看到的未读秘密",
                        "timestamp": 1,
                    },
                    {
                        "message_id": "trigger",
                        "user_id": "2",
                        "nickname": "朋友",
                        "text": "单条通知",
                        "timestamp": 1,
                        "mentioned_bot": True,
                    },
                ],
            },
        )
        # A previously attempted own write: the result stays unknown through condensation.
        kernel.store.db.execute(
            "INSERT INTO operations(id,app,tool,request,effect,status) VALUES(?,?,?,?,?,?)",
            ("observed-operation", "notes", "save", "{}", "write", "unknown"),
        )
        kernel.store.db.commit()
        agent = DSHAgent(kernel, config)
        try:
            await agent.run()
            result = await agent.compact()
            assert result["steps"] == 0
            # Maintenance must not deliver pending notifications or ask for a decision.
            assert len(requests) == 2
            await agent.close()
            agent = DSHAgent(kernel, config)
            await agent.run()
            messages = requests[-1]["messages"]
            text = json.dumps(messages, ensure_ascii=False)
            assert "压缩也不能看到的未读秘密" not in json.dumps(requests, ensure_ascii=False)
            assert "观察过一段合成内容" in text
            assert "Continue the task directly" not in text
            assert "observed-operation" in text and "unknown" in text
            assert "经历过的合成内容。" * 100 not in text
            events = [
                json.loads(row[0])
                for row in kernel.store.db.execute(
                    "SELECT data FROM events WHERE kind='debug.dsh_event'"
                )
            ]
            compacted = next(e for e in events if e["type"] == "compaction/summary")
            body = json.loads(compacted["data"]["summary"][0]["text"])
            assert body["operation_results"] == [
                {
                    "operation_id": "observed-operation",
                    "app": "notes",
                    "tool": "save",
                    "status": "unknown",
                }
            ]
            assert any(e["type"] == "compaction/end" for e in events)
            # The original history remains available through the observation-only recall tool.
            records = await agent.tools.execute(
                "world_recall", {"query": "经历过的合成内容"}, "recall"
            )
            assert records["items"]
            original = await agent.tools.execute(
                "world_recall", {"event_seq": records["items"][0]["seq"]}, "read"
            )
            assert "经历过的合成内容。" * 100 in json.dumps(original, ensure_ascii=False)
        finally:
            await agent.close()
            await kernel.close()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "output",
    [
        "invalid JSON",
        {
            "content": json.dumps(
                {"facts": [], "choices": [], "unfinished": [], "uncertainties": []}
            ),
            "finish_reason": "length",
        },
        [
            (
                "schedule_create",
                {"after_seconds": 3600, "title": "无效调用", "prompt": "压缩不能执行的提醒"},
            )
        ],
        json.dumps(
            {
                "facts": [{"text": "不能写入的虚构经历", "references": ["not-observed"]}],
                "choices": [],
                "unfinished": [],
                "uncertainties": [],
            }
        ),
    ],
)
def test_invalid_summary_does_not_replace_history(tmp_path, model_server, output):
    script, requests, config = model_server
    original = "原始观察内容必须能够恢复。" * 1000
    script.extend([original, output, [("world_finish", {})]])

    async def scenario():
        kernel = Kernel(tmp_path)
        agent = DSHAgent(kernel, config)
        try:
            await agent.run()
            kernel.store.notify(
                "qq", {"id": "new", "source": "群", "message": "压缩不应送达通知", "locator": {}}
            )
            with pytest.raises(RuntimeError, match="did not complete"):
                await agent.compact()
            assert kernel.store.pending_notification_ids() == {"qq:new"}
            await agent.run()
            assert original in json.dumps(requests[-1], ensure_ascii=False)
            assert "不能写入的虚构经历" not in json.dumps(requests[-1], ensure_ascii=False)
            summaries = kernel.store.db.execute(
                "SELECT count(*) FROM events WHERE kind='debug.dsh_event' "
                "AND json_extract(data,'$.type')='compaction/summary'"
            ).fetchone()[0]
            assert summaries == 0
            assert kernel.store.db.execute("SELECT count(*) FROM reminders").fetchone()[0] == 0
        finally:
            await agent.close()
            await kernel.close()

    asyncio.run(scenario())


def test_automatic_pressure_keeps_history_when_summary_cannot_shrink(tmp_path, model_server):
    script, requests, config = model_server
    decisions = 0

    def respond():
        nonlocal decisions
        content = requests[-1]["messages"][-1]["content"]
        if "可用消息ID：" in content:
            ids = json.loads(content.split("可用消息ID：")[-1])
            return json.dumps(
                {
                    "facts": [{
                        "text": "较早的合成经历已整理。" + "无法缩短的合成摘要" * 20000,
                        "references": ids[-1:],
                    }],
                    "choices": [],
                    "unfinished": [],
                    "uncertainties": [],
                },
                ensure_ascii=False,
            )
        decisions += 1
        if decisions == 8:
            return [("world_finish", {})]
        return {
            "content": f"第{decisions}次读到的合成经历。" * 500,
            "usage": {
                "prompt_tokens": 2000 + (decisions - 1) * 8000,
                "completion_tokens": 7000,
                "total_tokens": 9000 + (decisions - 1) * 8000,
            },
        }

    script.extend([respond] * 64)

    async def scenario():
        kernel = Kernel(tmp_path)
        agent = DSHAgent(kernel, config)
        try:
            for _ in range(8):
                await agent.run()
            events = [
                json.loads(row[0])
                for row in kernel.store.db.execute(
                    "SELECT data FROM events WHERE kind='debug.dsh_event'"
                )
            ]
            assert any(e["type"] == "compaction/start" for e in events)
            assert any(
                e["type"] == "compaction/end" and "not smaller" in e["data"].get("error", "")
                for e in events
            )
            assert "较早的合成经历已整理" not in json.dumps(requests[-1], ensure_ascii=False)
            assert "第1次读到的合成经历。" * 100 in json.dumps(requests[-1], ensure_ascii=False)
            assert "第7次读到的合成经历。" * 100 in json.dumps(requests[-1], ensure_ascii=False)
        finally:
            await agent.close()
            await kernel.close()

    asyncio.run(scenario())


def test_maintenance_inspection_does_not_pay_for_pressure_compaction(tmp_path, model_server):
    script, requests, config = model_server
    script.extend(
        [
            {
                "content": "actual-observation" * 100,
                # Above native pressure threshold, still below the provider window.
                "usage": {"prompt_tokens": 52000, "completion_tokens": 10, "total_tokens": 52010},
            },
            [("world_finish", {})],
        ]
    )

    async def scenario():
        kernel = Kernel(tmp_path)
        agent = DSHAgent(kernel, config)
        try:
            await agent.run()
            first = await agent.inspect_context()
            assert first and len(requests) == 1
            await agent.close()
            agent = DSHAgent(kernel, config)
            assert await agent.inspect_context()
            assert len(requests) == 1
            await agent.run()
            assert len(requests) == 2
            assert "actual-observation" in json.dumps(requests[-1])
        finally:
            await agent.close()
            await kernel.close()

    asyncio.run(scenario())


def test_pressure_can_include_retained_large_tool_pair_after_checkpoint(tmp_path, model_server):
    import sqlite3

    script, requests, config = model_server
    decisions = 0
    note_id = None

    def respond():
        nonlocal decisions
        text = requests[-1]["messages"][-1]["content"]
        if "可用消息ID：" in text:
            assert not requests[-1].get("tools")
            ids = json.loads(text.split("可用消息ID：")[-1])
            return json.dumps(
                {
                    "facts": [{"text": "保留关键笔记事实", "references": ids[-1:]}],
                    "choices": [],
                    "unfinished": [],
                    "uncertainties": [],
                }
            )
        decisions += 1
        if decisions == 1:
            return [("world_tools", {"app_id": "notes"})]
        if decisions == 2:
            return "较早经历" * 1200
        if decisions == 3:
            return {
                "content": [("notes__read", {"id": note_id})],
                "usage": {"prompt_tokens": 52000, "completion_tokens": 10, "total_tokens": 52010},
            }
        return [("world_finish", {})]

    script.extend([respond] * 20)

    async def scenario():
        nonlocal note_id
        kernel = Kernel(tmp_path)
        kernel.register(NOTES)
        raw = "完整笔记原文" * 12000
        note_id = (
            await kernel.call("notes", "save", {"title": "测试", "text": "placeholder"})
        ).data["id"]
        await kernel.apps["notes"].stop()
        # A legacy/imported note can exceed the current write form's size;
        # read has no text-length cap. The fixture exercises an atomic large result.
        with sqlite3.connect(tmp_path / "apps/notes/notes.sqlite3") as db:
            db.execute("UPDATE notes SET text=? WHERE id=?", (raw, note_id))
        agent = DSHAgent(kernel, config)
        try:
            await agent.run()
            await agent.compact()
            await agent.run()
            final = json.dumps(requests[-1], ensure_ascii=False)
            assert "保留关键笔记事实" in final
            assert "完整笔记原文" * 100 not in final
            summaries = [
                json.loads(r[0])["data"]
                for r in kernel.store.db.execute(
                    "SELECT data FROM events WHERE kind='debug.dsh_event' "
                    "AND json_extract(data,'$.type')='compaction/summary' ORDER BY seq"
                )
            ]
            assert len(summaries) >= 2
            assert (await kernel.call("notes", "read", {"id": note_id})).data["text"] == raw
            await agent.close()
            agent = DSHAgent(kernel, config)
            assert (await agent.inspect_context())["nodes"][-1]["balance"] == 0
        finally:
            await agent.close()
            await kernel.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("cached", [800, 0])
def test_real_sdk_usage_cache_pricing_and_request_link(tmp_path, model_server, cached):
    from decimal import Decimal

    from astcho_world.usage import provider_id

    script, requests, config = model_server
    script.append(
        {
            "content": "安静结束",
            "usage": {
                "prompt_tokens": 1000,
                "completion_tokens": 100,
                "total_tokens": 1100,
                "prompt_tokens_details": {"cached_tokens": cached},
                "prompt_cache_hit_tokens": cached,
                "prompt_cache_miss_tokens": 1000 - cached,
            },
        }
    )

    async def scenario():
        kernel = Kernel(tmp_path)
        kernel.store.usage.add_rate(
            {
                "provider": provider_id(config.base_url),
                "model": config.model,
                "currency": "CNY",
                "input_price": "2",
                "cached_price": "0.2",
                "output_price": "6",
            }
        )
        agent = DSHAgent(kernel, config)
        try:
            await agent.run()
            data = kernel.store.usage.report()
            assert data["totals"]["calls"] == len(requests) == 1
            row = data["items"][0]
            assert row["input_tokens"] == 1000
            assert row["output_tokens"] == 100
            assert row["cached_tokens"] == cached
            assert data["totals"]["unknown_cache"] == 0
            if cached == 0:
                assert "cacheReadTokens" not in row["raw"]
            assert row["status"] == "succeeded"
            assert row["context_seq"] is not None
            assert row["activity_id"] is not None
            assert Decimal(row["cost"]) == (Decimal("0.00116") if cached else Decimal("0.0026"))
        finally:
            await agent.close()
            await kernel.close()

    asyncio.run(scenario())
