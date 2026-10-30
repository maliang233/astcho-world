import asyncio
import json
from types import SimpleNamespace

import pytest
from jsonschema import ValidationError, validate

from astcho_world.apps.qq import MANIFEST, QQ
from astcho_world.prompting import environment_prompt
from tests.test_group_subject import incoming, params, setup_group


def preamble(observation, *, activity="activity-1", operation="preamble-1", **fields):
    return {
        **params("preamble", observation, operation, **{"text": "合成行动说明", **fields}),
        "activity_id": activity,
    }


def test_optional_preamble_is_one_message_without_consuming_final_reply_or_social_state(tmp_path):
    app, runtime, sends, memories, _ = setup_group(tmp_path)
    observation = app.read({"chat_id": "group:10003"})["observation_id"]

    async def scenario():
        first = await app.dispatch(
            "call", preamble(observation, text="合成说明\n下一行 [CQ:at,qq=all]")
        )
        assert first["status"] == "completed"
        assert first["kind"] == "preamble"
        assert len(sends) == 1  # One QQ message even if the model included a line break.
        assert sends[0]["segments"] == [
            {"type": "reply", "data": {"id": "123"}},
            {"type": "text", "data": {"text": "合成说明\n下一行 [CQ:at,qq=all]"}},
        ]
        assert runtime.emotion._moods == {}
        assert memories == []
        assert app.attention_state("group:10003")["consecutive_no_reply"] == 0
        assert app.attention_state("group:10003")["last_bot_reply"] > 0
        assert app.db.execute("SELECT count(*) FROM participation").fetchone()[0] == 0
        final = await app.dispatch("call", params("reply_group", observation, text="合成最终答复"))
        assert final["status"] == "completed"
        assert len(sends) == 2
        assert len(memories) == 1
        assert memories[0][0] == ("刚刚的问题", "合成最终答复")

    asyncio.run(scenario())


def test_preamble_cannot_repeat_by_rereading_retargeting_new_operation_or_restart(tmp_path):
    app, _, sends, _, _ = setup_group(tmp_path)

    async def scenario():
        observation = app.read({"chat_id": "group:10003"})["observation_id"]
        first = await app.dispatch("call", preamble(observation))
        again = app.read({"chat_id": "group:10003"})["observation_id"]
        assert (await app.dispatch("call", preamble(again, operation="new"))) == first
        app.ingest({"batch_id": "later", "chat_id": "group:10003", "messages": [incoming("124")]})
        another = app.read({"chat_id": "group:10003"})["observation_id"]
        blocked = await app.dispatch("call", preamble(another, target_message_id="124"))
        assert blocked["status"] == "limit_reached"
        app.db.close()
        restarted = QQ(tmp_path, app.config)
        assert (await restarted.dispatch("call", preamble(again, activity="activity-2"))) == first
        changed = await restarted.dispatch(
            "call", preamble(again, activity="activity-2", text="合成另一说明")
        )
        assert changed["status"] == "limit_reached"
        assert restarted.conversation is None
        assert len(sends) == 1
        # A genuinely new target in a new activity has its own optional opportunity.
        restarted.conversation = app.conversation
        next_reply = await restarted.dispatch(
            "call", preamble(another, activity="activity-2", target_message_id="124")
        )
        assert next_reply["status"] == "completed"
        assert len(sends) == 2
        restarted.db.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("interrupted", ["unknown", "running"])
def test_unknown_preamble_is_not_replayed_and_does_not_block_final_answer(tmp_path, interrupted):
    app, _, sends, memories, _ = setup_group(tmp_path, fail_after=0)
    observation = app.read({"chat_id": "group:10003"})["observation_id"]

    async def scenario():
        result = await app.dispatch("call", preamble(observation))
        assert result["status"] == "unknown"
        if interrupted == "running":
            with app.db:
                app.db.execute(
                    "UPDATE preambles SET result=?", (json.dumps({**result, "status": "running"}),)
                )
        app.db.close()
        restarted = QQ(tmp_path, app.config)
        replay = await restarted.dispatch("call", preamble(observation, operation="new-op"))
        assert replay["status"] == "unknown"
        assert restarted.conversation is None
        assert sends == memories == []
        assert restarted.db.execute("SELECT count(*) FROM participation").fetchone()[0] == 0

        async def final_send(event, message):
            sends.append(str(message))
            return {"message_id": 902}

        restarted.conversation = app.conversation
        restarted.conversation.bot.send = final_send
        final = await restarted.dispatch(
            "call", params("reply_group", observation, text="合成答复")
        )
        assert final["status"] == "completed"
        assert len(sends) == len(memories) == 1
        restarted.db.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("target", ["missing", "self"])
def test_preamble_rejects_unobserved_or_own_message_before_reservation(tmp_path, target):
    app, _, sends, _, _ = setup_group(tmp_path)
    app.ingest(
        {
            "batch_id": "own",
            "chat_id": "group:10003",
            "messages": [incoming("self", user_id="10001", is_bot=True)],
        }
    )
    observation = app.read({"chat_id": "group:10003"})["observation_id"]
    with pytest.raises(ValueError, match="observed incoming"):
        asyncio.run(app.dispatch("call", preamble(observation, target_message_id=target)))
    assert sends == []
    assert app.db.execute("SELECT count(*) FROM preambles").fetchone()[0] == 0


def test_final_decision_does_not_generate_a_late_preamble(tmp_path):
    app, _, sends, _, _ = setup_group(tmp_path)
    observation = app.read({"chat_id": "group:10003"})["observation_id"]

    async def scenario():
        await app.dispatch("call", params("no_reply", observation))
        assert (await app.dispatch("call", preamble(observation)))["status"] == "unavailable"
        assert sends == []

    asyncio.run(scenario())


def test_admin_private_preamble_keeps_recipient_checks_and_final_reply(tmp_path, monkeypatch):
    app = QQ(tmp_path, {"private_admins": ["10002"], "conversation": {"allow_send": True}})
    deliveries = []

    async def send(event, message):
        deliveries.append((event.user_id, [s.type for s in message], str(message)))
        return {"message_id": 901 + len(deliveries)}

    monkeypatch.setattr(app, "delivery_bot", lambda: SimpleNamespace(send=send))
    for sender in ["10002", "10003"]:
        app.ingest(
            {
                "batch_id": sender,
                "chat_id": f"private:{sender}",
                "kind": "private",
                "messages": [incoming(user_id=sender)],
            }
        )

    async def scenario():
        request = {
            "tool": "preamble",
            "operation_id": "before",
            "activity_id": "admin-task",
            "arguments": {
                "chat_id": "private:10002",
                "target_message_id": "123",
                "text": "合成任务说明",
            },
        }
        assert (await app.dispatch("call", request))["status"] == "completed"
        changed = {**request, "arguments": {**request["arguments"], "text": "再次说明"}}
        assert (await app.dispatch("call", changed))["status"] == "limit_reached"
        denied = {**request, "arguments": {**request["arguments"], "chat_id": "private:10003"}}
        with pytest.raises(ValueError, match="not a configured"):
            await app.dispatch("call", denied)
        fabricated = {**request, "arguments": {**request["arguments"], "target_message_id": "999"}}
        with pytest.raises(ValueError, match="actual incoming"):
            await app.dispatch("call", fabricated)
        final = await app.dispatch(
            "call",
            {
                "tool": "reply_admin",
                "operation_id": "after",
                "arguments": {"chat_id": "private:10002", "message_id": "123", "text": "合成结果"},
            },
        )
        assert final["status"] == "completed"
        assert len(deliveries) == 2
        assert all(item[0] == "10002" for item in deliveries)
        assert app.conversation is None

    asyncio.run(scenario())
    app.db.close()


def test_preamble_schema_and_guidance_do_not_supply_canned_lines():
    schema = MANIFEST.tool("preamble").input_schema
    validate({"observation_id": "seen", "target_message_id": "123", "text": "正文"}, schema)
    validate({"chat_id": "private:10002", "target_message_id": "123", "text": "正文"}, schema)
    for extra in [
        {},
        {"observation_id": "seen", "chat_id": "private:10002"},
        {"observation_id": "seen", "activity_id": "model-made"},
    ]:
        with pytest.raises(ValidationError):
            validate({"target_message_id": "123", "text": "正文", **extra}, schema)
    prompt = environment_prompt()
    assert "可以结合语境选择" in prompt
    assert "同一次活动的同一会话最多一条" in prompt
    assert "我去搜搜" not in prompt
