from pathlib import Path

from astcho.prompts import private_reply_prompt, reply_prompt
from astcho_world.prompting import environment_prompt
from astcho_world.speech import outbound_speech_guidance, speaking_rules


def test_original_speaking_blocks_are_reused_verbatim_without_chat_task():
    group = reply_prompt(
        bot_name="合成名字", profile={}, context="未读群消息", memories=[], schedule=""
    )
    private = private_reply_prompt(
        bot_name="合成名字", profile={}, nickname="未读对象", history=[], latest="未读私聊"
    )
    rules = speaking_rules()
    assert rules["group"] == group.split("## 说话方式\n")[1].split("\n\n## 消息格式说明")[0]
    assert rules["private"] == private.split("## 说话方式\n")[1].split("\n\n你正在和【")[0]
    expected_common = [
        "- 日常口语化，**务必简短自然** 。保持温暖体贴的人设，注意社交礼仪",
        "- 不要发 Emoji，用颜文字代替",
        "- 不要加任何前缀，直接说内容。**每次只针对最新的话题回复**",
    ]
    assert rules["common"].split("\n\n")[0].splitlines() == expected_common
    assert all(line in group for line in expected_common)
    assert rules["common"].split("\n\n")[1] in group
    rendered = outbound_speech_guidance()
    assert all(block in rendered for block in rules.values())
    for task_only in (
        "未读群消息", "未读私聊", "未读对象", "现在直接输出回复正文", "只输出 JSON",
        "暂无相关长时记忆", "当前对话者：普通用户", "【@了你】",
    ):
        assert task_only not in rendered


def test_shared_speech_is_available_without_qq_observation_or_profile():
    prompt = environment_prompt()
    assert outbound_speech_guidance() in prompt
    assert "所有准备发给人的正文都遵守" in prompt
    assert "说话要求只适用于对应 QQ 群聊正文" not in prompt
    assert "说话要求只用于 QQ 群聊正文" not in prompt
    assert "纯链接仍可直接分享" in prompt
    assert "只回复 1-3 句话" in prompt
    assert "完整经历记录和独立交付文件的内容" in prompt


def test_rules_come_from_local_protected_baseline():
    root = Path(__file__).resolve().parents[1]
    from astcho_world.baseline import verify

    assert verify(root) == []
