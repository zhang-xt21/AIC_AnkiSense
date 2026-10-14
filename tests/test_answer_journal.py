"""Durable answer recovery without Qt, Anki, a server, or a model."""

import json
from pathlib import Path
import tempfile
import unittest

from addons.ankisense_sync import answer_journal


def context(profile, *, card_id="card-one", version=3, digest="a" * 64,
            server_id="as-" + "1" * 32):
    return {"profile_path": profile, "server_id": server_id,
            "card_id": card_id, "card_version": version, "content_hash": digest,
            "note_id": 42, "guid": "fixture-guid", "review_card_id": 43,
            "fields": [card_id, "问题？", "参考答案", "", "", "", "", ""],
            "question": "问题？", "connection_path": str(profile / "plugin-connection.json")}


def journal_file(profile):
    files = [item for item in profile.rglob("*") if item.is_file()]
    assert len(files) == 1, files
    return files[0]


class AnswerJournalTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="ankisense-answer-journal-")
        self.profile = Path(self.temporary.name)
        self.card = context(self.profile)

    def tearDown(self):
        self.temporary.cleanup()

    def test_save_load_and_clear_preserve_original_answer_and_request_identity(self):
        record = {"request_id": "request-0001", "answer": "  原始作答\n保留空格。  ",
                  "attempt_id": None, "status": None, "retry_count": 0}
        answer_journal.save(self.profile, self.card, record)
        restored = answer_journal.load(self.profile, self.card)
        for key, expected in record.items():
            self.assertEqual(restored[key], expected)
        self.assertIs(restored["stale"], False)
        raw = journal_file(self.profile).read_text(encoding="utf-8")
        self.assertNotIn("allow_cloud", raw)
        self.assertNotIn("connection_path", raw)
        self.assertNotIn("plugin-connection.json", raw)
        with self.assertRaises(ValueError):
            answer_journal.save(self.profile, self.card, {**record, "allow_cloud": True})
        self.assertEqual(answer_journal.load(self.profile, self.card)["request_id"], record["request_id"])

        accepted = {**record, "attempt_id": "attempt-0001", "status": "failed", "retry_count": 1}
        answer_journal.save(self.profile, self.card, accepted)
        restored = answer_journal.load(self.profile, self.card)
        for key, expected in accepted.items():
            self.assertEqual(restored[key], expected)
        self.assertIs(restored["stale"], False)

        answer_journal.clear(self.profile, self.card)
        self.assertIsNone(answer_journal.load(self.profile, self.card))

    def test_recovery_is_scoped_to_profile_server_card_and_synced_version(self):
        record = {"request_id": "request-0002", "answer": "回答",
                  "attempt_id": None, "status": None, "retry_count": 0}
        answer_journal.save(self.profile, self.card, record)
        for changed in (
            context(self.profile, card_id="card-two"),
            context(self.profile, server_id="as-" + "2" * 32),
        ):
            self.assertIsNone(answer_journal.load(self.profile, changed))
        for changed in (context(self.profile, version=4),
                        context(self.profile, digest="b" * 64)):
            recovered = answer_journal.load(self.profile, changed)
            self.assertIs(recovered["stale"], True)
            self.assertEqual(recovered["request_id"], record["request_id"])
            self.assertEqual(recovered["saved_card_version"], self.card["card_version"])
            self.assertEqual(recovered["saved_content_hash"], self.card["content_hash"])
        with tempfile.TemporaryDirectory(prefix="ankisense-other-profile-") as other:
            self.assertIsNone(answer_journal.load(Path(other), context(Path(other))))
        self.assertEqual(answer_journal.load(self.profile, self.card)["request_id"], record["request_id"])

    def test_rejected_draft_survives_version_change_and_can_be_replaced(self):
        rejected = {"request_id": "request-0004", "answer": "旧题的原回答",
                    "attempt_id": None, "status": "rejected", "retry_count": 0}
        answer_journal.save(self.profile, self.card, rejected)
        self.assertIs(answer_journal.load(self.profile, self.card)["stale"], False)

        updated = context(self.profile, version=4, digest="b" * 64)
        restored = answer_journal.load(self.profile, updated)
        self.assertIs(restored["stale"], True)
        self.assertEqual(restored["answer"], rejected["answer"])
        self.assertEqual(restored["status"], "rejected")
        with self.assertRaises(ValueError):
            answer_journal.save(self.profile, updated,
                                {**rejected, "attempt_id": "attempt-0004"})

        fresh = {"request_id": "request-0005", "answer": "新题的新回答",
                 "attempt_id": None, "status": None, "retry_count": 0}
        answer_journal.save(self.profile, updated, fresh)
        self.assertEqual(answer_journal.load(self.profile, updated)["answer"], fresh["answer"])
        self.assertIs(answer_journal.load(self.profile, updated)["stale"], False)
        self.assertEqual(len(list(self.profile.glob(".ankisense-answer-*.json"))), 1)

    def test_corrupt_or_old_schema_is_not_restored_as_a_submission(self):
        record = {"request_id": "request-0003", "answer": "不应误恢复",
                  "attempt_id": None, "status": None, "retry_count": 0}
        answer_journal.save(self.profile, self.card, record)
        path = journal_file(self.profile)
        original = path.read_text(encoding="utf-8")
        path.write_text("{not valid json", encoding="utf-8")
        with self.assertRaises(ValueError):
            answer_journal.load(self.profile, self.card)

        payload = json.loads(original)
        version_key = "version" if "version" in payload else "schema_version"
        self.assertIn(version_key, payload)
        payload[version_key] = 0
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        with self.assertRaises(ValueError):
            answer_journal.load(self.profile, self.card)


if __name__ == "__main__":
    unittest.main()
