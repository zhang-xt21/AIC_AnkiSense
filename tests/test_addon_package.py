"""Package/install contracts in temporary directories, never a user Anki profile."""

import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import zipfile

import genanki
import pytest

from addons.ankisense_sync.collection_sync import BOOTSTRAP_GUID, BOOTSTRAP_NOTE_ID, FIELD_ORDER, MODEL_ID
from ankisense.apkg import BACK, CSS, FRONT, MODEL_NAME
from tools import build_anki_addon as builder


@pytest.fixture
def project(tmp_path, monkeypatch):
    """Copy only release inputs, so bootstrap generation cannot alter the repo."""
    root = tmp_path / "project"
    source = root / "addons" / builder.PACKAGE
    source.mkdir(parents=True)
    for name in builder.FILES:
        shutil.copyfile(builder.SOURCE / name, source / name)
    examples = root / "examples"
    examples.mkdir()
    shutil.copyfile(builder.ROOT / "examples" / "anki_plugin_client.py", examples / "anki_plugin_client.py")
    monkeypatch.setattr(builder, "ROOT", root)
    monkeypatch.setattr(builder, "SOURCE", source)
    # The builder prepends its source root; restore sys.path after each test.
    monkeypatch.setattr(sys, "path", list(sys.path))
    return root, source


def _link(link, target, *, directory=False):
    try:
        link.symlink_to(target, target_is_directory=directory)
    except OSError:
        if directory and os.name == "nt":
            # Directory junctions need no symlink privilege on Windows. Both
            # paths are children of pytest's private temporary directory.
            result = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)],
                                    capture_output=True, check=False)
            if result.returncode == 0:
                assert link.is_junction()
                return
        pytest.skip("This host does not permit creating the required test link")


def test_archive_has_exact_allowlist_and_no_connection_or_private_data(project):
    root, source = project
    marker = "TEST_ONLY_PRIVATE_CONNECTION_SECRET_7b17f479"
    (source / ".env").write_text("AI_API_KEY=" + marker, encoding="utf-8")
    (source / "plugin-connection.json").write_text(json.dumps({"token": marker}), encoding="utf-8")
    (source / "user_files").mkdir()
    (source / "user_files" / "private.txt").write_text(marker, encoding="utf-8")
    (source / "__pycache__").mkdir()
    (source / "__pycache__" / "private.pyc").write_bytes(marker.encode())
    output = builder.build(root / "dist" / "AnkiSense-Sync.ankiaddon")
    with zipfile.ZipFile(output) as archive:
        assert len(archive.namelist()) == len(builder.FILES)
        assert set(archive.namelist()) == set(builder.FILES)
        assert all("/" not in name and "\\" not in name and ".." not in name for name in archive.namelist())
        assert all(marker.encode() not in archive.read(name) for name in archive.namelist())
        manifest = json.loads(archive.read("manifest.json"))
        assert manifest["package"] == "ankisense_sync"
        assert "AnkiSense" in manifest["name"]
        assert manifest["human_version"]
        assert manifest["conflicts"] == []
        assert json.loads(archive.read("config.json")) == {"connection_file": ""}
        assert archive.read("client.py") == (root / "examples" / "anki_plugin_client.py").read_bytes()


def test_bootstrap_is_single_placeholder_in_standard_apkg_with_stable_identity(project, tmp_path):
    _, _source = project
    package = builder.build_bootstrap()
    with zipfile.ZipFile(package) as archive:
        assert set(archive.namelist()) == {"collection.anki2", "media"}
        assert json.loads(archive.read("media")) == {}
        database = tmp_path / "bootstrap-readonly.sqlite3"
        database.write_bytes(archive.read("collection.anki2"))
    with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as connection:
        notes = connection.execute("SELECT guid, mid, flds FROM notes").fetchall()
        assert len(notes) == 1
        guid, model_id, joined_fields = notes[0]
        assert guid == BOOTSTRAP_GUID == genanki.guid_for("AnkiSense-bootstrap-v1")
        assert model_id == MODEL_ID == 1786831001
        fields = joined_fields.split("\x1f")
        assert len(fields) == len(FIELD_ORDER) == 8
        assert fields[0] == BOOTSTRAP_NOTE_ID == "__ankisense-bootstrap-v1__"
        assert fields[1] == "AnkiSense 模板初始化"
        assert fields[2:] == [""] * 6
        assert connection.execute("SELECT count(*) FROM cards").fetchone()[0] == 1
        assert connection.execute("SELECT did FROM cards").fetchone()[0] == 1
        models, decks = connection.execute("SELECT models, decks FROM col").fetchone()
        models, decks = json.loads(models), json.loads(decks)
        assert set(models) == {str(MODEL_ID)}
        model = models[str(MODEL_ID)]
        assert model["name"] == MODEL_NAME
        assert model["type"] == 0
        assert model["sortf"] == 1
        assert [field["name"] for field in model["flds"]] == FIELD_ORDER
        assert len(model["tmpls"]) == 1
        assert model["tmpls"][0]["ord"] == 0
        assert model["tmpls"][0]["qfmt"] == FRONT
        assert model["tmpls"][0]["afmt"] == BACK
        assert model["css"] == CSS
        assert decks["1"]["name"] == "Default"


@pytest.mark.parametrize("problem", ["missing-file", "wrong-package", "client-drift"])
def test_build_rejects_incomplete_or_mismatched_release_inputs(project, problem):
    root, source = project
    if problem == "missing-file":
        (source / "ui.py").unlink()
    elif problem == "wrong-package":
        manifest = json.loads((source / "manifest.json").read_text(encoding="utf-8"))
        manifest["package"] = "different_addon"
        (source / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    else:
        with (source / "client.py").open("a", encoding="utf-8") as stream:
            stream.write("\n# unexpected local modification\n")
    output = root / "dist" / "invalid.ankiaddon"
    with pytest.raises(ValueError):
        builder.build(output)
    assert not output.exists()


def test_install_preserves_anki_metadata_user_files_and_old_release_backup(project, tmp_path):
    root, source = project
    addons = tmp_path / "isolated-anki" / "addons21"
    target = addons / builder.PACKAGE
    target.mkdir(parents=True)
    metadata = b'{"disabled": false, "config": {"personal_setting": 42}}'
    (target / "meta.json").write_bytes(metadata)
    (target / "user_files").mkdir()
    (target / "user_files" / "user-content.bin").write_bytes(b"keep my original bytes")
    (target / "ui.py").write_bytes(b"old release UI")
    connection = tmp_path / "local-connection.json"
    secret = "TEST_ONLY_INSTALL_TOKEN_23049bdce7"
    connection.write_text(json.dumps({"token": secret}), encoding="utf-8")

    installed = builder.install(addons, connection)
    assert installed == target.resolve()
    assert (target / "meta.json").read_bytes() == metadata
    assert (target / "user_files" / "user-content.bin").read_bytes() == b"keep my original bytes"
    for name in builder.FILES:
        if name != "config.json":
            assert (target / name).read_bytes() == (source / name).read_bytes()
        assert secret.encode() not in (target / name).read_bytes()
    config = json.loads((target / "config.json").read_text(encoding="utf-8"))
    assert config == {"connection_file": str(connection.resolve())}
    assert not (target / connection.name).exists()
    backups = list((root / "dist" / "installation-backups").glob("*.zip"))
    assert len(backups) == 1
    with zipfile.ZipFile(backups[0]) as backup:
        assert backup.read("ui.py") == b"old release UI"
        assert "meta.json" not in backup.namelist()
        assert not any(name.startswith("user_files") for name in backup.namelist())
    assert not list(target.glob(".*.installing"))


def test_install_requires_explicit_addons21_and_existing_connection_file(project, tmp_path):
    with pytest.raises(ValueError):
        builder.install(tmp_path / "wrong-folder")
    assert not (tmp_path / "wrong-folder").exists()
    with pytest.raises(ValueError):
        builder.install(tmp_path / "addons21", tmp_path / "missing-connection.json")
    assert not (tmp_path / "addons21").exists()


@pytest.mark.parametrize("name", ["ui.py", "bootstrap.apkg"])
def test_source_file_symlink_is_rejected_before_build_can_read_or_overwrite_it(project, tmp_path, name):
    root, source = project
    sentinel = tmp_path / "private-original.txt"
    sentinel.write_bytes(b"private data must not enter the archive or be overwritten")
    (source / name).unlink()
    _link(source / name, sentinel)
    output = root / "dist" / "blocked.ankiaddon"
    with pytest.raises(ValueError):
        builder.build(output)
    assert sentinel.read_bytes() == b"private data must not enter the archive or be overwritten"
    assert not output.exists()


def test_output_file_symlink_is_rejected_without_overwriting_target(project, tmp_path):
    _, _source = project
    sentinel = tmp_path / "original.txt"
    sentinel.write_bytes(b"untouched")
    output = tmp_path / "linked.ankiaddon"
    _link(output, sentinel)
    with pytest.raises(ValueError):
        builder.build(output)
    assert sentinel.read_bytes() == b"untouched"


@pytest.mark.parametrize("where", ["addons-dir", "package-dir", "parent-dir"])
def test_install_rejects_directory_aliases_including_windows_junctions(project, tmp_path, where):
    outside = tmp_path / "other-data"
    outside.mkdir()
    (outside / "sentinel.txt").write_bytes(b"untouched")
    if where == "addons-dir":
        actual = outside / "addons21"
        actual.mkdir()
        addons = tmp_path / "addons21"
        _link(addons, actual, directory=True)
    elif where == "package-dir":
        addons = tmp_path / "addons21"
        addons.mkdir()
        # Same-parent alias is significant: merely checking resolved.parent
        # cannot distinguish this from the intended package directory.
        actual = addons / "another_addon"
        actual.mkdir()
        (actual / "sentinel.txt").write_bytes(b"untouched")
        _link(addons / builder.PACKAGE, actual, directory=True)
    else:
        actual = outside / "addons21"
        actual.mkdir()
        alias = tmp_path / "parent-alias"
        _link(alias, outside, directory=True)
        addons = alias / "addons21"
    with pytest.raises(ValueError):
        builder.install(addons)
    assert (outside / "sentinel.txt").read_bytes() == b"untouched"
    assert not (actual / "ui.py").exists()
    assert not (actual / builder.PACKAGE).exists()


def test_existing_installed_code_symlink_is_rejected_before_any_overwrite(project, tmp_path):
    addons = tmp_path / "addons21"
    target = addons / builder.PACKAGE
    target.mkdir(parents=True)
    (target / "__init__.py").write_bytes(b"old entry point")
    sentinel = tmp_path / "outside-ui.py"
    sentinel.write_bytes(b"outside code")
    _link(target / "ui.py", sentinel)
    with pytest.raises(ValueError):
        builder.install(addons)
    assert (target / "__init__.py").read_bytes() == b"old entry point"
    assert sentinel.read_bytes() == b"outside code"


def test_legacy_predictable_temporary_symlink_is_never_followed_or_removed(project, tmp_path):
    _, source = project
    addons = tmp_path / "addons21"
    target = addons / builder.PACKAGE
    target.mkdir(parents=True)
    sentinel = tmp_path / "outside-original.txt"
    sentinel.write_bytes(b"must remain untouched")
    legacy = target / "__init__.py.installing"
    _link(legacy, sentinel)
    builder.install(addons)
    assert sentinel.read_bytes() == b"must remain untouched"
    assert legacy.is_symlink()
    assert (target / "__init__.py").read_bytes() == (source / "__init__.py").read_bytes()
    assert not list(target.glob(".*.installing"))
