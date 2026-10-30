import json
import sqlite3
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, call

from fastapi.testclient import TestClient

from astcho.services.emotion import EmotionService, Mood
from astcho.services.schedule import ScheduleService
from astcho_world.apps.qq import QQ
from astcho_world.dashboard import create_dashboard_app

TOKEN = "synthetic-life-dashboard-token"
EXAMPLES = Path(__file__).resolve().parents[1] / "examples/apps.json"


class FixedTime(datetime):
    @classmethod
    def now(cls, tz=None):
        return cls(2026, 10, 28, 8, 30, tzinfo=tz)


def test_dashboard_shows_original_schedule_without_creating_observations(tmp_path, monkeypatch):
    monkeypatch.setattr("astcho_world.life.datetime", FixedTime)
    monkeypatch.setattr("astcho.services.schedule.datetime", FixedTime)
    monkeypatch.setenv("ASTCHO_MEMORY_ENABLED", "false")
    profile = tmp_path / "profile.json"
    profile.write_text(json.dumps({"name": "星回", "private_field": "do-not-show"}))
    monkeypatch.setenv("ASTCHO_PROFILE_PATH", str(profile))
    path = tmp_path / "schedule.json"
    path.write_text(
        json.dumps(
            {
                "weekly_rules": {"Wednesday": "work"},
                "yearly_specials": {
                    "10-28": {"routine_id": "special", "mood_append": "特别的日子"}
                },
                "routines": {
                    "special": [
                        {
                            "start": "00:00",
                            "end": "09:00",
                            "talk_value": 20,
                            "mood_prompt": "早晨状态",
                        },
                        {"start": "09:00", "end": "23:59", "talk_value": 60, "mood": "白天状态"},
                    ]
                },
            }
        )
    )
    app = create_dashboard_app(
        config_path=EXAMPLES, data_dir=tmp_path / "world", token=TOKEN, schedule_path=path
    )
    with TestClient(app) as client:
        assert client.post("/api/session", json={"token": TOKEN}).status_code == 200
        before = client.portal.call(lambda: list(app.state.kernel.store.db.iterdump()))
        data = client.get("/api/snapshot").json()
        state = data["life"]
        assert state["schedule"]["current"] == asdict(ScheduleService(path).current())
        assert state["schedule"]["current"]["routine"] == "special"
        assert state["schedule"]["today"]["blocks"][0]["active"] is True
        assert state["profile"]["name"] == "星回"
        assert "do-not-show" not in json.dumps(data)
        assert state["cognition"]["initialized"] is False
        assert state["cognition"]["memory_count"] is None
        assert state["cognition"]["moods"] == []
        assert state["cognition"]["memory_enabled"] is False
        app.state.life.schedule.set_override(7, "临时状态", routine="temporary", minutes=5)
        overridden = client.get("/api/snapshot").json()["life"]["schedule"]
        assert overridden["current"]["overridden"] is True
        assert overridden["current"]["mood"] == "临时状态"
        assert client.portal.call(lambda: list(app.state.kernel.store.db.iterdump())) == before
        assert all(a["state"] == "ready" for a in data["apps"])


def test_demo_does_not_read_real_persona_or_schedule(tmp_path, monkeypatch):
    secret = tmp_path / "private.json"
    secret.write_text(json.dumps({"name": "never-expose-this"}))
    monkeypatch.setenv("ASTCHO_PROFILE_PATH", str(secret))
    monkeypatch.setenv("ASTCHO_SCHEDULE_PATH", str(secret))
    app = create_dashboard_app(
        config_path=EXAMPLES, data_dir=tmp_path / "demo", token=TOKEN, demo=True
    )
    with TestClient(app) as client:
        client.post("/api/session", json={"token": TOKEN})
        life = client.get("/api/snapshot").json()["life"]
        assert life["profile"]["configured"] is False
        assert life["schedule"]["configured"] is False
        assert "never-expose-this" not in json.dumps(life)


def test_stale_cognition_report_is_not_presented_as_live_state(tmp_path):
    folder = tmp_path / "apps/qq"
    folder.mkdir(parents=True)
    (folder / "dashboard-state.json").write_text(
        json.dumps(
            {
                "updated": 1234,
                "data": {
                    "cognition": {
                        "initialized": True,
                        "memory_count": 17,
                        "moods": [{"group_id": "a", "excitement": 0.9}],
                    }
                },
            }
        )
    )
    with TestClient(
        create_dashboard_app(config_path=EXAMPLES, data_dir=tmp_path, token=TOKEN)
    ) as client:
        client.post("/api/session", json={"token": TOKEN})
        cognition = client.get("/api/snapshot").json()["life"]["cognition"]
        assert cognition["initialized"] is False
        assert cognition["memory_count"] is None
        assert cognition["moods"] == []
        assert cognition["last_report"] == 1234
    with sqlite3.connect(tmp_path / "world.sqlite3") as db:
        assert db.execute("SELECT count(*) FROM observations").fetchone()[0] == 0


def test_cognition_projection_does_not_decay_emotion_or_read_relationships(tmp_path):
    store = Mock()
    emotion = EmotionService(store)
    emotion._moods["synthetic-group"] = Mood(excitement=0.7, shyness=0.2, updated_at=1)
    before = asdict(emotion._moods["synthetic-group"])
    vectors = Mock()
    vectors.memory_count.return_value = 17
    app = QQ(tmp_path, {})
    app.conversation = SimpleNamespace(
        runtime=SimpleNamespace(
            emotion=emotion,
            vectors=vectors,
            memory=SimpleNamespace(enabled=True),
            settings=SimpleNamespace(expression_learning=False),
        )
    )
    report = app.dashboard_state()["cognition"]
    assert report["memory_count"] == 17
    assert report["moods"] == [{"group_id": "synthetic-group", **before}]
    assert asdict(emotion._moods["synthetic-group"]) == before
    assert store.mock_calls == []
    assert vectors.mock_calls == [call.memory_count()]
    app.db.close()
