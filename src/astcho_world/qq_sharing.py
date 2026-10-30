"""Proactive links: recipient context, burst protection and durable receipts."""

import contextlib
import json
import math
import re
import time
from types import SimpleNamespace
from urllib.parse import urlsplit, urlunsplit

from astcho.domain.models import ChatMessage
from astcho_world.contracts import object_schema

SHARE_ITEM_SCHEMA = object_schema(
    {
        "url": {"type": "string"},
        "caption_preview": {"type": "string"},
        "at": {"type": ["number", "null"]},
        "status": {"enum": ["completed", "unknown"]},
    },
    ("url", "caption_preview", "at", "status"),
)
TARGET_SCHEMA = object_schema(
    {
        "chat_id": {"type": "string"},
        "kind": {"enum": ["group", "private_admin"]},
        "last_shared_at": {"type": ["number", "null"]},
        "shared_last_24h": {"type": "integer", "minimum": 0},
        "unknown_count": {"type": "integer", "minimum": 0},
        "group_next_allowed_at": {"type": ["number", "null"]},
        "recent_shares": {"type": "array", "items": SHARE_ITEM_SCHEMA},
    },
    (
        "chat_id",
        "kind",
        "last_shared_at",
        "shared_last_24h",
        "unknown_count",
        "group_next_allowed_at",
        "recent_shares",
    ),
)
SHARING_SCHEMA = object_schema(
    {
        "preferred_private_targets": {"type": "array", "items": {"type": "string"}},
        "recent_targets": {"type": "array", "items": TARGET_SCHEMA},
        "group_interval_seconds": {"type": "number", "minimum": 0},
        "group_context_max_age_seconds": {"type": "number", "minimum": 0},
    },
    (
        "preferred_private_targets",
        "recent_targets",
        "group_interval_seconds",
        "group_context_max_age_seconds",
    ),
)


def policy(app):
    values = app.config.get("sharing", {})
    result = {
        "group_interval_seconds": values.get("group_interval_seconds", 600),
        "group_context_max_age_seconds": values.get("group_context_max_age_seconds", 300),
    }
    if any(
        isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v < 0
        for v in result.values()
    ):
        raise ValueError("Sharing intervals must be finite non-negative seconds")
    return result


def ensure_schema(app):
    policy(app)
    with app.db:
        app.db.execute("""CREATE TABLE IF NOT EXISTS link_shares (
            operation TEXT PRIMARY KEY, chat TEXT, url TEXT, text TEXT, result TEXT,
            created REAL, request TEXT)""")
        columns = {r[1] for r in app.db.execute("PRAGMA table_info(link_shares)")}
        for name, sql_type in (("created", "REAL"), ("request", "TEXT")):
            if name not in columns:
                app.db.execute(f"ALTER TABLE link_shares ADD COLUMN {name} {sql_type}")
        # Recover actual timestamps from our confirmed outgoing messages only.
        # An old unknown attempt has no invented timestamp or new receipt.
        app.db.execute("""UPDATE link_shares SET created=(
            SELECT json_extract(m.body,'$.timestamp') FROM messages m
            WHERE m.chat=link_shares.chat
            AND json_extract(m.body,'$.is_bot')=1
            AND m.mid=json_extract(link_shares.result,'$.sent_message_id'))
            WHERE created IS NULL AND json_extract(result,'$.status')='completed'""")
        app.db.execute("CREATE INDEX IF NOT EXISTS shares_recipient ON link_shares(chat,created)")


def recipient(app, chat, *, for_send=False):
    row = app.db.execute("SELECT kind FROM chats WHERE id=?", (chat,)).fetchone()
    receiver = chat.split(":", 1)[-1]
    group = bool(chat.startswith("group:") and row and row[0] == "group")
    admin = chat == f"private:{receiver}" and receiver in app.config.get("private_admins", [])
    if not (group or admin) or (for_send and not receiver.isdecimal()):
        raise ValueError("Sharing target must be a known group or configured private administrator")
    return "group" if group else "private_admin"


def records(app, chat, *, limit=None, unknown_only=False):
    result = []
    where = "chat=?"
    params = [chat]
    if unknown_only:
        where += " AND COALESCE(json_extract(result,'$.status'),'unknown')!='completed'"
    sql = "SELECT * FROM link_shares WHERE " + where + " ORDER BY created DESC,rowid DESC"
    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit)
    for row in app.db.execute(sql, params):
        receipt = json.loads(row["result"]) if row["result"] else {}
        status = "completed" if receipt.get("status") == "completed" else "unknown"
        result.append(
            {
                "url": row["url"],
                "caption_preview": row["text"][:120],
                "at": row["created"],
                "status": status,
            }
        )
    return result


def target_context(app, chat, now):
    kind = recipient(app, chat)
    last_shared, recent_count, unknown_count, last = app.db.execute(
        "SELECT max(CASE WHEN json_extract(result,'$.status')='completed' THEN created END),"
        "count(CASE WHEN json_extract(result,'$.status')='completed' AND created>=? THEN 1 END),"
        "count(CASE WHEN COALESCE(json_extract(result,'$.status'),'unknown')!='completed' "
        "THEN 1 END),max(created) FROM link_shares WHERE chat=?",
        (now - 86400, chat),
    ).fetchone()
    return {
        "chat_id": chat,
        "kind": kind,
        "last_shared_at": last_shared,
        "shared_last_24h": recent_count,
        "unknown_count": unknown_count,
        "group_next_allowed_at": last + policy(app)["group_interval_seconds"]
        if kind == "group" and last is not None
        else None,
        "recent_shares": records(app, chat, limit=3),
    }


def sharing_state(app):
    now = time.time()
    admins = [f"private:{user}" for user in sorted(app.config.get("private_admins", []))]
    # Own action metadata only. No private inbox or unread message bodies.
    targets = [
        r[0]
        for r in app.db.execute(
            "SELECT chat FROM link_shares GROUP BY chat ORDER BY max(created) DESC LIMIT 30"
        )
    ]
    targets = list(dict.fromkeys([*admins, *targets]))
    contexts = []
    for chat in targets:
        with contextlib.suppress(ValueError):
            contexts.append(target_context(app, chat, now))
    return {"preferred_private_targets": admins, "recent_targets": contexts, **policy(app)}


def share_context(app, chat=None):
    result = {
        "scope": "own sharing history, not unread conversations",
        "updated": time.time(),
        "sharing": sharing_state(app),
    }
    if chat:
        result["target"] = target_context(app, chat, result["updated"])
    return result


def resource_key(url):
    parsed = urlsplit(url)
    if parsed.hostname in {"bilibili.com", "www.bilibili.com", "m.bilibili.com"}:
        video = re.match(r"/video/(BV[0-9A-Za-z]{10}|av[0-9]+)(?:/|$)", parsed.path)
        if video:
            return "bilibili:" + video[1]
    return urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), parsed.path, parsed.query, ""))


def not_sent(reason, **facts):
    return {
        "status": "not_sent",
        "reason": reason,
        **facts,
        "notice": "This action was not sent. No automatic retry, reminder or queued delivery."
        " Reconsider whether sharing is needed; do not bypass via another target or tool.",
    }


def group_admission(app, args, now):
    chat = args["chat_id"]
    context = target_context(app, chat, now)
    due = context["group_next_allowed_at"]
    if due is not None and now < due:
        return not_sent("group_share_cooldown", next_allowed_at=due, target=context)
    if not args.get("audience_reason", "").strip():
        return not_sent("audience_reason_required")
    snapshot_id = args.get("observation_id")
    row = app.db.execute("SELECT * FROM snapshots WHERE id=?", (snapshot_id,)).fetchone()
    if row is None or row["chat"] != chat:
        return not_sent("read_group_context_first")
    if now - row["created"] > policy(app)["group_context_max_age_seconds"]:
        return not_sent("group_context_expired")
    snapshot = json.loads(row["body"])
    last_id = snapshot["messages"][-1]["message_id"] if snapshot["messages"] else None
    last = app.db.execute(
        "SELECT seq FROM messages WHERE chat=? AND mid=?", (chat, last_id)
    ).fetchone()
    # Only a count is inspected here. New unread bodies must be opened explicitly.
    unseen = app.db.execute(
        "SELECT count(*) FROM messages WHERE chat=? AND seq>? "
        "AND json_extract(body,'$.is_bot')=0 AND json_extract(body,'$.user_id')!=?",
        (chat, last[0] if last else 0, app.bot_id),
    ).fetchone()[0]
    if unseen:
        return not_sent("group_context_changed", newer_messages=unseen)
    return None


async def share_link(app, args, operation):
    from nonebot.adapters.onebot.v11 import Message, MessageSegment

    chat, url = args["chat_id"], args["url"]
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username:
        raise ValueError("An ordinary HTTP(S) resource URL is required")
    kind = recipient(app, chat, for_send=True)
    request = json.dumps(args, sort_keys=True, ensure_ascii=False)
    # Receipts take precedence over changed context, cooldown and transport state.
    old = app.db.execute("SELECT * FROM link_shares WHERE operation=?", (operation,)).fetchone()
    if old:
        if (old["chat"], old["url"], old["text"]) != (chat, url, args.get("text", "")) or (
            old["request"] is not None and old["request"] != request
        ):
            raise ValueError("Share operation ID reused")
        return (
            json.loads(old["result"])
            if old["result"]
            else {"status": "unknown", "reason": "Not replayed"}
        )
    now = time.time()
    if any(
        r["status"] == "unknown" and resource_key(r["url"]) == resource_key(url)
        for r in records(app, chat, unknown_only=True)
    ):
        return not_sent("previous_share_outcome_unknown")
    if kind == "group":
        denied = group_admission(app, args, now)
        if denied:
            return denied
    quote = args.get("quote_message_id")
    if quote:
        if not args.get("observation_id"):
            return not_sent("quote_observation_required")
        observation = app.snapshot(args["observation_id"])
        if observation["chat_id"] != chat or not any(
            m["message_id"] == quote for m in observation["messages"]
        ):
            raise ValueError("Quote must be an actually observed message in this chat")
    if not app.config.get("conversation", {}).get("allow_send"):
        return {"status": "unavailable", "reason": "QQ delivery is disabled"}
    with app.db:
        app.db.execute(
            "INSERT INTO link_shares(operation,chat,url,text,result,created,request) "
            "VALUES(?,?,?,?,NULL,?,?)",
            (operation, chat, url, args.get("text", ""), now, request),
        )
    text = (args["text"] + "\n" if args.get("text") else "") + url
    message = Message()
    if quote:
        message += MessageSegment.reply(quote)
    message += MessageSegment.text(text)
    bot = None
    try:
        bot = app.delivery_bot()
        result = await bot.send(
            SimpleNamespace(group_id=int(chat.split(":", 1)[1]), user_id=int(chat.split(":", 1)[1]))
            if kind == "group"
            else SimpleNamespace(user_id=int(chat.split(":", 1)[1])),
            message,
        )
        sent_id = str(result["message_id"])
    except BaseException:
        with app.db:
            app.db.execute(
                "UPDATE link_shares SET result=? WHERE operation=?",
                (
                    json.dumps(
                        {"status": "unknown", "url": url, "chat_id": chat, "attempted_at": now}
                    ),
                    operation,
                ),
            )
        raise
    finally:
        if bot is not None and hasattr(bot, "client"):
            with contextlib.suppress(Exception):
                await bot.client.aclose()
    shared_at = time.time()
    receipt = {
        "status": "completed",
        "sent_message_id": sent_id,
        "url": url,
        "chat_id": chat,
        "format": "plain_link",
        "shared_at": shared_at,
    }
    sent = ChatMessage(
        message_id=sent_id,
        user_id=app.bot_id,
        nickname=app.bot_name,
        text=text,
        timestamp=shared_at,
        is_bot=True,
    )
    with app.db:
        app.db.execute(
            "INSERT OR IGNORE INTO messages(chat,mid,body) VALUES(?,?,?)",
            (chat, sent_id, sent.model_dump_json()),
        )
        app.db.execute(
            "UPDATE link_shares SET result=?,created=? WHERE operation=?",
            (json.dumps(receipt), shared_at, operation),
        )
    return receipt
