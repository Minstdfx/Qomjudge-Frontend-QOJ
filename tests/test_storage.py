"""ICPC scoring and persistent virtual-contest ownership regressions."""
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from storage import Store, score


PROBLEMS = [{"id": 10, "label": "A"}, {"id": 20, "label": "B"}]


def submission(identifier, verdict, seconds, problem_id=10):
    return {"id": identifier, "problem_id": problem_id,
            "verdict": verdict, "elapsed_seconds": seconds}


class ScoreTests(unittest.TestCase):
    def test_penalty_counts_wrong_attempts_before_first_acceptance_only(self):
        result = score(PROBLEMS, [
            submission(8, "WA", 2000),
            submission(7, "AC", 1259),
            submission(6, "CE", 1000),
            submission(5, "TLE", 800),
            submission(4, "WA", 700),
            submission(3, "WA", 600, problem_id=20),
        ])
        self.assertEqual(result["solved"], 1)
        self.assertEqual(result["penalty"], 60)  # 20 minutes + 2 * 20.
        self.assertEqual(result["problem_stats"]["10"], {
            "attempts": 4, "wrong": 2, "pending": 0,
            "solved": True, "time": 20,
        })
        self.assertFalse(result["problem_stats"]["20"]["solved"])
        self.assertEqual(result["problem_stats"]["20"]["wrong"], 1)

    def test_unsolved_and_compile_errors_never_add_penalty(self):
        result = score(PROBLEMS, [submission(1, "WA", 120),
                                  submission(2, "CE", 240, 20)])
        self.assertEqual((result["solved"], result["penalty"]), (0, 0))
        self.assertEqual(result["problem_stats"]["20"]["wrong"], 0)

    def test_pending_and_uncertain_submissions_do_not_count_as_wrong(self):
        result = score(PROBLEMS, [submission(1, "PENDING", 50),
                                  submission(2, "SUBMITTING", 60),
                                  submission(3, "UNKNOWN", 70)])
        self.assertEqual(result["problem_stats"]["10"]["pending"], 3)
        self.assertEqual(result["problem_stats"]["10"]["wrong"], 0)
        self.assertEqual(result["penalty"], 0)

    def test_timestamp_tie_uses_submission_id_and_unknown_problem_is_ignored(self):
        result = score(PROBLEMS, [submission(4, "AC", 120, 999),
                                  submission(3, "AC", 120),
                                  submission(2, "WA", 120),
                                  submission(1, "AC", 120)])
        self.assertEqual((result["solved"], result["penalty"]), (1, 2))
        self.assertEqual(result["problem_stats"]["10"]["attempts"], 1)


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "vp.sqlite3"
        self.store = Store(self.path)

    def accepted(self, run, problem=PROBLEMS[0], seconds=60):
        identifier = self.store.add_submission(run, problem, "C++17",
                                               now=run["started_at"] + seconds)
        self.store.update_submission(identifier, verdict="AC")
        return identifier

    def test_browser_and_contest_isolation(self):
        alice = self.store.start("alice", "qoj:1802", "Alice", 3600, now=100)
        bob = self.store.start("bob", "qoj:1802", "Bob", 3600, now=100)
        other = self.store.start("alice", "qoj:1803", "Other contest", 3600, now=100)
        self.accepted(alice)
        self.assertEqual(self.store.current("alice", "qoj:1802")["id"], alice["id"])
        self.assertEqual(self.store.current("bob", "qoj:1802")["id"], bob["id"])
        self.assertEqual(self.store.current("alice", "qoj:1803")["id"], other["id"])
        self.assertIsNone(self.store.current("unknown", "qoj:1802"))
        board = self.store.scoreboard("qoj:1802", "alice", PROBLEMS)
        self.assertEqual([row["team_name"] for row in board], ["Alice", "Bob"])
        self.assertEqual([row["is_self"] for row in board], [True, False])
        self.assertEqual(self.store.submissions(bob["id"]), [])

    def test_reset_preserves_old_run_and_uses_only_latest_score(self):
        old = self.store.start("alice", "contest", "Alice", 3600, now=100)
        identifier = self.accepted(old)
        latest = self.store.start("alice", "contest", "Alice retry", 3600, now=300)
        self.assertNotEqual(old["id"], latest["id"])
        self.assertEqual(self.store.current("alice", "contest")["id"], latest["id"])
        self.assertEqual(self.store.submissions(old["id"])[0]["id"], identifier)
        self.assertEqual(self.store.submissions(latest["id"]), [])
        with self.store.connect() as database:
            old_row = database.execute("SELECT finished_at FROM runs WHERE id=?",
                                       (old["id"],)).fetchone()
        self.assertEqual(old_row["finished_at"], 300)
        board = self.store.scoreboard("contest", "alice", PROBLEMS)
        self.assertEqual(len(board), 1)
        self.assertEqual((board[0]["team_name"], board[0]["solved"]), ("Alice retry", 0))

    def test_scoreboard_shared_ranks_and_penalty_order(self):
        for sid, name, solved, seconds in [
            ("b", "Beta", True, 120), ("a", "Alpha", True, 120),
            ("c", "Charlie", True, 180), ("d", "Delta", False, 0),
        ]:
            run = self.store.start(sid, "contest", name, 3600, now=100)
            if solved:
                self.accepted(run, seconds=seconds)
        board = self.store.scoreboard("contest", "a", PROBLEMS)
        self.assertEqual([r["team_name"] for r in board], ["Alpha", "Beta", "Charlie", "Delta"])
        self.assertEqual([r["rank"] for r in board], [1, 1, 3, 4])

    def test_reopen_preserves_data_and_marks_interrupted_submission_uncertain(self):
        run = self.store.start("alice", "contest", "Alice", 3600, now=100)
        accepted_id = self.accepted(run)
        interrupted_id = self.store.add_submission(run, PROBLEMS[1], "Python3", now=180)
        source = '# 私有源码\r\nprint("hello")\r\n'
        receipt = {"username": "alice", "before_ids": [12], "source_hash": "private-fingerprint"}
        self.store.save_submission_payload(interrupted_id, source, "main.py")
        self.store.save_submission_receipt(interrupted_id, receipt)
        reopened = Store(self.path)
        self.assertEqual(reopened.current("alice", "contest")["id"], run["id"])
        rows = {r["id"]: r for r in reopened.submissions(run["id"])}
        self.assertEqual(rows[accepted_id]["verdict"], "AC")
        self.assertEqual(rows[interrupted_id]["verdict"], "UNKNOWN")
        self.assertTrue(rows[interrupted_id]["message"])
        self.assertEqual(reopened.submission_payload(interrupted_id),
                         {"source": source, "filename": "main.py", "receipt": receipt})
        self.assertNotIn("source", rows[interrupted_id])
        self.assertNotIn("receipt", rows[interrupted_id])
        self.assertIsNone(reopened.submission_payload(accepted_id))

    def test_finish_is_idempotent_and_update_fields_are_restricted(self):
        run = self.store.start("alice", "contest", "Alice", 3600, now=100)
        self.store.finish(run["id"], now=200)
        self.store.finish(run["id"], now=300)
        self.assertEqual(self.store.current("alice", "contest")["finished_at"], 200)
        identifier = self.accepted(run)
        with self.assertRaises(ValueError):
            self.store.update_submission(identifier, run_id=900)
        with self.assertRaises(ValueError):
            self.store.update_submission(identifier)


if __name__ == "__main__":
    unittest.main()
