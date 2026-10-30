import asyncio
import json
import sqlite3
import time
from collections import defaultdict
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from astcho.domain.models import ChatMessage, RetrievedMemory
from astcho.prompts import planner_prompt, reasoning_reply_prompt, reply_prompt
from astcho.services.emotion import EmotionService
from astcho.services.schedule import ScheduleState
from astcho.storage.sqlite import SQLiteStore
from astcho_world.apps.qq import MANIFEST, QQ
from astcho_world.conversation import Conversation
from astcho_world.group_prompt import (
    DIRECT_RULE_ADAPTATIONS,
    TOOL_OUTPUT,
    decision_guidance,
    reply_guidance,
)
from astcho_world.host import create_app
from tests.test_dsh_capabilities import web_server as web_server_fixture
from tests.test_dsh_runtime import model_server as model_server_fixture
from tests.test_legacy_bridge import FrozenDateTime
from tests.test_reverse_ws import event, wait_for

model_server = model_server_fixture
web_server = web_server_fixture


def incoming(mid="123", **overrides):
    return ChatMessage(
        **{
            "message_id": mid,
            "user_id": "10002",
            "nickname": "合成群友",
            "text": "刚刚的问题",
            "timestamp": time.time(),
            "mentioned_bot": True,
            **overrides,
        }
    ).model_dump()


def setup_group(tmp_path, *, fail_after=None, reasoning=True):
    app = QQ(tmp_path, {"conversation": {"allow_send": True}})
    sends, memories, retrievals = [], [], []
    store = SQLiteStore(tmp_path / "cognition-test.sqlite3")

    async def forbidden(*args, **kwargs):
        raise AssertionError("Group must not call a separate Planner or Replyer")

    async def send(event, message):
        if fail_after is not None and len(sends) >= fail_after:
            raise ConnectionError("Synthetic disconnect after a confirmed segment")
        sends.append(
            {
                "group_id": event.group_id,
                "segments": [{"type": s.type, "data": s.data} for s in message],
            }
        )
        return {"message_id": 900 + len(sends)}

    def retrieve(query, **kwargs):
        retrievals.append({"query": query, **kwargs})
        return [
            RetrievedMemory(
                memory_id="memory-1",
                content="用户(UID:10002)喜欢散步",
                score=0.9,
                kind="long",
                group_id="10003",
                user_id="10002",
            )
        ]

    runtime = SimpleNamespace(
        settings=SimpleNamespace(
            profile={"name": "星回", "identity": {"self_cognition": "合成人格原文"}},
            allowed_groups=frozenset({"10003"}),
            max_memories=8,
            reasoning_enabled=reasoning,
        ),
        sqlite=store,
        chat=SimpleNamespace(plan=forbidden, reply=forbidden),
        emotion=EmotionService(store),
        locks=defaultdict(asyncio.Lock),
        attention={},
        schedule=SimpleNamespace(current=lambda: ScheduleState(73, "合成日程心情", "default")),
        expressions=SimpleNamespace(relevant_hint=lambda *args: "合成群表达提示"),
        memory=SimpleNamespace(
            retrieve=retrieve, queue_turn=lambda *args, **kw: memories.append((args, kw))
        ),
    )
    conversation = Conversation(tmp_path, {})
    conversation.runtime = runtime
    conversation.bot = SimpleNamespace(self_id="10001", send=send)
    app.conversation = conversation
    app.ingest({"batch_id": "batch", "chat_id": "group:10003", "messages": [incoming()]})
    return app, runtime, sends, memories, retrievals


def params(tool, observation, operation="action", **fields):
    return {
        "tool": tool,
        "operation_id": operation,
        "arguments": {"observation_id": observation, "target_message_id": "123", **fields},
    }


@pytest.mark.parametrize("reasoning", [False, True])
def test_group_guidance_preserves_original_text_except_reviewed_contract(reasoning, monkeypatch):
    monkeypatch.setattr("astcho.prompts.datetime", FrozenDateTime)
    arguments = dict(
        bot_name="星回",
        profile={"name": "星回"},
        context="[123] 群友: 合成消息",
        memories=["合成记忆"],
        schedule="合成日程",
        emotion={"affinity": 0.4},
        planner_reason="",
        expression_hint="合成表达",
        user_id="10002",
        user_name="合成群友",
        user_input="合成消息",
    )
    adapted = reply_guidance(reasoning=reasoning, **arguments)
    restored = adapted
    for old_rule, scoped_rule in DIRECT_RULE_ADAPTATIONS.items():
        restored = restored.replace(scoped_rule, old_rule)
    if reasoning:
        old = (
            "### 输出格式（必须是有效的 JSON）\n"
            '{"thinking":"你的推理分析过程","reply":"最终回复内容"}\n\n'
            "重要：只输出 JSON，不要输出其他任何内容！"
        )
        assert restored.replace(TOOL_OUTPUT, old, 1) == reasoning_reply_prompt(**arguments)
    else:
        original_args = {k: v for k, v in arguments.items() if k not in {"user_name", "user_input"}}
        assert restored.replace(TOOL_OUTPUT, '【输出格式】只输出 JSON：{"reply":"最终回复"}', 1) + (
            "\n现在按上述格式输出 JSON："
        ) == reply_prompt(**original_args)
    metadata = dict(
        current_time="12:00",
        accumulated_count=1,
        time_span_seconds=0,
        participant_count=1,
        last_bot_spoke_seconds=None,
    )
    original = planner_prompt("", bot_name="星回", **metadata)
    decision = decision_guidance(bot_name="星回", metadata=metadata)
    restored_decision = decision
    for old_rule, scoped_rule in DIRECT_RULE_ADAPTATIONS.items():
        restored_decision = restored_decision.replace(scoped_rule, old_rule)
    assert original.split("【权限边界】")[0] in restored_decision
    assert "不能执行管理、删除、搜索" not in decision
    assert "现在输出 JSON" not in decision
    assert "excitement/shyness: -0.2 到 +0.2，通常变化很小" in decision


def test_read_prepares_original_context_without_models_or_social_decision(tmp_path):
    app, runtime, sends, memories, retrievals = setup_group(tmp_path)

    async def scenario():
        opened = await app.dispatch(
            "call",
            {
                "tool": "read_messages",
                "operation_id": "read",
                "arguments": {"chat_id": "group:10003"},
            },
        )
        context = opened["reply_context"]
        assert context["status"] == "available"
        assert context["emotion"]["affinity"] == 0.3
        assert context["schedule"]["talk_value"] == 73
        assert "合成人格原文" in context["reply_guidance"]
        assert "合成日程心情" in context["reply_guidance"]
        assert "合成群表达提示" in context["reply_guidance"]
        assert "昵称:合成群友" in context["memories"][0]
        assert "刚刚的问题" in context["reply_guidance"]
        app.ingest(
            {
                "batch_id": "later",
                "chat_id": "group:10003",
                "messages": [incoming("124", text="尚未阅读的后续内容")],
            }
        )
        again = await app.dispatch("call", params("reply_context", opened["observation_id"]))
        assert "尚未阅读的后续内容" not in json.dumps(again, ensure_ascii=False)
        assert retrievals[-1]["user_id"] == "10002"
        assert app.db.execute("SELECT count(*) FROM participation").fetchone()[0] == 0
        assert app.attention_state("group:10003")["consecutive_no_reply"] == 0
        assert sends == memories == []
        assert runtime.emotion.state("10003", "10002")["excitement"] == 0

    asyncio.run(scenario())


def test_subject_reply_keeps_delivery_and_memory_with_exactly_once_state(tmp_path, monkeypatch):
    async def no_sleep(*args):
        pass

    monkeypatch.setattr("astcho_world.group_chat.asyncio.sleep", no_sleep)
    app, runtime, sends, memories, _ = setup_group(tmp_path)

    async def scenario():
        opened = app.read({"chat_id": "group:10003"})
        request = params(
            "reply_group",
            opened["observation_id"],
            text="星回：查到了\n来源里的信息",
            excitement=0.1,
            affinity_score=1,
        )
        receipt = await app.dispatch("call", request)
        assert receipt["status"] == "completed"
        assert receipt["sent_message_ids"] == ["901", "902"]
        assert sends == [
            {
                "group_id": "10003",
                "segments": [
                    {"type": "reply", "data": {"id": "123"}},
                    {"type": "text", "data": {"text": "查到了"}},
                ],
            },
            {"group_id": "10003", "segments": [{"type": "text", "data": {"text": "来源里的信息"}}]},
        ]
        assert memories == [
            (
                ("刚刚的问题", "星回：查到了\n来源里的信息"),
                {"group_id": "10003", "user_id": "10002", "user_name": "合成群友"},
            )
        ]
        assert app.attention_state("group:10003")["excitement"] == pytest.approx(0.1)
        assert runtime.emotion.state("10003", "10002")["affinity"] == pytest.approx(0.33)
        assert (await app.dispatch("call", {**request, "operation_id": "new-id"})) == receipt
        assert len(sends) == 2
        changed = {**request, "arguments": {**request["arguments"], "text": "换内容再发"}}
        assert (await app.dispatch("call", changed))["status"] == "conflict"
        assert len(sends) == 2
        assert (
            await app.dispatch(
                "call",
                {
                    "tool": "participate",
                    "operation_id": "old",
                    "arguments": {"observation_id": opened["observation_id"]},
                },
            )
        )["status"] == "unavailable"

    asyncio.run(scenario())


def test_no_reply_applies_original_state_once_without_sending_or_memory(tmp_path):
    app, runtime, sends, memories, _ = setup_group(tmp_path)

    async def scenario():
        request = params(
            "no_reply", app.read({"chat_id": "group:10003"})["observation_id"], shyness=0.1
        )
        first = await app.dispatch("call", request)
        assert first["status"] == "completed"
        assert (await app.dispatch("call", request)) == first
        assert app.attention_state("group:10003")["consecutive_no_reply"] == 1
        assert sends == memories == []
        assert runtime.emotion.state("10003", "10002")["shyness"] == pytest.approx(0.1)
        # A second distinct observed interaction follows the original watching rule.
        second = params("no_reply", app.read({"chat_id": "group:10003"})["observation_id"])
        await app.dispatch("call", second)
        assert app.attention_state("group:10003")["consecutive_no_reply"] == 2
        assert app.attention_state("group:10003")["watching_until"] > time.time()

    asyncio.run(scenario())


@pytest.mark.parametrize("saved_status", ["unknown", "running"])
def test_partial_send_survives_reopen_without_replay_or_duplicate_memory(
    tmp_path, monkeypatch, saved_status
):
    async def no_sleep(*args):
        pass

    monkeypatch.setattr("astcho_world.group_chat.asyncio.sleep", no_sleep)
    app, _, sends, memories, _ = setup_group(tmp_path, fail_after=1)

    async def scenario():
        request = params(
            "reply_group",
            app.read({"chat_id": "group:10003"})["observation_id"],
            text="第一段\n第二段",
            excitement=0.2,
        )
        receipt = await app.dispatch("call", request)
        assert receipt["status"] == "unknown"
        assert receipt["sent_message_ids"] == ["901"]
        assert receipt["sent_messages"][0]["text"] == "第一段"
        assert len(sends) == 1
        assert memories == []
        if saved_status == "running":
            # Abrupt kill after one committed receipt, before final settlement.
            progress = {**receipt, "status": "running"}
            with app.db:
                app.db.execute("UPDATE participation SET result=?", (json.dumps(progress),))
        app.db.close()
        restarted = QQ(tmp_path, app.config)
        replay = await restarted.dispatch("call", {**request, "operation_id": "retry"})
        assert replay["status"] == "unknown"
        assert replay["sent_messages"] == receipt["sent_messages"]
        assert replay["sent_message_ids"] == ["901"]
        assert restarted.conversation is None
        assert restarted.attention_state("group:10003")["excitement"] == pytest.approx(0.2)
        assert restarted.db.execute("SELECT mid FROM messages WHERE mid='901'").fetchone()
        restarted.db.close()

    asyncio.run(scenario())


def test_group_mood_survives_incoming_message_and_cognition_restart(tmp_path):
    app, runtime, sends, _, _ = setup_group(tmp_path)

    async def scenario():
        request = params(
            "no_reply",
            app.read({"chat_id": "group:10003"})["observation_id"],
            excitement=0.1,
            shyness=0.2,
        )
        await app.dispatch("call", request)
        app.ingest({"batch_id": "new", "chat_id": "group:10003", "messages": [incoming("124")]})
        app.db.close()
        restarted = QQ(tmp_path, app.config)
        restarted.conversation = app.conversation
        runtime.emotion._moods.clear()
        runtime.attention.clear()
        opened = await restarted.dispatch(
            "call",
            {
                "tool": "read_messages",
                "operation_id": "read",
                "arguments": {"chat_id": "group:10003"},
            },
        )
        assert opened["reply_context"]["emotion"]["excitement"] == pytest.approx(0.1, abs=0.001)
        assert opened["reply_context"]["emotion"]["shyness"] == pytest.approx(0.2, abs=0.001)
        assert restarted.attention_state("group:10003")["consecutive_no_reply"] == 1
        assert sends == []
        restarted.db.close()

    asyncio.run(scenario())


def test_optional_meme_uses_original_service_and_records_confirmed_delivery(tmp_path, monkeypatch):
    app, runtime, sends, memories, _ = setup_group(tmp_path)
    selected, marked = [], []

    async def no_sleep(*args):
        pass

    async def select(answer, query, **kwargs):
        selected.append((answer, query, kwargs))
        return {"url": "https://example.test/meme.png", "file_id": "fixture-meme"}

    monkeypatch.setattr("astcho_world.group_chat.asyncio.sleep", no_sleep)
    runtime.memes = SimpleNamespace(
        select=select, mark_used=lambda *args, **kw: marked.append(args)
    )
    request = params(
        "reply_group",
        app.read({"chat_id": "group:10003"})["observation_id"],
        text="好耶",
        should_meme=True,
        meme_query="开心",
    )
    receipt = asyncio.run(app.dispatch("call", request))
    assert receipt["sent_message_ids"] == ["901", "902"]
    assert selected == [("好耶", "开心", {"session_key": "10003"})]
    assert marked == [("fixture-meme",)]
    assert sends[1]["segments"][0]["type"] == "image"
    assert sends[1]["segments"][0]["data"]["file"] == "https://example.test/meme.png"
    assert len(memories) == 1


@pytest.mark.parametrize("target", ["missing", "self"])
def test_reply_rejects_unknown_or_own_target_before_mutating_state(tmp_path, target):
    app, runtime, sends, _, _ = setup_group(tmp_path)
    app.ingest(
        {
            "batch_id": "own",
            "chat_id": "group:10003",
            "messages": [incoming("self", user_id="10001", is_bot=True)],
        }
    )
    opened = app.read({"chat_id": "group:10003"})
    request = params("reply_group", opened["observation_id"], target_message_id=target, text="在")
    with pytest.raises(ValueError, match="observed incoming"):
        asyncio.run(app.dispatch("call", request))
    assert sends == []
    assert app.db.execute("SELECT count(*) FROM participation").fetchone()[0] == 0
    assert runtime.emotion._moods == {}


def test_group_tools_cannot_bypass_private_routing_or_group_allowlist(tmp_path):
    app, _, sends, _, _ = setup_group(tmp_path)
    for kind, chat in [("private", "private:10002"), ("group", "group:99999")]:
        app.ingest({"batch_id": chat, "kind": kind, "chat_id": chat, "messages": [incoming()]})
        request = params("reply_group", app.read({"chat_id": chat})["observation_id"], text="在")
        with pytest.raises(ValueError):
            asyncio.run(app.dispatch("call", request))
    assert sends == []
    assert app.db.execute("SELECT count(*) FROM participation").fetchone()[0] == 0


def test_group_text_cannot_embed_additional_cq_actions(tmp_path):
    app, _, sends, _, _ = setup_group(tmp_path)
    opened = app.read({"chat_id": "group:10003"})
    text = "看这个字面格式 [CQ:at,qq=all]"
    asyncio.run(app.dispatch("call", params("reply_group", opened["observation_id"], text=text)))
    assert sends[0]["segments"][-1] == {"type": "text", "data": {"text": text}}


@pytest.mark.parametrize("with_preamble", [False, True])
def test_real_sdk_reads_searches_fetches_and_replies_over_reverse_ws(
    tmp_path, monkeypatch, model_server, web_server, with_preamble
):
    script, requests, model = model_server
    base, seen = web_server
    for name, value in {
        "ASTCHO_LLM_API_KEY": "synthetic-legacy-key",
        "ASTCHO_LLM_BASE_URL": model.base_url,
        "ASTCHO_CHAT_MODEL": "legacy-must-not-run",
        "ASTCHO_PLANNER_MODEL": "legacy-must-not-run",
        "ASTCHO_REASONING_MODEL": "legacy-must-not-run",
        "ASTCHO_ADMINS": "10004",
        "ASTCHO_ALLOWED_GROUPS": "10003",
        "ASTCHO_MEMORY_ENABLED": "false",
        "DEEPSEEK_API_KEY": "synthetic-search-key",
        "DEEPSEEK_SEARCH_BASE_URL": base + "/anthropic/v1",
        "HTTP_PROXY": base,
        "http_proxy": base,
        "NO_PROXY": "127.0.0.1,localhost",
        "no_proxy": "127.0.0.1,localhost",
    }.items():
        monkeypatch.setenv(name, value)
    profile = tmp_path / "profile.json"
    profile.write_text('{"name":"星回","identity":{"self_cognition":"合成人格原文"}}')
    monkeypatch.setenv("ASTCHO_PROFILE_PATH", str(profile))
    monkeypatch.setattr("astcho_world.ingress.random.uniform", lambda *args: 0.01)

    def observed_snapshot():
        messages = requests[-1]["messages"]
        results = [
            json.loads(m["content"])
            for m in messages
            if m["role"] == "tool" and m["content"].startswith("{")
        ]
        snapshot = next(r["data"] for r in results if "observation_id" in r.get("data", {}))
        assert snapshot["reply_context"]["status"] == "available"
        return snapshot

    def preamble_call(text):
        return [
            (
                "qq__preamble",
                {
                    "observation_id": observed_snapshot()["observation_id"],
                    "target_message_id": "123",
                    "text": text,
                },
            )
        ]

    def reply_from_observation():
        # The actual SDK has received the same observation and native web results.
        assert "Search citation fact." in json.dumps(requests[-1]["messages"])
        assert "Visible web fact." in json.dumps(requests[-1]["messages"])
        if with_preamble:
            assert "limit_reached" in json.dumps(requests[-1]["messages"])
        snapshot = observed_snapshot()
        return [
            (
                "qq__reply_group",
                {
                    "observation_id": snapshot["observation_id"],
                    "target_message_id": "123",
                    "text": "查到啦，Search citation fact.；网页是 Visible web fact.",
                },
            )
        ]

    script.extend(
        [
            [("world_tools", {"app_id": "qq"})],
            [("qq__read_messages", {"chat_id": "group:10003"})],
            *([lambda: preamble_call("合成行动说明")] if with_preamble else []),
            [("web_search", {"queries": ["synthetic group question"]})],
            [("web_fetch", {"url": "http://example.com/page"})],
            *([lambda: preamble_call("合成重复说明")] if with_preamble else []),
            reply_from_observation,
            [("world_finish", {})],
        ]
    )
    (tmp_path / "qq.json").write_text(MANIFEST.model_dump_json())
    config = tmp_path / "apps.json"
    config.write_text(
        json.dumps(
            {"apps": [{"manifest": "qq.json", "config": {"conversation": {"allow_send": True}}}]}
        )
    )
    app = create_app(
        config_path=config,
        data_dir=tmp_path / "world",
        bot_id="10001",
        ingress_token=None,
        model=model,
        profile_path=profile,
        admins={"10004"},
    )
    with TestClient(app) as client:
        with client.websocket_connect("/onebot/v11/ws", headers={"X-Self-ID": "10001"}) as ws:
            ws.send_json(event())

            async def await_send():
                async with asyncio.timeout(25):
                    while not app.state.qq_bridge.pending:
                        activity = app.state.kernel.store.get("environment_activity", {})
                        assert not activity.get("finished"), activity
                        await asyncio.sleep(0.02)

            expected_texts = (["合成行动说明"] if with_preamble else []) + [
                "查到啦，Search citation fact.；网页是 Visible web fact."
            ]
            for index, text in enumerate(expected_texts):
                client.portal.call(await_send)
                action = ws.receive_json()
                assert action["action"] == "send_group_msg"
                assert action["params"] == {
                    "group_id": 10003,
                    "message": [
                        {"type": "reply", "data": {"id": "123"}},
                        {"type": "text", "data": {"text": text}},
                    ],
                }
                ws.send_json(
                    {
                        "status": "ok",
                        "retcode": 0,
                        "data": {"message_id": 901 + index},
                        "echo": action["echo"],
                    }
                )
                # Let the correlated receipt settle before polling for another send.
                client.portal.call(
                    wait_for, lambda echo=action["echo"]: echo not in app.state.qq_bridge.pending
                )
            client.portal.call(
                wait_for,
                lambda: app.state.kernel.store.get("environment_activity", {}).get("finished"),
            )
            operations = client.portal.call(
                lambda: [
                    tuple(row)
                    for row in app.state.kernel.store.db.execute(
                        "SELECT tool,status FROM operations"
                    )
                ]
            )
            assert ("reply_group", "succeeded") in operations
            assert not any(tool == "participate" for tool, _ in operations)
            activity_id = client.portal.call(
                lambda: app.state.kernel.store.db.execute(
                    "SELECT logical_id FROM activity_runs WHERE run_id=?",
                    (app.state.kernel.store.get("environment_activity")["id"],),
                ).fetchone()[0]
            )
    if with_preamble:
        with sqlite3.connect(tmp_path / "world/apps/qq/qq.sqlite3") as db:
            assert db.execute("SELECT activity FROM preambles").fetchall() == [(activity_id,)]
    assert len(requests) == (8 if with_preamble else 6)
    assert all(request["model"] == "world-fixture" for request in requests)
    assert len(seen) == 2  # Both native search and fetch, all through local fixtures.
