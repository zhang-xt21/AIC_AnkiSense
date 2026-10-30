"""Prepare the project Anki profile; never run MSI or open any collection."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from ankisense.paths import resolve_data_dir
from ankisense.plugin_api import PluginConnection
from tools.build import build_anki_addon as builder


def prepare(runtime: Path | None = None) -> dict:
    origin = builder._check_ordinary(ROOT / "origin")
    addons = builder._check_ordinary(origin / "AnkiData" / "addons21")
    runtime_file = builder._check_ordinary(origin / "anki-runtime.json")
    if runtime is not None:
        runtime = runtime.resolve()
        if not (runtime / "anki.exe").is_file():
            raise ValueError("The selected Anki program directory does not contain anki.exe.")
    # A manually disabled connection stays disabled; setup never rotates tokens.
    connection = PluginConnection(resolve_data_dir())
    builder._check_ordinary(connection.path)
    if connection.path.exists():
        if connection.read() is None:
            raise ValueError("Existing plugin connection is invalid; preserve it and repair it before setup.")
    else:
        connection.configure()
    # The checked-in bootstrap is used as-is: setup must not rewrite source files.
    installed = builder.install(addons, connection.path)
    if runtime is not None:
        try:
            program = str(runtime.relative_to(origin.resolve()))
        except ValueError:
            program = str(runtime)
        origin.mkdir(parents=True, exist_ok=True)
        builder._atomic_write(runtime_file, json.dumps({"schema_version": 1, "anki_root": program}, ensure_ascii=False, indent=2).encode("utf-8"))
    return {
        "plugin_installed": True,
        "plugin_path": str(installed),
        "connection_enabled": connection.public()["enabled"],
        "runtime_configured": runtime is not None or runtime_file.is_file(),
        "collections_opened": 0,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime", type=Path, help="Existing official Anki program directory")
    args = parser.parse_args()
    print(json.dumps(prepare(args.runtime), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
