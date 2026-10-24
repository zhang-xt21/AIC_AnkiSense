"""Build/install the local add-on without touching any Anki collection."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sys
from uuid import uuid4
import zipfile

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "addons" / "ankisense_sync"
PACKAGE = "ankisense_sync"
FILES = ["__init__.py", "ui.py", "onboarding.py", "answer_ui.py", "answer_journal.py", "client.py", "transport.py", "collection_sync.py",
         "manifest.json", "config.json", "config.md", "bootstrap.apkg"]


def _check_ordinary(path):
    path = Path(path).absolute()
    if any(item.is_symlink() or item.is_junction() for item in (path, *path.parents)):
        raise ValueError("构建和安装路径不能包含符号链接或目录联接。")
    return path


def _atomic_write(path, content):
    temporary = path.with_name("." + path.name + "." + uuid4().hex + ".installing")
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def build_bootstrap():
    """Build a template bootstrap; adapter removes its dedicated placeholder."""
    import genanki
    sys.path.insert(0, str(ROOT))
    from ankisense.apkg import anki_model
    _check_ordinary(SOURCE / "bootstrap.apkg")
    deck = genanki.Deck(1, "Default")
    deck.add_note(genanki.Note(model=anki_model(),
                              guid=genanki.guid_for("AnkiSense-bootstrap-v1"),
                              fields=["__ankisense-bootstrap-v1__", "AnkiSense 模板初始化", "", "", "", "", "", ""]))
    output = SOURCE / "bootstrap.apkg"
    genanki.Package(deck).write_to_file(str(output))
    return output


def build(output):
    for name in FILES:
        _check_ordinary(SOURCE / name)
    build_bootstrap()
    # The example is the canonical, thoroughly tested standard-library client.
    if (SOURCE / "client.py").read_bytes() != (ROOT / "examples/anki_plugin_client.py").read_bytes():
        raise ValueError("插件客户端与已测试接口客户端不同，请先核对。")
    missing = [name for name in FILES if not (SOURCE / name).is_file()]
    if missing:
        raise ValueError("插件源文件缺失：" + ", ".join(missing))
    manifest = json.loads((SOURCE / "manifest.json").read_text(encoding="utf-8"))
    if manifest.get("package") != PACKAGE:
        raise ValueError("插件包名称不匹配。")
    output = _check_ordinary(output).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for name in FILES:
            archive.write(SOURCE / name, name)
    return output


def install(addons_dir, connection_file=None):
    addons_dir = _check_ordinary(addons_dir).resolve()
    if addons_dir.name != "addons21":
        raise ValueError("安装位置必须是指定 Anki 数据目录中的 addons21 文件夹。")
    target = addons_dir / PACKAGE
    _check_ordinary(target)
    if target.is_symlink() or target.resolve().parent != addons_dir:
        raise ValueError("插件目标不是 addons21 下的普通目录。")
    if any((target / name).is_symlink() for name in FILES):
        raise ValueError("目标插件内含符号链接，停止覆盖。")
    for name in FILES:
        _check_ordinary(SOURCE / name)
    connection = Path(connection_file).resolve() if connection_file else None
    if connection and not connection.is_file():
        raise ValueError("连接文件不存在，请先初始化本机插件连接。")
    if target.exists():
        backup_dir = ROOT / "dist" / "installation-backups"
        _check_ordinary(backup_dir)
        backup_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
        with zipfile.ZipFile(backup_dir / (PACKAGE + "-" + stamp + ".zip"), "w", zipfile.ZIP_DEFLATED) as archive:
            for name in FILES:
                previous = target / name
                if previous.is_file():
                    archive.write(previous, name)
    target.mkdir(parents=True, exist_ok=True)
    for name in FILES:
        destination = target / name
        _check_ordinary(destination)
        _atomic_write(destination, (SOURCE / name).read_bytes())
    if connection:
        # Store only a path. Never bundle/copy/log the local credential itself.
        config = json.loads((target / "config.json").read_text(encoding="utf-8"))
        config["connection_file"] = str(connection)
        _atomic_write(target / "config.json", json.dumps(config, ensure_ascii=False, indent=2).encode("utf-8"))
    return target


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "dist" / "AnkiSense-Sync.ankiaddon")
    parser.add_argument("--install-to", type=Path)
    parser.add_argument("--connection-file", type=Path)
    args = parser.parse_args()
    output = build(args.output)
    result = {"package": str(output), "sha256": hashlib.sha256(output.read_bytes()).hexdigest()}
    if args.install_to:
        result["installed_to"] = str(install(args.install_to, args.connection_file))
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
