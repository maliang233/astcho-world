import asyncio
import json
import time

from astcho_world.apps.qq import MANIFEST, QQ
from astcho_world.dsh_runtime import DSHAgent
from astcho_world.kernel import Kernel
from astcho_world.qq_participation import participation_context
from tests.test_dsh_runtime import model_server as model_server_fixture
from tests.test_group_subject import incoming, params, setup_group

model_server = model_server_fixture


def seed(app, messages, *, batch="facts", chat="group:10003", kind="group"):
    return app.ingest(
        {
            "batch_id": batch,
            "chat_id": chat,
            "kind": kind,
            "messages": messages,
            "random_value": 1,
        }
    )


def own(mid, timestamp, **fields):
    return incoming(
        mid, timestamp=timestamp, user_id="10001", is_bot=True, mentioned_bot=False, **fields
    )


def test_own_history_is_known_but_other_counts_cover_only_the_displayed_snapshot(tmp_path):
    app = QQ(tmp_path, {})
    now = time.time()
    seed(
        app,
        [
            own("old", now - 601),
            *[incoming(str(i), timestamp=now - 400 + i, text="未展示的合成正文") for i in range(5)],
            own("self-1", now - 200),
            own("self-2", now - 150, text="", image_description="合成图片气泡"),
            incoming("seen-1", timestamp=now - 20),
            incoming("seen-2", timestamp=now - 10),
            incoming("seen-3", timestamp=now - 1, user_id="10005"),
        ],
    )
    snapshot = app.read({"chat_id": "group:10003", "limit": 2})
    facts = snapshot["participation"]
    assert facts["window_seconds"] == 600
    assert facts["own"]["message_count"] == 2  # Own actions need not be re-read.
    assert facts["own"]["last_message_at"] == now - 150
    assert facts["observed_conversation"] == {
        "scope": "only messages in this snapshot within the window; may be partial",
        "message_count": 2,
        "other_message_count": 2,
        "other_participant_count": 2,
        "other_messages_after_last_own": 2,
    }
    assert "未展示的合成正文" not in json.dumps(snapshot, ensure_ascii=False)
    assert "participation" not in app.ambient_state()
    assert app.attention_state("group:10003")["consecutive_no_reply"] == 0
    assert app.db.execute("SELECT count(*) FROM participation").fetchone()[0] == 0
    app.db.close()


def test_participation_is_frozen_with_snapshot_across_later_messages_and_restart(tmp_path):
    app, runtime, sends, memories, _ = setup_group(tmp_path)
    seed(app, [own("self-1", time.time() - 100)])
    snapshot = app.read({"chat_id": "group:10003"})
    facts = snapshot["participation"]
    seed(
        app,
        [
            own("self-2", time.time() - 50),
            incoming("later", text="后来未读的合成正文"),
        ],
        batch="later",
    )
    app.db.close()
    restarted = QQ(tmp_path, app.config)
    restarted.conversation = app.conversation
    restored = restarted.snapshot(snapshot["observation_id"])
    assert restored["participation"] == facts
    context = asyncio.run(
        restarted.dispatch("call", params("reply_context", snapshot["observation_id"]))
    )
    assert context["participation"] == facts
    assert "后来未读的合成正文" not in json.dumps(context, ensure_ascii=False)
    assert facts["own"]["message_count"] == 1
    fresh = restarted.read({"chat_id": "group:10003", "limit": 1})
    assert fresh["participation"]["own"]["message_count"] == 2
    assert sends == memories == []
    assert runtime.emotion.state("10003", "10002")["excitement"] == 0
    restarted.db.close()


def test_time_window_units_and_echoed_own_messages_do_not_invent_turn_counts(tmp_path):
    app = QQ(tmp_path, {})
    now = time.time()
    messages = [
        own("expired", now - 600.001),
        own("boundary", now - 600),
        own("segment", now - 10),
        own("future", now + 1),
        incoming("older", timestamp=now - 601),
        incoming("visible", timestamp=now),
    ]
    seed(app, messages)
    seed(app, messages, batch="echo")
    snapshot = {"chat_id": "group:10003", "messages": messages}
    facts = participation_context(app, snapshot, now)
    assert facts["own"]["message_count"] == 2
    assert facts["own"]["last_message_at"] == now - 10
    assert "not turns" in facts["own"]["unit"]
    assert facts["observed_conversation"]["message_count"] == 3
    assert facts["observed_conversation"]["other_message_count"] == 1
    assert facts["observed_conversation"]["other_messages_after_last_own"] == 1
    seed(app, [own("other-chat", now)], chat="group:10006", batch="other-chat")
    assert participation_context(app, snapshot, now) == facts
    app.db.close()


def test_no_own_history_and_private_read_remain_uninterpreted(tmp_path):
    app = QQ(tmp_path, {})
    seed(app, [incoming()])
    facts = app.read({"chat_id": "group:10003"})["participation"]
    assert facts["own"]["message_count"] == 0
    assert facts["own"]["last_message_at"] is None
    assert facts["observed_conversation"]["other_messages_after_last_own"] is None
    seed(app, [incoming("private")], chat="private:10002", kind="private", batch="private")
    assert "participation" not in app.read({"chat_id": "private:10002"})
    app.db.close()


def test_real_sdk_receives_participation_and_can_leave_without_a_social_action(
    tmp_path, model_server
):
    script, requests, config = model_server

    def leave():
        messages = requests[-1]["messages"]
        system = "\n".join(m["content"] for m in messages if m["role"] == "system")
        assert "阅读聊天本身就是一种参与，不需要每次都留下回复" in system
        assert "不要求每句话都有用" in system
        result = next(
            json.loads(m["content"])
            for m in messages
            if m["role"] == "tool" and '"participation"' in m["content"]
        )
        facts = result["data"]["participation"]
        assert facts["own"]["message_count"] == 2
        assert facts["observed_conversation"]["other_message_count"] == 1
        return [("world_finish", {})]

    script.extend(
        [
            [("world_tools", {"app_id": "qq"})],
            [("qq__read_messages", {"chat_id": "group:10003", "limit": 1})],
            leave,
        ]
    )

    async def scenario():
        kernel = Kernel(tmp_path)
        kernel.register(MANIFEST)
        now = time.time()
        await kernel.ingest(
            "qq",
            {
                "batch_id": "facts",
                "chat_id": "group:10003",
                "messages": [
                    own("1", now - 100),
                    own("2", now - 90),
                    incoming("3", timestamp=now - 10),
                ],
                "random_value": 1,
            },
        )
        agent = DSHAgent(kernel, config)
        try:
            result = await agent.run()
            assert result["status"] == "idle"
            assert len(requests) == 3
            operations = kernel.store.db.execute("SELECT tool,effect FROM operations").fetchall()
            assert [tuple(row) for row in operations] == [("read_messages", "read")]
        finally:
            await agent.close()
            await kernel.close()

    asyncio.run(scenario())
