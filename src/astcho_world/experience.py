"""Read only the subject's recorded observations and actions, never App archives."""

import json

KINDS = (
    "observation.created",
    "decision.tool",
    "decision.result",
    "model.finished",
    "activity.started",
    "activity.finished",
    "activity.failed",
)


def recall(
    store,
    *,
    query="",
    before=None,
    event_seq=None,
    limit=20,
    activity_id=None,
    full=False,
    app=None,
    since=None,
    until=None,
):
    where = [
        "kind IN (" + ",".join("?" for _ in KINDS) + ")",
        "COALESCE(json_extract(data,'$.tool'),'') != 'world_recall'",
    ]
    params = list(KINDS)
    if activity_id:
        where.append("activity_id IN (SELECT run_id FROM activity_runs WHERE logical_id=?)")
        params.append(activity_id)
    if app:
        where.append("(app=? OR json_extract(data,'$.tool') LIKE ?)")
        params.extend([app, app + "__%"])
    for field, operator, value in [("created", ">=", since), ("created", "<=", until)]:
        if value is not None:
            where.append(f"{field}{operator}?")
            params.append(value)
    if event_seq is not None:
        where.append("seq=?")
        params.append(event_seq)
    if before is not None:
        where.append("seq<?")
        params.append(before)
    if query:
        where.append("(summary LIKE ? OR data LIKE ?)")
        params.extend([f"%{query}%"] * 2)
    rows = store.db.execute(
        "SELECT seq,created,app,kind,activity_id,operation_id,summary,data FROM events WHERE "
        + " AND ".join(where)
        + " ORDER BY seq DESC LIMIT ?",
        (*params, limit),
    ).fetchall()
    items = []
    for row in rows:
        item = dict(row)
        mapping = store.db.execute(
            "SELECT logical_id FROM activity_runs WHERE run_id=?", (item["activity_id"],)
        ).fetchone()
        item["logical_activity_id"] = mapping[0] if mapping else None
        raw = item.pop("data")
        if event_seq is not None or full:
            item["data"] = json.loads(raw)
        else:
            item["preview"] = raw[:240]
            item["preview_only"] = True
        items.append(item)
    return {
        "items": items,
        "next_before": items[-1]["seq"] if items else None,
        "scope": "recorded observations and own actions; "
        "not personal memory or unread App archives",
    }


def operation_states(store, ids=None):
    where, params = "effect='write'", []
    if ids is None:
        where += " AND status IN ('running','unknown')"
    else:
        if not ids:
            return []
        where += " AND id IN (" + ",".join("?" for _ in ids) + ")"
        params.extend(ids)
    return [
        dict(row)
        for row in store.db.execute(
            "SELECT id AS operation_id,app,tool,status FROM operations WHERE "
            + where
            + " ORDER BY rowid",
            params,
        )
    ]
