from __future__ import annotations

import asyncio
import hmac
import logging
import os
import time
from contextlib import asynccontextmanager
from dataclasses import asdict

from astcho.services.schedule import ScheduleService
from astcho_world.cli import load_apps
from astcho_world.ingress import QQIngress
from astcho_world.kernel import Kernel
from astcho_world.onebot import normalize_event
from astcho_world.runtime import Agent

logger = logging.getLogger(__name__)


def create_app(
    *,
    config_path,
    data_dir,
    bot_id,
    ingress_token,
    model=None,
    skills=None,
    schedule_path=None,
    idle_seconds=0,
    web_token=None,
    qq_transport="reverse_ws",
    ws_token=None,
    profile_path=None,
    runner="dsh",
    admins=None,
    adaptive_wake=False,
    wake_min_seconds=120,
    wake_max_seconds=600,
    activity_context=False,
    recent_activities=10,
    mute_groups=False,
):
    """OneBot ingress and serialized subject loop; no connection on import."""
    from fastapi import FastAPI, HTTPException, Request, WebSocket

    if qq_transport not in {"reverse_ws", "http"}:
        raise ValueError("Unknown QQ transport")
    if qq_transport == "http" and not ingress_token:
        raise ValueError("An ingress token is required")
    if idle_seconds < 0:
        raise ValueError("idle_seconds cannot be negative")
    from astcho_world.wake import IdleClock

    idle_clock = IdleClock(
        time.monotonic(),
        adaptive=adaptive_wake,
        fixed=idle_seconds,
        minimum=wake_min_seconds,
        maximum=wake_max_seconds,
    )
    if runner not in {"dsh", "native"}:
        raise ValueError("Unknown environment runner")

    @asynccontextmanager
    async def lifespan(app):
        kernel = Kernel(data_dir)
        load_apps(kernel, config_path)
        if mute_groups and "qq" in kernel.apps:
            kernel.apps["qq"].config["default_group_muted"] = True
        from astcho.config import _csv_set
        from astcho_world.private_inbox import PrivateInbox

        admin_ids = (
            frozenset(admins) if admins is not None else _csv_set(os.getenv("ASTCHO_ADMINS"))
        )
        if model and "qq" in kernel.apps:
            config = kernel.apps["qq"].config
            config["private_routing"] = True
            config["private_admins"] = sorted(admin_ids)
            config.setdefault("conversation", {})["private_direct"] = True
        schedule = ScheduleService(schedule_path or data_dir / "schedule.unconfigured.json")
        ingress = QQIngress(kernel, schedule_provider=lambda: asdict(schedule.current()))

        def self_state():
            state = {"schedule": asdict(schedule.current())}
            if "qq" in kernel.apps:
                connection = kernel.store.get("qq_connection", {})
                state["qq"] = {
                    "transport": qq_transport,
                    "connection": connection.get("state", "unknown"),
                    "participation_enabled": bool(
                        kernel.apps["qq"].config.get("conversation", {}).get("allow_send", False)
                    ),
                    "group_reply_mode": kernel.apps["qq"]
                    .config.get("conversation", {})
                    .get("group_reply_mode", "agent"),
                }
            return state

        try:
            driver = Agent
            if runner == "dsh":
                from astcho_world.dsh_runtime import DSHAgent

                driver = DSHAgent
            driver_options = (
                {"activity_context": activity_context, "recent_activities": recent_activities}
                if runner == "dsh"
                else {}
            )
            agent = (
                driver(
                    kernel,
                    model,
                    skills=skills,
                    profile_path=profile_path,
                    context_provider=self_state,
                    wake_policy={
                        "mode": "continuous",
                        "notifications": True,
                        "reminders": True,
                        "periodic_seconds": idle_seconds,
                        "adaptive": adaptive_wake,
                    },
                    **driver_options,
                )
                if model
                else None
            )
        except Exception:
            await kernel.close()
            if model and hasattr(model, "client"):
                await model.client.close()
            raise
        wake = asyncio.Event()
        queued_reasons = set()

        def wake_admin():
            queued_reasons.add("admin_request")
            wake.set()

        if agent and "qq" in kernel.apps:
            private = PrivateInbox(kernel, admin_ids, wake_admin)
            ingress.private_handler = private.receive
        app.state.kernel, app.state.ingress = kernel, ingress
        app.state.schedule = schedule
        from astcho_world.life import LifeState

        app.state.life = LifeState(schedule=schedule, transport=qq_transport)
        bridge = None
        if qq_transport == "reverse_ws":
            from astcho_world.reverse_ws import ReverseWS

            bridge = ReverseWS(
                kernel, ingress, bot_id=bot_id, access_token=ws_token, schedule_path=schedule.path
            )
            await bridge.start()
        app.state.qq_bridge = bridge
        app.state.qq_transport = qq_transport
        kernel.store.set("runtime_status", {"phase": "idle" if agent else "observer"})

        async def subject():
            while True:
                await wake.wait()
                wake.clear()
                reasons = [{"kind": kind} for kind in sorted(queued_reasons)]
                queued_reasons.clear()
                if not kernel.store.pending_notification_ids():
                    reasons = [r for r in reasons if r["kind"] not in {"notification", "reminder"}]
                if not reasons:
                    continue
                if agent:
                    request = kernel.store.take_admin(admin_ids)
                    if request is None and reasons == [{"kind": "admin_request"}]:
                        continue
                    try:
                        options = {"wake_reasons": reasons}
                        if request is not None:
                            options["admin_request"] = request
                        result = await agent.run(**options)
                        if request:
                            kernel.store.settle_admin(request["id"], "processed", result)
                    except Exception:
                        if request:
                            kernel.store.settle_admin(request["id"], "interrupted", {})
                        logger.exception("Environment turn failed; waiting for another wake")
                    finally:
                        idle_clock.completed(time.monotonic())
                        if kernel.store.db.execute(
                            "SELECT 1 FROM admin_requests WHERE state='pending' LIMIT 1"
                        ).fetchone():
                            wake_admin()

        async def ticker():
            last_pending = set()
            while True:
                kernel.store.tick()
                resume = kernel.activities.tick(
                    {a for a, process in kernel.apps.items() if process.enabled}
                )
                if resume == "expired":
                    idle_clock.completed(time.monotonic())
                elif resume:
                    queued_reasons.add(resume)
                    wake.set()
                rows = kernel.store.db.execute(
                    "SELECT id,app FROM notifications WHERE state='pending'"
                ).fetchall()
                pending = {row[0] for row in rows}
                new = pending - last_pending
                busy = bool(agent and getattr(agent, "active", None))
                if new and not busy:
                    queued_reasons.update(
                        "reminder" if row[1] == "world" else "notification"
                        for row in rows
                        if row[0] in new
                    )
                    wake.set()
                if not busy:
                    last_pending = pending
                current = kernel.activities.current()
                opportunity = idle_clock.state(
                    time.monotonic(),
                    schedule.current().talk_value,
                    busy=busy,
                    waiting=bool(current and current["state"] == "waiting"),
                )
                kernel.store.set("idle_wake", {**opportunity, "updated": time.time()})
                if opportunity["ready"] and not wake.is_set():
                    queued_reasons.add("periodic")
                    wake.set()
                await asyncio.sleep(0.25)

        tasks = []
        try:
            if agent and runner == "dsh":
                await agent.start()
            if (
                agent
                and kernel.store.db.execute(
                    "SELECT 1 FROM admin_requests WHERE state='pending' LIMIT 1"
                ).fetchone()
            ):
                wake_admin()
            from astcho_world.usage import UsageCollector

            collector = UsageCollector(kernel)
            collector.sync()
            tasks = [
                asyncio.create_task(subject()),
                asyncio.create_task(ticker()),
                asyncio.create_task(collector.run()),
            ]
            yield
        finally:
            # Stop input windows before closing application processes and the store.
            try:
                await ingress.drain()
            except Exception:
                logger.exception("Failed to drain an incoming message window")
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            if agent is not None and hasattr(agent, "close"):
                await agent.close()
            if bridge is not None:
                await bridge.close()
            await kernel.close()
            if model and hasattr(model, "client"):
                await model.client.close()

    app = FastAPI(lifespan=lifespan)

    # Use the actual Request annotation (not a function-local forward reference).
    async def receive(request):
        expected = f"Bearer {ingress_token}"
        if not hmac.compare_digest(request.headers.get("authorization", ""), expected):
            raise HTTPException(status_code=401, detail="Invalid ingress token")
        try:
            event = normalize_event(await request.json(), bot_id)
        except (ValueError, KeyError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if event:
            chat_id, message, metadata = event
            app.state.ingress.receive(chat_id, message, **metadata)
        return {"accepted": event is not None}

    receive.__annotations__["request"] = Request
    if qq_transport == "http":
        app.post("/onebot/events")(receive)
    else:

        async def reverse(websocket):
            await app.state.qq_bridge.handle(websocket)

        reverse.__annotations__["websocket"] = WebSocket
        for path in ("/onebot/v11/", "/onebot/v11/ws", "/onebot/v11/ws/"):
            app.websocket(path)(reverse)
    if web_token:
        from astcho_world.dashboard import install_dashboard

        install_dashboard(app, web_token, mode="live" if model else "observer")
    return app
