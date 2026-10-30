"""Reuse protected speaking rules without importing a chat task or output envelope."""

from astcho.prompts import private_reply_prompt, reply_prompt


def speaking_rules() -> dict[str, str]:
    # Render with empty context. Only fixed rule sections enter the environment;
    # no fabricated conversation, memory, relationship or reply task is attached.
    group = reply_prompt(
        bot_name="Astcho", profile={}, context="", memories=[], schedule=""
    )
    group_rules = group.split("## 说话方式\n", 1)[1].split("\n\n## 消息格式说明", 1)[0]
    private = private_reply_prompt(
        bot_name="Astcho", profile={}, nickname="", history=[], latest=""
    )
    private_rules = private.split("## 说话方式\n", 1)[1].split("\n\n你正在和【", 1)[0]
    # Keep complete source lines, excluding only group-specific directions.
    # The complete group block is retained separately for that actual setting.
    common = "\n".join(line for line in group_rules.splitlines() if "群里聊天" not in line)
    humility = "不懂就问，不知道就沉默。不要装懂，不要强行接话。"
    if humility not in group:
        raise ValueError("Protected speaking rules changed; adapter needs review")
    return {"common": common + "\n\n" + humility, "group": group_rules, "private": private_rules}


def outbound_speech_guidance() -> str:
    rules = speaking_rules()
    return (
        "## 所有对人发言的共同入口\n\n"
        "以下说话规则逐字取自原版聊天模板。所有准备发给人的正文都遵守它们："
        "群聊回复、管理员私聊回答或任务结果、行动前说明、主动分享的附言、B站评论，"
        "以及以后新增应用中面向人的发言。切换应用、工具或任务类型不会切换你的身份和口吻；"
        "管理员任务的对外回答仍是你在和对方交流。\n\n"
        "### 通用说话原文\n\n" + rules["common"] + "\n\n"
        "### QQ群聊场景原文\n\n" + rules["group"] + "\n\n"
        "这段用于群聊回复、群里的行动前说明和群分享附言。QQ提供的 reply_context "
        "继续按原配置选择普通或推理Replyer的完整要求，并提供该对象的原关系、记忆、"
        "情绪与表达提示；本次群聊以其中对应的要求为准，不将别群的表达提示移到这里。\n\n"
        "### QQ私聊场景原文\n\n" + rules["private"] + "\n\n"
        "这段也用于管理员私聊、私聊中的行动前说明和分享附言；不需要先收到群聊的 "
        "reply_context 才生效。普通非管理员私聊继续由原聊天链路使用原完整模板。\n\n"
        "这些规则约束准备说出口的正文，不规定必须发言，不改变接收对象、权限、"
        "是否评论或分享、发送频率与回执规则。纯链接仍可直接分享，无需为满足风格补写附言。"
        "笔记、内部分析、工具参数中的内部理由、完整经历记录和独立交付文件的内容，"
        "不因此改成聊天短句；对人的说明正文仍遵守上述要求。"
    )
