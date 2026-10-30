from astcho.prompts import (
    meme_selection_prompt,
    meme_taste_prompt,
    memory_extraction_prompt,
    planner_prompt,
    private_reply_prompt,
    reasoning_reply_prompt,
    reply_prompt,
    vision_prompt,
)

PERSONA = {
    "identity": {
        "name": "星回",
        "self_cognition": "温暖但有主见的少年",
        "personality_tags": ["偶尔臭屁"],
        "behavioral_habits": ["熟人面前会吐槽"],
    },
    "preferences": {
        "likes": ["音游", "甜食"],
        "dislikes": ["鸡汤标语"],
    },
    "style": ["短句自然"],
    "relationships": {"private-user": {"desc": "不得进入全局策展 Prompt"}},
}


def test_planner_prompt_preserves_decision_contract():
    prompt = planner_prompt(
        "[m1] user: hello",
        bot_name="Astcho",
        current_time="12:00:00",
        accumulated_count=1,
        time_span_seconds=0,
        participant_count=1,
        last_bot_spoke_seconds=None,
    )
    for field in (
        "action",
        "target_message_id",
        "reason",
        "excitement",
        "shyness",
        "affinity_score",
        "should_meme",
        "meme_query",
    ):
        assert field in prompt
    assert "正在群里聊天" in prompt
    assert "有人提及了你且明确在跟你讲话/与你相关、熟悉话题、可以附和" in prompt
    assert "只有【@了你】标记时" in prompt
    assert "只有【回复了你】标记时" in prompt
    assert "文字中提到你的名字" in prompt
    assert "相对时间, 昵称" in prompt
    assert "不得填写你自己的消息" in prompt


def test_reply_prompt_keeps_persona_emotion_memory_and_context():
    prompt = reply_prompt(
        bot_name="Astcho",
        profile={"personality": "warm", "style": ["brief"]},
        context="group context",
        memories=["memory atom"],
        schedule="calm",
        emotion={"excitement": 0.2, "shyness": 0.1, "affinity": 0.8},
        planner_reason="direct mention",
    )
    for value in ("warm", "group context", "memory atom", "direct mention", "0.80"):
        assert value in prompt
    assert "brief" not in prompt


def test_memory_prompt_anchors_user_id():
    messages = memory_extraction_prompt(text="likes rhythm games", user_id="u42")
    assert "UID:u42" in messages[0]["content"]
    assert "u42" in messages[1]["content"]


def test_legacy_reply_persona_uses_identity_but_not_preferences():
    group = reply_prompt(
        bot_name="星回",
        profile=PERSONA,
        context="群聊",
        memories=[],
        schedule="日常",
    )
    private = private_reply_prompt(
        bot_name="星回", profile=PERSONA, nickname="朋友", history=[], latest="在吗"
    )
    reasoning = reasoning_reply_prompt(
        bot_name="星回", profile=PERSONA, context="群聊", memories=[], schedule="日常"
    )
    for prompt in (group, reasoning):
        for value in ("温暖但有主见", "偶尔臭屁", "熟人面前会吐槽"):
            assert value in prompt
        for value in ("音游", "鸡汤标语", "短句自然"):
            assert value not in prompt
    for value in ("音游", "鸡汤标语", "短句自然"):
        assert value in private


def test_reply_prompts_preserve_peer_tone_and_kaomoji_habit():
    arguments = dict(
        bot_name="星回",
        profile=PERSONA,
        context="群聊",
        memories=[],
        schedule="日常",
    )
    group = reply_prompt(**arguments)
    reasoning = reasoning_reply_prompt(**arguments, user_name="小明", user_input="说句话")
    private = private_reply_prompt(
        bot_name="星回", profile=PERSONA, nickname="朋友", history=[], latest="在吗"
    )

    for prompt in (group, reasoning):
        assert "颜文字" in prompt
        assert "emoji" in prompt.lower()
        assert "星回(你)" in prompt
    assert "邻家弟弟" in private
    assert "一般不超过30字" in reasoning
    assert "推理步骤（一步一步思考！）" in reasoning
    assert "小明: 说句话" in reasoning
    assert "使用颜文字代替 Emoji" in reasoning
    assert "你是群友，不是助手" in group


def test_legacy_reply_persona_keeps_marked_background_relationships():
    profile = {
        **PERSONA,
        "relationships": {
            "important-user": {
                "appellation": "重要的人",
                "role": "family",
                "desc": "一直放在心上",
                "always_include": True,
            }
        },
    }
    prompt = reasoning_reply_prompt(
        bot_name="星回",
        profile=profile,
        context="群聊",
        memories=[],
        schedule="日常",
        user_id="other-user",
    )
    assert "重要关系(背景)" in prompt
    assert "重要的人" in prompt
    assert "一直放在心上" in prompt


def test_legacy_group_profiles_never_reach_reply_prompts():
    profile = {**PERSONA, "group_profiles": {"g1": "不应进入回复的旧群聊画像"}}
    arguments = dict(
        bot_name="星回",
        profile=profile,
        context="群聊",
        memories=[],
        schedule="日常",
    )

    assert "旧群聊画像" not in reply_prompt(**arguments)
    assert "旧群聊画像" not in reasoning_reply_prompt(**arguments)


def test_meme_prompts_receive_persona_without_relationships():
    taste = meme_taste_prompt(
        "可爱贴纸",
        ["卖萌"],
        "治愈",
        profile=PERSONA,
        bot_name="星回",
    )
    selection = meme_selection_prompt(
        "好耶",
        [{"description": "庆祝", "inclination": "开心"}],
        profile=PERSONA,
        bot_name="星回",
    )
    for prompt in (taste, selection):
        assert "温暖但有主见" in prompt
        assert "音游" in prompt
        assert "鸡汤标语" in prompt
        assert "private-user" not in prompt
        assert "不得进入全局策展" not in prompt


def test_vision_prompt_strictly_rejects_real_people_and_screenshots():
    prompt = vision_prompt()

    for forbidden_type in (
        "任何真人",
        "聊天记录",
        "视频截图",
        "影视剧截图",
        "直播截图",
        "软件",
        "网页",
    ):
        assert forbidden_type in prompt
    assert "拿不准时一律判为 is_sticker=false" in prompt
    assert "contains_real_person" in prompt
    assert "is_screenshot" in prompt
    assert "绝杀级可爱" in prompt
    assert "百里挑一" in prompt


def test_meme_taste_prompt_has_absolute_second_gate():
    prompt = meme_taste_prompt("候选图片", [], "中性", bot_name="星回")

    assert "真人、照片、梗图及上述所有截图都是绝对禁区" in prompt
    assert "影视剧" in prompt
    assert "拿不准是否含真人或是否为截图" in prompt


def test_planner_keeps_all_changing_metadata_after_rules():
    first = planner_prompt(
        "first message",
        bot_name="Astcho",
        current_time="12:00:00",
        accumulated_count=1,
        time_span_seconds=10,
        participant_count=1,
        last_bot_spoke_seconds=None,
        recent_meme_rate="0/10",
    )
    second = planner_prompt(
        "second message",
        bot_name="Astcho",
        current_time="13:01:02",
        accumulated_count=9,
        time_span_seconds=3600,
        participant_count=4,
        last_bot_spoke_seconds=20,
        recent_meme_rate="3/10",
    )
    prefix = first.split("【本轮群聊状态】")[0]
    assert second.startswith(prefix)
    assert "【字段说明】" in prefix
    assert "first message" not in prefix
    assert "3/10" in second


def test_group_reply_rules_are_shared_across_users_and_turns():
    arguments = dict(bot_name="星回", profile=PERSONA)
    first = reply_prompt(
        **arguments,
        context="first context",
        memories=[],
        schedule="rest",
        user_id="u1",
    )
    second = reply_prompt(
        **arguments,
        context="second context",
        memories=["memory"],
        schedule="study",
        user_id="private-user",
        emotion={"affinity": 0.8},
        planner_reason="mention",
    )
    prefix = first.split("## 本轮上下文")[0]
    assert second.startswith(prefix)
    assert "【输出格式】" in prefix
    assert "当前时间" not in prefix
    assert "不得进入全局策展 Prompt" not in prefix
    assert "不得进入全局策展 Prompt" in second


def test_private_rules_are_shared_across_contacts_and_messages():
    first = private_reply_prompt(
        bot_name="星回",
        profile=PERSONA,
        nickname="Alice",
        history=[],
        latest="hello",
    )
    second = private_reply_prompt(
        bot_name="星回",
        profile=PERSONA,
        nickname="Bob",
        history=[{"role": "user", "content": "old message"}],
        latest="bye",
    )
    prefix = first.split("你正在和【")[0]
    assert second.startswith(prefix)
    assert "只回复 1-3 句话" in prefix
    assert "Bob" in second and "old message" in second and "bye" in second
