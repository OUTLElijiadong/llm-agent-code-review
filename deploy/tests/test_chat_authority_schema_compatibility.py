"""Regression tests for the migration-119 MySQL trigger schema gate."""
from __future__ import annotations

import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "lib"))
from chat_authority_schema_compatibility import classify_schema, expected_triggers  # noqa: E402

MIGRATION = pathlib.Path(__file__).resolve().parents[2] / "backend/alembic/versions/119_chat_message_authority.py"
COMMON_SH = pathlib.Path(__file__).resolve().parents[1] / "lib/common.sh"


def _snapshot(expected):
    return {
        "column_count": 1,
        "triggers": [
            {"trigger_name": name, **definition}
            for name, definition in expected.items()
        ],
    }


class ChatAuthoritySchemaCompatibilityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.expected = expected_triggers(MIGRATION)

    def test_exact_mysql_trigger_bodies_match_migration(self):
        self.assertEqual(classify_schema(_snapshot(self.expected), self.expected), "complete")

    def test_mysql_schema_probe_requests_utf8mb4_results(self):
        common = COMMON_SH.read_text(encoding="utf-8")
        query = common.split("checkpoint_database_query() {", 1)[1].split("\n}", 1)[0]
        self.assertIn("--default-character-set=utf8mb4", query)

    def test_trigger_with_migration_keywords_inside_dead_branch_is_partial(self):
        snapshot = _snapshot(self.expected)
        validate = next(
            trigger for trigger in snapshot["triggers"]
            if trigger["trigger_name"] == "trg_ent_chat_authority_validate"
        )
        original = validate["action_statement"]
        validate["action_statement"] = f"BEGIN IF 1 = 0 THEN {original} END IF; END"
        self.assertIn("new.sender_type = 'human'", validate["action_statement"].lower())
        self.assertIn("locate('@小菱', new.content) > 0", validate["action_statement"].lower())
        self.assertIn("signal sqlstate '45000'", validate["action_statement"].lower())
        self.assertEqual(classify_schema(snapshot, self.expected), "partial")

    def test_missing_or_modified_trigger_fails_closed(self):
        snapshot = _snapshot(self.expected)
        snapshot["triggers"].pop()
        self.assertEqual(classify_schema(snapshot, self.expected), "partial")

    def test_unrecognized_before_insert_trigger_fails_closed(self):
        snapshot = _snapshot(self.expected)
        snapshot["triggers"].append({
            "trigger_name": "trg_unmanaged_sender_authority",
            "action_timing": "BEFORE",
            "event_manipulation": "INSERT",
            "action_statement": "SET NEW.sender_authority_rank = 4",
        })
        self.assertEqual(classify_schema(snapshot, self.expected), "partial")

        snapshot = _snapshot(self.expected)
        snapshot["triggers"][0]["action_statement"] += (
            " IF 1 = 0 THEN SIGNAL SQLSTATE '45000'; END IF;"
        )
        self.assertEqual(classify_schema(snapshot, self.expected), "partial")

    def test_absent_schema_requires_no_authority_column_or_trigger(self):
        self.assertEqual(classify_schema({"column_count": 0, "triggers": []}, self.expected), "absent")
        self.assertEqual(
            classify_schema({"column_count": 0, "triggers": _snapshot(self.expected)["triggers"]}, self.expected),
            "partial",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
