import asyncio
import json
import sqlite3
from pathlib import Path

from fastapi.testclient import TestClient

from astcho_world.apps.notes import MANIFEST as NOTES
from astcho_world.contracts import Manifest
from astcho_world.dashboard import create_dashboard_app
from astcho_world.kernel import Kernel

TOKEN = "synthetic-dashboard-access-token"
EXAMPLES = Path(__file__).resolve().parents[1] / "examples/apps.json"


def connect(client):
    assert client.post("/api/session", json={"token": TOKEN}).status_code == 200


def dump(path):
    with sqlite3.connect(path) as db:
        return list(db.iterdump())


def test_dashboard_auth_and_reads_do_not_change_agent_knowledge(tmp_path):
    app = create_dashboard_app(config_path=EXAMPLES, data_dir=tmp_path, token=TOKEN, demo=True)
    with TestClient(app) as client:
        assert client.get("/").status_code == 200
        assert client.get("/api/snapshot").status_code == 401
        assert client.get("/api/events").status_code == 401
        assert client.get("/api/stream").status_code == 401
        assert client.post("/api/session", json={"token": "wrong"}).status_code == 401
        assert client.get("/", headers={"Host": "attacker.example"}).status_code == 400
        connect(client)
        world_before = dump(tmp_path / "world.sqlite3")
        qq_before = dump(tmp_path / "apps/qq/qq.sqlite3")
        for _ in range(2):
            data = client.get("/api/snapshot").json()
            assert data["mode"] == "demo"
            assert len(data["apps"]) == 3
            assert data["counts"]["operations"] == 2
            assert client.get("/api/events").status_code == 200
            for name in ("qq", "notes", "library"):
                assert client.get(f"/api/apps/{name}/views/records").status_code == 200
        assert dump(tmp_path / "world.sqlite3") == world_before
        assert dump(tmp_path / "apps/qq/qq.sqlite3") == qq_before
        # Views never expose undeclared data sources or execute tools.
        assert client.get("/api/apps/notes/views/secret").status_code == 404
        assert client.post("/api/apps/notes/save", json={"title": "x"}).status_code == 404


def test_events_pagination_activity_correlation_and_search(tmp_path):
    app = create_dashboard_app(config_path=EXAMPLES, data_dir=tmp_path, token=TOKEN, demo=True)
    with TestClient(app) as client:
        connect(client)
        latest = client.get("/api/events?limit=4").json()
        older = client.get(f"/api/events?limit=4&before={latest['next_before']}").json()
        assert len(latest["items"]) == len(older["items"]) == 4
        assert min(e["seq"] for e in latest["items"]) > max(e["seq"] for e in older["items"])
        activity_id = latest["items"][0]["activity_id"]
        trace = client.get("/api/events", params={"activity_id": activity_id}).json()["items"]
        assert {e["kind"] for e in trace} >= {
            "activity.started",
            "activity.finished",
            "model.finished",
            "operation.finished",
        }
        assert {e["activity_id"] for e in trace} == {activity_id}
        filtered = client.get("/api/events?app_id=qq").json()["items"]
        assert filtered and all(e["app"] == "qq" for e in filtered)
        found = client.get("/api/events", params={"q": "观星"}).json()["items"]
        assert found


def test_unmodified_plugin_gets_generic_ui_and_declared_view(tmp_path):
    data = NOTES.model_dump()
    data.update(
        app_id="journal",
        dashboard={
            "version": 1,
            "name": "日记",
            "icon": "J",
            "views": [
                {
                    "id": "saved",
                    "title": "保存记录",
                    "source": "operations",
                    "component": "cards",
                    "columns": {"tool": "动作"},
                }
            ],
        },
    )
    manifest = Manifest.model_validate(data)
    (tmp_path / "journal.json").write_text(manifest.model_dump_json())
    (tmp_path / "bare.json").write_text(
        NOTES.model_copy(update={"app_id": "bare", "dashboard": None}).model_dump_json()
    )
    config = tmp_path / "apps.json"
    config.write_text(
        json.dumps({"apps": [{"manifest": "journal.json"}, {"manifest": "bare.json"}]})
    )
    app = create_dashboard_app(config_path=config, data_dir=tmp_path / "data", token=TOKEN)
    with TestClient(app) as client:
        connect(client)
        data = client.get("/api/snapshot").json()
        assert {a["app_id"] for a in data["apps"]} == {"journal", "bare"}
        assert data["apps"][1]["dashboard"]["name"] == "bare"
        assert client.get("/api/apps/journal/views/saved").json()["view"]["component"] == "cards"
        # Viewing an application must not start its process.
        assert all(a["state"] == "ready" for a in client.get("/api/snapshot").json()["apps"])


def test_durable_events_survive_restart_and_redact_secrets(tmp_path):
    async def record():
        kernel = Kernel(tmp_path)
        kernel.store.event(
            "custom.future_event",
            "未来插件事件",
            app="future",
            data={"api_key": "not-public", "nested": {"access_token": "hidden"}},
        )
        await kernel.close()

    asyncio.run(record())
    app = create_dashboard_app(config_path=EXAMPLES, data_dir=tmp_path, token=TOKEN)
    with TestClient(app) as client:
        connect(client)
        data = client.get("/api/events").json()["items"][0]
        assert data["kind"] == "custom.future_event"
        assert data["data"] == {"api_key": "••••••", "nested": {"access_token": "••••••"}}


def test_pending_notification_stays_pending_when_admin_reads(tmp_path):
    async def prepare():
        kernel = Kernel(tmp_path)
        kernel.store.notify(
            "qq",
            {
                "id": "unread",
                "source": "测试群",
                "message": "尚未送达星回",
                "locator": {"chat_id": "group:test"},
            },
        )
        await kernel.close()

    asyncio.run(prepare())
    app = create_dashboard_app(config_path=EXAMPLES, data_dir=tmp_path, token=TOKEN)
    with TestClient(app) as client:
        connect(client)
        before = dump(tmp_path / "world.sqlite3")
        result = client.get("/api/snapshot").json()
        assert result["notifications"][0]["state"] == "pending"
        assert result["counts"]["observations"] == 0
        assert client.get("/api/apps/qq/views/records").status_code == 200
        assert dump(tmp_path / "world.sqlite3") == before


def test_notification_opener_is_declared_by_app(tmp_path):
    async def scenario():
        kernel = Kernel(tmp_path)
        kernel.register(NOTES.model_copy(update={"notification_tool": "read"}))
        try:
            saved = await kernel.call("notes", "save", {"title": "测试", "text": "可打开的笔记"})
            kernel.store.notify(
                "notes",
                {
                    "id": "note",
                    "source": "笔记",
                    "message": "笔记更新",
                    "locator": {"id": saved.data["id"]},
                },
            )
            kernel.store.notifications()
            result = await kernel.open_notification("notes:note")
            assert result.status == "succeeded"
            assert result.data["text"] == "可打开的笔记"
        finally:
            await kernel.close()

    asyncio.run(scenario())


def test_debug_logs_are_authenticated_bounded_and_read_only(tmp_path, monkeypatch):
    monkeypatch.setenv("ASTCHO_WORLD_API_KEY", "synthetic-secret-value")
    folder = tmp_path / "launcher"
    folder.mkdir()
    (folder / "server.log").write_text("x" * 150000 + "\nsynthetic-secret-value\n")
    app = create_dashboard_app(config_path=EXAMPLES, data_dir=tmp_path, token=TOKEN)
    with TestClient(app) as client:
        assert client.get("/api/logs").status_code == 401
        connect(client)
        before = dump(tmp_path / "world.sqlite3")
        log = client.get("/api/logs").json()
        assert log["truncated"] is True
        assert "synthetic-secret-value" not in log["text"]
        assert "••••••" in log["text"]
        assert client.get("/api/logs", params={"source": "../world.sqlite3"}).status_code == 404
        assert client.get("/api/logs?source=notes").json()["exists"] is False
        assert dump(tmp_path / "world.sqlite3") == before
        assert client.get("/assets/astcho-desk.png").headers["content-type"] == "image/png"


def test_debug_request_matches_actual_input_and_decisions_link_results(tmp_path):
    from astcho_world.runtime import ENVIRONMENT_PROMPT, Agent

    class Model:
        async def decide(self, messages, tools):
            event = kernel.store.db.execute(
                "SELECT data FROM events WHERE kind='debug.model_request' ORDER BY seq DESC"
            ).fetchone()
            recorded = json.loads(event[0])
            assert recorded["messages"] == messages
            assert recorded["tools"] == tools
            assert recorded["request_id"]  # Accounting metadata does not change model inputs.
            assert messages[0] == {"role": "system", "content": ENVIRONMENT_PROMPT}
            return {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": "test-call",
                        "type": "function",
                        "function": {"name": "world_finish", "arguments": "{}"},
                    }
                ],
            }

    async def run():
        nonlocal kernel
        kernel = Kernel(tmp_path)
        try:
            await Agent(kernel, Model()).run()
        finally:
            await kernel.close()

    kernel = None
    asyncio.run(run())
    app = create_dashboard_app(config_path=EXAMPLES, data_dir=tmp_path, token=TOKEN)
    with TestClient(app) as client:
        connect(client)
        items = client.get("/api/events?decisions=true").json()["items"]
        assert {e["kind"] for e in items} == {"decision.tool", "decision.result"}
        assert {e["data"]["call_id"] for e in items} == {"test-call"}
        assert len({e["activity_id"] for e in items}) == 1


def test_context_view_is_authenticated_redacted_and_read_only(tmp_path, monkeypatch):
    monkeypatch.setenv("SYNTHETIC_API_KEY", "private-context-secret")
    kernel = Kernel(tmp_path)
    kernel.store.event(
        "debug.model_request",
        "DSH 实际输入",
        data={
            "request_id": "recorded-request",
            "runner": "dsh",
            "purpose": "compaction",
            "model": "synthetic",
            "messages": [{"role": "system", "content": "合成提示 private-context-secret"}],
            "tools": [],
            "characters_by_role": {"system": 30},
        },
    )
    seq = kernel.store.db.execute("SELECT max(seq) FROM events").fetchone()[0]
    kernel.store.event(
        "debug.model_usage",
        "用量",
        data={
            "request_id": "recorded-request",
            "usage": {"inputTokens": 20, "outputTokens": 10},
        },
    )
    asyncio.run(kernel.close())
    app = create_dashboard_app(config_path=EXAMPLES, data_dir=tmp_path, token=TOKEN)
    with TestClient(app) as client:
        assert client.get("/api/context").status_code == 401
        connect(client)
        before = dump(tmp_path / "world.sqlite3")
        listing = client.get("/api/context?limit=1").json()
        item = listing["items"][0]
        assert "input" not in item
        assert item["usage"]["inputTokens"] == 20
        assert item["purpose"] == "compaction"
        assert listing["input_budget"] is None
        detail = client.get(f"/api/context?seq={seq}").json()
        assert "合成提示" in json.dumps(detail, ensure_ascii=False)
        assert "private-context-secret" not in json.dumps(detail)
        assert client.get(f"/api/context?before={seq}").json()["items"] == []
        assert dump(tmp_path / "world.sqlite3") == before


def test_request_storage_deduplicates_without_losing_or_truncating_input(tmp_path):
    from astcho_world.context_log import expand_input, record_input

    kernel = Kernel(tmp_path)
    payload = {
        "request_id": "one",
        "messages": [{"role": "user", "content": "完整观察内容" * 10000}],
        "tools": [{"name": "world_recall"}],
    }
    record_input(kernel.store, payload)
    record_input(kernel.store, {**payload, "request_id": "two"})
    assert kernel.store.db.execute("SELECT count(*) FROM context_fragments").fetchone()[0] == 2
    asyncio.run(kernel.close())
    kernel = Kernel(tmp_path)
    try:
        records = [
            json.loads(row[0])
            for row in kernel.store.db.execute(
                "SELECT data FROM events WHERE kind='debug.model_request' ORDER BY seq"
            )
        ]
        assert expand_input(kernel.store, records[0])["messages"] == payload["messages"]
        assert expand_input(kernel.store, records[1])["messages"] == payload["messages"]
        assert records[0]["messages"] == records[1]["messages"]
        assert records[0]["request_id"] != records[1]["request_id"]
    finally:
        asyncio.run(kernel.close())


def test_notification_pages_are_authenticated_complete_and_do_not_mark_read(tmp_path):
    app = create_dashboard_app(config_path=EXAMPLES, data_dir=tmp_path, token=TOKEN)
    with TestClient(app) as client:
        assert client.get("/api/notifications").status_code == 401
        connect(client)
        store = app.state.kernel.store
        for i in range(45):
            client.portal.call(
                store.notify,
                "notes",
                {
                    "id": f"page-{i}",
                    "source": "synthetic",
                    "message": f"fixture {i}",
                    "locator": {"index": i},
                },
            )
        before = dump(tmp_path / "world.sqlite3")
        first = client.get("/api/notifications?limit=21").json()["items"]
        assert len(first) == 21
        cursor = first[19]["cursor"]
        second = client.get("/api/notifications", params={"before": cursor, "limit": 21}).json()[
            "items"
        ]
        last = client.get(
            "/api/notifications", params={"before": second[19]["cursor"], "limit": 21}
        ).json()["items"]
        ids = [r["id"] for r in first[:20] + second[:20] + last]
        assert len(ids) == len(set(ids)) == 45
        assert all(r["state"] == "pending" for r in first + second + last)
        assert isinstance(first[0]["locator"], dict)
        assert dump(tmp_path / "world.sqlite3") == before
        assert client.get("/assets/pagination.js").status_code == 200
        client.portal.call(
            store.notify,
            "notes",
            {
                "id": "arrived-later",
                "source": "synthetic",
                "message": "new fixture",
                "locator": {},
            },
        )
        again = client.get("/api/notifications", params={"before": cursor, "limit": 21}).json()[
            "items"
        ]
        assert again == second
