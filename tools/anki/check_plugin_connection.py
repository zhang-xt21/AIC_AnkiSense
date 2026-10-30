"""Read-only integration check: no answer submissions or Anki database writes."""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from examples.anki_plugin_client import PluginClient
from ankisense.paths import resolve_data_dir


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, help="Data directory; relative paths start at the project root")
    parser.add_argument("--limit", type=int, default=3)
    args = parser.parse_args()
    if not 0 <= args.limit <= 20:
        parser.error("--limit must be between 0 and 20")
    storage = resolve_data_dir(args.data_dir)
    client = PluginClient.from_connection_file(storage / "plugin-connection.json", "read-only-connection-check")
    status = client.status()
    manifest = client.catalog()
    seen = set()
    verified = 0
    for summary in manifest["cards"][:args.limit]:
        card = client.card(summary["id"], expected_hash=summary["content_hash"])
        for asset in card["media"]:
            if asset["sha256"] not in seen:
                client.media(asset["id"], asset["sha256"])
                seen.add(asset["sha256"])
        verified += 1
    print(json.dumps({"connected": True, "protocol_version": status["protocol_version"],
                      "approved_cards": len(manifest["cards"]), "excluded_cards": len(manifest["excluded"]),
                      "verified_cards": verified, "verified_images": len(seen),
                      "ai_mode": status["ai"]["mode"], "answer_submissions": 0}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
