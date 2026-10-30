import time

import astcho.services.attention as attention_module
from astcho.domain.models import ChatMessage
from astcho.services.attention import AttentionService
from astcho.services.schedule import ScheduleState


def message(**changes):
    values = {
        "message_id": "1",
        "user_id": "user",
        "nickname": "u",
        "text": "hello",
        "timestamp": time.time(),
    }
    values.update(changes)
    return ChatMessage(**values)


def test_real_bot_identity_updates_last_reply():
    service = AttentionService("42")
    service.add(message(user_id="42"))
    assert service.seconds_since_bot_reply() == 0


def test_context_uses_legacy_roles_and_explicit_direction_markers():
    service = AttentionService("42", bot_name="星回")
    service.add(
        message(
            message_id="m1",
            nickname="小明",
            mentioned_bot=True,
            at_targets=["你"],
            timestamp=90,
        )
    )
    service.add(
        message(
            message_id="m2",
            nickname="小红",
            text="接上",
            replied_to_bot=True,
            reply_to_user_id="42",
            reply_to_nickname="星回",
            reply_text="上一句",
            timestamp=95,
        )
    )
    service.add(message(message_id="m3", user_id="42", nickname="星回", text="在呢", timestamp=99))

    context = service.context(now=100)

    assert "[m1] 10秒前, 小明: @你 hello 【@了你】" in context
    assert '[m2] 5秒前, 小红: [回复: 你: "上一句"] 接上 【回复了你】' in context
    assert "[m3] 1秒前, 星回(你): 在呢" in context


def test_context_keeps_other_at_target_even_without_plain_text():
    service = AttentionService("42", bot_name="星回")
    service.add(message(message_id="m1", text="", at_targets=["QQ:99"]))

    assert service.context(now=service.messages[0].timestamp) == "[m1] 0秒前, u: @QQ:99"


def test_find_message_resolves_planner_target_from_recent_context():
    service = AttentionService("42")
    service.add(message(message_id="m1"))
    service.add(message(message_id="m2", user_id="42", is_bot=True))

    assert service.find_message("m1").user_id == "user"
    assert service.find_message("missing") is None


def test_low_activity_only_allows_mention():
    service = AttentionService("42")
    sleepy = ScheduleState(5, "sleepy", "night")
    assert not service.should_plan(message(), sleepy, random_value=0)
    assert service.last_trace["reason"] == "日程低活跃，仅响应提及/引用"
    assert service.should_plan(message(mentioned_bot=True), sleepy, random_value=1)


def test_probability_trace_exposes_decision_factors():
    service = AttentionService("42", base_talk_probability=0.1)
    active = ScheduleState(50, "calm", "day")

    assert not service.should_plan(message(), active, excitement=0.25, random_value=0.99)
    assert service.last_trace["reason"] == "概率判定"
    assert service.last_trace["talk"] == 50
    assert 0 < service.last_trace["probability"] < 1
    assert service.last_trace["sample"] == 0.99


def test_busy_group_does_not_reduce_frequency_after_bot_has_been_silent(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(attention_module.time, "time", lambda: now[0])
    service = AttentionService("42", base_talk_probability=0.1)
    service.add(message(user_id="42", timestamp=now[0]))

    now[0] += 301
    for offset in range(10):
        service.add(message(message_id=str(offset), timestamp=now[0] - 9 + offset))

    active = ScheduleState(75, "calm", "day")
    assert not service.should_plan(message(timestamp=now[0]), active, random_value=0.99)
    assert service.last_trace["frequency"] == 1.0


def test_silence_grows_from_first_observed_message_before_bot_replies(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(attention_module.time, "time", lambda: now[0])
    service = AttentionService("42", base_talk_probability=0.1)
    service.add(message(timestamp=now[0]))

    now[0] += 600
    active = ScheduleState(50, "calm", "day")
    assert not service.should_plan(message(timestamp=now[0]), active, random_value=0.99)
    assert service.last_trace["silence"] == 600
    assert service.last_trace["probability"] > 0.05
    assert service.seconds_since_bot_reply() is None
