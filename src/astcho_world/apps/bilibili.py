"""Bilibili capability App. No timer, separate agent, duty prompt or QQ dependency."""

from __future__ import annotations

import base64
import json
import os
import sqlite3
import time
from pathlib import Path

from astcho_world.activities import atomic_json
from astcho_world.bilibili_details import (
    collection_details,
    history_index,
    load_details,
    retain_details,
    video_details,
)
from astcho_world.contracts import Manifest, Tool, object_schema

BV = {"type": "string", "pattern": "^BV[0-9A-Za-z]{10}$"}
PAGE = {"type": "integer", "minimum": 1}
NUM = {"type": "integer", "minimum": 1, "maximum": 40}
TEXT = {"type": "string", "minLength": 1, "maxLength": 4000}
DETAILS = {"type": "string", "pattern": "^[0-9a-f]{64}$"}


def saved_session(root):
    """A file/SDK object alone does not establish that a session cookie exists."""
    try:
        data = json.loads((root / "credential.json").read_text())
    except (OSError, ValueError):
        return False
    return (
        isinstance(data, dict)
        and isinstance(data.get("sessdata"), str)
        and bool(data["sessdata"].strip())
    )


def tool(name, description, fields=None, required=(), *, write=False, timeout=60):
    dedup = write and name != "account"
    if dedup:
        fields = {
            **(fields or {}),
            "intent_id": {"type": "string", "minLength": 1, "maxLength": 200},
        }
        required = (*required, "intent_id")
        description += "同一行动意图重试沿用 intent_id；新意图才换新ID。未知回执不得换ID重发。"
    return Tool(
        name=name,
        description=description,
        input_schema=object_schema(fields or {}, required),
        effect="write" if write else "read",
        timeout=timeout,
        idempotency_field="intent_id" if dedup else None,
    )


MANIFEST = Manifest(
    app_id="bilibili",
    version="0.2.0",
    entrypoint="astcho_world.apps.bilibili:Bilibili",
    description="自主浏览 B站推荐、搜索、读取字幕与画面，选择站内互动和获取分享链接",
    admin_tools=["account"],
    dashboard={
        "version": 1,
        "name": "Bilibili",
        "icon": "B",
        "views": [],
        "activities": {
            "recommend": {
                "kind": "activity.phone",
                "label": "正在浏览推荐",
                "fallback": "base.idle",
                "hold_seconds": 20,
            },
            "search": {
                "kind": "activity.phone",
                "label": "正在搜索视频",
                "fallback": "base.idle",
                "hold_seconds": 20,
            },
            "video": {
                "kind": "activity.phone",
                "label": "正在查看视频资料",
                "fallback": "base.idle",
                "hold_seconds": 20,
            },
            "collection": {
                "kind": "activity.phone",
                "label": "正在查看合集目录",
                "fallback": "base.idle",
                "hold_seconds": 20,
            },
            "subtitles": {
                "kind": "activity.phone",
                "label": "正在阅读视频字幕",
                "fallback": "base.idle",
                "hold_seconds": 20,
            },
            "comments": {
                "kind": "activity.phone",
                "label": "正在看评论",
                "fallback": "base.idle",
                "hold_seconds": 20,
            },
            "view": {
                "kind": "activity.phone",
                "label": "正在查看视频画面",
                "fallback": "base.idle",
                "hold_seconds": 20,
            },
            "like": {
                "kind": "activity.phone",
                "label": "正在操作 B站",
                "fallback": "base.idle",
                "hold_seconds": 20,
            },
            "comment": {
                "kind": "activity.phone",
                "label": "正在写评论",
                "fallback": "base.idle",
                "hold_seconds": 20,
            },
            "favorites": {
                "kind": "activity.phone",
                "label": "正在查看收藏",
                "fallback": "base.idle",
                "hold_seconds": 20,
            },
            "share": {
                "kind": "activity.phone",
                "label": "正在获取分享链接",
                "fallback": "base.idle",
                "hold_seconds": 20,
            },
            "history": {
                "kind": "activity.phone",
                "label": "正在回看浏览记录",
                "fallback": "base.idle",
                "hold_seconds": 20,
            },
        },
    },
    ambient_schema=object_schema(
        {
            "logged_in": {"type": "boolean"},
            "available": {"type": "boolean"},
            "asr": {"type": "boolean"},
        },
        ("logged_in", "available", "asr"),
    ),
    tools=[
        tool(
            "account",
            "查看登录状态、生成扫码登录二维码、检查扫码或退出；凭证不经聊天传输。",
            {
                "action": {"enum": ["status", "login", "check", "cancel", "logout"]},
                "login_id": {"type": "string", "minLength": 1, "maxLength": 100},
            },
            ("action",),
            write=True,
            timeout=120,
        ),
        tool(
            "recommend",
            "浏览当前账号推荐流；候选信息不代表已看过视频。refresh 生成新一批，page 翻本批缓存。",
            {"refresh": {"type": "boolean"}, "page": PAGE, "limit": NUM},
        ),
        tool(
            "search",
            "按兴趣搜索视频，支持排序和分页。",
            {
                "keyword": TEXT,
                "page": PAGE,
                "limit": NUM,
                "order": {"enum": ["totalrank", "click", "pubdate", "dm"]},
            },
            ("keyword",),
        ),
        tool(
            "video",
            "查看单个视频资料及分页分P目录，不附整个合集；不表示已观看。"
            "source_ref 可回看已取得的同份资料。",
            {"bvid": BV, "page": PAGE, "limit": NUM, "source_ref": DETAILS},
            ("bvid",),
        ),
        tool(
            "collection",
            "按需分页读取视频所属合集的候选目录，不展开全部视频详情。"
            "source_ref 可沿用 video 返回的同份资料。",
            {"bvid": BV, "page": PAGE, "limit": NUM, "source_ref": DETAILS},
            ("bvid",),
        ),
        tool(
            "history",
            "查看本应用浏览记录索引，不重复附上字幕、画面和工具全文；"
            "可选择 platform 读取账号已有播放历史。",
            {"platform": {"type": "boolean"}, "page": PAGE},
        ),
        tool(
            "subtitles",
            "读取指定分P的原字幕和时间戳；无字幕、登录限制会明确返回。ASR 暂不可用。",
            {"bvid": BV, "page": PAGE, "language": {"type": "string"}},
            ("bvid",),
        ),
        tool(
            "comments",
            "读取热门评论及部分楼中楼，返回实际范围；可指定 rpid 读取其回复页。",
            {"bvid": BV, "limit": NUM, "rpid": {"type": "integer", "minimum": 1}, "page": PAGE},
            ("bvid",),
        ),
        tool(
            "view",
            "读取封面或指定时刻故事板帧，经独立视觉模型回答问题。"
            "故事板通常仅首P，结果标明真实时间、覆盖范围及未识别项。",
            {
                "bvid": BV,
                "kind": {"enum": ["cover", "frames"]},
                "question": TEXT,
                "at": {"type": "array", "maxItems": 6, "items": {"type": "number", "minimum": 0}},
            },
            ("bvid", "kind", "question"),
            timeout=120,
        ),
        tool(
            "like",
            "显式设置已赞/取消，并读取平台状态；不是切换按钮。",
            {"bvid": BV, "liked": {"type": "boolean"}},
            ("bvid", "liked"),
            write=True,
        ),
        tool(
            "comment",
            "谨慎选择发表或回复评论：先了解实际内容与已有讨论，有具体问题、补充或独立观点"
            "时才考虑。泛泛赞美、复述或留下浏览痕迹通常不必发表；浏览无需评论来收尾。"
            "评论正文遵守环境中的原版通用说话规则。"
            "返回平台真实评论ID。",
            {
                "bvid": BV,
                "text": TEXT,
                "root": {"type": "integer", "minimum": 0},
                "parent": {"type": "integer", "minimum": 0},
            },
            ("bvid", "text"),
            write=True,
        ),
        tool(
            "favorites",
            "列出收藏夹、查询视频状态，或显式加入/移出收藏夹。",
            {
                "action": {"enum": ["list", "status", "add", "remove"]},
                "bvid": BV,
                "folder_id": {"type": "integer", "minimum": 1},
            },
            ("action",),
            write=True,
        ),
        tool(
            "share",
            "取得规范视频链接，可带已有时间点。只获取资源，不表示应当发送；"
            "可以不分享，也不需要给每次浏览提供对外产出。",
            {"bvid": BV, "at": {"type": "number", "minimum": 0}},
            ("bvid",),
        ),
    ],
)


class Bilibili:
    def __init__(self, data_dir, config):
        import certifi

        os.environ.setdefault("SSL_CERT_FILE", certifi.where())
        self.root, self.config = Path(data_dir), config
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db = sqlite3.connect(self.root / "bilibili.sqlite3")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS receipts (id TEXT PRIMARY KEY, request TEXT, result TEXT);
            CREATE TABLE IF NOT EXISTS browsed (
                id INTEGER PRIMARY KEY, created REAL, tool TEXT, data TEXT);
        """)
        self.backend = None
        self.login = None
        self.feed = []
        self.logged_in = saved_session(self.root)

    def ambient_state(self):
        return {"logged_in": self.logged_in, "available": True, "asr": False}

    def dashboard_state(self):
        return self.ambient_state()

    async def dispatch(self, method, params):
        if method == "status":
            return self.ambient_state()
        if method != "call":
            raise ValueError("Unknown method")
        name, args, operation = params["tool"], params["arguments"], params["operation_id"]
        write = MANIFEST.tool(name).effect == "write" and not (
            name == "account" and args["action"] in {"status", "check"}
        )
        request = json.dumps([name, args], sort_keys=True)
        if write:
            old = self.db.execute(
                "SELECT request,result FROM receipts WHERE id=?", (operation,)
            ).fetchone()
            if old:
                if old[0] != request:
                    raise ValueError("Operation ID reused")
                return (
                    json.loads(old[1])
                    if old[1]
                    else {"status": "unknown", "reason": "Not replayed"}
                )
            with self.db:
                self.db.execute("INSERT INTO receipts VALUES(?,?,NULL)", (operation, request))
        result = await self.execute(name, args)
        if write:
            with self.db:
                self.db.execute(
                    "UPDATE receipts SET result=? WHERE id=?", (json.dumps(result), operation)
                )
        if name not in {"account", "history"}:
            with self.db:
                self.db.execute(
                    "INSERT INTO browsed(created,tool,data) VALUES(?,?,?)",
                    (time.time(), name, json.dumps(result)),
                )
        return result

    def load_backend(self):
        if self.backend is None:
            from astcho_world.vendor import bili_scroll

            bili_scroll.CRED_FILE = self.root / "credential.json"
            self.backend = bili_scroll
        return self.backend

    def credential(self):
        if not saved_session(self.root):
            self.logged_in = False
            return None
        credential = self.load_backend().load_credential()
        if credential is None or not credential.has_sessdata():
            self.logged_in = False
            return None
        return credential

    def video(self, bvid):
        from bilibili_api import video

        return video.Video(bvid=bvid, credential=self.credential())

    async def execute(self, name, args):
        # Synthetic HTTP backend exercises the same isolated worker and ledger.
        if self.config.get("test_backend"):
            import httpx

            async with httpx.AsyncClient(timeout=90) as client:
                response = await client.post(
                    self.config["test_backend"], json={"tool": name, "arguments": args}
                )
                response.raise_for_status()
                result = response.json()
            if name == "view":
                return await self.describe(result, args["question"])
            if name == "video" and "info" in result:
                return self.video_result(result["info"], args)
            return result
        if name == "account":
            return await self.account(args["action"], login_id=args.get("login_id"))
        if name == "history":
            if args.get("platform"):
                from bilibili_api import user

                return {
                    "source": "platform",
                    "data": await user.get_self_history(
                        page_num=args.get("page", 1), credential=self.credential()
                    ),
                }
            rows = self.db.execute(
                "SELECT id,created,tool,data FROM browsed ORDER BY id DESC LIMIT 30"
            ).fetchall()
            return {
                "source": "opened in this App",
                "items": [
                    {
                        "id": r[0],
                        "created": r[1],
                        "tool": r[2],
                        "data": history_index(json.loads(r[3])),
                    }
                    for r in rows
                ],
            }
        if name == "share":
            url = f"https://www.bilibili.com/video/{args['bvid']}/"
            if "at" in args:
                url += "?t=" + str(int(args["at"]))
            return {"resource": {"type": "link", "url": url, "app": "bilibili", "id": args["bvid"]}}
        backend = self.load_backend()
        if name == "recommend":
            if not self.feed or args.get("refresh"):
                result = json.loads(await backend.bili_recommend(ps=40))
                if not result.get("ok"):
                    return {"status": "unavailable", **result}
                self.feed = result["videos"]
            page, limit = args.get("page", 1), args.get("limit", 10)
            start = (page - 1) * limit
            return {
                "videos": self.feed[start : start + limit],
                "page": page,
                "next_page": page + 1 if start + limit < len(self.feed) else None,
                "source": "account_recommendation"
                if self.credential()
                else "anonymous_recommendation",
                "scope": "candidate metadata, not watched content",
            }
        if name == "search":
            result = json.loads(
                await backend.bili_search(
                    args["keyword"],
                    num=args.get("limit", 10),
                    page=args.get("page", 1),
                    order=args.get("order", "totalrank"),
                )
            )
            return {**result, "page": args.get("page", 1), "scope": "candidate metadata"}
        bvid = args["bvid"] if "bvid" in args else None
        if name in {"video", "collection"}:
            info = (
                load_details(self.root, args["source_ref"], bvid)
                if args.get("source_ref")
                else await self.video(bvid).get_info()
            )
            if name == "video":
                return self.video_result(info, args)
            return collection_details(
                info,
                retain_details(self.root, info),
                page=args.get("page", 1),
                limit=args.get("limit", 10),
            )
        if name == "subtitles":
            return await self.subtitles(args)
        if name == "comments":
            if args.get("rpid"):
                from bilibili_api import comment

                aid = (await self.video(bvid).get_info())["aid"]
                reply = comment.Comment(
                    oid=aid,
                    type_=comment.CommentResourceType.VIDEO,
                    rpid=args["rpid"],
                    credential=self.credential(),
                )
                return {
                    "data": await reply.get_sub_comments(page_index=args.get("page", 1)),
                    "scope": "one reply page",
                }
            return {
                **json.loads(await backend.bili_comments(bvid, num=args.get("limit", 20))),
                "scope": "requested popular comments and returned replies",
            }
        if name == "view":
            blocks = await (
                backend.bili_cover(bvid)
                if args["kind"] == "cover"
                else backend.bili_frames(bvid, at=args.get("at"))
            )
            result = {"metadata": [], "images": []}
            for block in blocks:
                if block.type == "text":
                    result["metadata"].append(json.loads(block.text))
                elif block.type == "image":
                    result["images"].append({"data": block.data, "mime": block.mimeType})
            return await self.describe(result, args["question"])
        if not self.credential():
            return {"status": "login_required"}
        if name == "like":
            v = self.video(bvid)
            receipt = await v.like(status=args["liked"])
            return {"receipt": receipt, "liked": await v.has_liked(), "requested": args["liked"]}
        if name == "comment":
            from bilibili_api import comment

            aid = (await self.video(bvid).get_info())["aid"]
            receipt = await comment.send_comment(
                text=args["text"],
                oid=aid,
                type_=comment.CommentResourceType.VIDEO,
                root=args.get("root") or None,
                parent=args.get("parent") or None,
                credential=self.credential(),
            )
            return {
                "receipt": receipt,
                "comment_id": str(receipt.get("rpid_str") or receipt.get("rpid") or ""),
            }
        if name == "favorites":
            from bilibili_api import favorite_list

            action = args["action"]
            if action == "list":
                return {
                    "folders": await favorite_list.get_video_favorite_list(
                        uid=int(self.credential().dedeuserid), credential=self.credential()
                    )
                }
            if not bvid:
                raise ValueError("bvid required")
            v = self.video(bvid)
            if action == "status":
                return {
                    "state": await v.has_favoured(),
                    "folders": await favorite_list.get_video_favorite_list(
                        uid=int(self.credential().dedeuserid), video=v, credential=self.credential()
                    ),
                }
            if "folder_id" not in args:
                raise ValueError("folder_id required")
            receipt = await v.set_favorite(
                add_media_ids=[args["folder_id"]] if action == "add" else [],
                del_media_ids=[args["folder_id"]] if action == "remove" else [],
            )
            return {
                "receipt": receipt,
                "state": await v.has_favoured(),
                "folders": await favorite_list.get_video_favorite_list(
                    uid=int(self.credential().dedeuserid), video=v, credential=self.credential()
                ),
                "folder_id": args["folder_id"],
                "action": action,
            }
        raise ValueError("Unknown Bilibili tool")

    def video_result(self, info, args):
        return video_details(
            info,
            retain_details(self.root, info),
            page=args.get("page", 1),
            limit=args.get("limit", 10),
        )

    async def subtitles(self, args):
        import httpx

        v = self.video(args["bvid"])
        info = await v.get_info()
        page = args.get("page", 1)
        pages = info.get("pages") or [{"cid": info.get("cid"), "duration": info.get("duration")}]
        if page > len(pages):
            raise ValueError("Unknown video page")
        tracks = (await v.get_subtitle(cid=pages[page - 1]["cid"])).get("subtitles", [])
        if not tracks:
            return {"status": "no_subtitles", "page": page, "asr_available": False}
        language = args.get("language")
        selected = next((t for t in tracks if t.get("lan") == language), None) if language else None
        selected = selected or next(
            (t for t in tracks if t.get("lan") in {"ai-zh", "zh-CN", "zh"}), tracks[0]
        )
        url = selected["subtitle_url"]
        if url.startswith("//"):
            url = "https:" + url
        async with httpx.AsyncClient(timeout=25, follow_redirects=True) as client:
            response = await client.get(url)
            response.raise_for_status()
            segments = response.json().get("body", [])
        return {
            "status": "available",
            "bvid": args["bvid"],
            "page": page,
            "pages": len(pages),
            "language": selected.get("lan"),
            "segments": segments,
            "scope": "original subtitle track for this page only; not whole-video observation",
        }

    def clear_login(self):
        if self.login is not None and hasattr(self.login, "clear"):
            self.login.clear()
        self.login = None
        (self.root / "login-session.json").unlink(missing_ok=True)
        (self.root / "login.png").unlink(missing_ok=True)

    async def account(self, action, *, login_id=None):
        from astcho_world.bilibili_login import WebQRLogin

        if self.login is None and (self.root / "login-session.json").exists():
            self.login = WebQRLogin(self.root)
        if login_id and (
            self.login is None or getattr(self.login, "data", {}).get("id") != login_id
        ):
            return {"status": "superseded", "logged_in": self.logged_in}
        if action == "logout":
            (self.root / "credential.json").unlink(missing_ok=True)
            self.clear_login()
            self.logged_in = False
            return {"status": "not_logged_in", "logged_in": False}
        if action == "cancel":
            self.clear_login()
            return {"status": "cancelled", "logged_in": self.logged_in}
        if action == "login":
            self.login = WebQRLogin(self.root)
            await self.login.generate_qrcode()
            return self.login.public()
        if action == "status" and self.login is not None:
            return self.login.public()
        if action == "check" and self.login:
            state = await self.login.check_state()
            if state.name == "DONE":
                from bilibili_api import user
                from bilibili_api.exceptions import ResponseCodeException

                cred = self.login.get_credential()
                # DONE is a QR-flow state, not proof that usable cookies were
                # returned. Never overwrite an existing session with empty data.
                if not cred.has_sessdata() or not cred.has_bili_jct():
                    self.clear_login()
                    return {
                        "status": "login_failed", "reason": "incomplete_login_credential",
                        "logged_in": self.logged_in,
                    }
                try:
                    info = await user.get_self_info(credential=cred)
                except ResponseCodeException as exc:
                    if exc.code != -101:
                        raise
                    self.clear_login()
                    return {
                        "status": "login_failed", "reason": "session_rejected",
                        "logged_in": self.logged_in,
                    }
                if not info.get("mid"):
                    self.clear_login()
                    return {
                        "status": "login_failed", "reason": "account_identity_missing",
                        "logged_in": self.logged_in,
                    }
                # Read the ID from the authenticated account response, rather
                # than depending on the QR library's redirect-query parsing.
                fields = {
                    k: getattr(cred, k, None) for k in (
                        "sessdata", "bili_jct", "buvid3", "buvid4", "dedeuserid", "ac_time_value"
                    )
                }
                fields["dedeuserid"] = str(info["mid"])
                atomic_json(
                    self.root / "credential.json",
                    fields,
                )
                self.clear_login()
                self.logged_in = True
                return {
                    "status": "logged_in", "logged_in": True,
                    "name": info.get("name"), "uid": info["mid"],
                }
            return self.login.public()
        cred = self.credential()
        if cred is None:
            self.logged_in = False
            return {"status": "not_logged_in", "logged_in": False, "reason": "missing_session"}
        from bilibili_api import user
        from bilibili_api.exceptions import ResponseCodeException

        try:
            info = await user.get_self_info(credential=cred)
        except ResponseCodeException as exc:
            if exc.code != -101:
                raise
            self.logged_in = False
            return {"status": "not_logged_in", "logged_in": False, "reason": "session_expired"}
        self.logged_in = bool(info.get("mid"))
        return {
            "status": "logged_in" if self.logged_in else "not_logged_in",
            "logged_in": self.logged_in, "name": info.get("name"), "uid": info.get("mid"),
        }

    async def describe(self, result, question):
        if not result.get("images"):
            return {**result, "status": "unavailable"}
        from astcho.config import Settings
        from astcho.services.llm import LLMService
        from astcho.services.vision import VisionService
        from astcho.storage.sqlite import SQLiteStore
        from astcho_world.images import ImageReader
        from astcho_world.usage_capture import UsageReporter

        settings = Settings.from_env()
        llm = LLMService(settings)
        UsageReporter(self.root).instrument(llm.vision_client, purpose="bilibili_vision")
        vision = VisionService(settings, llm, SQLiteStore(self.root / "vision.sqlite3"))
        reader = ImageReader(self.root / "images", vision)
        observations = []
        try:
            for image in result["images"]:
                try:
                    observations.append(
                        await reader.inspect_bytes(
                            base64.b64decode(image["data"], validate=True), question
                        )
                    )
                except Exception as exc:
                    observations.append({"status": "unavailable", "reason": str(exc)})
        finally:
            import asyncio

            await asyncio.gather(
                *(
                    client.close()
                    for client in [
                        llm.text_client,
                        llm.planner_client,
                        llm.vision_client,
                        llm.reasoning_client,
                    ]
                )
            )
        return {
            "metadata": result.get("metadata", []),
            "observations": observations,
            "scope": "only supplied cover/storyboard frames; independent vision model",
        }
