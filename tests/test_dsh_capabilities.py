"""Shipped DSH tools, real SDK/native process, synthetic local HTTP endpoints only."""

import asyncio
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from astcho_world.dashboard import snapshot
from astcho_world.dsh_runtime import DSHAgent
from astcho_world.kernel import Kernel
from tests.test_dsh_runtime import model_server as model_server_fixture

model_server = model_server_fixture


@pytest.fixture
def web_server():
    seen = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            seen.append({"method": "GET", "path": self.path})
            if self.path.endswith("/slow"):
                time.sleep(0.25)
            self.reply(
                b"<html><title>Local page</title><body><h1>Page fixture</h1>"
                b"<p>Visible web fact.</p></body></html>",
                "text/html; charset=utf-8",
            )

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            seen.append(
                {
                    "method": "POST",
                    "path": self.path,
                    "body": body,
                    "key": self.headers.get("x-api-key"),
                }
            )
            self.reply(
                json.dumps(
                    {
                        "content": [
                            {
                                "type": "web_search_tool_result",
                                "content": [
                                    {
                                        "type": "web_search_result",
                                        "url": "https://example.test/result",
                                        "title": "Fixture result",
                                    }
                                ],
                            },
                            {
                                "type": "text",
                                "text": "Summary",
                                "citations": [
                                    {
                                        "url": "https://example.test/result",
                                        "cited_text": "Search citation fact.",
                                    }
                                ],
                            },
                        ]
                    }
                ).encode(),
                "application/json",
            )

        def reply(self, body, content_type):
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}", seen
    server.shutdown()
    server.server_close()
    thread.join(2)


def test_native_web_search_fetch_and_skill_are_observed_on_demand(
    tmp_path, model_server, web_server, monkeypatch
):
    script, requests, config = model_server
    base, seen = web_server
    monkeypatch.setenv("DEEPSEEK_API_KEY", "fixture-search-key")
    monkeypatch.setenv("DEEPSEEK_SEARCH_BASE_URL", base + "/anthropic/v1")
    # Exercise the shipped public-URL provider through a local synthetic proxy;
    # do not turn off its private-address rejection to accommodate the test.
    monkeypatch.setenv("HTTP_PROXY", base)
    monkeypatch.setenv("http_proxy", base)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    skills = tmp_path / "skills"
    (skills / "fixture").mkdir(parents=True)
    (skills / "fixture" / "SKILL.md").write_text(
        "---\nname: fixture\ndescription: Synthetic skill description\n---\n"
        "Full skill body only after deliberate reading.\n"
    )
    script.extend(
        [
            [("web_fetch", {"url": "http://example.com/page"})],
            [("web_search", {"queries": ["synthetic query"]})],
            [("skill", {"name": "fixture"})],
            [("world_finish", {})],
        ]
    )

    async def scenario():
        kernel = Kernel(tmp_path / "world")
        agent = DSHAgent(kernel, config, skills=skills)
        try:
            assert (await agent.run())["status"] == "idle"
            first = json.dumps(requests[0], ensure_ascii=False)
            assert "Synthetic skill description" in first
            assert "Full skill body" not in first
            assert "Visible web fact." not in first
            assert "Visible web fact." in json.dumps(requests[1])
            assert "Search citation fact." in json.dumps(requests[2]), [
                m for m in requests[2]["messages"] if m["role"] == "tool"
            ]
            assert "Full skill body only after deliberate reading." in json.dumps(requests[3])
            rows = kernel.store.db.execute("SELECT tool,status FROM operations").fetchall()
            assert [tuple(r) for r in rows] == [
                ("web_fetch", "succeeded"),
                ("web_search", "succeeded"),
                ("skill", "succeeded"),
            ]
            assert (
                kernel.store.db.execute(
                    "SELECT count(*) FROM observations WHERE kind='tool'"
                ).fetchone()[0]
                == 3
            )
            assert kernel.store.get("dsh_capabilities")["web_search"]["configured"] is True
        finally:
            await agent.close()
            await kernel.close()

    asyncio.run(scenario())
    assert [(r["method"], r["path"]) for r in seen] == [
        ("GET", "http://example.com/page"),
        ("POST", "/anthropic/v1/messages"),
    ]
    assert seen[1]["key"] == "fixture-search-key"
    assert seen[1]["body"]["tools"][0]["name"] == "web_search"


def test_native_reminder_survives_restart_delivers_notification_without_model_run(
    tmp_path, model_server
):
    script, requests, config = model_server
    script.extend(
        [
            [
                (
                    "schedule_create",
                    {"after_seconds": 2, "title": "留给自己", "prompt": "到期仍可不处理"},
                )
            ],
            [("world_finish", {})],
            [("world_finish", {})],
        ]
    )

    async def scenario():
        kernel = Kernel(tmp_path)
        agent = DSHAgent(kernel, config)
        try:
            await agent.run()
            reminders = snapshot(kernel, "live", [])["reminders"]
            assert len(reminders) == 1
            identity = reminders[0]["id"]
            assert reminders[0]["backend"] == "dsh"
            assert not reminders[0]["fired"]
            assert kernel.store.db.execute("SELECT count(*) FROM reminders").fetchone()[0] == 0
        finally:
            await agent.close()
            await kernel.close()
        await asyncio.sleep(2.1)
        kernel = Kernel(tmp_path)
        agent = DSHAgent(kernel, config)
        try:
            await agent.start()
            async with asyncio.timeout(10):
                while not kernel.store.pending_notification_ids():
                    await asyncio.sleep(0.02)
            assert len(requests) == 2  # Native timer didn't spend a model call.
            assert (
                kernel.store.db.execute(
                    "SELECT count(*) FROM observations WHERE kind='notification'"
                ).fetchone()[0]
                == 0
            )
            async with asyncio.timeout(5):
                while not snapshot(kernel, "live", [])["reminders"][0]["fired"]:
                    await asyncio.sleep(0.02)
            assert snapshot(kernel, "live", [])["reminders"][0]["id"] == identity
            # Emulate a crash after the native acknowledgment but before World's
            # notification write. Reconciliation must relay the saved receipt.
            kernel.store.db.execute("DELETE FROM notifications")
            kernel.store.db.commit()
            await agent.close()
            agent = DSHAgent(kernel, config)
            await agent.start()
            assert kernel.store.pending_notification_ids()
            assert len(requests) == 2
            await agent.run(wake_reasons=[{"kind": "reminder"}])
            assert "到期仍可不处理" in json.dumps(requests[-1], ensure_ascii=False)
            assert kernel.store.db.execute("SELECT count(*) FROM notifications").fetchone()[0] == 1
        finally:
            await agent.close()
            await kernel.close()
        kernel = Kernel(tmp_path)
        agent = DSHAgent(kernel, config)
        try:
            await agent.start()
            await asyncio.sleep(0.2)
            assert not kernel.store.pending_notification_ids()
            assert kernel.store.db.execute("SELECT count(*) FROM notifications").fetchone()[0] == 1
            assert len(requests) == 3
        finally:
            await agent.close()
            await kernel.close()

    asyncio.run(scenario())


def test_legacy_reminder_migration_is_idempotent_and_keeps_original_data(tmp_path, model_server):
    _, requests, config = model_server

    async def scenario():
        kernel = Kernel(tmp_path)
        due = time.time() + 3600
        original = "  保留原提醒文字与时间\n"
        kernel.store.remind(due, original, reminder_id="legacy-fixture")
        for _ in range(2):
            agent = DSHAgent(kernel, config)
            try:
                await agent.start()
                assert not requests
                rows = [dict(r) for r in kernel.store.db.execute("SELECT * FROM reminders")]
                assert rows == [{"id": "legacy-fixture", "due": due, "text": original, "fired": 2}]
                assert len(kernel.store.get("dsh_reminders")) == 1
                assert len(snapshot(kernel, "live", [])["reminders"]) == 1
            finally:
                await agent.close()
        await kernel.close()

    asyncio.run(scenario())


def test_missing_search_credential_and_blocked_fetch_are_truthful_failures(
    tmp_path, model_server, web_server, monkeypatch
):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    script, requests, config = model_server
    base, seen = web_server
    script.extend(
        [
            [("web_search", {"queries": ["unconfigured search"]})],
            [("web_fetch", {"url": base + "/private"})],
            [("world_finish", {})],
        ]
    )

    async def scenario():
        kernel = Kernel(tmp_path)
        agent = DSHAgent(kernel, config)
        try:
            await agent.run()
            assert not kernel.store.get("dsh_capabilities")["web_search"]["configured"]
            tool_results = [m["content"] for m in requests[-1]["messages"] if m["role"] == "tool"]
            assert "no API key" in tool_results[0]
            assert "non-public IP address" in tool_results[1]
            assert [r[0] for r in kernel.store.db.execute("SELECT status FROM operations")] == [
                "failed",
                "failed",
            ]
            assert all("browser" not in t["function"]["name"] for t in requests[0]["tools"])
            assert seen == []
        finally:
            await agent.close()
            await kernel.close()

    asyncio.run(scenario())


def test_native_timer_arriving_during_activity_uses_serialized_world_wake(tmp_path, model_server):
    script, requests, config = model_server

    def let_timer_expire():
        time.sleep(1.3)
        return [("world_finish", {})]

    script.extend(
        [
            [
                (
                    "schedule_create",
                    {"after_seconds": 1, "title": "活动中到期", "prompt": "仍然只是提醒"},
                )
            ],
            let_timer_expire,
            [("world_finish", {})],
        ]
    )

    async def scenario():
        kernel = Kernel(tmp_path)
        agent = DSHAgent(kernel, config)
        try:
            result = await agent.run()
            assert result["steps"] == 2
            assert len(requests) == 2
            async with asyncio.timeout(5):
                while not kernel.store.pending_notification_ids():
                    await asyncio.sleep(0.02)
            assert not kernel.store.db.execute(
                "SELECT count(*) FROM observations WHERE kind='notification'"
            ).fetchone()[0]
            second = await agent.run(wake_reasons=[{"kind": "reminder"}])
            assert second["status"] == "idle"
            assert len(requests) == 3
            assert (
                kernel.store.db.execute(
                    "SELECT count(*) FROM events WHERE kind='activity.started'"
                ).fetchone()[0]
                == 2
            )
        finally:
            await agent.close()
            await kernel.close()

    asyncio.run(scenario())


def test_native_schedule_list_update_delete_and_skill_reload(tmp_path, model_server):
    script, requests, config = model_server
    skills = tmp_path / "skills"
    skills.mkdir()
    path = skills / "fixture.md"
    prefix = "---\nname: fixture\ndescription: On demand instructions\n---\n"
    path.write_text(prefix + "Skill version one.")

    def last_result():
        return json.loads(
            next(m["content"] for m in reversed(requests[-1]["messages"]) if m["role"] == "tool")
        )

    def update_reminder():
        return [
            (
                "schedule_update",
                {
                    "id": last_result()[0]["id"],
                    "title": "修改后的提醒",
                    "prompt": "自己选择是否继续",
                    "every_seconds": 900,
                },
            )
        ]

    def delete_reminder():
        result = last_result()
        assert result["title"] == "修改后的提醒"
        assert result["everySeconds"] == 900
        assert result["prompt"] == "自己选择是否继续"
        return [("schedule_delete", {"id": result["id"]})]

    def reload_skill():
        path.write_text(prefix + "Skill version two.")
        time.sleep(0.5)  # Native filesystem watch stability interval.
        return [("skill", {"name": "fixture"})]

    script.extend(
        [
            [
                (
                    "schedule_create",
                    {"every_seconds": 600, "title": "自主提醒", "prompt": "还可以放下"},
                )
            ],
            [("schedule_list", {})],
            update_reminder,
            delete_reminder,
            [("skill", {"name": "fixture"})],
            reload_skill,
            [("world_finish", {})],
        ]
    )

    async def scenario():
        kernel = Kernel(tmp_path / "world")
        agent = DSHAgent(kernel, config, skills=skills)
        try:
            assert (await agent.run())["status"] == "idle"
            assert "Skill version one." not in json.dumps(requests[0])
            assert "Skill version one." in json.dumps(requests[5])
            assert "Skill version two." in json.dumps(requests[6])
            async with asyncio.timeout(5):
                while kernel.store.get("dsh_reminders"):
                    await asyncio.sleep(0.02)
            assert not snapshot(kernel, "live", [])["reminders"]
            assert not kernel.store.pending_notification_ids()
            assert {r[0] for r in kernel.store.db.execute("SELECT status FROM operations")} == {
                "succeeded"
            }
        finally:
            await agent.close()
            await kernel.close()

    asyncio.run(scenario())


def test_native_web_timeout_records_the_final_model_facing_failure(
    tmp_path, model_server, web_server, monkeypatch
):
    import astcho_world.dsh_runtime as runtime

    script, requests, config = model_server
    base, _ = web_server
    for name in ("HTTP_PROXY", "http_proxy"):
        monkeypatch.setenv(name, base)
    for name in ("NO_PROXY", "no_proxy"):
        monkeypatch.setenv(name, "127.0.0.1,localhost")
    compose = runtime.plugin_rows

    def short_timeout(*args):
        rows = compose(*args)
        next(r for r in rows if r["id"] == "tool-web")["config"] = {"fetchTimeoutMs": 50}
        return rows

    monkeypatch.setattr(runtime, "plugin_rows", short_timeout)
    script.extend(
        [
            [("web_fetch", {"url": "http://example.com/slow"})],
            [("world_finish", {})],
        ]
    )

    async def scenario():
        kernel = Kernel(tmp_path)
        agent = DSHAgent(kernel, config)
        try:
            await agent.run()
            row = kernel.store.db.execute("SELECT status,result FROM operations").fetchone()
            assert row["status"] == "failed"
            result = json.loads(row["result"])
            assert result["isError"] is True
            actual = next(m["content"] for m in requests[1]["messages"] if m["role"] == "tool")
            assert result["content"] == [{"type": "text", "text": actual}]
        finally:
            await agent.close()
            await kernel.close()

    asyncio.run(scenario())
