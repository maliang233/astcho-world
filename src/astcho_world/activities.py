"""Persistent life activities and archives of delivered experience, not App inboxes."""

import hashlib
import json
import os
import time
import uuid
from datetime import datetime
from pathlib import Path

from astcho_world.experience import KINDS


def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    temporary.chmod(0o600)
    with temporary.open("rb") as saved:
        os.fsync(saved.fileno())
    temporary.replace(path)


class Activities:
    def __init__(self, store, root):
        self.store, self.root = store, Path(root)
        store.db.executescript("""
            CREATE TABLE IF NOT EXISTS life_activities (
                id TEXT PRIMARY KEY, created REAL, updated REAL, state TEXT, data TEXT);
            CREATE TABLE IF NOT EXISTS activity_runs (
                run_id TEXT PRIMARY KEY, logical_id TEXT REFERENCES life_activities(id));
        """)
        store.db.execute("UPDATE life_activities SET state='suspended' WHERE state='running'")
        store.db.commit()
        # Classify existing indexes once; keep their original summaries/archives.
        for row in store.db.execute(
            "SELECT id FROM life_activities WHERE state='finished' "
            "AND json_extract(data,'$.summary_policy.version') IS NULL"
        ).fetchall():
            self.summary_policy(row[0])

    def current(self):
        row = self.store.db.execute(
            "SELECT * FROM life_activities WHERE state!='finished' ORDER BY created DESC LIMIT 1"
        ).fetchone()
        return self._decode(row) if row else None

    @staticmethod
    def _decode(row):
        return {**dict(row), **json.loads(row["data"]), "data": None}

    def get(self, activity_id):
        row = self.store.db.execute(
            "SELECT * FROM life_activities WHERE id=?", (activity_id,)
        ).fetchone()
        if row is None:
            raise ValueError("Unknown activity")
        return self._decode(row)

    def save(self, item):
        item = {**item, "updated": time.time()}
        data = {
            k: v for k, v in item.items() if k not in {"id", "created", "updated", "state", "data"}
        }
        with self.store.db:
            self.store.db.execute(
                "INSERT OR REPLACE INTO life_activities VALUES(?,?,?,?,?)",
                (item["id"], item["created"], item["updated"], item["state"], json.dumps(data)),
            )
        return item

    def begin(self, run_id):
        item = self.current() or {
            "id": uuid.uuid4().hex,
            "created": time.time(),
            "state": "running",
            "resume_note": "",
            "resource_refs": [],
            "archive_state": "pending",
        }
        item.update(state="running", wait=None)
        self.save(item)
        with self.store.db:
            self.store.db.execute("INSERT INTO activity_runs VALUES(?,?)", (run_id, item["id"]))
        return item

    def update(self, *, resume_note=None, resource_refs=None):
        item = self.current()
        if item is None:
            raise ValueError("No current activity")
        if resume_note is not None:
            item["resume_note"] = resume_note
        if resource_refs is not None:
            # A reference must have actually been observed by this subject.
            for ref in resource_refs:
                if not self.store.db.execute(
                    "SELECT 1 FROM observations WHERE reference=?", (ref,)
                ).fetchone():
                    raise ValueError("Resource reference has not been observed")
            item["resource_refs"] = resource_refs
        return self.save(item)

    def settle(self, state, **extra):
        item = self.current()
        if item:
            item.update(state=state, **extra)
            if state != "waiting":
                item["wait"] = None
            return self.save(item)

    def pause(self, *, resume_note, expires_in, app_id=None, scope=None, after_seconds=None):
        if not 0 < expires_in <= 3600:
            raise ValueError("Wait must expire within one hour")
        if (app_id is None) == (after_seconds is None):
            raise ValueError("Choose an App event or one time condition")
        if app_id and not scope:
            raise ValueError("An event wait requires an explicit scope")
        if after_seconds is not None and not 0 < after_seconds <= expires_in:
            raise ValueError("Time condition must be within expiry")
        self.update(resume_note=resume_note)
        now = time.time()
        return self.settle(
            "waiting",
            wait={
                "app_id": app_id,
                "scope": scope,
                "expires_at": now + expires_in,
                "due": now + after_seconds if after_seconds is not None else None,
            },
        )

    def tick(self, enabled_apps, now=None):
        now = time.time() if now is None else now
        item = self.current()
        if not item or item["state"] != "waiting":
            return None
        wait = item["wait"]
        if wait["due"] is not None and now >= wait["due"]:
            self.settle("suspended", resume_reason="chosen_time")
            return "activity_time"
        if now >= wait["expires_at"] or (wait["app_id"] and wait["app_id"] not in enabled_apps):
            self.settle("finished", finish_reason="wait_expired")
            return "expired"
        return None

    def subscription(self, app_id):
        item = self.current()
        if item and item["state"] == "waiting" and item.get("wait"):
            wait = item["wait"]
            if wait["app_id"] == app_id and wait["expires_at"] > time.time():
                return {
                    "scope": wait["scope"],
                    "expires_at": wait["expires_at"],
                    "activity_id": item["id"],
                }
        return None

    def finished_ids(self):
        return [
            r[0]
            for r in self.store.db.execute(
                "SELECT id FROM life_activities WHERE state='finished' ORDER BY created"
            )
        ]

    def summary_policy(self, activity_id):
        from astcho_world.activity_policy import empty_activity

        item = self.get(activity_id)
        policy = item.get("summary_policy")
        if policy and policy.get("version") == 1:
            return policy
        policy = {"version": 1, "kind": "empty" if empty_activity(self.store, item) else "summary"}
        item["summary_policy"] = policy
        if policy["kind"] == "empty":
            item.update(archive_state="empty", archive_ref=f"activity:{activity_id}")
        self.save(item)
        return policy

    def recent(self, keep):
        result = []
        for row in self.store.db.execute(
            "SELECT id FROM life_activities WHERE state='finished' "
            "AND json_extract(data,'$.archive_state')='ready' ORDER BY created DESC LIMIT ?",
            (keep,),
        ):
            item = self.recall(row[0])
            if "summary" in item:
                result.append(
                    {
                        "type": "world.activity_summary",
                        "logical_activity_id": row[0],
                        **self.index_metadata(row[0]),
                        "brief": item["summary"]["brief"],
                    }
                )
        return list(reversed(result))

    def index_metadata(self, activity_id):
        """Only actual experience supplies time and App names, never an App inbox."""
        rows = self.store.db.execute(
            "SELECT e.created,e.app,json_extract(e.data,'$.tool') tool,"
            "json_extract(e.data,'$.kind') observation_kind,"
            "json_extract(e.data,'$.content.app') observed_app "
            "FROM events e JOIN activity_runs r ON e.activity_id=r.run_id "
            "WHERE r.logical_id=? AND e.kind IN (" + ",".join("?" for _ in KINDS) + ") "
            "AND COALESCE(json_extract(e.data,'$.tool'),'') != 'world_recall' ORDER BY seq",
            (activity_id, *KINDS),
        ).fetchall()
        apps = set()
        for row in rows:
            apps.add(row["app"])
            if row["tool"] and "__" in row["tool"]:
                apps.add(row["tool"].split("__", 1)[0])
            if row["observed_app"]:
                apps.add(row["observed_app"])
            if row["observation_kind"] == "admin_request":
                apps.add("qq")
        if len(apps) > 1:
            apps.discard("world")
        times = [row["created"] for row in rows]
        return {
            "time": {
                key: datetime.fromtimestamp(value).astimezone().isoformat(timespec="seconds")
                for key, value in (("start", min(times)), ("end", max(times)))
            }
            if times
            else {},
            "apps": sorted(apps),
        }

    def import_legacy(self):
        """Idempotent index, preserving old run boundaries and the original Session."""
        rows = self.store.db.execute(
            "SELECT e.activity_id,min(e.created) created FROM events e "
            "LEFT JOIN activity_runs r ON e.activity_id=r.run_id "
            "WHERE e.activity_id IS NOT NULL AND r.run_id IS NULL "
            "GROUP BY e.activity_id ORDER BY min(e.created)"
        ).fetchall()
        imported = []
        # A legacy archive is explicitly a batch of old execution records,
        # never presented as a reconstructed human activity.
        for offset in range(0, len(rows), 4):
            batch = rows[offset : offset + 4]
            item = {
                "id": uuid.uuid4().hex,
                "created": batch[0]["created"],
                "state": "finished",
                "legacy": True,
                "archive_state": "pending",
                "boundary": "旧环境运行记录；保留原 execution_ids，不推断生活活动边界",
                "execution_ids": [r["activity_id"] for r in batch],
            }
            with self.store.db:
                self.save(item)
                self.store.db.executemany(
                    "INSERT INTO activity_runs VALUES(?,?)",
                    [(r["activity_id"], item["id"]) for r in batch],
                )
            self.archive(item["id"])
            imported.append(item["id"])
        return imported

    def legacy_ids(self):
        return [
            r[0]
            for r in self.store.db.execute(
                "SELECT id FROM life_activities WHERE json_extract(data,'$.legacy')=1 "
                "ORDER BY created"
            )
        ]

    def archive(self, activity_id):
        item = self.get(activity_id)
        if item["state"] != "finished":
            raise ValueError("Only finished activities can leave visible history")
        rows = self.store.db.execute(
            "SELECT e.* FROM events e JOIN activity_runs r ON e.activity_id=r.run_id "
            "WHERE r.logical_id=? AND e.kind IN (" + ",".join("?" for _ in KINDS) + ") "
            "AND COALESCE(json_extract(e.data,'$.tool'),'') != 'world_recall' ORDER BY seq",
            (activity_id, *KINDS),
        ).fetchall()
        events = [{**dict(row), "data": json.loads(row["data"])} for row in rows]
        directory = self.root / "activities" / activity_id
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        raw = "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in events)
        temporary = directory / "events.jsonl.tmp"
        temporary.write_text(raw, encoding="utf-8")
        temporary.chmod(0o600)
        with temporary.open("rb") as saved:
            os.fsync(saved.fileno())
        temporary.replace(directory / "events.jsonl")
        atomic_json(
            directory / "manifest.json",
            {
                "version": 1,
                "activity_id": activity_id,
                "event_count": len(events),
                "sha256": hashlib.sha256(raw.encode()).hexdigest(),
                "scope": "observed experience",
            },
        )
        path = directory / "summary.json"
        policy = self.summary_policy(activity_id)
        groups, seen = {}, {}
        projections = self.store.get("tool_result_projections", {})
        for event in events:
            source = {**event, "data": dict(event["data"])}
            data = source["data"]
            if event["kind"] == "model.finished" and "message" in data:
                # Preserve the original output, without repeated SSE/accounting data.
                data = source["data"] = {"message": data["message"]}
            field = (
                "result"
                if event["kind"] == "decision.result"
                else (
                    "content"
                    if event["kind"] == "observation.created" and data.get("kind") == "tool"
                    else None
                )
            )
            if field and field in data:
                content = data[field]
                if field == "content" and isinstance(content, dict) and "tool" in content:
                    content = content.get("result", content)
                if isinstance(content, dict) and content.get("operation_id") in projections:
                    # Reuse the same validated view supplied to the subject. The
                    # immutable archive above keeps the original result in full.
                    content = {**content, "data": projections[content["operation_id"]]}
                    data[field] = content
                digest = hashlib.sha256(json.dumps(content, sort_keys=True).encode()).hexdigest()
                digest = (event["activity_id"], digest)
                if digest in seen:
                    data[field] = {"same_content_as_event_seq": seen[digest]}
                else:
                    seen[digest] = event["seq"]
            groups.setdefault(event["activity_id"], []).append(source)
        cached = json.loads(path.read_text()) if path.exists() else None
        if cached is not None:
            self.validate_summary(cached, events)
            if policy["kind"] != "empty" and item.get("archive_state") != "ready":
                item = self.get(activity_id)
                item.update(archive_state="ready", archive_ref=f"activity:{activity_id}")
                self.save(item)
        return {
            "events": events,
            "source_groups": list(groups.values()),
            "reference": f"activity:{activity_id}",
            "summary": cached,
            "summary_policy": policy,
        }

    @staticmethod
    def validate_summary(summary, events):
        refs = {e["seq"] for e in events}
        if (
            set(summary) not in ({"brief", "references"}, {"brief", "references", "version"})
            or (
                "version" in summary
                and (type(summary["version"]) is not int or summary["version"] != 2)
            )
            or not isinstance(summary["brief"], str)
            or not summary["brief"].strip()
            or not isinstance(summary["references"], list)
            or not summary["references"]
            or any(type(ref) is not int or ref not in refs for ref in summary["references"])
        ):
            raise ValueError("Invalid activity summary or unobserved source")

    def index(self, activity_id):
        """Archive first, reuse existing indexes, otherwise build local facts only."""
        from astcho_world.activity_index import factual_index

        archive = self.archive(activity_id)
        if archive["summary_policy"]["kind"] == "empty":
            return {"empty": True}
        if archive["summary"] is not None:
            return archive["summary"]
        summary = factual_index(
            archive["events"], legacy=self.get(activity_id).get("legacy", False)
        )
        return self.summary(activity_id, summary)

    def summary(self, activity_id, summary):
        archive = self.archive(activity_id)
        self.validate_summary(summary, archive["events"])
        atomic_json(self.root / "activities" / activity_id / "summary.json", summary)
        item = self.get(activity_id)
        item.update(
            archive_state="empty" if archive["summary_policy"]["kind"] == "empty" else "ready",
            archive_ref=archive["reference"],
        )
        self.save(item)
        return summary

    def recall(self, activity_id, *, full=False):
        item = self.get(activity_id)
        directory = self.root / "activities" / activity_id
        result = {"activity": item}
        if (directory / "summary.json").exists():
            result["summary"] = json.loads((directory / "summary.json").read_text())
        if full:
            manifest = json.loads((directory / "manifest.json").read_text())
            raw = (directory / "events.jsonl").read_bytes()
            if hashlib.sha256(raw).hexdigest() != manifest["sha256"]:
                raise ValueError("Archive checksum mismatch; original World events retained")
            result["events"] = [json.loads(line) for line in raw.splitlines()]
        return result
