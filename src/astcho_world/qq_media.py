"""QQ observation adapter for the reusable image reader; no background ingestion."""

from __future__ import annotations

import asyncio
import json

from astcho_world.images import ImageReader
from astcho_world.qq_notifications import raw_segments

# Fits the existing 120-second App tool deadline, leaving time for chat context.
OPEN_MEDIA_SECONDS = 60
IMAGE_SECONDS = 40


def entries(raw):
    result, image_index = [], 0
    for segment in raw_segments(raw):
        kind = segment.get("type")
        if kind not in {"image", "video", "record", "forward"}:
            continue
        item = {"type": kind, "status": "not_understood"}
        if kind == "image":
            image_index += 1
            item["image_index"] = image_index
        else:
            item["reason"] = "This reader currently supports still images only"
        result.append(item)
    return result


def raw_event(app, snapshot, message_id):
    row = app.db.execute(
        "SELECT body FROM raw_events WHERE chat=? AND mid=?", (snapshot["chat_id"], message_id)
    ).fetchone()
    return json.loads(row[0]) if row else None


def image_source(app, snapshot, message_id, image_index):
    if not any(m["message_id"] == message_id for m in snapshot["messages"]):
        raise ValueError("Image must belong to a message in the observed snapshot")
    images = [
        s for s in raw_segments(raw_event(app, snapshot, message_id)) if s.get("type") == "image"
    ]
    if image_index < 1 or image_index > len(images):
        raise ValueError("Image index is outside this observed message")
    return str((images[image_index - 1].get("data") or {}).get("url") or "")


def reader(app):
    if not app.config.get("conversation", {}).get("allow_send"):
        raise ValueError("QQ vision backend is not enabled")
    if not hasattr(app, "_image_reader"):
        runtime = app.group_backend().runtime
        app._image_reader = ImageReader(app.data_dir / "images", runtime.vision)
    return app._image_reader


async def enrich(app, snapshot):
    pending = [
        (message, item)
        for message in snapshot["messages"]
        for item in message.get("media", [])
        if item["type"] == "image"
    ]
    try:
        backend = reader(app) if pending else None
    except Exception as exc:
        for _, item in pending:
            item.update(
                status="unavailable", reason=f"Vision backend unavailable: {type(exc).__name__}"
            )
    else:
        semaphore = asyncio.Semaphore(3)

        async def describe(message, item):
            async with semaphore:
                try:
                    source = image_source(app, snapshot, message["message_id"], item["image_index"])
                    if not source:
                        item.update(status="unavailable", reason="No downloadable image URL")
                        return
                    async with asyncio.timeout(IMAGE_SECONDS):
                        item.update(await backend.brief(source))
                except Exception as exc:
                    item.update(
                        status="failed", reason=f"Image recognition failed: {type(exc).__name__}"
                    )

        try:
            async with asyncio.timeout(OPEN_MEDIA_SECONDS):
                await asyncio.gather(*(describe(message, item) for message, item in pending))
        except TimeoutError:
            for _, item in pending:
                if item["status"] == "not_understood":
                    item.update(
                        status="deferred", reason="Read time budget reached; view_image can retry"
                    )
    for message in snapshot["messages"]:
        # Do not replace a description supplied by an existing trusted pipeline.
        if not message["image_description"] and message.get("media"):
            message["image_description"] = "; ".join(
                item.get("description") or f"[{item['type']}未理解：{item['status']}]"
                for item in message["media"]
            )
    snapshot["requires_media"] = any(
        item["status"] != "understood"
        for message in snapshot["messages"]
        for item in message.get("media", [])
    )
    with app.db:
        app.db.execute(
            "UPDATE snapshots SET body=? WHERE id=?",
            (json.dumps(snapshot), snapshot["observation_id"]),
        )


async def view_image(app, args):
    snapshot = app.snapshot(args["observation_id"])
    message_id, index = args["message_id"], args.get("image_index", 1)
    source = image_source(app, snapshot, message_id, index)  # Validate before opening any backend.
    if not source:
        return {"status": "unavailable", "reason": "No downloadable image URL"}
    try:
        async with asyncio.timeout(80):
            result = await reader(app).inspect(source, args["question"])
    except Exception as exc:
        return {"status": "failed", "reason": f"Image inspection failed: {type(exc).__name__}"}
    return {
        "observation_id": snapshot["observation_id"],
        "message_id": message_id,
        "image_index": index,
        **result,
    }
