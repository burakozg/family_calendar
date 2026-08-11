"""Atomic writes, default seeding, corrupt-store handling, backups."""
import json

import pytest

import main


def test_atomic_write_writes_content_and_leaves_no_tmp(tmp_path):
    p = tmp_path / "x.json"
    main._atomic_write_text(p, '{"a": 1}')
    assert json.loads(p.read_text()) == {"a": 1}
    assert list(tmp_path.glob("*.tmp")) == []


def test_atomic_write_overwrites(tmp_path):
    p = tmp_path / "x.json"
    main._atomic_write_text(p, "old")
    main._atomic_write_text(p, "new")
    assert p.read_text() == "new"


def test_read_seeds_defaults():
    settings = main.read_settings()
    assert settings["familyName"]
    assert main.F_SETTINGS.exists()


def test_corrupt_store_raises_and_logs():
    good = main.F_SETTINGS.read_text()
    try:
        main.F_SETTINGS.write_text("{broken")
        with pytest.raises(Exception):
            main.read_settings()
        entries = main.read_logs(q="could not parse settings.json")
        assert entries and entries[0]["level"] == "error"
    finally:
        main.F_SETTINGS.write_text(good)   # restore for other tests
    assert main.read_settings()["familyName"]


def test_backup_creates_zip_once_per_day():
    import zipfile
    main.read_settings()   # ensure at least one store exists
    # A backup may already exist from another test — remove today's to test creation.
    for z in main.BACKUPS.glob("*.zip"):
        z.unlink()
    made = main.make_backup()
    assert made is not None and made.exists()
    names = zipfile.ZipFile(made).namelist()
    assert "settings.json" in names
    assert main.make_backup() is None      # second call same day: no-op
    assert not list(main.BACKUPS.glob("*.tmp"))
