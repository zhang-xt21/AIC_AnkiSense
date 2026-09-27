"""Portable project paths shared by the web server and local CLI tools."""

import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]


def resolve_data_dir(value: str | Path | None = None) -> Path:
    """Resolve relative storage paths against the project, never the shell cwd.

    Explicit arguments take priority over the environment, then the project's
    .env file, then ./data. Absolute paths remain valid for external storage.
    """
    if value is None:
        if "ANKISENSE_DATA_DIR" not in os.environ:
            load_dotenv(ROOT / ".env", override=False)
        value = os.getenv("ANKISENSE_DATA_DIR") or "data"
    directory = Path(value).expanduser()
    if not directory.is_absolute():
        directory = ROOT / directory
    return directory.resolve()
