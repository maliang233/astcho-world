import asyncio
import json
import time

import pytest
from fastapi.testclient import TestClient

from astcho_world.apps.notes import MANIFEST as NOTES
from astcho_world.apps.qq import MANIFEST as QQ
from astcho_world.host import create_app
from astcho_world.kernel import Kernel
from astcho_world.prompting import environment_prompt
from astcho_world.runtime import Agent


def call(name, identifier="call"):
    return {
        "role": "assistant",
        "tool_calls": [
            {
                "id": identifier,
                "type": "function",
                "function": {"name": name, "arguments": "{}"},
            }
        ],
    }


def test_persona_is_attached_verbatim_without_rewriting_original_file(tmp_path):
    path = tmp_path / "profile.json"
    original = '{\r\n  "name": "星回", "identity": {"text": "原文， 空格也保留。"}\r\n}\r\n'
    path.write_bytes(original.encode())
    rendered = environment_prompt(path)
    assert rendered.endswith(original)
    assert path.read_bytes() == original.encode()
    assert "原文， 空格也保留。" not in environment_prompt()


@pytest.mark.parametrize("text", ["[]", "invalid-json"])
def test_invalid_identity_fails_before_it_can_be_used_as_instructions(tmp_path, text):
    path = tmp_path / "profile.json"
    path.write_text(text)
    with pytest.raises(ValueError, match="JSON object"):
        environment_prompt(path)


def test_step_context_is_current_and_continuity_contains_only_recorded_activity(tmp_path):
    class Model:
        contexts = []

        async def decide(self, messages, tools):
            context = json.loads(messages[-1]["content"])
            self.contexts.append(context)
            return call("world_apps") if len(self.contexts) == 1 else call("world_finish")

    async def scenario():
        kernel = Kernel(tmp_path)
        kernel.register(NOTES)
        await kernel.stop_app("notes")
        model = Model()
        agent = Agent(kernel, model)
        try:
            result = await agent.run(max_steps=2)
            assert result == {"status": "idle", "steps": 2}
            first, second = model.contexts
            assert first["activity"]["remaining_including_current"] == 2
            assert second["activity"]["remaining_including_current"] == 1
            assert first["activity"]["previous"] is None
            assert first["apps"][0]["state"] == "stopped"
            assert "T" in first["clock"]["local_time"]
            assert first["clock"]["timezone"]
            assert kernel.apps["notes"].process is None
            assert first["wake_policy"]["mode"] == "single_run"
            await agent.run()
            assert model.contexts[-1]["activity"]["previous"]["status"] == "idle"
            assert model.contexts[-1]["activity"]["previous"]["steps"] == 2
            history = kernel.store.get("agent_history")
            assert not any('"type": "world.runtime"' in m.get("content", "") for m in history)
        finally:
            await kernel.close()

    asyncio.run(scenario())


def test_wake_separates_new_notifications_and_never_includes_unread_context(tmp_path):
    class Model:
        wakes = []

        async def decide(self, messages, tools):
            assert "尚未读取的秘密" not in json.dumps(messages, ensure_ascii=False)
            self.wakes.append(
                next(
                    json.loads(m["content"])
                    for m in reversed(messages)
                    if m["role"] == "user" and '"type": "world.wake"' in m["content"]
                )
            )
            return {"role": "assistant", "content": ""}

    async def scenario():
        kernel = Kernel(tmp_path)
        kernel.register(QQ)
        model = Model()
        try:
            await kernel.ingest(
                "qq",
                {
                    "batch_id": "one",
                    "chat_id": "group:test",
                    "messages": [
                        {
                            "message_id": "secret",
                            "user_id": "2",
                            "nickname": "朋友",
                            "text": "尚未读取的秘密",
                            "timestamp": time.time(),
                        },
                        {
                            "message_id": "trigger",
                            "user_id": "2",
                            "nickname": "朋友",
                            "text": "在吗",
                            "timestamp": time.time(),
                            "mentioned_bot": True,
                        },
                    ],
                },
            )
            agent = Agent(kernel, model)
            await agent.run(wake_reasons=[{"kind": "notification"}])
            await agent.run()
            first, second = model.wakes
            identifier = "qq:group:test:trigger"
            assert first["reasons"] == [{"kind": "notification"}]
            assert first["notification_delivery"]["new_ids"] == [identifier]
            assert second["notification_delivery"]["new_ids"] == []
            assert second["notifications"] == []
            assert "earlier_ids" not in second["notification_delivery"]
            assert kernel.store.db.execute("SELECT count(*) FROM operations").fetchone()[0] == 0
        finally:
            await kernel.close()

    asyncio.run(scenario())


def test_budget_exhaustion_is_reported_as_such_on_the_next_activity(tmp_path):
    class Model:
        contexts = []

        async def decide(self, messages, tools):
            self.contexts.append(json.loads(messages[-1]["content"]))
            return call("world_apps") if len(self.contexts) == 1 else call("world_finish")

    async def scenario():
        kernel = Kernel(tmp_path)
        model = Model()
        try:
            agent = Agent(kernel, model)
            assert (await agent.run(max_steps=1))["status"] == "budget_exhausted"
            await agent.run()
            assert model.contexts[-1]["activity"]["previous"]["status"] == "budget_exhausted"
        finally:
            await kernel.close()

    asyncio.run(scenario())


def test_host_reports_actual_reminder_and_notification_wakes_with_original_schedule(tmp_path):
    manifest = tmp_path / "notes.json"
    manifest.write_text(NOTES.model_dump_json())
    config = tmp_path / "apps.json"
    (tmp_path / "qq.json").write_text(QQ.model_dump_json())
    config.write_text(
        json.dumps(
            {
                "apps": [
                    {"manifest": "notes.json"},
                    {
                        "manifest": "qq.json",
                        "config": {
                            "conversation": {"allow_send": False},
                            "private_setting": "must-not-enter-context",
                        },
                    },
                ]
            }
        )
    )
    profile = tmp_path / "profile.json"
    original = '{"name":"星回","identity":{"text":"保留的身份文字"}}'
    profile.write_text(original)
    schedule = tmp_path / "schedule.json"
    schedule.write_text(
        json.dumps(
            {
                "default": [
                    {
                        "start": "00:00",
                        "end": "23:59",
                        "talk_value": 5,
                        "mood": "原日程状态",
                    }
                ]
            }
        )
    )

    class Model:
        wakes = []
        contexts = []

        async def decide(self, messages, tools):
            assert messages[0]["content"].endswith(original)
            assert "must-not-enter-context" not in json.dumps(messages)
            self.contexts.append(json.loads(messages[-1]["content"]))
            self.wakes.append(
                next(
                    json.loads(m["content"])
                    for m in reversed(messages)
                    if m["role"] == "user" and '"type": "world.wake"' in m["content"]
                )
            )
            return {"role": "assistant", "content": ""}

    model = Model()
    app = create_app(
        config_path=config,
        data_dir=tmp_path / "world",
        bot_id=None,
        ingress_token=None,
        model=model,
        profile_path=profile,
        schedule_path=schedule,
        runner="native",
    )

    async def wait_count(count):
        async with asyncio.timeout(3):
            while len(model.wakes) < count:
                await asyncio.sleep(0.01)

    with TestClient(app) as client:
        client.portal.call(app.state.kernel.store.remind, time.time() + 0.02, "自己的提醒")
        client.portal.call(wait_count, 1)
        assert model.wakes[0]["reasons"] == [{"kind": "reminder"}]
        state = model.contexts[0]
        assert state["self_state"]["schedule"]["mood"] == "原日程状态"
        assert state["self_state"]["qq"]["participation_enabled"] is False
        assert state["self_state"]["qq"]["connection"] == "disconnected"
        assert state["wake_policy"]["periodic_seconds"] == 0
        client.portal.call(
            app.state.kernel.store.notify,
            "notes",
            {
                "id": "synthetic",
                "source": "合成来源",
                "message": "通知",
                "locator": {},
            },
        )
        client.portal.call(wait_count, 2)
        assert model.wakes[1]["reasons"] == [{"kind": "notification"}]


def test_invalid_profile_does_not_leave_host_owner_lock_or_call_model(tmp_path):
    config = tmp_path / "apps.json"
    config.write_text('{"apps": []}')
    profile = tmp_path / "profile.json"
    profile.write_text("invalid-json")

    class Model:
        async def decide(self, *args):
            raise AssertionError("Invalid identity must fail before any model call")

    app = create_app(
        config_path=config,
        data_dir=tmp_path / "world",
        bot_id=None,
        ingress_token=None,
        model=Model(),
        profile_path=profile,
        runner="native",
    )
    with pytest.raises(ValueError, match="JSON object"), TestClient(app):
        pass
    kernel = Kernel(tmp_path / "world")
    asyncio.run(kernel.close())
