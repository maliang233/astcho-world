import asyncio
from collections import defaultdict
from types import SimpleNamespace

from nonebot.adapters.onebot.v11 import GroupMessageEvent, Message, MessageSegment

from astcho.domain.models import ChatMessage, PlannerDecision, RetrievedMemory, VisionResult
from astcho.handlers.common import at_targets_of, local_image_source, reply_details_of
from astcho.handlers.group import (
    _handle_custom_follow,
    _memory_contents_for_prompt,
    _process_after_window,
    is_bot_mentioned,
    qq_nickname,
)
from astcho.handlers.media import describe_event_media, describe_forward
from astcho.services.attention import AttentionService
from astcho.services.schedule import ScheduleState


def group_event(message: Message, *, to_me: bool = False) -> GroupMessageEvent:
    return GroupMessageEvent(
        time=1,
        self_id=10001,
        post_type="message",
        sub_type="normal",
        user_id=10002,
        message_type="group",
        message_id=123,
        message=message,
        original_message=message,
        raw_message=str(message),
        font=0,
        sender={"user_id": 10002, "nickname": "tester", "card": ""},
        group_id=10003,
        to_me=to_me,
    )


def test_real_onebot_at_segment_is_detected():
    event = group_event(Message([MessageSegment.at(10001), MessageSegment.text("说句话")]))
    assert is_bot_mentioned(event, "10001") is True


def test_at_segments_are_rendered_for_legacy_context():
    event = group_event(
        Message(
            [
                MessageSegment.at(10001),
                MessageSegment.at(20002),
                MessageSegment.text("说句话"),
            ]
        )
    )
    assert at_targets_of(event, "10001") == ["你", "QQ:20002"]


def test_reply_metadata_preserves_quoted_author_and_text():
    event = SimpleNamespace(
        reply=SimpleNamespace(
            message_id=88,
            sender=SimpleNamespace(user_id=99, nickname="QQ昵称", card="群名片"),
            get_plaintext=lambda: "被引用的原话",
        )
    )

    assert reply_details_of(event) == ("88", "99", "QQ昵称", "被引用的原话")


def test_adapter_to_me_is_authoritative_even_without_at_segment():
    event = group_event(Message("星回，说句话"), to_me=True)
    assert is_bot_mentioned(event, "10001") is True


def test_group_sender_uses_qq_nickname_instead_of_group_card():
    event = group_event(Message("你好"))
    event.sender.nickname = "QQ昵称"
    event.sender.card = "群名片"
    assert qq_nickname(event) == "QQ昵称"


def test_local_meme_is_encoded_for_containerized_napcat(tmp_path):
    image = tmp_path / "meme.gif"
    image.write_bytes(b"GIF89a")
    assert local_image_source(str(image)) == "base64://R0lGODlh"


def test_forward_message_is_expanded():
    class Bot:
        async def get_forward_msg(self, **kwargs):
            return {
                "messages": [
                    {
                        "sender": {"nickname": "甲"},
                        "message": [{"type": "text", "data": {"text": "第一句"}}],
                    },
                    {
                        "sender": {"nickname": "乙"},
                        "message": [{"type": "text", "data": {"text": "第二句"}}],
                    },
                ]
            }

    runtime = SimpleNamespace(
        settings=SimpleNamespace(forward_max_text_segments=30, forward_max_media_segments=10),
        vision=SimpleNamespace(),
        llm=SimpleNamespace(),
    )
    result = asyncio.run(describe_forward(runtime, Bot(), "forward-id"))
    assert result == "[转发自 甲] 甲: 第一句 | 乙: 第二句"


def test_two_distinct_users_trigger_custom_follow():
    sent = []

    class Bot:
        self_id = "10001"

        async def send_group_msg(self, **kwargs):
            sent.append(kwargs)

    def event(user_id, *, reply=True):
        original = Message([MessageSegment.at(99)])
        return SimpleNamespace(
            message_id=user_id,
            reply=(
                SimpleNamespace(message_id=88, sender=SimpleNamespace(user_id=99))
                if reply
                else None
            ),
            original_message=original,
            get_message=lambda: original,
            get_plaintext=lambda: "",
        )

    runtime = SimpleNamespace(
        schedule=SimpleNamespace(current=lambda: SimpleNamespace(talk_value=50)),
        custom_reply_tracker={"1": {}},
        custom_reply_handled={"1": {}},
    )

    async def scenario():
        assert await _handle_custom_follow(runtime, Bot(), event(10), "1", "10") is False
        assert await _handle_custom_follow(runtime, Bot(), event(11), "1", "11") is True

    asyncio.run(scenario())
    assert len(sent) == 1


def test_bare_at_can_continue_an_active_custom_follow_chain():
    sent = []

    class Bot:
        self_id = "10001"

        async def send_group_msg(self, **kwargs):
            sent.append(kwargs)

    def event(user_id, *, reply):
        original = Message([MessageSegment.at(99)])
        return SimpleNamespace(
            message_id=user_id,
            reply=(
                SimpleNamespace(message_id=88, sender=SimpleNamespace(user_id=99))
                if reply
                else None
            ),
            original_message=original,
            get_message=lambda: original,
            get_plaintext=lambda: "",
        )

    runtime = SimpleNamespace(
        schedule=SimpleNamespace(current=lambda: SimpleNamespace(talk_value=50)),
        custom_reply_tracker={"1": {}},
        custom_reply_handled={"1": {}},
    )

    async def scenario():
        first = await _handle_custom_follow(runtime, Bot(), event(10, reply=True), "1", "10")
        second = await _handle_custom_follow(runtime, Bot(), event(11, reply=False), "1", "11")
        assert first is False
        assert second is True

    asyncio.run(scenario())
    assert len(sent) == 1


def test_empty_reply_without_mention_does_not_start_custom_follow():
    class Bot:
        self_id = "10001"

    reply = SimpleNamespace(message_id=88, sender=SimpleNamespace(user_id=99))
    event = SimpleNamespace(
        message_id=10,
        reply=reply,
        original_message=Message([MessageSegment.reply(88)]),
        get_message=lambda: Message(),
        get_plaintext=lambda: "",
    )
    runtime = SimpleNamespace(
        schedule=SimpleNamespace(current=lambda: SimpleNamespace(talk_value=50)),
        custom_reply_tracker={"1": {}},
        custom_reply_handled={"1": {}},
    )

    handled = asyncio.run(_handle_custom_follow(runtime, Bot(), event, "1", "10"))
    assert handled is False
    assert runtime.custom_reply_tracker["1"] == {}


def test_media_failure_degrades_without_stopping_message():
    image = MessageSegment(type="image", data={"url": "https://invalid.example/image.jpg"})
    event = group_event(Message([image]))

    class Vision:
        async def describe(self, url):
            raise RuntimeError("vision unavailable")

    runtime = SimpleNamespace(vision=Vision())
    result = asyncio.run(describe_event_media(runtime, SimpleNamespace(), event))
    assert result.descriptions == ["[图片]"]


def test_media_only_offers_safe_heart_throb_stickers_for_learning():
    images = Message(
        [
            MessageSegment(type="image", data={"url": "https://example.test/safe.png"}),
            MessageSegment(type="image", data={"url": "https://example.test/person.png"}),
            MessageSegment(type="image", data={"url": "https://example.test/screenshot.png"}),
        ]
    )
    event = group_event(images)
    results = {
        "https://example.test/safe.png": VisionResult(
            description="虚构角色贴纸",
            is_sticker=True,
            heart_throb=True,
            contains_real_person=False,
            is_screenshot=False,
        ),
        "https://example.test/person.png": VisionResult(
            description="真人反应照片",
            is_sticker=True,
            heart_throb=True,
            contains_real_person=True,
        ),
        "https://example.test/screenshot.png": VisionResult(
            description="影视画面截图",
            is_sticker=True,
            heart_throb=True,
            is_screenshot=True,
        ),
    }

    class Vision:
        async def describe(self, url):
            return results[url]

    runtime = SimpleNamespace(vision=Vision())
    media = asyncio.run(describe_event_media(runtime, SimpleNamespace(), event))

    assert [url for url, _ in media.learned_images] == ["https://example.test/safe.png"]


def test_group_event_runs_planner_reply_and_send(monkeypatch):
    event = group_event(Message([MessageSegment.at(10001), MessageSegment.text("说句话")]))
    message = ChatMessage(
        message_id="123",
        user_id="10002",
        nickname="tester",
        text="说句话",
        timestamp=1,
        mentioned_bot=True,
    )
    attention = AttentionService("10001", bot_name="星回")
    attention.add(message)
    sent, remembered = [], []

    class Bot:
        self_id = "10001"

        async def send(self, event, message):
            sent.append(message)

    class Chat:
        async def plan(self, *args, **kwargs):
            return PlannerDecision(action="reply", reason="明确@了机器人")

        async def reply(self, *args, **kwargs):
            return "第一句\n第二句"

    class Memory:
        def retrieve(self, *args, **kwargs):
            return []

        def queue_turn(self, *args, **kwargs):
            remembered.append((args, kwargs))

    runtime = SimpleNamespace(
        locks=defaultdict(asyncio.Lock),
        pending_group_messages={"10003": [(event, message)]},
        aggregation_tasks={},
        attention={"10003": attention},
        schedule=SimpleNamespace(current=lambda: ScheduleState(50, "正常", "default")),
        emotion=SimpleNamespace(
            state=lambda *args: {"excitement": 0, "shyness": 0, "affinity": 0.3},
            apply=lambda *args, **kwargs: {"excitement": 0, "shyness": 0, "affinity": 0.3},
        ),
        chat=Chat(),
        memory=Memory(),
        expressions=SimpleNamespace(relevant_hint=lambda *args: ""),
        settings=SimpleNamespace(max_memories=8, profile={"name": "星回"}),
        memes=SimpleNamespace(),
    )

    async def no_sleep(*args, **kwargs):
        return None

    monkeypatch.setattr("astcho.handlers.group.asyncio.sleep", no_sleep)
    asyncio.run(_process_after_window(runtime, Bot(), "10003"))
    assert len(sent) == 2
    assert len(remembered) == 1
    assert [item.text for item in list(attention.messages)[-2:]] == ["第一句", "第二句"]


def test_aggregated_name_trigger_keeps_triggering_user_identity(monkeypatch):
    first_event = group_event(Message("星回在吗"))
    later_event = group_event(Message("顺便说一句"))
    first = ChatMessage(
        message_id="first",
        user_id="first-user",
        nickname="甲",
        text="星回在吗",
        timestamp=1,
    )
    later = ChatMessage(
        message_id="later",
        user_id="later-user",
        nickname="乙",
        text="顺便说一句",
        timestamp=2,
    )
    attention = AttentionService("10001", bot_name="星回")
    attention.add(first)
    attention.add(later)
    reply_calls, retrieved_for = [], []

    class Bot:
        self_id = "10001"

        async def send(self, event, message):
            return None

    class Chat:
        async def plan(self, *args, **kwargs):
            return PlannerDecision(action="reply", reason="被呼名")

        async def reply(self, *args, **kwargs):
            reply_calls.append(kwargs)
            return "在呢"

    class Memory:
        def retrieve(self, *args, **kwargs):
            retrieved_for.append(kwargs["user_id"])
            return []

        def queue_turn(self, *args, **kwargs):
            return None

    runtime = SimpleNamespace(
        locks=defaultdict(asyncio.Lock),
        pending_group_messages={"10003": [(first_event, first), (later_event, later)]},
        aggregation_tasks={},
        attention={"10003": attention},
        schedule=SimpleNamespace(current=lambda: ScheduleState(50, "正常", "default")),
        emotion=SimpleNamespace(
            state=lambda *args: {"excitement": 0, "shyness": 0, "affinity": 0.3},
            apply=lambda *args, **kwargs: {"excitement": 0, "shyness": 0, "affinity": 0.3},
        ),
        chat=Chat(),
        memory=Memory(),
        expressions=SimpleNamespace(relevant_hint=lambda *args: ""),
        settings=SimpleNamespace(max_memories=8, profile={"name": "星回"}),
        memes=SimpleNamespace(),
    )

    async def no_sleep(*args, **kwargs):
        return None

    monkeypatch.setattr("astcho.handlers.group.asyncio.sleep", no_sleep)
    asyncio.run(_process_after_window(runtime, Bot(), "10003"))

    assert retrieved_for == ["first-user"]
    assert reply_calls[0]["user_id"] == "first-user"
    assert reply_calls[0]["user_name"] == "甲"
    assert reply_calls[0]["user_input"] == "星回在吗"


def test_planner_target_controls_identity_memory_emotion_and_persistence(monkeypatch):
    first_event = group_event(Message("星回看看这个"))
    later_event = group_event(Message("我觉得挺好"))
    first = ChatMessage(
        message_id="first",
        user_id="first-user",
        nickname="甲",
        text="星回看看这个",
        timestamp=1,
    )
    later = ChatMessage(
        message_id="later",
        user_id="later-user",
        nickname="乙",
        text="我觉得挺好",
        timestamp=2,
    )
    attention = AttentionService("10001", bot_name="星回")
    attention.add(first)
    attention.add(later)
    reply_calls, retrieved, applied, queued = [], [], [], []

    class Bot:
        self_id = "10001"

        async def send(self, event, message):
            return {"message_id": 900 + len(reply_calls)}

    class Chat:
        async def plan(self, *args, **kwargs):
            return PlannerDecision(action="reply", target_message_id="later", reason="接乙的话")

        async def reply(self, *args, **kwargs):
            reply_calls.append(kwargs)
            return "确实"

    class Memory:
        def retrieve(self, query, **kwargs):
            retrieved.append((query, kwargs["user_id"]))
            return []

        def queue_turn(self, *args, **kwargs):
            queued.append((args, kwargs))

    def apply(group_id, user_id, **kwargs):
        applied.append(user_id)
        return {"excitement": 0, "shyness": 0, "affinity": 0.3}

    runtime = SimpleNamespace(
        locks=defaultdict(asyncio.Lock),
        pending_group_messages={"10003": [(first_event, first), (later_event, later)]},
        aggregation_tasks={},
        attention={"10003": attention},
        schedule=SimpleNamespace(current=lambda: ScheduleState(50, "正常", "default")),
        emotion=SimpleNamespace(
            state=lambda *args: {"excitement": 0, "shyness": 0, "affinity": 0.3},
            apply=apply,
        ),
        chat=Chat(),
        memory=Memory(),
        expressions=SimpleNamespace(relevant_hint=lambda *args: ""),
        settings=SimpleNamespace(max_memories=8, profile={"name": "星回"}),
        memes=SimpleNamespace(),
    )

    async def no_sleep(*args, **kwargs):
        return None

    monkeypatch.setattr("astcho.handlers.group.asyncio.sleep", no_sleep)
    asyncio.run(_process_after_window(runtime, Bot(), "10003"))

    assert applied == ["later-user"]
    assert retrieved == [("我觉得挺好", "later-user")]
    assert reply_calls[0]["user_id"] == "later-user"
    assert reply_calls[0]["user_name"] == "乙"
    assert reply_calls[0]["user_input"] == "我觉得挺好"
    assert len(queued) == 1
    assert queued[0][1]["user_id"] == "later-user"
    assert queued[0][0][0] == "我觉得挺好"
    assert attention.messages[-1].message_id == "901"


def test_memory_prompt_content_resolves_uid_to_current_qq_nickname():
    memory = RetrievedMemory(
        memory_id="mem-1",
        content="用户(UID:99)喜欢音游",
        score=0.9,
        kind="long",
        group_id="10003",
        user_id="99",
    )
    runtime = SimpleNamespace(
        sqlite=SimpleNamespace(get_user=lambda group_id, user_id: {"nickname": "小明"})
    )

    assert _memory_contents_for_prompt(runtime, "10003", [memory]) == [
        "用户(UID:99, 昵称:小明)喜欢音游"
    ]
