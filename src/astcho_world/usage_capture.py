"""Optional passive adapter around existing clients; requests/results are passed unchanged."""

from __future__ import annotations

import json
import logging
import sqlite3
import time
import uuid
from contextvars import ContextVar
from functools import wraps
from pathlib import Path

from astcho_world.usage import provider_id

call_purpose = ContextVar("usage_purpose", default=None)
operation_id = ContextVar("usage_operation", default=None)
logger = logging.getLogger(__name__)


class UsageReporter:
    """Plugin reporting contract: start + terminal report, stable request ID, no prompt bodies."""

    def __init__(self, data_dir: Path):
        self.path = data_dir / "usage-spool.sqlite3"
        data_dir.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.path) as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY,value TEXT);
                CREATE TABLE IF NOT EXISTS reports (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT, data TEXT NOT NULL);
            """)
            db.execute("INSERT OR IGNORE INTO metadata VALUES('installed',?)", (str(time.time()),))

    def emit(self, value):
        # Telemetry must not turn a settled model request into a business failure.
        try:
            with sqlite3.connect(self.path) as db:
                db.execute("INSERT INTO reports(data) VALUES(?)", (json.dumps(value),))
        except Exception:
            logger.exception("Could not persist optional usage telemetry")

    def instrument(self, client, *, purpose):
        resource = client.chat.completions
        if getattr(resource, "_world_usage_instrumented", False):
            return
        original = resource.create
        provider = provider_id(client.base_url)

        @wraps(original)
        async def create(*args, **kwargs):
            rid = uuid.uuid4().hex
            started = time.monotonic()
            self.emit(
                {
                    "event": "start",
                    "request_id": rid,
                    "created": time.time(),
                    "provider": provider,
                    "model": kwargs.get("model", "unknown"),
                    "purpose": call_purpose.get() or purpose,
                    "operation_id": operation_id.get(),
                }
            )
            try:
                response = await original(*args, **kwargs)
            except BaseException:
                self.emit(
                    {
                        "event": "end",
                        "request_id": rid,
                        "status": "failed",
                        "duration_ms": round((time.monotonic() - started) * 1000),
                    }
                )
                raise
            try:
                usage = getattr(response, "usage", None)
                if usage is not None:
                    usage = usage.model_dump() if hasattr(usage, "model_dump") else dict(usage)
                self.emit(
                    {
                        "event": "end",
                        "request_id": rid,
                        "status": "succeeded",
                        "duration_ms": round((time.monotonic() - started) * 1000),
                        "usage": usage,
                    }
                )
            except Exception:
                logger.exception("Could not decode optional usage telemetry")
            return response

        resource.create = create
        resource._world_usage_instrumented = True


def instrument_legacy(runtime, data_dir):
    try:
        reporter = UsageReporter(data_dir)
        original_json = runtime.llm.json_completion
        schema_purposes = {
            "PlannerDecision": "chat_planning",
            "ReplyPayload": "chat_reply",
            "MemoryExtraction": "memory",
            "ExpressionExtraction": "expression",
            "ExpressionReview": "expression",
            "VisionResult": "vision",
            "MemeAnalysis": "vision",
            "MemeTasteDecision": "meme",
            "MemeSelection": "meme",
        }

        @wraps(original_json)
        async def json_completion(*args, **kwargs):
            schema = kwargs.get("schema")
            token = call_purpose.set(schema_purposes.get(getattr(schema, "__name__", "")))
            try:
                return await original_json(*args, **kwargs)
            finally:
                call_purpose.reset(token)

        runtime.llm.json_completion = json_completion
        for name, purpose in (
            ("text_client", "chat_other"),
            ("planner_client", "chat_planning"),
            ("vision_client", "vision"),
            ("reasoning_client", "chat_reasoning"),
        ):
            reporter.instrument(getattr(runtime.llm, name), purpose=purpose)
    except Exception:
        logger.exception("Legacy usage instrumentation unavailable")
