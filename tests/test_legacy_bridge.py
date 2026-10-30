import asyncio
import json
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

from astcho.domain.models import ChatMessage, PlannerDecision, ReplyPayload
from astcho.handlers.group import _process_after_window
from astcho.services.attention import AttentionService
from astcho.services.chat import ChatService
from astcho.services.schedule import ScheduleState
from astcho_world.legacy import participate_in_group


class FrozenDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        return cls(2026, 10, 27, 23, 55, 7, tzinfo=tz)


class RecordedLLM:
    planner_client = "planner-client"
    reasoning_client = "reasoning-client"

    def __init__(self, *, reply="第一句\n第二句", action="reply"):
        self.calls = []
        self.reply = reply
        self.action = action

    def record(self, method, kwargs):
        saved = {**kwargs}
        if "schema" in saved:
            saved["schema"] = saved["schema"].__name__
        self.calls.append({"method": method, **saved})

    async def json_completion(self, **kwargs):
        self.record("json", kwargs)
        if kwargs["schema"] is PlannerDecision:
            return PlannerDecision(action=self.action, target_message_id="202", reason="接这句话")
        return ReplyPayload(reply=self.reply)

    async def raw_completion(self, **kwargs):
        self.record("raw", kwargs)
        return json.dumps({"thinking": "观察语境", "reply": self.reply}, ensure_ascii=False)

    async def text_completion(self, **kwargs):
        self.record("text", kwargs)
        return "星回：在呢"


def settings(reasoning=True):
    return SimpleNamespace(
        profile={"name": "星回", "identity": {"self_cognition": "示例人格"}},
        planner_model="fixture-planner",
        chat_model="fixture-chat",
        reasoning_model="fixture-reasoning",
        reasoning_enabled=reasoning,
        chat_temperature=0.7,
        reasoning_temperature=0.1,
        max_memories=8,
    )


def fixture_runtime(*, reasoning=True, action="reply"):
    llm = RecordedLLM(action=action)
    events = []
    config = settings(reasoning)
    mood = {"excitement": 0, "shyness": 0, "affinity": 0.3}

    class Bot:
        self_id = "10001"

        async def send(self, event, message):
            events.append({"send": str(message)})
            return {"message_id": 900 + len(events)}

    class Memory:
        def retrieve(self, query, **kwargs):
            events.append({"retrieve": query, **kwargs})
            return []

        def queue_turn(self, *args, **kwargs):
            events.append({"remember": list(args), **kwargs})

    def apply(group_id, user_id, **kwargs):
        events.append({"emotion": user_id, **kwargs})
        return mood.copy()

    messages = [
        ChatMessage(
            message_id="201",
            user_id="user-a",
            nickname="甲",
            text="星回看看",
            timestamp=1000,
            mentioned_bot=True,
        ),
        ChatMessage(
            message_id="202", user_id="user-b", nickname="乙", text="我觉得不错", timestamp=1001
        ),
    ]
    attention = AttentionService("10001", bot_name="星回")
    for message in messages:
        attention.add(message)
    pending = [(SimpleNamespace(message_id=m.message_id), m) for m in messages]
    runtime = SimpleNamespace(
        settings=config,
        chat=ChatService(config, llm),
        locks=defaultdict(asyncio.Lock),
        pending_group_messages=defaultdict(list),
        aggregation_tasks={},
        attention={"group": attention},
        schedule=SimpleNamespace(current=lambda: ScheduleState(50, "正常", "default")),
        emotion=SimpleNamespace(state=lambda *args: mood.copy(), apply=apply),
        memory=Memory(),
        expressions=SimpleNamespace(relevant_hint=lambda *args: ""),
        memes=SimpleNamespace(),
    )
    return runtime, Bot(), messages, pending, llm, events


def test_opened_group_uses_unchanged_chain_and_same_requests(monkeypatch):
    async def no_sleep(*args):
        pass

    monkeypatch.setattr("astcho.handlers.group.asyncio.sleep", no_sleep)
    monkeypatch.setattr("time.time", lambda: 2000)
    monkeypatch.setattr("astcho.prompts.datetime", FrozenDateTime)
    monkeypatch.setattr("astcho.handlers.group.datetime", FrozenDateTime)

    async def scenario():
        original, bot, messages, pending, llm, events = fixture_runtime()
        original.pending_group_messages["group"] = pending
        await _process_after_window(original, bot, "group")
        adapted, bot2, messages2, pending2, llm2, events2 = fixture_runtime()
        await participate_in_group(
            adapted, bot2, group_id="group", observed_messages=messages2, pending=pending2
        )
        assert llm.calls == llm2.calls
        assert events == events2
        assert [m.text for m in original.attention["group"].messages] == [
            m.text for m in adapted.attention["group"].messages
        ]
        assert [event["send"] for event in events if "send" in event] == ["第一句", "第二句"]
        assert events[-1]["user_id"] == "user-b"

    asyncio.run(scenario())


def test_unobserved_pending_is_rejected_before_calling_models():
    async def scenario():
        runtime, bot, messages, pending, llm, events = fixture_runtime()
        try:
            await participate_in_group(
                runtime, bot, group_id="group", observed_messages=messages[:1], pending=pending
            )
        except ValueError:
            pass
        else:
            raise AssertionError("Unobserved input accepted")
        assert llm.calls == []
        assert events == []

    asyncio.run(scenario())


def test_no_reply_does_not_generate_or_send(monkeypatch):
    async def no_sleep(*args):
        pass

    monkeypatch.setattr("astcho.handlers.group.asyncio.sleep", no_sleep)

    async def scenario():
        runtime, bot, messages, pending, llm, events = fixture_runtime(action="no_reply")
        await participate_in_group(
            runtime, bot, group_id="group", observed_messages=messages, pending=pending
        )
        assert len(llm.calls) == 1
        assert not any("send" in event or "remember" in event for event in events)
        assert runtime.attention["group"].consecutive_no_reply == 1

    asyncio.run(scenario())


async def capture_requests():
    llm = RecordedLLM()
    service = ChatService(settings(), llm)
    await service.plan("固定群聊上下文", "正常", metadata={"current_time": "12:00:00"})
    await service.reply("固定群聊上下文", ["合成记忆"], "正常", user_name="甲", user_input="在吗")
    normal = ChatService(settings(False), llm)
    await normal.reply("固定群聊上下文", [], "正常", user_name="甲", user_input="在吗")
    await normal.private_reply(
        nickname="甲", history=[{"role": "user", "content": "在吗"}], latest="在吗"
    )
    return llm.calls


def test_full_legacy_requests_match_frozen_fixture(monkeypatch):
    # Match the clock used for the initial capture, without editing the stored request.
    monkeypatch.setattr("astcho.prompts.datetime", FrozenDateTime)
    expected = json.loads((Path(__file__).parent / "fixtures" / "legacy_requests.json").read_text())
    assert asyncio.run(capture_requests()) == expected
