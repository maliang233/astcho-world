from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
import uuid

from astcho.domain.models import ChatMessage, PlannerDecision, ReplyPayload
from astcho.services.attention import AttentionService
from astcho.services.schedule import ScheduleState
from astcho_world.contracts import Manifest, Tool, object_schema
from astcho_world.qq_notifications import (
    NotificationAttention,
    empty_addressing,
    notification_candidate,
    raw_segments,
)
from astcho_world.qq_sharing import SHARING_SCHEMA, ensure_schema, share_context, sharing_state

ID = {"type": "string", "minLength": 1, "maxLength": 200}
READ_SCHEMA = object_schema(
    {
        "chat_id": ID,
        "limit": {"type": "integer", "minimum": 1, "maximum": 100},
        "before_sequence": {"type": "integer", "minimum": 1},
        "anchor_message_id": ID,
    },
    ("chat_id",),
)
SOCIAL_FIELDS = {
    "observation_id": ID,
    "target_message_id": ID,
    "reason": {"type": "string", "maxLength": 200},
    "excitement": {"type": "number", "minimum": -0.2, "maximum": 0.2},
    "shyness": {"type": "number", "minimum": -0.2, "maximum": 0.2},
    "affinity_score": {"type": "number", "minimum": -1, "maximum": 1},
}
PREAMBLE_SCHEMA = object_schema(
    {
        "observation_id": ID,
        "chat_id": ID,
        "target_message_id": ID,
        "text": {"type": "string", "minLength": 1, "maxLength": 1000},
    },
    ("target_message_id", "text"),
)
PREAMBLE_SCHEMA["oneOf"] = [
    {"required": ["observation_id"], "not": {"required": ["chat_id"]}},
    {"required": ["chat_id"], "not": {"required": ["observation_id"]}},
]
MANIFEST = Manifest(
    app_id="qq",
    version="0.6.3",
    description="查看 QQ、读取聊天状态并自主组织和发送群聊回复",
    entrypoint="astcho_world.apps.qq:QQ",
    events=["message_received", "notification"],
    ambient_schema=object_schema(
        {
            "sharing": SHARING_SCHEMA,
            "chats": {
                "type": "array",
                "items": object_schema(
                    {
                        "id": ID,
                        "title": {"type": "string"},
                        "muted": {"type": "integer", "enum": [0, 1]},
                        "unread": {"type": "integer", "minimum": 0},
                    },
                    ("id", "title", "muted", "unread"),
                ),
            },
        },
        ("chats",),
    ),
    dashboard={
        "version": 1,
        "name": "QQ",
        "icon": "Q",
        "activities": {
            "read_messages": {
                "kind": "activity.phone",
                "label": "正在看群消息",
                "fallback": "base.idle",
                "hold_seconds": 20,
            },
            "reply_context": {
                "kind": "activity.phone",
                "label": "正在查看聊天上下文",
                "fallback": "base.idle",
                "hold_seconds": 20,
            },
            "reply_group": {
                "kind": "activity.phone",
                "label": "正在回复群聊",
                "fallback": "base.idle",
                "hold_seconds": 20,
            },
            "reply_admin": {
                "kind": "activity.phone",
                "label": "正在回复管理员",
                "fallback": "base.idle",
                "hold_seconds": 20,
            },
            "preamble": {
                "kind": "activity.phone",
                "label": "正在发消息",
                "fallback": "base.idle",
                "hold_seconds": 20,
            },
            "share_link": {
                "kind": "activity.phone",
                "label": "正在分享链接",
                "fallback": "base.idle",
                "hold_seconds": 20,
            },
            "view_image": {
                "kind": "activity.phone",
                "label": "正在查看聊天图片",
                "fallback": "base.idle",
                "hold_seconds": 20,
            },
            "search_messages": {
                "kind": "activity.phone",
                "label": "正在查找聊天记录",
                "fallback": "base.idle",
                "hold_seconds": 20,
            },
        },
        "views": [
            {
                "id": "records",
                "title": "应用记录",
                "source": "notifications",
                "component": "table",
                "columns": {"source": "来源", "message": "触发消息", "state": "送达状态"},
            }
        ],
    },
    notification_tool="read_messages",
    tools=[
        Tool(
            name="share_context",
            description="查看已配置管理员私聊入口及面向接收人的近期分享：最近时间、"
            "24小时内次数、已分享链接与未知回执。不读任何未读正文，不发送；"
            "群的冷却到期不代表应该发送。可指定 chat_id 查看该目标。",
            input_schema=object_schema({"chat_id": ID}),
        ),
        Tool(
            name="shared_links",
            description="查找自己曾分享的资源和真实消息ID；"
            "收到旧分享的引用后，可按 sent_message_id 找回原视频链接。不会发送或读取未读消息。",
            input_schema=object_schema({"sent_message_id": ID, "chat_id": ID}),
        ),
        Tool(
            name="share_link",
            description="向已知群或已配置管理员主动分享已经观察过的链接。"
            "url 来自其他工具的真实资源；可只发链接，也可附自己正文。不要自制分享卡片。"
            "附言遵守环境中的原版说话规则，私聊沿用原私聊要求，群聊结合对应 reply_context；"
            "可选引用需 observation_id 和 quote_message_id。"
            "优先私聊分享给管理员。主动群分享须有新近该群观察（默认5分钟）和具体 audience_reason；"
            "理由仅内部留存，不发送。群分享受间隔保护，not_sent 表示没有发送，不自动补发。"
            "同一发送意图重试必须沿用 intent_id，确实重新分享才换新意图。",
            input_schema=object_schema(
                {
                    "chat_id": ID,
                    "intent_id": ID,
                    "url": {"type": "string"},
                    "text": {"type": "string", "maxLength": 4000},
                    "observation_id": ID,
                    "quote_message_id": ID,
                    "audience_reason": {"type": "string", "minLength": 1, "maxLength": 400},
                },
                ("chat_id", "url", "intent_id"),
            ),
            effect="write",
            timeout=120,
            observed_url_fields=["url"],
            idempotency_field="intent_id",
        ),
        Tool(
            name="list_chats",
            description="查看会话列表、未读数和免打扰状态",
            input_schema=object_schema({}),
        ),
        Tool(
            name="read_messages",
            description="主动读取消息；群聊返回观察快照和默认回复对象的原版聊天信息与要求。"
            "participation 提供近期自己的已知发言与当前快照内的交流概况，仅供自行判断。"
            "打开范围内的图片会沿用原视觉服务初步理解，media 逐条标明状态和图片序号。"
            "可用 view_image 针对图片提问，也可根据已有文本回复；换回复对象时可用 reply_context。",
            input_schema=READ_SCHEMA,
            timeout=120,
        ),
        Tool(
            name="view_image",
            description="仔细查看已观察消息中的指定图片，向视觉模型提出具体问题。"
            "提供 observation_id、message_id、该消息内从 1 开始的 image_index 和 question。"
            "使用保留的原图，可换问题反复查看；返回文字观察，不代表主体直接看到了原图。"
            "不发送消息、不收藏表情、不改写聊天的初步描述。",
            input_schema=object_schema(
                {
                    "observation_id": ID,
                    "message_id": ID,
                    "image_index": {"type": "integer", "minimum": 1},
                    "question": {"type": "string", "minLength": 1, "maxLength": 4000},
                },
                ("observation_id", "message_id", "question"),
            ),
            timeout=120,
        ),
        Tool(
            name="reply_context",
            description="读取已观察群友消息对应的记忆、关系、情绪、日程及原版群聊要求；"
            "不生成回复、不执行社交判断、不读取快照外的新消息。",
            input_schema=object_schema(
                {"observation_id": ID, "target_message_id": ID},
                ("observation_id", "target_message_id"),
            ),
            timeout=120,
        ),
        Tool(
            name="preamble",
            description="可选地向正在回应的对象发一条简短行动前说明，之后可继续使用工具并答复。"
            "群聊使用 observation_id；管理员私聊使用 chat_id；二选一，均需真实目标消息 ID。"
            "是否有必要和具体措辞由你判断，遵守环境中的原版说话规则及对应群聊/私聊要求。"
            "同一活动的同一会话最多一次，同一目标消息也最多一次；重读或重试不能增加次数。"
            "只发一条字面正文，不分段、不配图、不结算情绪或长期记忆，不占最终回复位置。",
            input_schema=PREAMBLE_SCHEMA,
            effect="write",
            timeout=120,
        ),
        Tool(
            name="reply_group",
            description="把你在主体链路中组织的正文发到已观察的群聊目标；不调用 Planner/Replyer。"
            "text 只含群友可见的正文，遵循该群 reply_context 的原版要求。"
            "可附原 Planner 范围内的情绪变化与配图决定；默认均为零/不配图。"
            "同一观察快照只执行一次社交决定，结果未知时不可换 ID 重发。",
            input_schema=object_schema(
                {
                    **SOCIAL_FIELDS,
                    "text": {"type": "string", "minLength": 1, "maxLength": 1000},
                    "should_meme": {"type": "boolean"},
                    "meme_query": {"type": ["string", "null"], "maxLength": 80},
                },
                ("observation_id", "target_message_id", "text"),
            ),
            effect="write",
            timeout=120,
        ),
        Tool(
            name="no_reply",
            description="明确决定不回复一份已观察群聊，可附情绪变化；沿用原注意力状态更新。"
            "不会发送或调用模型。同一快照与 reply_group/兼容参与共用一次决定。"
            "只是阅读、搜索、忽略通知或结束活动时无需调用。",
            input_schema=object_schema(SOCIAL_FIELDS, ("observation_id", "target_message_id")),
            effect="write",
            timeout=120,
        ),
        Tool(
            name="set_notifications",
            description="设置会话免打扰；带实际内容的@和回复仍可通知，空引用/@不通知",
            effect="write",
            input_schema=object_schema(
                {"chat_id": ID, "muted": {"type": "boolean"}}, ("chat_id", "muted")
            ),
        ),
        Tool(
            name="search_messages",
            description="在已接收到的本地消息中搜索；不保证远端历史完整",
            input_schema=object_schema({"chat_id": ID, "query": ID}, ("chat_id", "query")),
        ),
        Tool(
            name="reply_admin",
            description="向已确认的管理员私聊回复任务结果或澄清问题；只允许配置中的管理员，"
            "正文遵守环境中的原版私聊说话规则，任务结果也保持相同口吻。"
            "必须指定该私聊已收到的 message_id，不能发送到群或其他用户。",
            effect="write",
            timeout=30,
            input_schema=object_schema(
                {"chat_id": ID, "message_id": ID, "text": {"type": "string", "minLength": 1}},
                ("chat_id", "message_id", "text"),
            ),
        ),
        Tool(
            name="participate",
            description="兼容原聊天链路：用于普通私聊，或明确配置为 legacy 的群聊。"
            "默认群聊使用 reply_group/no_reply；本工具会返回不可用，不会替主体生成回复。",
            effect="write",
            timeout=120,
            input_schema=object_schema({"observation_id": ID}, ("observation_id",)),
        ),
    ],
)


class QQ:
    """Passive inbox and legacy notification policy. No automatic learning or sending."""

    def __init__(self, data_dir, config):
        self.data_dir, self.config = data_dir, config
        self.conversation = None
        self.bot_id = str(
            config.get("bot_id")
            or os.getenv(config.get("bot_id_env", "ASTCHO_WORLD_BOT_ID"), "10001")
        )
        self.bot_name = config.get("bot_name", "星回")
        self.db = sqlite3.connect(data_dir / "qq.sqlite3")
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS chats (
                id TEXT PRIMARY KEY, kind TEXT, title TEXT, muted INTEGER DEFAULT 0,
                read_through INTEGER DEFAULT 0, attention TEXT DEFAULT '{}');
            CREATE TABLE IF NOT EXISTS messages (
                seq INTEGER PRIMARY KEY AUTOINCREMENT, chat TEXT, mid TEXT, body TEXT,
                UNIQUE(chat,mid));
            CREATE TABLE IF NOT EXISTS snapshots (
                id TEXT PRIMARY KEY, chat TEXT, body TEXT, created REAL);
            CREATE TABLE IF NOT EXISTS batches (id TEXT PRIMARY KEY,response TEXT);
            CREATE TABLE IF NOT EXISTS batch_members (
                batch TEXT, chat TEXT, mid TEXT, PRIMARY KEY(batch,chat,mid));
            CREATE TABLE IF NOT EXISTS participation (
                observation TEXT PRIMARY KEY, operation TEXT, result TEXT);
            CREATE TABLE IF NOT EXISTS raw_events (
                chat TEXT, mid TEXT, body TEXT, PRIMARY KEY(chat,mid));
            CREATE TABLE IF NOT EXISTS account (id INTEGER PRIMARY KEY CHECK(id=1), bot TEXT);
            CREATE TABLE IF NOT EXISTS admin_replies (
                operation TEXT PRIMARY KEY, result TEXT);
            CREATE TABLE IF NOT EXISTS group_decisions (
                observation TEXT PRIMARY KEY, request TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS preambles (
                id TEXT PRIMARY KEY, activity TEXT, chat TEXT NOT NULL, target TEXT NOT NULL,
                request TEXT NOT NULL, operation TEXT NOT NULL, result TEXT,
                UNIQUE(chat,target), UNIQUE(activity,chat));
        """)
        # One-time rollout only; later explicit choices remain authoritative.
        self.db.execute("CREATE TABLE IF NOT EXISTS migrations (id TEXT PRIMARY KEY)")
        if (
            config.get("default_group_muted")
            and not self.db.execute(
                "SELECT 1 FROM migrations WHERE id='default-group-muted'"
            ).fetchone()
        ):
            with self.db:
                self.db.execute("UPDATE chats SET muted=1 WHERE kind='group'")
                self.db.execute("INSERT INTO migrations VALUES('default-group-muted')")
        ensure_schema(self)

    async def dispatch(self, method, params):
        if method == "connect":
            account = str(params["bot_id"])
            saved = self.db.execute("SELECT bot FROM account WHERE id=1").fetchone()
            if saved and saved[0] != account:
                raise ValueError("This QQ inbox belongs to a different account")
            with self.db:
                self.db.execute("INSERT OR REPLACE INTO account VALUES(1,?)", (account,))
            self.bot_id = account
            conversation = self.config.setdefault("conversation", {"allow_send": False})
            conversation["transport"] = params["transport"]
            if params.get("schedule_path"):
                conversation["schedule_path"] = params["schedule_path"]
            return {"connected": True}
        if method == "ingest":
            return self.ingest(params)
        if method == "private_observation":
            chat = params["chat_id"]
            sender = chat.removeprefix("private:")
            if not chat.startswith("private:") or sender in self.config.get("private_admins", []):
                raise ValueError("Not an ordinary private conversation")
            snapshot = (
                "private-" + hashlib.sha256(f"{chat}:{params['message_id']}".encode()).hexdigest()
            )
            if not self.db.execute("SELECT 1 FROM snapshots WHERE id=?", (snapshot,)).fetchone():
                value = self.read(
                    {
                        "chat_id": chat,
                        "anchor_message_id": params["message_id"],
                        "_snapshot_id": snapshot,
                    }
                )
                raw = self.db.execute(
                    "SELECT body FROM raw_events WHERE chat=? AND mid=?",
                    (chat, params["message_id"]),
                ).fetchone()
                if raw:
                    value["private_nickname"] = (
                        json.loads(raw[0]).get("sender", {}).get("nickname") or "朋友"
                    )
                    with self.db:
                        self.db.execute(
                            "UPDATE snapshots SET body=? WHERE id=?", (json.dumps(value), snapshot)
                        )
            return {"observation_id": snapshot}
        if method != "call":
            raise ValueError("Unknown method")
        tool, args = params["tool"], params["arguments"]
        if tool == "share_context":
            return share_context(self, args.get("chat_id"))
        if tool == "share_link":
            from astcho_world.qq_sharing import share_link

            return await share_link(self, args, params["operation_id"])
        if tool == "shared_links":
            if not self.db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='link_shares'"
            ).fetchone():
                return {"shares": []}
            where, values = ["result IS NOT NULL"], []
            for field in ("chat_id", "sent_message_id"):
                if args.get(field):
                    where.append("json_extract(result,?)=?")
                    values.extend(["$." + field, args[field]])
            return {
                "shares": [
                    json.loads(r[0])
                    for r in self.db.execute(
                        "SELECT result FROM link_shares WHERE "
                        + " AND ".join(where)
                        + " ORDER BY rowid DESC LIMIT 30",
                        values,
                    )
                ]
            }
        if tool == "preamble":
            from astcho_world.qq_preamble import send_preamble

            return await send_preamble(
                self,
                args,
                params["operation_id"],
                params.get("logical_activity_id") or params.get("activity_id"),
            )
        if tool == "reply_admin":
            return await self.reply_admin(args, params["operation_id"])
        if tool == "list_chats":
            return {
                "chats": [
                    dict(row)
                    for row in self.db.execute(
                        """
                SELECT c.id,c.kind,c.title,c.muted,
                    (SELECT COUNT(*) FROM messages m WHERE m.chat=c.id AND m.seq>c.read_through
                    AND json_extract(m.body,'$.is_bot')=0
                    AND json_extract(m.body,'$.user_id')!=?) unread
                FROM chats c ORDER BY c.id
                """,
                        (self.bot_id,),
                    )
                ]
            }
        if tool == "read_messages":
            from astcho_world.qq_media import enrich

            snapshot = self.read(args)
            await enrich(self, snapshot)
            if snapshot["kind"] == "group":
                snapshot["reply_context"] = self.group_context(snapshot)
                snapshot["share_context"] = share_context(self, snapshot["chat_id"])
            return snapshot
        if tool == "view_image":
            from astcho_world.qq_media import view_image

            return await view_image(self, args)
        if tool == "reply_context":
            snapshot = self.snapshot(args["observation_id"])
            context = self.group_context(snapshot, args["target_message_id"])
            if "participation" in snapshot:
                context["participation"] = snapshot["participation"]
            return context
        if tool in {"reply_group", "no_reply"}:
            return await self.group_decision(tool, args, params["operation_id"])
        if tool == "set_notifications":
            with self.db:
                result = self.db.execute(
                    "UPDATE chats SET muted=? WHERE id=?", (int(args["muted"]), args["chat_id"])
                )
                if not result.rowcount:
                    raise ValueError("Unknown chat")
            return {"chat_id": args["chat_id"], "muted": args["muted"]}
        if tool == "search_messages":
            # instr performs literal matching, including '%' and '_'.
            rows = self.db.execute(
                "SELECT * FROM messages WHERE chat=? AND instr(body,?)>0 "
                "ORDER BY seq DESC LIMIT 30",
                (args["chat_id"], args["query"]),
            ).fetchall()
            return {
                "messages": [json.loads(row["body"]) for row in rows],
                "history_scope": "messages received by this installation",
            }
        if tool == "participate":
            if not self.config.get("conversation", {}).get("allow_send", False):
                return {"status": "unavailable", "reason": "Conversation backend not enabled"}
            row = self.db.execute(
                "SELECT * FROM snapshots WHERE id=?", (args["observation_id"],)
            ).fetchone()
            if row is None:
                raise ValueError("Unknown observation snapshot")
            snapshot = json.loads(row["body"])
            if snapshot.get("requires_media") and not (
                snapshot["kind"] == "private"
                and self.config.get("conversation", {}).get("private_direct")
            ):
                return {
                    "status": "unavailable",
                    "reason": "Some observed media remain unrecognized; legacy participation "
                    "requires understood media. Agent mode can decide from available text.",
                }
            if snapshot["kind"] == "group" and self.group_mode != "legacy":
                return {
                    "status": "unavailable",
                    "reason": "Group replies are composed by the subject; use reply_group/no_reply",
                }
            previous = self.db.execute(
                "SELECT result FROM participation WHERE observation=?", (args["observation_id"],)
            ).fetchone()
            if previous:
                return (
                    json.loads(previous[0])
                    if previous[0]
                    else {
                        "status": "unknown",
                        "reason": "Previous participation was interrupted; not replayed",
                    }
                )
            from astcho_world.conversation import Conversation

            if self.conversation is None:
                self.conversation = Conversation(
                    self.data_dir,
                    {
                        **self.config["conversation"],
                        "expected_bot_id": self.bot_id,
                    },
                )
            state = json.loads(
                self.db.execute(
                    "SELECT attention FROM chats WHERE id=?", (row["chat"],)
                ).fetchone()[0]
            )
            with self.db:
                self.db.execute(
                    "INSERT INTO participation VALUES(?,?,NULL)",
                    (args["observation_id"], params["operation_id"]),
                )
            result = await self.conversation.participate(json.loads(row["body"]), state)
            with self.db:
                for item in result.get("sent", []):
                    sent = ChatMessage.model_validate(item)
                    self.db.execute(
                        "INSERT OR IGNORE INTO messages(chat,mid,body) VALUES(?,?,?)",
                        (row["chat"], sent.message_id, sent.model_dump_json()),
                    )
                if "attention" in result:
                    self.db.execute(
                        "UPDATE chats SET attention=? WHERE id=?",
                        (json.dumps(result["attention"]), row["chat"]),
                    )
                receipt = {
                    "status": result["status"],
                    "sent_message_ids": [m["message_id"] for m in result.get("sent", [])],
                }
                self.db.execute(
                    "UPDATE participation SET result=? WHERE observation=?",
                    (json.dumps(receipt), args["observation_id"]),
                )
            # Chat output is already delivered; the environment must not forward it again.
            return receipt
        raise ValueError("Unknown tool")

    @property
    def group_mode(self):
        mode = self.config.get("conversation", {}).get("group_reply_mode", "agent")
        if mode not in {"agent", "legacy"}:
            raise ValueError("group_reply_mode must be agent or legacy")
        return mode

    def snapshot(self, observation_id):
        row = self.db.execute("SELECT body FROM snapshots WHERE id=?", (observation_id,)).fetchone()
        if row is None:
            raise ValueError("Unknown observation snapshot")
        return json.loads(row[0])

    def group_backend(self):
        from astcho_world.conversation import Conversation
        from astcho_world.group_chat import GroupChat

        if self.conversation is None:
            self.conversation = Conversation(
                self.data_dir, {**self.config["conversation"], "expected_bot_id": self.bot_id}
            )
        self.conversation.initialize()
        return GroupChat(self.conversation.runtime, self.conversation.bot)

    def attention_state(self, chat_id):
        return json.loads(
            self.db.execute("SELECT attention FROM chats WHERE id=?", (chat_id,)).fetchone()[0]
        )

    def group_context(self, snapshot, target_id=None):
        if snapshot["kind"] != "group":
            raise ValueError("Group context requires a group observation")
        if not self.config.get("conversation", {}).get("allow_send"):
            return {"status": "unavailable", "reason": "Conversation backend not enabled"}
        try:
            return self.group_backend().context(
                snapshot, self.attention_state(snapshot["chat_id"]), target_id
            )
        except ValueError as exc:
            if target_id is not None:
                raise
            return {"status": "unavailable", "reason": str(exc)}
        except Exception as exc:
            # Preserve the observed messages even if the optional cognition backend fails.
            return {
                "status": "unavailable",
                "reason": f"Chat context unavailable: {type(exc).__name__}",
            }

    async def group_decision(self, tool, args, operation):
        if self.group_mode != "agent":
            return {"status": "unavailable", "reason": "Group uses the legacy participation route"}
        if not self.config.get("conversation", {}).get("allow_send"):
            return {"status": "unavailable", "reason": "Conversation backend not enabled"}
        snapshot = self.snapshot(args["observation_id"])
        if snapshot["kind"] != "group":
            raise ValueError("Group reply tools cannot target private messages")
        decision = PlannerDecision(
            action="reply" if tool == "reply_group" else "no_reply",
            **{k: v for k, v in args.items() if k not in {"observation_id", "text"}},
        )
        text = ReplyPayload(reply=args["text"]).reply if tool == "reply_group" else None
        request = json.dumps({"decision": decision.model_dump(), "text": text}, sort_keys=True)
        observation = args["observation_id"]
        old = self.db.execute(
            "SELECT request FROM group_decisions WHERE observation=?", (observation,)
        ).fetchone()
        previous = self.db.execute(
            "SELECT result FROM participation WHERE observation=?", (observation,)
        ).fetchone()
        if previous:
            receipt = json.loads(previous[0]) if previous[0] else {"status": "unknown"}
            if old is None or old[0] != request:
                return {
                    "status": "conflict",
                    "reason": "Observation already has a social decision",
                    "previous": receipt,
                }
            if receipt.get("status") == "running":
                receipt["status"] = "unknown"
                receipt["reason"] = "Interrupted decision; not replayed"
            return receipt
        backend = self.group_backend()
        backend.target(snapshot, decision.target_message_id)  # Validate before any mutation.
        state = self.attention_state(snapshot["chat_id"])
        receipt = {
            "status": "running",
            "action": decision.action,
            "target_message_id": decision.target_message_id,
            "sent_message_ids": [],
            "sent_messages": [],
        }

        def save_receipt():
            self.db.execute(
                "UPDATE participation SET result=? WHERE observation=?",
                (json.dumps(receipt), observation),
            )

        with self.db:
            self.db.execute("INSERT INTO group_decisions VALUES(?,?)", (observation, request))
            self.db.execute(
                "INSERT INTO participation VALUES(?,?,?)",
                (observation, operation, json.dumps(receipt)),
            )

        def on_state(value):
            receipt["attention"] = value
            with self.db:
                self.db.execute(
                    "UPDATE chats SET attention=? WHERE id=?",
                    (json.dumps(value), snapshot["chat_id"]),
                )
                save_receipt()

        def on_sent(message):
            receipt["sent_message_ids"].append(message.message_id)
            receipt["sent_messages"].append(message.model_dump())
            with self.db:
                self.db.execute(
                    "INSERT OR IGNORE INTO messages(chat,mid,body) VALUES(?,?,?)",
                    (snapshot["chat_id"], message.message_id, message.model_dump_json()),
                )
                save_receipt()

        try:
            extra = await backend.execute(snapshot, state, decision, text, on_state, on_sent)
            receipt.update(extra)
            receipt["status"] = "completed"
        except BaseException as exc:
            receipt.update(status="unknown", reason=f"Decision interrupted: {type(exc).__name__}")
            with self.db:
                save_receipt()
            if not isinstance(exc, Exception):
                raise
            return receipt
        with self.db:
            save_receipt()
        return receipt

    def admin_target(self, chat_id, message_id):
        receiver = chat_id.removeprefix("private:")
        if (
            chat_id != f"private:{receiver}"
            or not receiver.isdecimal()
            or receiver not in self.config.get("private_admins", [])
        ):
            raise ValueError("Recipient is not a configured private administrator")
        row = self.db.execute(
            "SELECT body FROM messages WHERE chat=? AND mid=?",
            (chat_id, message_id),
        ).fetchone()
        message = json.loads(row[0]) if row else None
        if not message or message["is_bot"] or message["user_id"] != receiver:
            raise ValueError("Reply requires an actual incoming administrator message")
        return receiver

    def delivery_bot(self):
        transport = self.config.get("conversation", {}).get("transport", {})
        if transport.get("kind") == "reverse_ws":
            from astcho_world.reverse_ws import WSDelivery

            return WSDelivery(self.bot_id, transport["socket_path"])
        from astcho_world.conversation import OneBotDelivery

        return OneBotDelivery(
            self.bot_id,
            os.environ["ASTCHO_WORLD_QQ_API"],
            os.getenv("ASTCHO_WORLD_QQ_TOKEN", ""),
        )

    async def reply_admin(self, args, operation):
        from types import SimpleNamespace

        from nonebot.adapters.onebot.v11 import Message, MessageSegment

        receiver = self.admin_target(args["chat_id"], args["message_id"])
        if not self.config.get("conversation", {}).get("allow_send"):
            return {"status": "unavailable", "reason": "QQ delivery is disabled"}
        old = self.db.execute(
            "SELECT result FROM admin_replies WHERE operation=?", (operation,)
        ).fetchone()
        if old:
            return json.loads(old[0]) if old[0] else {"status": "unknown", "reason": "Not replayed"}
        bot = self.delivery_bot()
        with self.db:
            self.db.execute("INSERT INTO admin_replies VALUES(?,NULL)", (operation,))
        try:
            result = await bot.send(
                SimpleNamespace(user_id=receiver), Message(MessageSegment.text(args["text"]))
            )
        finally:
            if hasattr(bot, "client"):
                await bot.client.aclose()
        sent = ChatMessage(
            message_id=str(result["message_id"]),
            user_id=self.bot_id,
            nickname=self.bot_name,
            text=args["text"],
            timestamp=time.time(),
            is_bot=True,
        )
        receipt = {"status": "completed", "sent_message_id": sent.message_id}
        with self.db:
            self.db.execute(
                "INSERT OR IGNORE INTO messages(chat,mid,body) VALUES(?,?,?)",
                (args["chat_id"], sent.message_id, sent.model_dump_json()),
            )
            self.db.execute(
                "UPDATE admin_replies SET result=? WHERE operation=?",
                (json.dumps(receipt), operation),
            )
        return receipt

    def dashboard_state(self):
        runtime = self.conversation.runtime if self.conversation else None
        if runtime is None:
            return {"cognition": {"initialized": False}}
        return {
            "cognition": {
                "initialized": True,
                "memory_enabled": runtime.memory.enabled,
                "memory_count": runtime.vectors.memory_count(),
                "expression_learning": runtime.settings.expression_learning,
                "moods": [
                    {
                        "group_id": group,
                        "excitement": mood.excitement,
                        "shyness": mood.shyness,
                        "updated_at": mood.updated_at,
                    }
                    for group, mood in runtime.emotion._moods.items()
                ],
            }
        }

    def ambient_state(self):
        # No unread bodies, private inbox or cognition. Sharing is our own action history.
        return {
            "sharing": sharing_state(self),
            "chats": [
                dict(row)
                for row in self.db.execute(
                    "SELECT c.id,c.title,c.muted,(SELECT COUNT(*) FROM messages m "
                    "WHERE m.chat=c.id AND m.seq>c.read_through "
                    "AND json_extract(m.body,'$.is_bot')=0 "
                    "AND json_extract(m.body,'$.user_id')!=?) unread "
                    "FROM chats c WHERE c.kind='group' ORDER BY c.id",
                    (self.bot_id,),
                )
            ],
        }

    def ingest(self, packet):
        batch_id, chat_id = packet["batch_id"], packet["chat_id"]
        old = self.db.execute("SELECT response FROM batches WHERE id=?", (batch_id,)).fetchone()
        if old:
            return json.loads(old[0])
        kind = packet.get("kind", "group")
        if kind not in {"group", "private"}:
            raise ValueError("Unknown chat kind")
        allowed_groups = self.config.get("allowed_groups", [])
        if kind == "group" and allowed_groups:
            if chat_id.removeprefix("group:") not in {str(group) for group in allowed_groups}:
                return {"notifications": []}
        notifications = []
        with self.db:
            self.db.execute(
                "INSERT OR IGNORE INTO chats(id,kind,title,muted) VALUES(?,?,?,?)",
                (
                    chat_id,
                    kind,
                    packet.get("title", chat_id),
                    int(kind == "group" and self.config.get("default_group_muted", False)),
                ),
            )
            chat = self.db.execute("SELECT * FROM chats WHERE id=?", (chat_id,)).fetchone()
            if chat["kind"] != kind:
                raise ValueError("Chat kind changed")
            attention_type = NotificationAttention if kind == "group" else AttentionService
            attention = attention_type(self.bot_id, bot_name=self.bot_name)
            saved = json.loads(chat["attention"])
            for key in (
                "observed_since",
                "last_bot_reply",
                "consecutive_no_reply",
                "watching_until",
            ):
                if key in saved:
                    setattr(attention, key, saved[key])
            new = []
            raw_events = {}
            for data in packet["messages"]:
                message = ChatMessage.model_validate(data)
                inserted = self.db.execute(
                    "INSERT OR IGNORE INTO messages(chat,mid,body) VALUES(?,?,?)",
                    (chat_id, message.message_id, message.model_dump_json()),
                ).rowcount
                if inserted:
                    if "raw_event" in data:
                        raw_events[message.message_id] = data["raw_event"]
                        self.db.execute(
                            "INSERT OR IGNORE INTO raw_events VALUES(?,?,?)",
                            (chat_id, message.message_id, json.dumps(data["raw_event"])),
                        )
                    self.db.execute(
                        "INSERT INTO batch_members VALUES(?,?,?)",
                        (batch_id, chat_id, message.message_id),
                    )
                    attention.add(message)
                    if not message.is_bot and message.user_id != self.bot_id:
                        new.append(message)
            # Keep all received facts. Notification eligibility is a separate policy.
            eligible = (
                [
                    m
                    for m in new
                    if notification_candidate(
                        m, bot_name=self.bot_name, raw_event=raw_events.get(m.message_id)
                    )
                ]
                if kind == "group"
                else [m for m in new if m.text.strip() or m.mentioned_bot or m.replied_to_bot]
            )
            subscription = packet.get("_world_subscription") or {}
            watching = (
                subscription.get("scope") == chat_id
                and subscription.get("expires_at", 0) > time.time()
            )
            if chat["muted"] and not watching:
                eligible = [m for m in eligible if m.mentioned_bot or m.replied_to_bot]
            if eligible:
                trigger = next(
                    (m for m in reversed(eligible) if attention.is_direct(m)), eligible[-1]
                )
                schedule = ScheduleState(
                    **packet.get(
                        "schedule",
                        {
                            "talk_value": 50,
                            "mood": "calm",
                            "routine": "default",
                        },
                    )
                )
                # No substitute no_reply is invented when the notification is ignored.
                notify = (kind == "private" and not self.config.get("private_routing")) or (
                    kind == "group"
                    and (
                        (watching and schedule.talk_value >= 10)
                        or attention.should_plan(
                            trigger,
                            schedule,
                            excitement=float(saved.get("excitement", 0)),
                            random_value=packet.get("random_value"),
                        )
                    )
                )
                if notify:
                    notifications.append(
                        {
                            "id": f"{chat_id}:{trigger.message_id}",
                            "source": f"QQ · {chat['title']} · {trigger.nickname}",
                            "message": trigger.text or "[媒体消息]",
                            "locator": {
                                "chat_id": chat_id,
                                "anchor_message_id": trigger.message_id,
                            },
                        }
                    )
            state = {
                key: getattr(attention, key)
                for key in (
                    "observed_since",
                    "last_bot_reply",
                    "consecutive_no_reply",
                    "watching_until",
                )
            }
            state["excitement"] = saved.get("excitement", 0)
            if "mood" in saved:
                state["mood"] = saved["mood"]
            self.db.execute("UPDATE chats SET attention=? WHERE id=?", (json.dumps(state), chat_id))
            response = {"notifications": notifications}
            self.db.execute("INSERT INTO batches VALUES(?,?)", (batch_id, json.dumps(response)))
        return response

    def read(self, args):
        from astcho_world.qq_media import entries
        from astcho_world.qq_participation import participation_context

        chat_id = args["chat_id"]
        chat = self.db.execute("SELECT * FROM chats WHERE id=?", (chat_id,)).fetchone()
        if chat is None:
            raise ValueError("Unknown chat")
        if "anchor_message_id" in args:
            anchor = self.db.execute(
                "SELECT seq FROM messages WHERE chat=? AND mid=?",
                (chat_id, args["anchor_message_id"]),
            ).fetchone()
            if anchor is None:
                raise ValueError("Anchor message unavailable")
            before = anchor[0] + 1
        else:
            before = args.get("before_sequence", 2**63 - 1)
        rows = list(
            reversed(
                self.db.execute(
                    "SELECT * FROM messages WHERE chat=? AND seq<? ORDER BY seq DESC LIMIT ?",
                    (chat_id, before, args.get("limit", 50 if chat["kind"] == "private" else 30)),
                ).fetchall()
            )
        )
        snapshot_id = args.get("_snapshot_id") or uuid.uuid4().hex
        messages = [json.loads(row["body"]) for row in rows]
        result = {
            "observation_id": snapshot_id,
            "chat_id": chat_id,
            "kind": chat["kind"],
            "messages": messages,
            "before_sequence": rows[0]["seq"] if rows else None,
            "anchor_message_id": args.get("anchor_message_id"),
            "history_scope": "messages received by this installation",
        }
        observed_at = time.time()
        if chat["kind"] == "group":
            result["participation"] = participation_context(self, result, observed_at)
        result["requires_media"] = False
        for message in messages:
            raw = self.db.execute(
                "SELECT body FROM raw_events WHERE chat=? AND mid=?",
                (chat_id, message["message_id"]),
            ).fetchone()
            raw_event = json.loads(raw[0]) if raw else None
            segments = raw_segments(raw_event)
            message["media"] = entries(raw_event)
            if chat["kind"] == "group":
                # Structural fact only, never infer a fixed emotion from the gesture.
                message["empty_addressing"] = empty_addressing(
                    ChatMessage.model_validate(message), raw_event
                )
                message["content_types"] = list(dict.fromkeys(s["type"] for s in segments))
            if raw_event and not message["image_description"]:
                if any(
                    segment.get("type") in {"image", "video", "forward", "record"}
                    for segment in segments
                ):
                    result["requires_media"] = True
        if args.get("anchor_message_id"):
            batch = self.db.execute(
                "SELECT batch FROM batch_members WHERE chat=? AND mid=?",
                (chat_id, args["anchor_message_id"]),
            ).fetchone()
            members = (
                {
                    row[0]
                    for row in self.db.execute(
                        "SELECT mid FROM batch_members WHERE chat=? AND batch=?",
                        (chat_id, batch[0]),
                    )
                }
                if batch
                else set()
            )
            result["pending_message_ids"] = [
                m["message_id"]
                for m in messages
                if m["message_id"] in members and m["text"].strip()
            ]
        with self.db:
            self.db.execute(
                "INSERT INTO snapshots VALUES(?,?,?,?)",
                (snapshot_id, chat_id, json.dumps(result), observed_at),
            )
            if rows:
                self.db.execute(
                    "UPDATE chats SET read_through=max(read_through,?) WHERE id=?",
                    (rows[-1]["seq"], chat_id),
                )
        return result
