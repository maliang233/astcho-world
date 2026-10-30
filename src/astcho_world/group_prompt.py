"""QQ-scoped reuse of protected prompts, with explicit Agent output-contract adapters.

The source templates stay untouched. No external text is edited or summarized here.
"""

from astcho.prompts import planner_prompt, reasoning_reply_prompt, reply_prompt

TOOL_OUTPUT = (
    "【本场景输出方式】由你在当前主体链路中决定和组织回复，可以先使用其他可用工具。"
    "答复正文放入 QQ reply_group 工具的 text 参数，指定已观察的目标消息。"
    "可选的行动前说明通过 preamble 发出，不代替答复。"
    "不要把推理分析、工具调用或这份说明当作正文。决定不回复时可调用 no_reply；"
    "单纯阅读或离开无需提交社交判断。这里的说话要求用于本次群聊的回复、"
    "行动前说明与分享附言；其他对人发言仍遵守环境中的原版说话规则。"
)

# Only these static direction rules are adapted. Templates and user-supplied
# conversation/persona/memory text remain untouched.
DIRECT_RULE_ADAPTATIONS = {
    "- 只有【@了你】标记时，才表示该消息通过 QQ 的 @ 功能@了你，需要回复": (
        "- 只有【@了你】标记时，才表示该消息通过 QQ 的 @ 功能@了你；有实际内容时需要回复，"
        "仅有空引用/@时通常无需接话"
    ),
    "- 只有【回复了你】标记时，才表示该消息引用了你的消息，需要回复": (
        "- 只有【回复了你】标记时，才表示该消息引用了你的消息；有实际内容时需要回复，"
        "仅有空引用/@时通常无需接话"
    ),
    "这类消息提到了你，必须回复": "这类消息有实际内容时需要回复；仅有空引用/@时通常无需接话",
}


def scope_direct_rules(header):
    for original, replacement in DIRECT_RULE_ADAPTATIONS.items():
        header = header.replace(original, replacement)
    return header


def decision_guidance(*, bot_name, metadata):
    original = planner_prompt("", bot_name=bot_name, **metadata)
    # Keep the original social/meme rules and field definitions verbatim. The old
    # Planner-only capability boundary and JSON envelope do not apply to the Agent.
    rules = scope_direct_rules(original.split("【权限边界】", 1)[0])
    fields = original.split("【字段说明】", 1)[1].split("【本轮群聊状态】", 1)[0]
    state = original.split("【本轮群聊状态】", 1)[1].split("\n聊天内容：", 1)[0]
    return rules + "【字段说明】" + fields + "【本轮群聊状态】" + state


def reply_guidance(*, reasoning, **arguments):
    if reasoning:
        original = reasoning_reply_prompt(**arguments)
        old = (
            "### 输出格式（必须是有效的 JSON）\n"
            '{"thinking":"你的推理分析过程","reply":"最终回复内容"}\n\n'
            "重要：只输出 JSON，不要输出其他任何内容！"
        )
        # This fixed block precedes all chat/persona/memory content.
        head, tail = original.split(old, 1)
        return head + TOOL_OUTPUT + tail
    arguments = {k: v for k, v in arguments.items() if k not in {"user_name", "user_input"}}
    original = reply_prompt(**arguments)
    head, tail = original.split('【输出格式】只输出 JSON：{"reply":"最终回复"}', 1)
    ending = "\n现在按上述格式输出 JSON："
    if not tail.endswith(ending):
        raise ValueError("Protected Replyer output contract changed; adapter needs review")
    return scope_direct_rules(head) + TOOL_OUTPUT + tail.removesuffix(ending)
