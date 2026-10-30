"""Local facts from recorded experience. No model, inbox access, or semantic guess."""

from collections import Counter

from astcho_world.activity_policy import STATUS_TOOLS, unsuccessful


def factual_index(events, *, legacy=False):
    actions, results, references, note = {}, {}, set(), ""
    for event in events:
        data, seq = event["data"], event["seq"]
        tool = data.get("tool")
        if event["kind"] == "decision.result":
            results[(event["activity_id"], data.get("call_id"))] = data.get("result")
        if event["kind"] != "decision.tool":
            continue
        args = data.get("arguments", {})
        if tool == "world_finish" and isinstance(args.get("note"), str):
            note = args["note"].strip()
            if note:
                references.add(seq)
        if tool in STATUS_TOOLS and not (
            tool == "world_activity"
            and args
            or tool == "world_notifications"
            and args.get("include_delivered")
        ):
            continue
        entry = actions.setdefault(tool, {"count": 0, "targets": [], "calls": []})
        entry["count"] += 1
        entry["calls"].append((event["activity_id"], data.get("call_id")))
        target = next(
            (args[k] for k in ("chat_id", "bvid", "target_message_id", "note_id") if args.get(k)),
            None,
        )
        if isinstance(target, str) and target not in entry["targets"]:
            entry["targets"].append(target)
        references.add(seq)
    facts = []
    for tool, entry in actions.items():
        statuses = Counter()
        for call in entry["calls"]:
            if call[1] is None or call not in results:
                statuses["未记录关联回执"] += 1
                continue
            result = results[call]
            if not isinstance(result, dict):
                statuses["已返回"] += 1
                continue
            states = [result.get("status")]
            if isinstance(result.get("data"), dict):
                states.append(result["data"].get("status"))
            status = "/".join(s for s in states if isinstance(s, str) and s)
            if result.get("error") or result.get("isError"):
                status = (status + "/" if status else "") + "有错误"
            statuses[status or "已返回"] += 1
        outcome = "、".join(f"{key}×{count}" for key, count in statuses.items())
        targets = "，对象：" + "、".join(entry["targets"]) if entry["targets"] else ""
        facts.append(f"{tool}×{entry['count']}（回执：{outcome}{targets}）")
    for event in events:
        data, kind = event["data"], event["kind"]
        if kind == "decision.result":
            references.add(event["seq"])
            if data.get("tool") in STATUS_TOOLS and unsuccessful(data.get("result")):
                facts.append(f"{data.get('tool')} 回执包含失败、未完成或未知项，详见完整记录")
        if kind == "activity.failed":
            facts.append("运行中断或失败，详见完整记录")
            references.add(event["seq"])
        if kind == "observation.created" and data.get("kind") not in {"tool", "notification"}:
            observation = (
                "收到管理员请求" if data.get("kind") == "admin_request" else "读取了观察记录"
            )
            if observation not in facts:
                facts.append(observation)
            references.add(event["seq"])
    if not facts:
        facts.append("留下内部记录；具体内容按活动编号回看")
    prefix = "旧环境运行记录；" if legacy else ""
    brief = prefix + (f"自留：{note}；实际记录：" if note else "") + "；".join(facts)
    return {
        "version": 2,
        "brief": brief,
        "references": sorted(references) or [events[0]["seq"]],
    }
