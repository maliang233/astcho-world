from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path

from jsonschema import validate

from astcho_world.contracts import Manifest, Result
from astcho_world.process import AppProcess
from astcho_world.store import Store


class Kernel:
    def __init__(self, root: Path):
        self.root = root
        self.store = Store(root / "world.sqlite3")
        from astcho_world.activities import Activities

        self.activities = Activities(self.store, root / "experience")
        self.apps: dict[str, AppProcess] = {}
        self._calls: dict[str, asyncio.Lock] = {}

    def register(self, manifest: Manifest, *, config: dict | None = None):
        if manifest.app_id in self.apps:
            raise ValueError("Application already registered")
        self.apps[manifest.app_id] = AppProcess(
            manifest, self.root / "apps" / manifest.app_id, config
        )

    def catalog(self) -> list[dict]:
        return [
            {
                "app_id": app.manifest.app_id,
                "description": app.manifest.description,
                "version": app.manifest.version,
                "state": "stopped"
                if not app.enabled
                else ("running" if app.process and app.process.returncode is None else "ready"),
            }
            for app in self.apps.values()
        ]

    async def stop_app(self, app_id):
        app = self.apps[app_id]
        app.enabled = False
        await app.stop()

    async def start_app(self, app_id):
        app = self.apps[app_id]
        async with app.lock:
            app.enabled = True
            await app.start()

    async def unregister(self, app_id):
        await self.stop_app(app_id)
        del self.apps[app_id]
        # Application data is retained for explicit backup/reinstallation.

    async def call(
        self, app_id: str, tool_name: str, arguments: dict, *, operation_id: str | None = None
    ) -> Result:
        definition = self.apps[app_id].manifest.tool(tool_name)
        if definition.idempotency_field:
            intent = arguments.get(definition.idempotency_field)
            if not isinstance(intent, str) or not intent.strip():
                raise ValueError("An explicit action intent ID is required")
            operation_id = f"intent:{app_id}:{tool_name}:{intent}"
        operation_id = operation_id or uuid.uuid4().hex
        lock = self._calls.setdefault(operation_id, asyncio.Lock())
        async with lock:
            return await self._call(app_id, tool_name, arguments, operation_id)

    async def _call(self, app_id, tool_name, arguments, operation_id):
        app = self.apps[app_id]
        tool = app.manifest.tool(tool_name)
        validate(arguments, tool.input_schema)
        for field in tool.observed_url_fields:
            target = arguments.get(field)

            def contains(value, expected=target):
                if isinstance(value, str):
                    if value == expected:
                        return True
                    try:
                        nested = json.loads(value)
                    except ValueError:
                        return False
                    return contains(nested, expected) if not isinstance(nested, str) else False
                if isinstance(value, dict):
                    return any(contains(v, expected) for v in value.values())
                if isinstance(value, list):
                    return any(contains(v, expected) for v in value)
                return False

            if not target or not any(
                contains(json.loads(row[0]))
                for row in self.store.db.execute("SELECT content FROM observations")
            ):
                raise ValueError("Link must come from an actually observed resource")
        request = json.dumps(arguments, sort_keys=True, ensure_ascii=False)
        old = self.store.db.execute(
            "SELECT * FROM operations WHERE id=?", (operation_id,)
        ).fetchone()
        if old is not None:
            if (old["app"], old["tool"], old["request"]) != (app_id, tool_name, request):
                raise ValueError("Operation ID already belongs to a different request")
            if old["result"]:
                return Result.model_validate_json(old["result"])
            return Result(
                operation_id=operation_id,
                status=old["status"],
                error="Interrupted operation; not automatically replayed",
            )
        with self.store.db:
            self.store.db.execute(
                "INSERT INTO operations VALUES(?,?,?,?,?,'running',NULL)",
                (operation_id, app_id, tool_name, request, tool.effect),
            )
        self.store.event(
            "operation.started",
            f"调用 {app_id}.{tool_name}",
            app=app_id,
            operation_id=operation_id,
            data={"arguments": arguments},
        )
        try:
            response = await app.request(
                "call",
                {
                    "tool": tool_name,
                    "arguments": arguments,
                    "operation_id": operation_id,
                    # Trusted execution metadata, never a model-controlled tool argument.
                    "activity_id": self.store.activity.get(),
                    "logical_activity_id": (self.activities.current() or {}).get("id"),
                },
                tool.timeout,
            )
            validate(response, tool.output_schema)
            result = Result(operation_id=operation_id, status="succeeded", data=response)
        except asyncio.CancelledError:
            result = Result(
                operation_id=operation_id,
                status="unknown" if tool.effect == "write" else "cancelled",
            )
            self._settle(result)
            raise
        except Exception as exc:
            # A write may have happened even if decoding/validation or the connection failed.
            result = Result(
                operation_id=operation_id,
                status="unknown" if tool.effect == "write" else "failed",
                error=f"{type(exc).__name__}: {exc}",
            )
        self._settle(result)
        self.store.observe("tool", operation_id, result.model_dump())
        return result

    def _settle(self, result):
        with self.store.db:
            self.store.db.execute(
                "UPDATE operations SET status=?,result=? WHERE id=?",
                (result.status, result.model_dump_json(), result.operation_id),
            )

        row = self.store.db.execute(
            "SELECT app,tool FROM operations WHERE id=?", (result.operation_id,)
        ).fetchone()
        self.store.event(
            "operation.finished",
            f"{row['tool']} · {result.status}",
            app=row["app"],
            operation_id=result.operation_id,
            data=result.model_dump(),
        )

    async def ingest(self, app_id: str, payload: dict) -> int:
        """Trusted transport entry. Never advertised as a model-facing tool."""
        response = await self.apps[app_id].request(
            "ingest", {**payload, "_world_subscription": self.activities.subscription(app_id)}, 15
        )
        count = 0
        for event in response.get("notifications", []):
            self.store.notify(app_id, event)
            count += 1
        return count

    async def open_notification(self, notification_id: str):
        app_id = notification_id.split(":", 1)[0]
        locator = self.store.locator(notification_id, app_id)
        manifest = self.apps[app_id].manifest
        tool = manifest.notification_tool
        # Compatibility for existing protocol-v1 manifests.
        if tool is None and any(t.name == "read_messages" for t in manifest.tools):
            tool = "read_messages"
        if tool is None:
            raise ValueError("Application has no notification opener")
        return await self.call(app_id, tool, locator)

    async def close(self):
        await asyncio.gather(*(app.stop() for app in self.apps.values()))
        self.store.close()
