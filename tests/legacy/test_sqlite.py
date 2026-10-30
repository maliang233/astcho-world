from astcho.storage.sqlite import SQLiteStore


def test_user_isolated_by_group_and_user(tmp_path):
    store = SQLiteStore(tmp_path / "test.sqlite3")
    store.touch_user("g1", "u1", "one")
    store.touch_user("g2", "u1", "two")
    assert store.get_user("g1", "u1")["nickname"] == "one"
    assert store.get_user("g2", "u1")["nickname"] == "two"


def test_meme_lifecycle(tmp_path):
    store = SQLiteStore(tmp_path / "test.sqlite3")
    store.upsert_meme(
        {
            "file_id": "m1",
            "url": "https://example.invalid/1.jpg",
            "description": "smile",
            "tags": ["happy"],
        }
    )
    store.mark_meme_used("m1")
    assert store.get_meme("m1")["use_count"] == 1
    assert store.delete_meme("m1")["file_id"] == "m1"


def test_meme_upsert_without_local_path_preserves_existing_file(tmp_path):
    store = SQLiteStore(tmp_path / "test.sqlite3")
    path = tmp_path / "meme.png"
    path.write_bytes(b"image")
    store.upsert_meme(
        {
            "file_id": "m1",
            "url": "https://example.invalid/1.png",
            "local_path": str(path),
            "description": "original",
            "tags": ["happy"],
        }
    )

    store.upsert_meme(
        {
            "file_id": "m1",
            "url": "https://example.invalid/1.png",
            "description": "updated",
            "tags": ["happy"],
        }
    )

    assert store.get_meme("m1")["local_path"] == str(path)
    assert store.get_meme("m1")["description"] == "updated"


def test_last_sent_meme_survives_restart_and_is_session_scoped(tmp_path):
    path = tmp_path / "test.sqlite3"
    store = SQLiteStore(path)
    for file_id in ("m1", "m2"):
        store.upsert_meme(
            {
                "file_id": file_id,
                "url": f"https://example.invalid/{file_id}",
                "description": file_id,
            }
        )
    store.mark_meme_used("m1", session_key="group-1")
    store.mark_meme_used("m2", session_key="group-2")

    restarted = SQLiteStore(path)
    assert restarted.get_last_sent_meme("group-1")["file_id"] == "m1"
    assert restarted.get_last_sent_meme("group-2")["file_id"] == "m2"
    assert restarted.get_last_sent_meme("group-3") is None

    restarted.delete_meme("m1")
    assert restarted.get_last_sent_meme("group-1") is None
    assert restarted.get_last_sent_meme("group-2")["file_id"] == "m2"


def test_existing_database_gets_last_sent_table_on_upgrade(tmp_path):
    path = tmp_path / "test.sqlite3"
    original = SQLiteStore(path)
    original.upsert_meme(
        {"file_id": "m1", "url": "https://example.invalid/1", "description": "sticker"}
    )
    with original.connection() as db:
        db.execute("DROP TABLE meme_last_sent")

    upgraded = SQLiteStore(path)
    upgraded.mark_meme_used("m1", session_key="group-1")

    assert upgraded.get_meme("m1")["use_count"] == 1
    assert upgraded.get_last_sent_meme("group-1")["file_id"] == "m1"
