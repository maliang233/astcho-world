from __future__ import annotations

import time

from astcho.domain.models import ChatMessage


def normalize_event(
    payload: dict, bot_id: str, *, parsed_event=None
) -> tuple[str, dict, dict] | None:
    """Use the original identity/direction parsers, without registering automatic handlers."""
    from nonebot.adapters.onebot.v11 import GroupMessageEvent, PrivateMessageEvent

    from astcho.handlers.common import at_targets_of, reply_details_of, text_of
    from astcho.handlers.group import is_bot_mentioned, qq_nickname

    if payload.get("post_type") not in {"message", "message_sent"}:
        return None
    if str(payload.get("self_id")) != str(bot_id):
        raise ValueError("Event belongs to a different QQ account")
    data = {**payload, "post_type": "message"}
    if data.get("message_type") == "group":
        event = parsed_event if parsed_event is not None else GroupMessageEvent.model_validate(data)
        chat_id, kind = f"group:{event.group_id}", "group"
        nickname = qq_nickname(event)
        mentioned = is_bot_mentioned(event, bot_id)
    elif data.get("message_type") == "private":
        event = (
            parsed_event if parsed_event is not None else PrivateMessageEvent.model_validate(data)
        )
        chat_id, kind = f"private:{event.user_id}", "private"
        nickname = event.sender.nickname or str(event.user_id)
        mentioned = False
    else:
        return None
    reply_id, reply_uid, reply_name, quote = reply_details_of(event)
    message = ChatMessage(
        message_id=str(event.message_id),
        user_id=str(bot_id) if payload.get("post_type") == "message_sent" else str(event.user_id),
        nickname=nickname,
        text=text_of(event),
        timestamp=time.time(),
        mentioned_bot=mentioned,
        replied_to_bot=reply_uid == str(bot_id),
        reply_to_message_id=reply_id,
        reply_to_user_id=reply_uid,
        reply_to_nickname=reply_name,
        reply_text=quote,
        at_targets=at_targets_of(event, bot_id),
        is_bot=payload.get("post_type") == "message_sent" or str(event.user_id) == str(bot_id),
    )
    return chat_id, {**message.model_dump(), "raw_event": payload}, {"kind": kind}
