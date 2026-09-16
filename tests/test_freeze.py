"""Local scoreboard freeze policy, independent of upstream QOJ standings."""

import json
import sqlite3
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from storage import Store, score


PROBLEMS = [{"id": 10, "label": "A"}, {"id": 20, "label": "B"}]
START = 1000
DURATION = 3600
FREEZE = 600
CUTOFF = DURATION - FREEZE


class FreezeTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "vp.sqlite3"
        self.store = Store(self.path)

    def start(self, sid="alice", name="Alice", start=START, duration=DURATION, freeze=FREEZE):
        return self.store.start(sid, "contest", name, duration, now=start, freeze_seconds=freeze)

    def submit(self, run, verdict, elapsed, problem=PROBLEMS[0], **fields):
        identifier = self.store.add_submission(run, problem, "C++17", now=run["started_at"] + elapsed)
        self.store.update_submission(identifier, verdict=verdict, **fields)
        return identifier

    def board(self, elapsed, viewer="alice"):
        return self.store.scoreboard("contest", viewer, PROBLEMS, now=START + elapsed)

    def test_freeze_begins_exactly_at_cutoff_and_includes_boundary_submission(self):
        run = self.start()
        self.submit(run, "WA", CUTOFF - 1)
        before = self.board(CUTOFF - 0.001)[0]
        self.assertFalse(before["frozen"])
        self.assertEqual(before["problem_stats"]["10"]["wrong"], 1)
        self.submit(run, "AC", CUTOFF)
        at_cutoff = self.board(CUTOFF)[0]
        self.assertTrue(at_cutoff["frozen"])
        self.assertEqual(at_cutoff["freeze_at"], START + CUTOFF)
        self.assertEqual(at_cutoff["unfreeze_at"], START + DURATION)
        self.assertEqual((at_cutoff["solved"], at_cutoff["penalty"]), (0, 0))
        self.assertEqual(at_cutoff["problem_stats"]["10"], {
            "attempts": 2, "wrong": 1, "pending": 1, "solved": False, "time": None, "submissions": 2,
        })

    def test_prefreeze_submission_judged_late_remains_visible(self):
        run = self.start()
        identifier = self.submit(run, "PENDING", CUTOFF - 1)
        self.assertEqual(self.board(CUTOFF + 10)[0]["problem_stats"]["10"]["pending"], 1)
        self.store.update_submission(identifier, verdict="AC", checked_at=START + CUTOFF + 20)
        after = self.board(CUTOFF + 21)[0]
        self.assertTrue(after["frozen"])
        self.assertEqual((after["solved"], after["penalty"]), (1, (CUTOFF - 1) // 60))
        self.assertEqual(after["problem_stats"]["10"]["pending"], 0)

    def test_postfreeze_verdict_update_cannot_change_public_result(self):
        run = self.start()
        identifier = self.submit(run, "PENDING", CUTOFF + 1)
        pending = self.board(CUTOFF + 10)
        self.store.update_submission(identifier, verdict="AC", checked_at=START + CUTOFF + 20,
                                     qoj_id=87654321, qoj_url="https://qoj.ac/submission/87654321", message="secret-result-detail")
        self.assertEqual(self.board(CUTOFF + 30), pending)
        public = json.dumps(self.board(CUTOFF + 30))
        for value in ("87654321", "qoj_url", "qoj_id", "secret-result-detail", "verdict"):
            self.assertNotIn(value, public)
        self.assertEqual(self.board(CUTOFF + 30)[0]["problem_stats"]["10"]["submissions"], 1)

    def test_hidden_acceptance_does_not_suppress_later_hidden_attempts(self):
        run = self.start()
        self.submit(run, "WA", 120)
        for offset, verdict in enumerate(("AC", "CE", "WA", "AC")):
            self.submit(run, verdict, CUTOFF + offset)
        public = self.board(CUTOFF + 10)[0]
        self.assertEqual(public["problem_stats"]["10"], {
            "attempts": 5, "wrong": 1, "pending": 4, "solved": False, "time": None, "submissions": 5,
        })
        actual = score(PROBLEMS, self.store.submissions(run["id"]))
        self.assertEqual(actual["solved"], 1)
        self.assertEqual(actual["problem_stats"]["10"]["attempts"], 2)

    def test_all_postfreeze_verdicts_are_indistinguishable_including_compile_error(self):
        run = self.start()
        identifier = self.submit(run, "CE", CUTOFF + 1)
        expected = self.board(CUTOFF + 10)
        for verdict in ("AC", "WA", "TLE", "MLE", "RE", "OLE", "PE", "JE", "PENDING", "UNKNOWN", "SUBMITTING"):
            with self.subTest(verdict=verdict):
                self.store.update_submission(identifier, verdict=verdict)
                self.assertEqual(self.board(CUTOFF + 10), expected)

    def test_prefreeze_acceptance_keeps_cell_solved_and_ignores_later_attempts(self):
        run = self.start()
        self.submit(run, "AC", 120)
        self.submit(run, "WA", CUTOFF)
        self.submit(run, "AC", CUTOFF + 1)
        row = self.board(CUTOFF + 10)[0]
        self.assertEqual((row["solved"], row["penalty"]), (1, 2))
        self.assertEqual(row["problem_stats"]["10"], {
            "attempts": 1, "wrong": 0, "pending": 0, "solved": True, "time": 2, "submissions": 3,
        })

    def test_total_record_count_stays_constant_when_hidden_acceptance_is_revealed(self):
        run = self.start()
        for offset, verdict in enumerate(("AC", "CE", "WA", "AC", "PENDING")):
            self.submit(run, verdict, CUTOFF + offset)
        frozen = self.board(CUTOFF + 10)[0]
        self.assertEqual((frozen["solved"], frozen["penalty"]), (0, 0))
        self.assertEqual(frozen["problem_stats"]["10"]["submissions"], 5)
        self.assertEqual(frozen["problem_stats"]["10"]["pending"], 5)
        revealed = self.board(DURATION)[0]
        self.assertEqual(revealed["solved"], 1)
        self.assertEqual(revealed["problem_stats"]["10"]["submissions"], 5)
        self.assertEqual(revealed["problem_stats"]["10"]["attempts"], 1)

    def test_own_public_row_and_ranking_use_same_filtered_result_as_other_viewers(self):
        own = self.start()
        other = self.start("bob", "Bob")
        self.submit(own, "AC", CUTOFF)
        self.submit(other, "AC", 600)
        boards = [self.board(CUTOFF + 1, viewer) for viewer in ("alice", "bob", "spectator")]
        public_boards = [[{key: value for key, value in row.items() if key != "is_self"} for row in board] for board in boards]
        self.assertEqual(public_boards[0], public_boards[1])
        self.assertEqual(public_boards[1], public_boards[2])
        self.assertEqual([(r["team_name"], r["rank"], r["solved"]) for r in boards[0]], [("Bob", 1, 1), ("Alice", 2, 0)])
        self.assertTrue(boards[0][1]["is_self"])

    def test_reading_frozen_board_does_not_modify_private_run_or_submission_records(self):
        run = self.start()
        self.submit(run, "AC", CUTOFF + 1, qoj_id=123, message="private accepted detail")
        before_run = self.store.current("alice", "contest")
        before_submissions = self.store.submissions(run["id"])
        self.assertEqual(self.board(CUTOFF + 10)[0]["solved"], 0)
        self.assertEqual(self.store.current("alice", "contest"), before_run)
        self.assertEqual(self.store.submissions(run["id"]), before_submissions)
        self.assertEqual(score(PROBLEMS, before_submissions)["solved"], 1)

    def test_automatic_unfreeze_happens_at_natural_end(self):
        run = self.start()
        self.submit(run, "AC", CUTOFF)
        self.assertEqual(self.board(DURATION - 0.001)[0]["solved"], 0)
        at_end = self.board(DURATION)[0]
        self.assertFalse(at_end["frozen"])
        self.assertEqual((at_end["solved"], at_end["penalty"]), (1, CUTOFF // 60))
        self.assertEqual(self.board(DURATION + 100)[0], at_end)

    def test_manual_finish_does_not_unfreeze_before_scheduled_end(self):
        run = self.start()
        self.submit(run, "AC", CUTOFF)
        self.store.finish(run["id"], now=START + CUTOFF + 1)
        early = self.board(CUTOFF + 2)[0]
        self.assertTrue(early["frozen"])
        self.assertEqual(early["solved"], 0)
        self.assertEqual(early["unfreeze_at"], START + DURATION)
        self.assertEqual(self.board(DURATION)[0]["solved"], 1)

    def test_asynchronous_vps_use_independent_freeze_and_end_times(self):
        early = self.start()
        late = self.start("bob", "Bob", start=START + 100)
        self.submit(early, "AC", CUTOFF)
        self.submit(late, "AC", CUTOFF - 150)
        rows = {row["team_name"]: row for row in self.board(CUTOFF)}
        self.assertTrue(rows["Alice"]["frozen"])
        self.assertFalse(rows["Bob"]["frozen"])
        self.assertEqual(rows["Bob"]["freeze_at"], START + CUTOFF + 100)
        self.submit(late, "AC", CUTOFF + 10, problem=PROBLEMS[1])
        rows = {row["team_name"]: row for row in self.board(DURATION)}
        self.assertFalse(rows["Alice"]["frozen"])
        self.assertTrue(rows["Bob"]["frozen"])
        self.assertEqual(rows["Bob"]["solved"], 1)
        self.assertEqual(rows["Bob"]["problem_stats"]["20"]["pending"], 1)
        rows = {row["team_name"]: row for row in self.board(DURATION + 100)}
        self.assertEqual(rows["Bob"]["solved"], 2)

    def test_freeze_configuration_persists_across_restarts(self):
        run = self.start()
        self.submit(run, "AC", CUTOFF)
        expected = self.board(CUTOFF + 1)
        reopened = Store(self.path)
        self.assertEqual(reopened.current("alice", "contest")["freeze_seconds"], FREEZE)
        self.assertEqual(reopened.scoreboard("contest", "alice", PROBLEMS, now=START + CUTOFF + 1), expected)

    def test_existing_database_migration_preserves_old_run_and_disables_freeze(self):
        legacy_path = Path(self.directory.name) / "legacy.sqlite3"
        with sqlite3.connect(legacy_path) as db:
            db.execute("""CREATE TABLE runs (
                id INTEGER PRIMARY KEY, sid TEXT NOT NULL, contest_key TEXT NOT NULL,
                team_name TEXT NOT NULL, started_at REAL NOT NULL, ends_at REAL NOT NULL,
                duration_seconds INTEGER NOT NULL, finished_at REAL)""")
            db.execute("INSERT INTO runs VALUES(13,'legacy','contest','Legacy',?,?,?,NULL)", (START, START + DURATION, DURATION))
        migrated = Store(legacy_path)
        run = migrated.current("legacy", "contest")
        self.assertEqual((run["id"], run["team_name"], run["freeze_seconds"]), (13, "Legacy", 0))
        identifier = migrated.add_submission(run, PROBLEMS[0], "C++17", now=START + CUTOFF)
        migrated.update_submission(identifier, verdict="AC")
        row = migrated.scoreboard("contest", "legacy", PROBLEMS, now=START + CUTOFF)[0]
        self.assertFalse(row["frozen"])
        self.assertEqual(row["solved"], 1)
        self.assertIsNone(row["freeze_at"])
        self.assertIsNone(row["unfreeze_at"])

    def test_disabled_by_default_preserves_original_score_and_adds_record_counts(self):
        run = self.store.start("alice", "contest", "Alice", DURATION, now=START)
        self.assertEqual(run["freeze_seconds"], 0)
        self.submit(run, "WA", CUTOFF)
        self.submit(run, "AC", CUTOFF + 1)
        row = self.board(CUTOFF + 2)[0]
        expected = score(PROBLEMS, self.store.submissions(run["id"]))
        self.assertEqual((row["solved"], row["penalty"]), (expected["solved"], expected["penalty"]))
        self.assertEqual({pid: {key: value for key, value in item.items() if key != "submissions"}
                          for pid, item in row["problem_stats"].items()}, expected["problem_stats"])
        self.assertFalse(row["frozen"])
        self.assertIsNone(row["freeze_at"])
        self.assertIsNone(row["unfreeze_at"])
        self.assertEqual(set(row["problem_stats"]["10"]), {"attempts", "wrong", "pending", "solved", "time", "submissions"})
        self.assertEqual(row["problem_stats"]["10"]["submissions"], 2)
        self.assertEqual(row["problem_stats"]["20"]["submissions"], 0)

    def test_short_run_clamps_freeze_to_whole_duration(self):
        run = self.start(duration=90, freeze=600)
        self.assertEqual(run["freeze_seconds"], 90)
        self.submit(run, "AC", 0)
        row = self.board(0)[0]
        self.assertTrue(row["frozen"])
        self.assertEqual(row["freeze_at"], START)
        self.assertEqual(row["solved"], 0)
        self.assertEqual(self.board(90)[0]["solved"], 1)

    def test_invalid_freeze_values_fail_before_finishing_current_run(self):
        run = self.start()
        for value in (-1, 604801, 1.5, True, None, "600", [], {}):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    self.store.start("alice", "contest", "Replacement", DURATION, now=START + 1, freeze_seconds=value)
                self.assertEqual(self.store.current("alice", "contest"), run)

    def test_reset_uses_latest_run_policy_and_keeps_previous_private_history(self):
        previous = self.start()
        identifier = self.submit(previous, "AC", CUTOFF)
        latest = self.start(name="Alice retry", start=START + CUTOFF + 10, freeze=0)
        board = self.board(CUTOFF + 11)
        self.assertEqual(len(board), 1)
        self.assertEqual(board[0]["team_name"], "Alice retry")
        self.assertFalse(board[0]["frozen"])
        self.assertEqual(self.store.current("alice", "contest")["id"], latest["id"])
        self.assertEqual(self.store.submissions(previous["id"])[0]["id"], identifier)
        self.assertEqual(self.store.submissions(previous["id"])[0]["verdict"], "AC")


if __name__ == "__main__":
    unittest.main()
