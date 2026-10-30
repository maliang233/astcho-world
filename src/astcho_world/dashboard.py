"""Administrative projections and local billing settings; never invokes subject tools."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import re
import secrets
import time
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from starlette.middleware.trustedhost import TrustedHostMiddleware

from astcho_world.kernel import Kernel
from astcho_world.life import LifeState
from astcho_world.usage import UsageCollector

ASSETS = Path(__file__).with_name("web")
SECRET_NAMES = {"api_key", "token", "password", "secret", "authorization", "access_token"}


def redact(value):
    if isinstance(value, dict):
        return {
            k: "••••••"
            if k.lower() in SECRET_NAMES
            or k.lower().endswith("_token")
            or k.lower().endswith("_key")
            else redact(v)
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [redact(v) for v in value]
    if isinstance(value, str):
        for key, secret in os.environ.items():
            if len(secret) >= 8 and any(part in key.lower() for part in SECRET_NAMES):
                value = value.replace(secret, "••••••")
        value = re.sub(r"(?i)(bearer\s+)[^\s\"\']+", r"\1••••••", value)
    return value


def rows(store, table, limit=100, app=None):
    # All table names are supplied by this module, never interpolated from client input.
    where = " WHERE app=?" if app and table != "observations" else ""
    values = [app] if where else []
    records = store.db.execute(
        f"SELECT * FROM {table}{where} ORDER BY rowid DESC LIMIT ?", (*values, limit)
    ).fetchall()
    result = []
    for row in records:
        item = dict(row)
        for key in ("data", "request", "result", "content", "locator"):
            if item.get(key) is not None:
                try:
                    item[key] = json.loads(item[key])
                except (ValueError, TypeError):
                    pass
        result.append(item)
    return result


def snapshot(kernel, mode, issues, *, life=None):
    from astcho_world import presentation

    life_view = life.snapshot(kernel) if life else None
    store = kernel.store
    catalog = kernel.catalog()
    apps = []
    for item in catalog:
        manifest = kernel.apps[item["app_id"]].manifest
        apps.append(
            {
                **item,
                "dashboard": manifest.dashboard.model_dump()
                if manifest.dashboard
                else {"version": 1, "name": item["app_id"], "icon": "◇", "views": []},
                "tools": [tool.model_dump() for tool in manifest.tools],
                "admin_tools": manifest.admin_tools,
                "availability": kernel.apps[item["app_id"]].ambient,
            }
        )
    counts = {
        name: store.db.execute(f"SELECT count(*) FROM {name}").fetchone()[0]
        for name in ("notifications", "operations", "observations", "events")
    }
    counts["pending"] = store.db.execute(
        "SELECT count(*) FROM notifications WHERE state='pending'"
    ).fetchone()[0]
    counts["errors"] = store.db.execute(
        "SELECT count(*) FROM operations WHERE status IN ('failed','unknown')"
    ).fetchone()[0]
    return redact(
        {
            "mode": mode,
            "issues": issues,
            "server_time": time.time(),
            "usage_today": store.usage.today_totals(),
            "apps": apps,
            "runtime": store.get("runtime_status", {"phase": "offline"}),
            "logical_activity": kernel.activities.current(),
            "idle_wake": store.get("idle_wake", {}),
            "archive_error": store.get("activity_archive_error"),
            "life": life_view,
            "presentation": presentation.snapshot(kernel, life_view, mode),
            "counts": counts,
            "notifications": rows(store, "notifications"),
            "operations": rows(store, "operations"),
            "reminders": reminder_rows(store),
            "capabilities": store.get("dsh_capabilities", {}),
            "cursor": store.db.execute("SELECT coalesce(max(seq),0) FROM events").fetchone()[0],
        }
    )


def reminder_rows(store):
    legacy = rows(store, "reminders")
    imported = store.get("dsh_reminder_imports", {})
    return [r for r in legacy if r["id"] not in imported] + [
        {
            **r,
            "due": datetime.fromisoformat(r["scheduledAt"]).timestamp(),
            "text": r["prompt"],
            "fired": int(r["status"] == "inactive"),
            "backend": "dsh",
        }
        for r in store.get("dsh_reminders", [])
    ]


def install_dashboard(app, token, *, mode="live", issues=None):
    if len(token) < 16:
        raise ValueError("Web token must contain at least 16 characters")
    session = hmac.new(token.encode(), b"astcho-dashboard-session-v1", hashlib.sha256).hexdigest()
    cookie_name = "astcho_web_" + hashlib.sha256(token.encode()).hexdigest()[:12]
    app.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=["127.0.0.1", "localhost", "testserver", "[::1]", "host.docker.internal"],
    )

    def authenticate(request):
        if not hmac.compare_digest(request.cookies.get(cookie_name, ""), session):
            raise HTTPException(401, "请使用启动器打开观察台，或输入 Web 访问令牌")

    @app.middleware("http")
    async def security_headers(request, call_next):
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'"
        )
        return response

    @app.get("/", include_in_schema=False)
    async def index():
        return FileResponse(ASSETS / "index.html")

    @app.get("/assets/{name}", include_in_schema=False)
    async def asset(name: str):
        if name not in {
            "app.js",
            "app-controls.js",
            "usage.js",
            "settings.js",
            "backfill.js",
            "tiers.js",
            "portraits.js",
            "pagination.js",
            "style.css",
            "astcho-desk.png",
        }:
            raise HTTPException(404)
        return FileResponse(ASSETS / name)

    @app.post("/api/session")
    async def login(request: Request):
        if request.headers.get("sec-fetch-site") == "cross-site":
            raise HTTPException(403)
        body = await request.json()
        if not isinstance(body, dict) or not hmac.compare_digest(str(body.get("token", "")), token):
            raise HTTPException(401, "访问令牌不正确")
        response = JSONResponse({"ok": True})
        response.set_cookie(cookie_name, session, httponly=True, samesite="strict", max_age=86400)
        return response

    @app.get("/api/snapshot")
    async def current(request: Request):
        authenticate(request)
        return snapshot(app.state.kernel, mode, issues or [], life=app.state.life)

    @app.get("/api/notifications")
    async def notifications(request: Request, before: int = 0, limit: int = 21):
        authenticate(request)
        where = " WHERE rowid<?" if before else ""
        values = (before,) if before else ()
        records = app.state.kernel.store.db.execute(
            f"SELECT rowid AS cursor,* FROM notifications{where} ORDER BY rowid DESC LIMIT ?",
            (*values, min(max(limit, 1), 100)),
        ).fetchall()
        items = [dict(row) for row in records]
        for item in items:
            if item["locator"] is not None:
                try:
                    item["locator"] = json.loads(item["locator"])
                except (ValueError, TypeError):
                    pass
        return redact({"items": items})

    @app.get("/api/events")
    async def events(
        request: Request,
        before: int = 0,
        after: int = 0,
        app_id: str = "",
        activity_id: str = "",
        kind: str = "",
        q: str = "",
        limit: int = 50,
        decisions: bool = False,
        include_debug: bool = False,
    ):
        authenticate(request)
        conditions, values = [], []
        if not include_debug:
            conditions.append("kind NOT LIKE 'debug.%'")
        if decisions:
            conditions.append("kind IN ('decision.tool','decision.result','activity.failed')")
        for column, value in (("app", app_id), ("activity_id", activity_id), ("kind", kind)):
            if value:
                conditions.append(f"{column}=?")
                values.append(value)
        if before:
            conditions.append("seq<?")
            values.append(before)
        if after:
            conditions.append("seq>?")
            values.append(after)
        if q:
            conditions.append("(summary LIKE ? OR data LIKE ?)")
            values.extend([f"%{q[:200]}%"] * 2)
        where = " WHERE " + " AND ".join(conditions) if conditions else ""
        records = app.state.kernel.store.db.execute(
            f"SELECT * FROM events{where} ORDER BY seq DESC LIMIT ?",
            (*values, min(max(limit, 1), 200)),
        ).fetchall()
        items = [{**dict(row), "data": json.loads(row["data"])} for row in records]
        return redact({"items": items, "next_before": items[-1]["seq"] if items else None})

    @app.post("/api/apps/{app_id}/control")
    async def control_app(app_id: str, request: Request):
        authenticate(request)
        same_origin(request)
        from jsonschema import validate

        kernel = app.state.kernel
        process = kernel.apps.get(app_id)
        if process is None:
            raise HTTPException(404, "Unknown App")
        body = await request.json()
        name, arguments = body.get("tool"), body.get("arguments", {})
        if name not in process.manifest.admin_tools:
            raise HTTPException(403, "Not an administrative App control")
        definition = process.manifest.tool(name)
        try:
            validate(arguments, definition.input_schema)
            # Administrative account setup is not an observation by the subject.
            return await process.request(
                "call",
                {
                    "tool": name,
                    "arguments": arguments,
                    "operation_id": "admin:" + secrets.token_hex(16),
                },
                definition.timeout,
            )
        except Exception as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.get("/api/logs")
    async def logs(request: Request, source: str = "server"):
        authenticate(request)
        kernel = app.state.kernel
        sources = {"server": kernel.root / "launcher" / "server.log"}
        sources.update({key: item.data_dir / "worker.log" for key, item in kernel.apps.items()})
        if source not in sources:
            raise HTTPException(404, "未知日志来源")
        path = sources[source]
        text, truncated = "", False
        if path.is_file():
            with path.open("rb") as file:
                size = file.seek(0, 2)
                truncated = size > 131072
                file.seek(max(0, size - 131072))
                text = file.read(131072).decode("utf-8", errors="replace")
        return redact(
            {
                "source": source,
                "sources": list(sources),
                "text": text,
                "truncated": truncated,
                "exists": path.is_file(),
            }
        )

    @app.get("/api/usage")
    async def usage(
        request: Request,
        period: str = "today",
        start: str = "",
        end: str = "",
        provider: str = "",
        model: str = "",
        purpose: str = "",
        app_id: str = "",
        activity_id: str = "",
        tariff: str = "",
        before: int = 0,
        limit: int = 50,
    ):
        authenticate(request)
        try:
            return app.state.kernel.store.usage.report(
                period=period,
                start=start,
                end=end,
                provider=provider,
                model=model,
                purpose=purpose,
                app=app_id,
                activity_id=activity_id,
                tariff=tariff,
                before=before,
                limit=limit,
            )
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None

    @app.get("/api/settings")
    async def settings(request: Request):
        authenticate(request)
        from astcho_world.settings import settings_snapshot

        return settings_snapshot(app.state.kernel, mode, life=app.state.life)

    def same_origin(request):
        origin = request.headers.get("origin")
        if request.headers.get("sec-fetch-site") == "cross-site" or (
            origin and origin != str(request.base_url).rstrip("/")
        ):
            raise HTTPException(403, "仅允许本地页面修改配置")

    @app.get("/api/presentation/assets/{asset_id}")
    async def portrait_asset(request: Request, asset_id: str):
        authenticate(request)
        from astcho_world.presentation import asset_path

        path = asset_path(app.state.kernel, asset_id)
        if not path or not path.is_file():
            raise HTTPException(404)
        return FileResponse(path, media_type="image/png")

    @app.post("/api/presentation/assets")
    async def upload_portrait(request: Request):
        authenticate(request)
        same_origin(request)
        from astcho_world.presentation import upload

        data = bytearray()
        async for chunk in request.stream():
            data.extend(chunk)
            if len(data) > 8 * 1024 * 1024:
                raise HTTPException(413, "图片不能超过 8 MiB")
        try:
            return upload(app.state.kernel, bytes(data))
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None

    @app.post("/api/presentation/config")
    async def configure_portraits(request: Request):
        authenticate(request)
        same_origin(request)
        from astcho_world.presentation import save_config

        try:
            return save_config(app.state.kernel, await request.json())
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None

    @app.post("/api/presentation/schedule")
    async def configure_schedule_portraits(request: Request):
        authenticate(request)
        same_origin(request)
        from astcho_world.presentation import save_schedule

        try:
            return save_schedule(app.state.kernel, app.state.life, await request.json())
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None

    @app.post("/api/settings/usage-backfill/preview")
    async def preview_backfill(request: Request):
        authenticate(request)
        same_origin(request)
        from astcho_world.usage_backfill import UsageBackfill

        try:
            return UsageBackfill(app.state.kernel.store.db, token).preview(await request.json())
        except (ValueError, TypeError) as exc:
            raise HTTPException(422, str(exc)) from None

    @app.post("/api/settings/usage-backfill/execute")
    async def execute_backfill(request: Request):
        authenticate(request)
        same_origin(request)
        from astcho_world.usage_backfill import StalePreview, UsageBackfill

        try:
            value = await request.json()
            if not isinstance(value, dict):
                raise ValueError("请先预览补算范围")
            return UsageBackfill(app.state.kernel.store.db, token).execute(
                value.get("preview_token")
            )
        except StalePreview as exc:
            raise HTTPException(409, str(exc)) from None
        except (ValueError, TypeError) as exc:
            raise HTTPException(422, str(exc)) from None

    @app.post("/api/settings/rates")
    @app.post("/api/usage/rates")
    async def save_rate(request: Request):
        authenticate(request)
        same_origin(request)
        try:
            value = await request.json()
            if not isinstance(value, dict):
                raise ValueError("单价配置应为对象")
            rate_id = app.state.kernel.store.usage.add_rate(value)
        except (ValueError, TypeError) as exc:
            raise HTTPException(422, str(exc)) from None
        return {"id": rate_id, "effective": "仅影响此后发起的调用；旧记录保留当时计价状态"}

    @app.get("/api/context")
    async def context(request: Request, before: int = 0, seq: int = 0, limit: int = 20):
        authenticate(request)
        store = app.state.kernel.store
        where, params = ["kind='debug.model_request'"], []
        if seq:
            where.append("seq=?")
            params.append(seq)
        if before:
            where.append("seq<?")
            params.append(before)
        records = store.db.execute(
            "SELECT seq,created,activity_id,data FROM events WHERE "
            + " AND ".join(where)
            + " ORDER BY seq DESC LIMIT ?",
            (*params, min(max(limit, 1), 100)),
        ).fetchall()
        items = []
        for record in records:
            item = dict(record)
            payload = json.loads(item.pop("data"))
            item.update(
                {
                    key: payload.get(key)
                    for key in ("request_id", "purpose", "runner", "model", "characters_by_role")
                }
            )
            item["message_count"] = len(payload.get("messages", []))
            item["tool_count"] = len(payload.get("tools", []))
            usage = store.db.execute(
                "SELECT data FROM events WHERE kind='debug.model_usage' "
                "AND json_extract(data,'$.request_id')=? ORDER BY seq DESC LIMIT 1",
                (payload.get("request_id"),),
            ).fetchone()
            item["usage"] = json.loads(usage[0])["usage"] if usage else None
            item["compaction"] = None
            if payload.get("compaction_id"):
                landed = store.db.execute(
                    "SELECT data FROM events WHERE kind='compaction.finished' "
                    "AND json_extract(data,'$.compactionId')=? ORDER BY seq DESC LIMIT 1",
                    (payload["compaction_id"],),
                ).fetchone()
                item["compaction"] = json.loads(landed[0]) if landed else {"status": "running"}
            if seq:
                from astcho_world.context_log import expand_input

                item["input"] = expand_input(store, payload)
            items.append(item)
        return redact(
            {
                "items": items,
                "next_before": items[-1]["seq"] if items else None,
                "input_budget": None,
                "meaning": "DSH input before provider formatting; usage is provider-reported; "
                "character counts are not tokens. Older unrecorded inputs are not reconstructed.",
            }
        )

    @app.get("/api/apps/{app_id}/views/{view_id}")
    async def view(request: Request, app_id: str, view_id: str):
        authenticate(request)
        plugin = app.state.kernel.apps.get(app_id)
        dashboard = plugin.manifest.dashboard if plugin else None
        definition = (
            next((v for v in dashboard.views if v.id == view_id), None) if dashboard else None
        )
        if definition is None:
            raise HTTPException(404, "未声明的应用视图")
        store = app.state.kernel.store
        if definition.source == "observations":
            # Attribute tool observations by their operation, not a guessed reference prefix.
            records = store.db.execute(
                "SELECT o.* FROM observations o JOIN operations p ON o.reference=p.id "
                "WHERE o.kind='tool' AND p.app=? ORDER BY o.id DESC LIMIT 100",
                (app_id,),
            ).fetchall()
            items = [{**dict(r), "content": json.loads(r["content"])} for r in records]
        else:
            items = rows(store, definition.source, app=app_id)
        return redact({"view": definition.model_dump(), "items": items})

    @app.get("/api/stream")
    async def stream(request: Request):
        authenticate(request)

        async def updates():
            previous = None
            while not await request.is_disconnected():
                store = app.state.kernel.store
                cursor = store.db.execute("SELECT coalesce(max(seq),0) FROM events").fetchone()[0]
                revision = json.dumps(
                    [
                        cursor,
                        store.get("runtime_status"),
                        app.state.kernel.catalog(),
                        int(time.time() // 60),
                        store.get("presentation_config", {}).get("revision", 0),
                        store.get("idle_wake"),
                        int(time.time() // 5)
                        if store.get("runtime_status", {}).get("phase") in {"thinking", "executing"}
                        else None,
                        store.get("presentation_schedule_revision"),
                    ]
                )
                if revision != previous:
                    # Durable history comes from /api/events; the stream invalidates snapshots.
                    payload = json.dumps({"cursor": cursor})
                    yield f"id: {cursor}\nevent: refresh\ndata: {payload}\n\n"
                    previous = revision
                else:
                    yield ": heartbeat\n\n"
                await asyncio.sleep(1)

        return StreamingResponse(
            updates(), media_type="text/event-stream", headers={"X-Accel-Buffering": "no"}
        )


def create_dashboard_app(
    *, config_path, data_dir, token, demo=False, issues=None, schedule_path=None
):
    from astcho_world.cli import load_apps

    @asynccontextmanager
    async def lifespan(app):
        kernel = Kernel(data_dir)
        app.state.kernel = kernel
        app.state.life = LifeState(schedule_path=schedule_path, demo=demo)
        try:
            load_apps(kernel, config_path)
            kernel.store.set("runtime_status", {"phase": "observer", "updated": time.time()})
            if demo:
                await seed_demo(kernel)
                from astcho_world.usage import seed_demo_usage

                seed_demo_usage(kernel.store)
            collector = UsageCollector(kernel)
            collector.sync()
            telemetry_task = asyncio.create_task(collector.run())
            try:
                yield
            finally:
                telemetry_task.cancel()
                await asyncio.gather(telemetry_task, return_exceptions=True)
        finally:
            await kernel.close()

    app = FastAPI(lifespan=lifespan)
    install_dashboard(app, token, mode="demo" if demo else "observer", issues=issues)
    return app


async def seed_demo(kernel):
    """Only synthetic records in an explicitly selected, disposable demo directory."""
    from astcho_world.runtime import Agent

    await kernel.ingest(
        "qq",
        {
            "batch_id": "web-demo",
            "chat_id": "group:demo",
            "title": "星际茶话会 · 示例群",
            "messages": [
                {
                    "message_id": "1",
                    "user_id": "demo",
                    "nickname": "示例朋友",
                    "text": "今晚一起看看星星吧。",
                    "timestamp": time.time(),
                },
                {
                    "message_id": "2",
                    "user_id": "demo",
                    "nickname": "示例朋友",
                    "text": "星回，刚刚分享了观星资料，要来看看吗？",
                    "timestamp": time.time(),
                    "mentioned_bot": True,
                },
            ],
        },
    )

    class Script:
        index = 0

        async def decide(self, messages, tools):
            sequence = [
                ("world_open", {"id": "qq:group:demo:2"}),
                ("world_tools", {"app_id": "notes"}),
                (
                    "notes__save",
                    {
                        "title": "今晚的观星邀请",
                        "text": "我打开了群聊，读到了朋友的邀请。稍后再看看资料。",
                    },
                ),
                ("world_remind", {"due": time.time() + 1800, "text": "回来看一看观星资料"}),
                ("world_finish", {}),
            ]
            name, args = sequence[self.index]
            self.index += 1
            return {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": f"demo-{self.index}",
                        "type": "function",
                        "function": {
                            "name": name,
                            "arguments": json.dumps(args, ensure_ascii=False),
                        },
                    }
                ],
            }

    await Agent(kernel, Script()).run()
    kernel.store.set("runtime_status", {"phase": "idle", "updated": time.time()})
