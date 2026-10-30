from argparse import Namespace
from pathlib import Path

import pytest

from astcho_world.configuration import apply_paths, load_environment, require_environment


def test_env_file_loads_values_without_overriding_shell(tmp_path, monkeypatch):
    monkeypatch.delenv("ASTCHO_WORLD_MODEL", raising=False)
    monkeypatch.setenv("ASTCHO_WORLD_API_KEY", "shell-fixture")
    path = tmp_path / ".env"
    path.write_text("ASTCHO_WORLD_MODEL='fixture-model'\nASTCHO_WORLD_API_KEY='file-fixture'\n")
    load_environment(path)
    assert require_environment("ASTCHO_WORLD_MODEL")["ASTCHO_WORLD_MODEL"] == "fixture-model"
    assert require_environment("ASTCHO_WORLD_API_KEY")["ASTCHO_WORLD_API_KEY"] == "shell-fixture"


def test_missing_configuration_reports_names_only(monkeypatch):
    monkeypatch.setenv("ASTCHO_WORLD_MODEL", "")
    monkeypatch.setenv("ASTCHO_WORLD_API_KEY", "private-fixture")
    with pytest.raises(SystemExit) as exc:
        require_environment("ASTCHO_WORLD_MODEL", "ASTCHO_WORLD_API_KEY")
    assert str(exc.value) == "Missing configuration: ASTCHO_WORLD_MODEL"


def test_paths_use_migrated_env_and_preserve_explicit_options(monkeypatch):
    monkeypatch.setenv("ASTCHO_WORLD_DATA_DIR", "/tmp/world-fixture")
    monkeypatch.setenv("ASTCHO_WORLD_SKILLS_PATH", "/tmp/skills-fixture")
    monkeypatch.setenv("ASTCHO_SCHEDULE_PATH", "/tmp/schedule-fixture.json")
    args = Namespace(data_dir=None, skills=None, schedule=None)
    apply_paths(args)
    assert args.data_dir == Path("/tmp/world-fixture")
    assert args.skills == Path("/tmp/skills-fixture")
    assert args.schedule == Path("/tmp/schedule-fixture.json")
    args = Namespace(data_dir=Path("chosen"), skills=Path("chosen"), schedule=Path("chosen"))
    apply_paths(args)
    assert args.data_dir == args.skills == args.schedule == Path("chosen")


def test_qq_uses_configured_account_env_and_preserves_group_allowlist(tmp_path, monkeypatch):
    from astcho_world.apps.qq import QQ

    monkeypatch.setenv("ASTCHO_WORLD_BOT_ID", "fixture-bot")
    app = QQ(tmp_path, {"bot_id_env": "ASTCHO_WORLD_BOT_ID", "allowed_groups": ["allowed"]})
    assert app.bot_id == "fixture-bot"
    assert app.ingest({"batch_id": "ignored", "chat_id": "group:other", "messages": []}) == {
        "notifications": [],
    }
    assert app.db.execute("SELECT count(*) FROM chats").fetchone()[0] == 0
