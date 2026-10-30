"""Display-only semantic state, explicit actions, fallback assets and admin changes."""

import asyncio
import copy
import json
import sqlite3
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from astcho_world import presentation as p
from astcho_world.apps.notes import MANIFEST as NOTES
from astcho_world.apps.qq import MANIFEST as QQ
from astcho_world.contracts import Manifest
from astcho_world.dashboard import create_dashboard_app
from astcho_world.kernel import Kernel
from astcho_world.life import LifeState
from astcho_world.toolbox import WorldTools

ROOT = Path(__file__).resolve().parents[1]
TOKEN = "synthetic-portrait-access-token"
LIFE = {"schedule": {"presentation": {"base": "base.class", "configured": True}}}


@pytest.fixture
def kernel(tmp_path):
    k = Kernel(tmp_path / "world")
    k.register(NOTES)
    loop = asyncio.new_event_loop()
    k.test_loop = loop
    yield k
    loop.run_until_complete(k.close())
    loop.close()


def running(k):
    token = k.store.activity.set("run-1")
    k.store.event("activity.started", "synthetic run")
    k.store.set("runtime_status", {"phase": "thinking", "updated": time.time()})
    return token


def test_class_notes_share_art_but_preserve_semantics_and_read_only_projection(kernel):
    kernel.store.set("runtime_status", {"phase": "idle"})
    before = list(kernel.store.db.iterdump())
    s = p.snapshot(kernel, LIFE, "live")
    assert s["selected"] == "base.class"
    assert s["base"]["label"] == "上课"
    assert p.resolve_asset(kernel, "activity.notes")["asset"] == s["image"]["asset"]
    assert list(kernel.store.db.iterdump()) == before
    assert p.snapshot(kernel, LIFE, "observer")["selected"] == "base.idle"


def test_notifications_do_not_imply_opening_a_phone_and_never_mark_read(kernel):
    kernel.register(QQ)
    kernel.store.set("runtime_status", {"phase": "idle"})
    kernel.store.notify(
        "qq", {"id": "m1", "source": "synthetic", "message": "private unread", "locator": {}}
    )
    before = list(kernel.store.db.iterdump())
    assert p.snapshot(kernel, LIFE, "live")["selected"] == "base.class"
    data = p.settings(kernel, LIFE, "live")
    assert "private unread" not in json.dumps(data)
    assert list(kernel.store.db.iterdump()) == before


def test_real_note_action_holds_only_during_current_run_then_expires(kernel):
    token = running(kernel)
    try:
        result = kernel.test_loop.run_until_complete(
            kernel.call("notes", "save", {"title": "fixture", "text": "fixture text"})
        )
        assert result.status == "succeeded"
        now = time.time()
        before = list(kernel.store.db.iterdump())
        s = p.snapshot(kernel, LIFE, "live", now=now)
        assert s["selected"] == "activity.notes"
        assert s["label"] == "正在写笔记"
        assert s["activity"]["source"] == "operation.recent"
        assert s["activity"]["operation_id"] == result.operation_id
        assert s["base"]["kind"] == "base.class"
        assert list(kernel.store.db.iterdump()) == before
        assert p.snapshot(kernel, LIFE, "live", now=now + 21)["selected"] == "activity.thinking"
        kernel.store.set("runtime_status", {"phase": "idle"})
        assert p.snapshot(kernel, LIFE, "live")["selected"] == "base.class"
        # New run does not inherit the previous run's phone/notebook pose.
        kernel.store.activity.set("run-2")
        kernel.store.event("activity.started", "new run")
        kernel.store.set("runtime_status", {"phase": "thinking"})
        assert p.snapshot(kernel, LIFE, "live")["selected"] == "activity.thinking"
    finally:
        kernel.store.activity.reset(token)


def test_sleep_is_base_real_action_can_override_and_finish_restores_it(kernel):
    sleeping = {"schedule": {"presentation": {"base": "base.sleep", "configured": True}}}
    token = running(kernel)
    try:
        kernel.test_loop.run_until_complete(
            kernel.call("notes", "save", {"title": "t", "text": "x"})
        )
        assert p.snapshot(kernel, sleeping, "live")["selected"] == "activity.notes"
        kernel.store.set("runtime_status", {"phase": "idle"})
        s = p.snapshot(kernel, sleeping, "live")
        assert s["selected"] == "base.sleep" and s["image"]["asset"] == "sleep"
        kernel.store.set("runtime_status", {"phase": "offline"})
        assert p.snapshot(kernel, sleeping, "live")["selected"] == "base.idle"
    finally:
        kernel.store.activity.reset(token)


def test_new_app_declares_semantics_without_core_edit_and_not_in_model_tools(kernel):
    manifest = NOTES.model_dump()
    manifest.update(
        app_id="journal",
        dashboard={
            "version": 1,
            "name": "Journal",
            "activities": {
                "save": {
                    "kind": "activity.journal",
                    "label": "正在写日记",
                    "fallback": "activity.notes",
                }
            },
        },
    )
    kernel.register(Manifest.model_validate(manifest))
    before_tools = [t.model_dump() for t in NOTES.tools]
    discovered = asyncio.run(
        WorldTools(kernel).execute("world_tools", {"app_id": "notes"}, "discover")
    )
    assert discovered["tools"] == before_tools
    assert "activities" not in json.dumps(discovered)
    result = p.resolve_asset(kernel, "activity.journal")
    assert result["asset"] == "school" and result["fallback"] is True
    assert result["chain"] == ["activity.journal", "activity.notes"]
    token = running(kernel)
    try:
        kernel.test_loop.run_until_complete(
            kernel.call("journal", "save", {"title": "diary", "text": "fixture"})
        )
        s = p.snapshot(kernel, LIFE, "live")
        assert s["selected"] == "activity.journal" and s["label"] == "正在写日记"
    finally:
        kernel.store.activity.reset(token)


def test_failed_new_action_does_not_resurrect_older_success_pose(kernel):
    token = running(kernel)
    try:
        kernel.test_loop.run_until_complete(
            kernel.call("notes", "save", {"title": "t", "text": "x"})
        )
        bad = kernel.test_loop.run_until_complete(kernel.call("notes", "read", {"id": "missing"}))
        assert bad.status == "failed"
        assert p.snapshot(kernel, LIFE, "live")["selected"] == "activity.thinking"
    finally:
        kernel.store.activity.reset(token)


def test_settings_mapping_persists_and_detects_cycles_and_stale_writes(kernel):
    states = copy.deepcopy(p.registry(kernel))
    states["activity.notes"]["asset"] = "phone"
    saved = p.save_config(
        kernel, {"revision": 0, "states": states, "tools": {"notes__read": "activity.phone"}}
    )
    assert saved["revision"] == 1
    assert p.resolve_asset(kernel, "activity.notes")["asset"] == "phone"
    assert p.tool_signal(kernel, "notes__read")["kind"] == "activity.phone"
    with pytest.raises(ValueError, match="变化"):
        p.save_config(kernel, {"revision": 0, "states": states})
    states["base.class"]["fallback"] = "activity.notes"
    states["activity.notes"]["fallback"] = "base.class"
    with pytest.raises(ValueError, match="成环"):
        p.save_config(kernel, {"revision": 1, "states": states})
    assert p.config(kernel) == saved


def test_bad_asset_and_administrative_tool_mapping_rejected(kernel):
    kernel.register(__import__("astcho_world.apps.bilibili", fromlist=["MANIFEST"]).MANIFEST)
    states = copy.deepcopy(p.registry(kernel))
    states["activity.notes"]["asset"] = "../../secret"
    with pytest.raises(ValueError, match="图片"):
        p.save_config(kernel, {"revision": 0, "states": states})
    with pytest.raises(ValueError, match="工具"):
        p.save_config(
            kernel, {"revision": 0, "states": {}, "tools": {"bilibili__account": "activity.phone"}}
        )
    assert p.asset_path(kernel, "../world.sqlite3") is None


def test_upload_is_versioned_deduplicated_and_old_asset_kept(kernel):
    a = p.upload(kernel, (p.ASSETS / "astcho-desk.png").read_bytes())
    again = p.upload(kernel, (p.ASSETS / "astcho-desk.png").read_bytes())
    b = p.upload(kernel, (p.ASSETS / "astcho-school-v1.png").read_bytes())
    assert a == again and a != b
    assert p.asset_path(kernel, a["asset"]).is_file()
    assert p.asset_path(kernel, b["asset"]).is_file()
    assert p.config(kernel)["revision"] == 0  # Upload alone does not apply a mapping.
    with pytest.raises(ValueError, match="校验|完整|结束"):
        p.upload(kernel, (p.ASSETS / "astcho-desk.png").read_bytes()[:-10])
    with pytest.raises(ValueError, match="PNG"):
        p.upload(kernel, b'<svg onload="bad()"></svg>')


def test_missing_image_falls_back_without_losing_state_identity(kernel, monkeypatch):
    monkeypatch.setitem(p.BUILTINS, "school", ("Class", "does-not-exist.png"))
    result = p.resolve_asset(kernel, "base.class")
    assert result["asset"] == "idle" and result["fallback"]
    assert result["chain"] == ["base.class", "base.idle"]


def test_schedule_metadata_survives_time_text_changes_and_only_edits_display(kernel):
    file = kernel.root / "schedule.json"
    original = {
        "routines": {
            "day": [
                {
                    "start": "08:00",
                    "end": "12:00",
                    "talk_value": 20,
                    "mood_prompt": "original bytes unchanged",
                }
            ]
        },
        "weekly_rules": {"Thursday": "day"},
    }
    file.write_text(json.dumps(original))
    life = LifeState(schedule_path=file)
    raw = life.snapshot(kernel)["schedule"]
    assert raw["presentation_blocks"][0]["configured"] is False
    args = {
        "revision": raw["presentation_revision"],
        "blocks": [{"routine": "day", "index": 0, "base": "base.class"}],
    }
    p.save_schedule(kernel, life, args)
    updated = json.loads(file.read_text())
    block = updated["routines"]["day"][0]
    assert {k: v for k, v in block.items() if k != "presentation"} == original["routines"]["day"][0]
    assert block["presentation"]["base"] == "base.class"
    block.update(start="09:00", mood_prompt="new mood by schedule editor")
    file.write_text(json.dumps(updated))
    life.schedule.reload()
    assert life.snapshot(kernel)["schedule"]["presentation_blocks"][0]["base"] == "base.class"
    with pytest.raises(ValueError, match="变化"):
        p.save_schedule(kernel, life, args)
    assert list(kernel.root.glob("schedule.json.portrait-*.bak"))


def test_display_endpoints_auth_origin_validation_and_read_only(tmp_path):
    app = create_dashboard_app(
        config_path=ROOT / "examples/apps.json", data_dir=tmp_path, token=TOKEN, demo=True
    )
    with TestClient(app) as c:
        assert c.get("/api/presentation/assets/phone").status_code == 401
        assert c.post("/api/presentation/config", json={}).status_code == 401
        c.post("/api/session", json={"token": TOKEN})

        def dump():
            with sqlite3.connect(tmp_path / "world.sqlite3") as db:
                return list(db.iterdump())

        before = dump()
        settings = c.get("/api/settings").json()["presentation"]
        assert c.get("/api/presentation/assets/phone").status_code == 200
        assert c.get("/api/presentation/assets/invalid").status_code == 404
        assert c.get("/api/snapshot").json()["presentation"]["selected"] == "base.idle"
        assert dump() == before
        assert (
            c.post(
                "/api/presentation/assets", content=b"bad", headers={"Origin": "https://evil.test"}
            ).status_code
            == 403
        )
        assert (
            c.post(
                "/api/presentation/config", json={"revision": 0, "states": settings["states"]}
            ).status_code
            == 200
        )
        assert (
            c.post("/api/presentation/config", json={"revision": 0, "states": {}}).status_code
            == 422
        )
        assert c.post("/api/presentation/schedule", json={"blocks": []}).status_code == 422
        assert c.post("/api/presentation/assets", content=b"bad").status_code == 422
        raw = (p.ASSETS / "astcho-desk.png").read_bytes()
        reply = c.post("/api/presentation/assets", content=raw)
        assert reply.status_code == 200
        assert c.get(reply.json()["url"]).content == raw


def test_native_web_operation_uses_same_projection_without_sdk_changes(kernel):
    token = running(kernel)
    try:
        kernel.store.db.execute(
            "INSERT INTO operations VALUES('native-1','dsh','web_fetch','{}','read','running',NULL)"
        )
        kernel.store.db.commit()
        kernel.store.event(
            "decision.tool", "native web", operation_id="native-1", data={"tool": "web_fetch"}
        )
        result = p.snapshot(kernel, LIFE, "live")
        assert result["selected"] == "activity.phone"
        assert result["label"] == "正在阅读网页"
        kernel.store.db.execute("UPDATE operations SET status='succeeded' WHERE id='native-1'")
        kernel.store.db.commit()
        kernel.store.event("decision.result", "native done", operation_id="native-1")
        assert p.snapshot(kernel, LIFE, "live")["activity"]["source"] == "operation.recent"
        kernel.store.set("runtime_status", {"phase": "idle"})
        assert p.snapshot(kernel, LIFE, "live")["selected"] == "base.class"
    finally:
        kernel.store.activity.reset(token)


@pytest.mark.parametrize("malformed", [None, [], {"base": ["bad"]}, {"base": "activity.phone"}])
def test_invalid_schedule_display_metadata_falls_back_without_breaking_schedule(kernel, malformed):
    file = kernel.root / "schedule.json"
    file.write_text(
        json.dumps(
            {
                "default": [
                    {"start": "00:00", "end": "23:59", "talk_value": 50, "presentation": malformed}
                ]
            }
        )
    )
    life = LifeState(schedule_path=file)
    s = life.snapshot(kernel)["schedule"]
    assert s["presentation"]["base"] == "base.idle"
    assert not s["presentation"]["configured"]


@pytest.mark.parametrize("bad", [[], {}, False, 7])
def test_invalid_asset_type_rejected_cleanly(kernel, bad):
    states = copy.deepcopy(p.registry(kernel))
    states["activity.phone"]["asset"] = bad
    with pytest.raises(ValueError, match="图片"):
        p.save_config(kernel, {"revision": 0, "states": states})
