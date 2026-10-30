"""Lossless, content-addressed request telemetry; no input truncation or budget."""

import hashlib
import json


def record_input(store, payload):
    record = {**payload, "content_storage": "sha256-fragments-v1"}
    for key in ("messages", "tools"):
        references = []
        for item in payload.get(key, []):
            content = json.dumps(item, ensure_ascii=False, separators=(",", ":"))
            digest = hashlib.sha256(content.encode()).hexdigest()
            store.db.execute(
                "INSERT OR IGNORE INTO context_fragments(hash,content) VALUES(?,?)",
                (digest, content),
            )
            references.append(digest)
        record[key] = references
    store.event("debug.model_request", "DSH 模型实际输入（提供商协议转换前）", data=record)


def expand_input(store, record):
    if record.get("content_storage") != "sha256-fragments-v1":
        return record
    value = dict(record)
    for key in ("messages", "tools"):
        items = []
        for digest in record[key]:
            row = store.db.execute(
                "SELECT content FROM context_fragments WHERE hash=?", (digest,)
            ).fetchone()
            if row is None:
                raise ValueError("Recorded context fragment is missing")
            items.append(json.loads(row[0]))
        value[key] = items
    return value
