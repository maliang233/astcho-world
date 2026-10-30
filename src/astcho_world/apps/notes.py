from __future__ import annotations

import sqlite3
import uuid

from astcho_world.contracts import Manifest, Tool, object_schema

TEXT = {"type": "string", "minLength": 1, "maxLength": 20000}
MANIFEST = Manifest(
    app_id="notes",
    version="0.1.0",
    description="保存、列出和阅读自己的笔记",
    entrypoint="astcho_world.apps.notes:Notes",
    dashboard={
        "version": 1,
        "name": "笔记",
        "icon": "N",
        "activities": {
            "save": {
                "kind": "activity.notes",
                "label": "正在写笔记",
                "fallback": "base.idle",
                "hold_seconds": 20,
            },
            "read": {
                "kind": "activity.notes",
                "label": "正在阅读笔记",
                "fallback": "base.idle",
                "hold_seconds": 20,
            },
            "list": {
                "kind": "activity.notes",
                "label": "正在整理笔记",
                "fallback": "base.idle",
                "hold_seconds": 20,
            },
        },
        "views": [
            {
                "id": "records",
                "title": "应用记录",
                "source": "operations",
                "component": "table",
                "columns": {
                    "tool": "操作",
                    "request.title": "标题",
                    "request.text": "内容",
                    "status": "结果",
                },
            }
        ],
    },
    tools=[
        Tool(
            name="save",
            description="保存笔记；不安排任何生活任务",
            effect="write",
            input_schema=object_schema({"title": TEXT, "text": TEXT}, ("title", "text")),
        ),
        Tool(name="list", description="列出笔记标题", input_schema=object_schema({})),
        Tool(
            name="read",
            description="阅读指定笔记",
            input_schema=object_schema({"id": TEXT}, ("id",)),
        ),
    ],
)


class Notes:
    def __init__(self, data_dir, config):
        self.db = sqlite3.connect(data_dir / "notes.sqlite3")
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS notes (id TEXT PRIMARY KEY,title TEXT,text TEXT)"
        )
        self.db.execute("CREATE TABLE IF NOT EXISTS receipts (op TEXT PRIMARY KEY,note TEXT)")
        self.db.commit()

    async def dispatch(self, method, params):
        if method != "call":
            raise ValueError("Unknown method")
        name, args = params["tool"], params["arguments"]
        if name == "save":
            op = params["operation_id"]
            old = self.db.execute("SELECT note FROM receipts WHERE op=?", (op,)).fetchone()
            if old:
                return {"id": old[0]}
            note_id = uuid.uuid4().hex
            with self.db:
                self.db.execute(
                    "INSERT INTO notes VALUES(?,?,?)", (note_id, args["title"], args["text"])
                )
                self.db.execute("INSERT INTO receipts VALUES(?,?)", (op, note_id))
            return {"id": note_id}
        if name == "list":
            return {
                "notes": [
                    {"id": row[0], "title": row[1]}
                    for row in self.db.execute(
                        "SELECT id,title FROM notes ORDER BY rowid DESC LIMIT 100"
                    )
                ]
            }
        if name == "read":
            row = self.db.execute(
                "SELECT title,text FROM notes WHERE id=?", (args["id"],)
            ).fetchone()
            if row is None:
                raise ValueError("Note not found")
            return {"id": args["id"], "title": row[0], "text": row[1]}
        raise ValueError("Unknown tool")
