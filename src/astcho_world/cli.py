from __future__ import annotations

import argparse
import asyncio
import json
import os
import secrets
import tempfile
import time
from pathlib import Path

from astcho_world.baseline import verify
from astcho_world.configuration import apply_paths, load_environment, require_environment
from astcho_world.contracts import Manifest
from astcho_world.kernel import Kernel
from astcho_world.runtime import Agent, OpenAIModel


def environment_model(runner):
    options = {
        "model": os.environ["ASTCHO_WORLD_MODEL"],
        "api_key": os.environ["ASTCHO_WORLD_API_KEY"],
        "base_url": os.environ["ASTCHO_WORLD_BASE_URL"],
    }
    if runner == "native":
        return OpenAIModel(**options)
    if runner != "dsh":
        raise ValueError("Unknown environment runner")
    from astcho_world.dsh_runtime import DSHModelConfig

    return DSHModelConfig(
        **options,
        context_window=int(os.getenv("ASTCHO_WORLD_CONTEXT_WINDOW", "65536")),
        max_tokens=int(os.getenv("ASTCHO_WORLD_MAX_TOKENS", "8192")),
    )


def load_apps(kernel, config_path):
    for item in json.loads(config_path.read_text())["apps"]:
        if item.get("enabled_env") and os.getenv(item["enabled_env"], "1") == "0":
            continue
        path = (config_path.parent / item["manifest"]).resolve()
        config = json.loads(
            json.dumps(item.get("config", {})).replace(
                "${CONFIG_DIR}", str(config_path.parent.resolve())
            )
        )
        kernel.register(Manifest.model_validate_json(path.read_text()), config=config)


async def demo(root):
    from astcho_world.apps.notes import MANIFEST as notes
    from astcho_world.apps.qq import MANIFEST as qq

    kernel = Kernel(root)
    kernel.register(notes)
    kernel.register(qq)
    try:
        await kernel.ingest(
            "qq",
            {
                "batch_id": "demo-1",
                "chat_id": "group:demo",
                "title": "示例群",
                "messages": [
                    {
                        "message_id": "1",
                        "user_id": "10002",
                        "nickname": "示例群友",
                        "text": "这条只有打开群聊才会看见",
                        "timestamp": time.time(),
                    },
                    {
                        "message_id": "2",
                        "user_id": "10002",
                        "nickname": "示例群友",
                        "text": "星回，要看看吗？",
                        "timestamp": time.time(),
                        "mentioned_bot": True,
                    },
                ],
            },
        )
        print(
            json.dumps(
                {"apps": kernel.catalog(), "notifications": kernel.store.notifications()},
                ensure_ascii=False,
                indent=2,
            )
        )
        opened = await kernel.open_notification("qq:group:demo:2")
        print(json.dumps({"explicitly_opened": opened.model_dump()}, ensure_ascii=False, indent=2))
        note = await kernel.call(
            "notes", "save", {"title": "示例观察", "text": "我选择打开了示例群。"}
        )
        print(
            json.dumps(
                {"note": note.model_dump(), "external_messages_sent": 0},
                ensure_ascii=False,
                indent=2,
            )
        )
    finally:
        await kernel.close()


async def run_agent(args):
    kernel = Kernel(args.data_dir)
    model = None
    subject = None
    try:
        load_apps(kernel, args.config)
        require_environment("ASTCHO_WORLD_MODEL", "ASTCHO_WORLD_API_KEY", "ASTCHO_WORLD_BASE_URL")
        model = environment_model(args.runner)
        from dataclasses import asdict

        from astcho.services.schedule import ScheduleService

        schedule = ScheduleService(args.schedule or args.data_dir / "schedule.unconfigured.json")
        driver = Agent
        if args.runner == "dsh":
            from astcho_world.dsh_runtime import DSHAgent

            driver = DSHAgent
        subject = driver(
            kernel,
            model,
            skills=args.skills,
            profile_path=Path(os.environ["ASTCHO_PROFILE_PATH"])
            if os.getenv("ASTCHO_PROFILE_PATH")
            else None,
            context_provider=lambda: {"schedule": asdict(schedule.current())},
        )
        result = await subject.run(max_steps=args.max_steps)
        print(json.dumps(result, ensure_ascii=False))
    finally:
        if subject is not None and hasattr(subject, "close"):
            await subject.close()
        if model is not None and hasattr(model, "client"):
            await model.client.close()
        await kernel.close()


def main():
    parser = argparse.ArgumentParser(description="Astcho's isolated application environment")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("demo", help="离线合成消息演示；不连接 QQ、不调用模型")
    check = commands.add_parser("check-baseline", help="检查原代码字节保护")
    check.add_argument("--project", type=Path, default=Path.cwd())
    check.add_argument("--original", type=Path)
    agent = commands.add_parser("run", help="运行一轮环境决策；应用来自显式配置")
    agent.add_argument("--config", type=Path, default=Path("config/apps.local.json"))
    agent.add_argument("--env-file", type=Path, default=Path(".env"))
    agent.add_argument("--data-dir", type=Path)
    agent.add_argument("--skills", type=Path)
    agent.add_argument("--schedule", type=Path)
    agent.add_argument("--max-steps", type=int, default=20)
    agent.add_argument("--runner", choices=["dsh", "native"], default=None)
    server = commands.add_parser("serve", help="接收 OneBot 反向 WS 并按通知/提醒运行")
    server.add_argument("--config", type=Path, default=Path("config/apps.local.json"))
    server.add_argument("--env-file", type=Path, default=Path(".env"))
    server.add_argument("--data-dir", type=Path)
    server.add_argument("--skills", type=Path)
    server.add_argument("--schedule", type=Path)
    server.add_argument("--port", type=int, default=8091)
    server.add_argument("--host", default=None)
    server.add_argument("--qq-transport", choices=["reverse_ws", "http"], default="reverse_ws")
    server.add_argument("--read-only", action="store_true", help="只接收记录，不启动模型")
    server.add_argument("--idle-seconds", type=float, default=0)
    server.add_argument("--adaptive-wake", action="store_true", default=None)
    server.add_argument("--wake-min-seconds", type=float, default=None)
    server.add_argument("--wake-max-seconds", type=float, default=None)
    server.add_argument("--activity-context", action="store_true", default=None)
    server.add_argument("--mute-groups", action="store_true", default=None)
    server.add_argument("--web-token", default=None, help="优先使用 ASTCHO_WORLD_WEB_TOKEN")
    server.add_argument("--runner", choices=["dsh", "native"], default=None)
    web = commands.add_parser("web", help="启动本地只读观察台，不调用模型或连接 QQ")
    web.add_argument("--config", type=Path, default=Path("config/apps.local.json"))
    web.add_argument("--env-file", type=Path, default=Path(".env"))
    web.add_argument("--data-dir", type=Path)
    web.add_argument("--port", type=int, default=8091)
    web.add_argument("--demo", action="store_true", help="使用独立临时目录和合成数据")
    web.add_argument("--issue", action="append", default=[], help="观察模式的启动说明")
    args = parser.parse_args()
    if args.command in {"run", "serve"}:
        load_environment(args.env_file)
        apply_paths(args)
        args.runner = args.runner or os.getenv("ASTCHO_WORLD_RUNNER", "dsh")
    if args.command == "demo":
        with tempfile.TemporaryDirectory(prefix="astcho-world-demo-") as directory:
            asyncio.run(demo(Path(directory)))
    elif args.command == "check-baseline":
        changed = verify(args.project, original=args.original)
        print(json.dumps({"ok": not changed, "changed": changed}, ensure_ascii=False, indent=2))
        raise SystemExit(bool(changed))
    elif args.command == "run":
        asyncio.run(run_agent(args))
    elif args.command == "web":
        import uvicorn

        from astcho_world.dashboard import create_dashboard_app

        if not args.demo:
            load_environment(args.env_file)
        token = os.getenv("ASTCHO_WORLD_WEB_TOKEN") or secrets.token_urlsafe(32)
        print(f"观察台：http://127.0.0.1:{args.port}/#token={token}", flush=True)
        if args.demo:
            with tempfile.TemporaryDirectory(prefix="astcho-web-demo-") as directory:
                app = create_dashboard_app(
                    config_path=Path(__file__).resolve().parents[2] / "examples/apps.json",
                    data_dir=Path(directory),
                    token=token,
                    demo=True,
                )
                uvicorn.run(app, host="127.0.0.1", port=args.port, timeout_graceful_shutdown=3)
        else:
            app = create_dashboard_app(
                config_path=args.config,
                data_dir=args.data_dir or Path(os.getenv("ASTCHO_WORLD_DATA_DIR", "var")),
                token=token,
                issues=args.issue,
            )
            uvicorn.run(app, host="127.0.0.1", port=args.port, timeout_graceful_shutdown=3)
    else:
        import uvicorn

        from astcho_world.host import create_app

        if args.qq_transport == "http":
            require_environment("ASTCHO_WORLD_BOT_ID", "ASTCHO_WORLD_INGRESS_TOKEN")
        if not args.read_only:
            require_environment(
                "ASTCHO_WORLD_MODEL",
                "ASTCHO_WORLD_API_KEY",
                "ASTCHO_WORLD_BASE_URL",
                "ASTCHO_ADMINS",
            )
            from astcho.config import _csv_set

            if not all(uid.isdecimal() for uid in _csv_set(os.environ["ASTCHO_ADMINS"])):
                raise SystemExit("ASTCHO_ADMINS must contain numeric QQ user IDs")
        model = None if args.read_only else environment_model(args.runner)
        web_token = (
            os.getenv("ASTCHO_WORLD_WEB_TOKEN") or args.web_token or secrets.token_urlsafe(32)
        )
        print(f"观察台：http://127.0.0.1:{args.port}/#token={web_token}", flush=True)
        app = create_app(
            config_path=args.config,
            data_dir=args.data_dir,
            bot_id=os.getenv("ASTCHO_WORLD_BOT_ID") or None,
            ingress_token=os.getenv("ASTCHO_WORLD_INGRESS_TOKEN"),
            model=model,
            skills=args.skills,
            schedule_path=args.schedule,
            idle_seconds=args.idle_seconds,
            adaptive_wake=args.adaptive_wake or os.getenv("ASTCHO_WORLD_ADAPTIVE_WAKE") == "1",
            wake_min_seconds=args.wake_min_seconds
            if args.wake_min_seconds is not None
            else float(os.getenv("ASTCHO_WORLD_WAKE_MIN_SECONDS", "120")),
            wake_max_seconds=args.wake_max_seconds
            if args.wake_max_seconds is not None
            else float(os.getenv("ASTCHO_WORLD_WAKE_MAX_SECONDS", "600")),
            activity_context=args.activity_context
            or os.getenv("ASTCHO_WORLD_ACTIVITY_CONTEXT") == "1",
            recent_activities=int(os.getenv("ASTCHO_WORLD_RECENT_ACTIVITIES", "10")),
            mute_groups=args.mute_groups or os.getenv("ASTCHO_WORLD_MUTE_GROUPS") == "1",
            web_token=web_token,
            qq_transport=args.qq_transport,
            ws_token=os.getenv("ASTCHO_WORLD_WS_TOKEN") or os.getenv("ONEBOT_ACCESS_TOKEN") or None,
            profile_path=Path(os.environ["ASTCHO_PROFILE_PATH"])
            if os.getenv("ASTCHO_PROFILE_PATH")
            else None,
            runner=args.runner,
        )
        uvicorn.run(
            app,
            host=args.host or os.getenv("ASTCHO_WORLD_HOST") or os.getenv("HOST") or "127.0.0.1",
            port=args.port,
            timeout_graceful_shutdown=3,
        )


if __name__ == "__main__":
    main()
