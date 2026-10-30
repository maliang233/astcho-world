"""Optional one-message QQ acknowledgement, separate from final social decisions."""

from __future__ import annotations

import json
import time
import uuid
from types import SimpleNamespace

from nonebot.adapters.onebot.v11 import Message, MessageSegment

from astcho.domain.models import ChatMessage, ReplyPayload


async def send_preamble(app, args, operation, activity_id):
    if ("observation_id" in args) == ("chat_id" in args):
        raise ValueError("Choose an observed group or a configured administrator private chat")
    if not app.config.get("conversation", {}).get("allow_send"):
        return {"status": "unavailable", "reason": "QQ delivery is disabled"}
    text = ReplyPayload(reply=args["text"]).reply
    target_id = args["target_message_id"]
    snapshot = None
    if "observation_id" in args:
        if app.group_mode != "agent":
            return {"status": "unavailable", "reason": "Group uses the legacy participation route"}
        snapshot = app.snapshot(args["observation_id"])
        if snapshot["kind"] != "group":
            raise ValueError("Private preambles require a verified administrator chat")
        chat = snapshot["chat_id"]
        if not any(
            m["message_id"] == target_id and not m["is_bot"] and m["user_id"] != app.bot_id
            for m in snapshot["messages"]
        ):
            raise ValueError("Preamble target must be an observed incoming group message")
    else:
        chat = args["chat_id"]
        app.admin_target(chat, target_id)
    request = json.dumps(
        {"chat_id": chat, "target_message_id": target_id, "text": text}, sort_keys=True
    )
    # The second key blocks retargeting/re-reading within an activity. The first
    # survives new snapshots, new operation IDs, later wakes and process restarts.
    previous = app.db.execute(
        "SELECT * FROM preambles WHERE (chat=? AND target=?) OR (activity=? AND chat=?)",
        (chat, target_id, activity_id, chat),
    ).fetchall()
    if previous:
        same = next((row for row in previous if row["request"] == request), None)
        if same:
            receipt = json.loads(same["result"]) if same["result"] else {"status": "unknown"}
            if receipt.get("status") == "running":
                receipt.update(status="unknown", reason="Interrupted preamble; not replayed")
            return receipt
        return {
            "status": "limit_reached",
            "reason": "Preamble already attempted for this reply/chat",
        }
    if snapshot is not None:
        if app.db.execute(
            "SELECT 1 FROM participation WHERE observation=?", (snapshot["observation_id"],)
        ).fetchone():
            return {"status": "unavailable", "reason": "Observation already has a social decision"}
        backend = app.group_backend()
        receiver, _, target = backend.target(snapshot, target_id)
        bot = backend.bot
        event = SimpleNamespace(group_id=receiver)
        quote = target.message_id if target.mentioned_bot or target.replied_to_bot else None
    else:
        bot = app.delivery_bot()
        event = SimpleNamespace(user_id=chat.removeprefix("private:"))
        quote = None
    message = Message()
    if quote:
        message += MessageSegment.reply(int(quote))
    message += MessageSegment.text(text)  # Exactly one QQ send, never split or parsed as CQ.
    identifier = uuid.uuid4().hex
    receipt = {
        "status": "running",
        "kind": "preamble",
        "chat_id": chat,
        "target_message_id": target_id,
    }
    with app.db:
        app.db.execute(
            "INSERT INTO preambles VALUES(?,?,?,?,?,?,?)",
            (identifier, activity_id, chat, target_id, request, operation, json.dumps(receipt)),
        )

    def save():
        app.db.execute(
            "UPDATE preambles SET result=? WHERE id=?", (json.dumps(receipt), identifier)
        )

    try:
        result = await bot.send(event, message)
        sent = ChatMessage(
            message_id=str(result["message_id"]),
            user_id=app.bot_id,
            nickname=app.bot_name,
            text=text,
            timestamp=time.time(),
            is_bot=True,
        )
        receipt.update(
            status="completed", sent_message_id=sent.message_id, sent_message=sent.model_dump()
        )
        with app.db:
            app.db.execute(
                "INSERT OR IGNORE INTO messages(chat,mid,body) VALUES(?,?,?)",
                (chat, sent.message_id, sent.model_dump_json()),
            )
            if snapshot is not None:
                state = app.attention_state(chat)
                state["last_bot_reply"] = max(state.get("last_bot_reply", 0), sent.timestamp)
                app.db.execute("UPDATE chats SET attention=? WHERE id=?", (json.dumps(state), chat))
            save()
        if snapshot is not None and receiver in backend.runtime.attention:
            backend.runtime.attention[receiver].add(sent)
        return receipt
    except BaseException as exc:
        receipt.update(status="unknown", reason=f"Preamble interrupted: {type(exc).__name__}")
        with app.db:
            save()
        if not isinstance(exc, Exception):
            raise
        return receipt
    finally:
        if snapshot is None and hasattr(bot, "client"):
            await bot.client.aclose()
