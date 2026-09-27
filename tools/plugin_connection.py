"""Enable/rotate a local add-on connection; never print its secret token."""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from ankisense.plugin_api import PluginConnection
from ankisense.paths import resolve_data_dir


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["init", "status", "rotate", "disable"])
    parser.add_argument("--data-dir", type=Path, help="Data directory; relative paths start at the project root")
    parser.add_argument("--base-url", help="Local AnkiSense URL, for example http://127.0.0.1:8765")
    args = parser.parse_args()
    storage = resolve_data_dir(args.data_dir)
    connection = PluginConnection(storage)
    if args.action == "status":
        result = connection.public()
    elif args.action == "disable":
        result = connection.configure(enabled=False)
    else:
        result = connection.configure(rotate=args.action == "rotate", base_url=args.base_url)
    print(json.dumps({**result, "connection_file": str(connection.path)}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
