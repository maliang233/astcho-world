from __future__ import annotations

import fcntl
import json
import sqlite3
import time
import uuid
from contextvars import ContextVar
from pathlib import Path


class Store:
    """Environment metadata only; application records live in application databases."""

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.owner = path.with_suffix(".lock").open("a")
        try:
            fcntl.flock(self.owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.owner.close()
            raise RuntimeError("This environment already has an owner") from None
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.activity = ContextVar("activity_id", default=None)
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS events (
                seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE, created REAL,
                app TEXT, kind TEXT, activity_id TEXT, operation_id TEXT,
                summary TEXT, data TEXT);
            CREATE INDEX IF NOT EXISTS events_activity ON events(activity_id, seq);
            CREATE INDEX IF NOT EXISTS events_app ON events(app, seq);
            CREATE TABLE IF NOT EXISTS operations (
                id TEXT PRIMARY KEY, app TEXT, tool TEXT, request TEXT, effect TEXT,
                status TEXT, result TEXT);
            CREATE TABLE IF NOT EXISTS notifications (
                id TEXT PRIMARY KEY, app TEXT, source TEXT, message TEXT, locator TEXT,
                state TEXT DEFAULT 'pending', created REAL);
            CREATE TABLE IF NOT EXISTS observations (
                id INTEGER PRIMARY KEY, kind TEXT, reference TEXT, content TEXT, created REAL,
                UNIQUE(kind, reference));
            CREATE TABLE IF NOT EXISTS reminders (
                id TEXT PRIMARY KEY, due REAL, text TEXT, fired INTEGER DEFAULT 0);
            CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE IF NOT EXISTS context_fragments (hash TEXT PRIMARY KEY, content TEXT);
            CREATE TABLE IF NOT EXISTS admin_requests (
                id TEXT PRIMARY KEY, created REAL, sender TEXT, message TEXT,
                state TEXT DEFAULT 'pending', result TEXT);
        """)
        from astcho_world.usage import UsageLedger

        self.usage = UsageLedger(self.db)
        self.db.execute("UPDATE usage_calls SET status='interrupted' WHERE status='running'")
        # A process died mid-call: never assume an external write did not happen.
        self.db.execute("""
            UPDATE operations SET status = CASE effect WHEN 'write' THEN 'unknown' ELSE 'failed' END
            WHERE status='running'
        """)
        self.db.commit()
        with self.db:
            self.db.execute("UPDATE admin_requests SET state='interrupted' WHERE state='running'")

    def enqueue_admin(self, request):
        with self.db:
            cursor = self.db.execute(
                "INSERT OR IGNORE INTO admin_requests(id,created,sender,message) VALUES(?,?,?,?)",
                (
                    request["id"],
                    time.time(),
                    request["sender_id"],
                    json.dumps(request, ensure_ascii=False),
                ),
            )
        return bool(cursor.rowcount)

    def take_admin(self, admins):
        with self.db:
            for row in self.db.execute(
                "SELECT * FROM admin_requests WHERE state='pending' ORDER BY created,rowid"
            ).fetchall():
                if row["sender"] not in admins:
                    self.db.execute(
                        "UPDATE admin_requests SET state='revoked' WHERE id=?", (row["id"],)
                    )
                    continue
                self.db.execute(
                    "UPDATE admin_requests SET state='running' WHERE id=?", (row["id"],)
                )
                return json.loads(row["message"])
        return None

    def settle_admin(self, request_id, state, result):
        with self.db:
            self.db.execute(
                "UPDATE admin_requests SET state=?,result=? WHERE id=?",
                (state, json.dumps(result, ensure_ascii=False), request_id),
            )

    def event(self, kind, summary, *, app="world", operation_id=None, data=None):
        with self.db:
            cursor = self.db.execute(
                "INSERT INTO events(id,created,app,kind,activity_id,operation_id,summary,data) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (
                    uuid.uuid4().hex,
                    time.time(),
                    app,
                    kind,
                    self.activity.get(),
                    operation_id,
                    summary,
                    json.dumps(data or {}, ensure_ascii=False),
                ),
            )

        from astcho_world.usage import MODEL_EVENTS

        if kind in MODEL_EVENTS:
            import logging

            try:
                with self.db:
                    row = self.db.execute(
                        "SELECT * FROM events WHERE seq=?", (cursor.lastrowid,)
                    ).fetchone()
                    self.usage.event(dict(row))
            except Exception:
                logging.getLogger(__name__).exception("Usage projection failed; event retained")

    def observe(self, kind: str, reference: str, content: dict):
        with self.db:
            cursor = self.db.execute(
                "INSERT OR IGNORE INTO observations(kind,reference,content,created) "
                "VALUES(?,?,?,?)",
                (kind, reference, json.dumps(content, ensure_ascii=False), time.time()),
            )

            if cursor.rowcount:
                self.event(
                    "observation.created",
                    "记录了一次观察",
                    data={
                        "kind": kind,
                        "reference": reference,
                        "content": content,
                    },
                )

    def notify(self, app: str, event: dict):
        with self.db:
            cursor = self.db.execute(
                "INSERT OR IGNORE INTO notifications(id,app,source,message,locator,created) "
                "VALUES(?,?,?,?,?,?)",
                (
                    f"{app}:{event['id']}",
                    app,
                    event["source"],
                    event["message"],
                    json.dumps(event["locator"]),
                    time.time(),
                ),
            )

            if cursor.rowcount:
                self.event("notification.created", "收到新通知", app=app, data=event)

    def pending_notification_ids(self) -> set[str]:
        return {
            row[0] for row in self.db.execute("SELECT id FROM notifications WHERE state='pending'")
        }

    def notifications(self, *, include_delivered=False, limit=100) -> list[dict]:
        where = "state IN ('pending','delivered')" if include_delivered else "state='pending'"
        rows = self.db.execute(
            "SELECT * FROM notifications WHERE " + where
            + " ORDER BY (state='pending') DESC,created DESC,id DESC LIMIT ?",
            (min(max(limit, 1), 100),),
        ).fetchall()
        result = []
        with self.db:
            for row in rows:
                item = {key: row[key] for key in ("id", "app", "source", "message")}
                result.append(item)
                if row["state"] == "pending":
                    self.db.execute(
                        "UPDATE notifications SET state='delivered' WHERE id=?", (row["id"],)
                    )
                    self.observe("notification", row["id"], item)
                    self.event(
                        "notification.delivered", "通知已送达星回", app=row["app"], data=item
                    )
        return result

    def locator(self, notification_id: str, app: str) -> dict:
        row = self.db.execute(
            "SELECT locator FROM notifications WHERE id=? AND app=? AND state='delivered'",
            (notification_id, app),
        ).fetchone()
        if row is None:
            raise ValueError("Notification must be delivered before opening")
        return json.loads(row[0])

    def ignore(self, notification_id: str):
        with self.db:
            cursor = self.db.execute(
                "UPDATE notifications SET state='ignored' WHERE id=? "
                "AND state IN ('delivered','ignored')",
                (notification_id,),
            )
            if not cursor.rowcount:
                raise ValueError("Notification not delivered or already ignored")

            self.event(
                "notification.ignored",
                "星回忽略了通知",
                app=notification_id.split(":", 1)[0],
                data={"id": notification_id},
            )

    def remind(self, due: float, text: str, *, reminder_id: str | None = None) -> str:
        if reminder_id:
            old = self.db.execute(
                "SELECT due,text FROM reminders WHERE id=?", (reminder_id,)
            ).fetchone()
            if old:
                if tuple(old) != (due, text):
                    raise ValueError("Reminder ID reused")
                return reminder_id
        if due < time.time() or not text.strip():
            raise ValueError("Reminder needs a future due time and text")
        reminder_id = reminder_id or uuid.uuid4().hex
        with self.db:
            self.db.execute(
                "INSERT INTO reminders(id,due,text) VALUES(?,?,?)", (reminder_id, due, text)
            )
        self.event(
            "reminder.created", "设置了稍后提醒", data={"id": reminder_id, "due": due, "text": text}
        )
        return reminder_id

    def tick(self, now: float | None = None):
        if self.get("reminder_backend") == "dsh":
            return
        now = time.time() if now is None else now
        with self.db:
            for row in self.db.execute(
                "SELECT * FROM reminders WHERE fired=0 AND due<=?", (now,)
            ).fetchall():
                self.notify(
                    "world",
                    {
                        "id": row["id"],
                        "source": "自己设置的提醒",
                        "message": row["text"],
                        "locator": {},
                    },
                )
                self.db.execute("UPDATE reminders SET fired=1 WHERE id=?", (row["id"],))

    def get(self, key: str, default=None):
        row = self.db.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
        return default if row is None else json.loads(row[0])

    def set(self, key: str, value):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO state VALUES(?,?)", (key, json.dumps(value)))

    def close(self):
        self.db.close()
        fcntl.flock(self.owner, fcntl.LOCK_UN)
        self.owner.close()
