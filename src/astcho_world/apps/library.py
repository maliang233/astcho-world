from __future__ import annotations

from pathlib import Path

from astcho_world.contracts import Manifest, Tool, object_schema

MANIFEST = Manifest(
    app_id="library",
    version="0.1.0",
    description="按需浏览配置目录中的 Markdown 资料",
    entrypoint="astcho_world.apps.library:Library",
    dashboard={
        "version": 1,
        "name": "资料库",
        "icon": "L",
        "views": [
            {
                "id": "records",
                "title": "应用记录",
                "source": "observations",
                "component": "table",
                "columns": {"content.data.name": "资料名称", "content.data.text": "已阅读内容"},
            }
        ],
    },
    tools=[
        Tool(
            name="list_documents",
            description="列出资料名称，不自动读取内容",
            input_schema=object_schema({}),
        ),
        Tool(
            name="read_document",
            description="读取指定资料",
            input_schema=object_schema({"name": {"type": "string", "minLength": 1}}, ("name",)),
        ),
    ],
)


class Library:
    def __init__(self, data_dir, config):
        self.root = Path(config["root"]).resolve()
        if not self.root.is_dir():
            raise ValueError("Library directory does not exist")

    async def dispatch(self, method, params):
        if method != "call":
            raise ValueError("Unknown method")
        if params["tool"] == "list_documents":
            return {"documents": sorted(p.name for p in self.root.glob("*.md"))}
        if params["tool"] == "read_document":
            path = (self.root / params["arguments"]["name"]).resolve()
            if path.parent != self.root or path.suffix != ".md":
                raise ValueError("Document is outside the configured library")
            if path.stat().st_size > 100_000:
                raise ValueError("Document exceeds the read limit")
            return {"name": path.name, "text": path.read_text()}
        raise ValueError("Unknown tool")
