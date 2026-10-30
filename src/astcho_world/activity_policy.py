"""Conservative evidence-based omission of empty wake summaries; no model judge."""

import json

STATUS_TOOLS = {
    "world_finish",
    "world_apps",
    "world_tools",
    "world_notifications",
    "world_ignore",
    "world_skills",
    "world_activity",
    "qq__list_chats",
}


def unsuccessful(value):
    if isinstance(value, dict):
        status = value.get("status")
        if (
            value.get("isError")
            or value.get("error")
            or (
                isinstance(status, str)
                and status
                in {
                    "unknown",
                    "failed",
                    "running",
                    "cancelled",
                    "unavailable",
                    "error",
                }
            )
        ):
            return True
        return any(unsuccessful(item) for item in value.values())
    if isinstance(value, list):
        return any(unsuccessful(item) for item in value)
    return False


def empty_activity(store, item):
    if item["state"] != "finished" or any(
        item.get(k)
        for k in (
            "legacy",
            "resume_note",
            "resource_refs",
            "wait",
        )
    ):
        return False
    where = " FROM events e JOIN activity_runs r ON e.activity_id=r.run_id WHERE r.logical_id=?"
    decisions = [
        json.loads(row[0])
        for row in store.db.execute(
            "SELECT e.data" + where + " AND e.kind='decision.tool'", (item["id"],)
        )
    ]
    for decision in decisions:
        tool, args = decision.get("tool"), decision.get("arguments", {})
        if tool not in STATUS_TOOLS:
            return False
        if tool == "world_activity" and args:
            return False
        if tool == "world_finish" and isinstance(args.get("note"), str) and args["note"].strip():
            return False
        if tool == "world_notifications" and args.get("include_delivered"):
            return False
    calls = {d["call_id"] for d in decisions if d.get("call_id")}
    results, observed_operations = set(), set()
    for row in store.db.execute(
        "SELECT e.data" + where + " AND e.kind='decision.result'", (item["id"],)
    ):
        data = json.loads(row[0])
        if data.get("tool") not in STATUS_TOOLS or unsuccessful(data.get("result")):
            return False
        results.add(data.get("call_id"))
        result = data.get("result")
        if isinstance(result, dict) and result.get("operation_id"):
            observed_operations.add(result["operation_id"])
    if calls - results:
        return False
    for row in store.db.execute(
        "SELECT e.kind,e.data" + where + " AND e.kind IN "
        "('observation.created','model.finished','activity.failed')",
        (item["id"],),
    ):
        kind, data = row[0], json.loads(row[1])
        if kind == "activity.failed":
            return False
        if kind == "observation.created":
            if data.get("kind") == "notification":
                continue
            if data.get("kind") != "tool" or data.get("reference") not in observed_operations:
                return False
        if kind == "model.finished":
            # Reasoning before a finish call is not a separate activity. Preserve
            # substantive plain completion text, rather than guessing its meaning.
            message = data.get("message", data)
            if not isinstance(message, dict):
                return False
            content = message.get("content", [])
            if isinstance(content, str):
                if content.strip():
                    return False
            elif isinstance(content, list):
                for block in content:
                    if not isinstance(block, dict):
                        return False
                    if block.get("type") == "text" and block.get("text", "").strip():
                        return False
                    if block.get("type") not in {"text", "reasoning", "tool-call"}:
                        return False
            else:
                return False
    return True
