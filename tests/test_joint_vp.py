"""Synthetic native VP coordination, isolation and no-replay checks."""
import copy
import json
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from joint_vp import JointVPError, JointVPService
from teams import TeamStore


class JointVPTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "state.sqlite3"
        self.teams = TeamStore(self.path)
        self.host = self.teams.create("sid-host-private", "qoj:1", "Synthetic Host")
        self.guest = self.teams.create("sid-guest-private", "qoj:1", "Synthetic Guest")
        self.other = self.teams.create("sid-other-private", "qoj:1", "Synthetic Other")
        self.snapshots = {team["id"]: {"authenticated": True, "username": "user-" + str(i),
            "mode": "vp", "status": "unregistered"} for i, team in enumerate((self.host, self.guest, self.other))}
        self.plans = {team["id"]: {"fingerprint": "fingerprint-" + team["id"],
            "action": {"id": "register_vp", "fields": []}, "message": "Synthetic registration"}
            for team in (self.host, self.guest, self.other)}
        self.launches = []
        self.observations = []
        self.launch_hook = None
        self.service = self.make_service()
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(self.service.close)

    def make_service(self):
        return JointVPService(self.path, "qoj:1", self.observe, self.prepare, self.launch)

    def observe(self, team_id, owner_sid):
        self.observations.append(team_id)
        return copy.deepcopy(self.snapshots[team_id])

    def prepare(self, team_id, owner_sid):
        return copy.deepcopy(self.plans[team_id])

    def launch(self, team_id, owner_sid, grant):
        self.launches.append((team_id, copy.deepcopy(grant)))
        if self.launch_hook:
            return self.launch_hook(team_id, grant)
        self.snapshots[team_id].update(status="running", started_at=100, ends_at=18100)
        return copy.deepcopy(self.snapshots[team_id])

    def room(self):
        view = self.service.create(self.host)
        self.service.join(self.guest, view["room"]["code"])
        return view

    def ready(self, team, values=None):
        plan = self.service.prepare(team)["room"]["self"]["plan"]
        return self.service.ready(team, plan["fingerprint"], plan["action"]["id"], values or {})

    def finish_workers(self):
        # shutdown joins outstanding tasks without guessing timing or polling.
        self.service.close()

    def test_public_projection_and_captain_boundaries(self):
        view = self.room()
        self.ready(self.host)
        self.ready(self.guest)
        public = self.service.view(self.host)
        encoded = json.dumps(public)
        self.assertNotIn("code", public["room"])
        self.assertNotIn("sid-", encoded)
        self.assertNotIn("grant", encoded)
        self.assertNotIn("fingerprint", encoded)
        self.assertNotIn("user-1", encoded)
        self.assertEqual(len(public["room"]["members"]), 2)
        member = self.teams.join("sid-member", "qoj:1", self.host["join_code"], "Member")
        self.assertNotIn("code", self.service.view(member, captain=True)["room"])
        self.assertFalse(self.service.view(member, captain=True)["room"]["can_start"])
        for call in (lambda: self.service.create(member), lambda: self.service.start(member),
                     lambda: self.service.cancel(member), lambda: self.service.ready(member, "x", "y", {})):
            with self.assertRaises(JointVPError) as caught:
                call()
            self.assertEqual(caught.exception.status, 403)
        fake = dict(self.host, owner_sid="different")
        with self.assertRaises(JointVPError):
            self.service.view(fake, captain=True)
        self.assertTrue(self.service.view(self.host, True)["room"]["can_start"])
        self.assertFalse(self.service.view(self.guest, True)["room"]["can_start"])
        self.assertEqual(self.service.view(self.guest, True)["room"]["code"], view["room"]["code"])

    def test_joint_start_uses_native_times_and_never_launches_twice(self):
        self.room()
        self.ready(self.host)
        self.ready(self.guest)
        self.service.start(self.host)
        with self.assertRaises(JointVPError):
            self.service.start(self.host)
        self.finish_workers()
        view = self.service.view(self.host, True)["room"]
        self.assertEqual(view["status"], "started")
        self.assertEqual(len(self.launches), 2)
        for member in view["members"]:
            self.assertEqual((member["status"], member["started_at"], member["ends_at"]), ("started", 100, 18100))
        with self.service.connect() as db:
            self.assertTrue(all(row[0] is None for row in db.execute("SELECT grant_json FROM joint_vp_members")))
        self.service.refresh(self.host)
        self.assertEqual(len(self.launches), 2)

    def test_concurrent_start_atomic_claim_and_independent_workers(self):
        self.room()
        self.ready(self.host)
        self.ready(self.guest)
        barrier = threading.Barrier(2)
        release = threading.Event()
        entered = threading.Event()
        def hook(team_id, grant):
            barrier.wait(timeout=5)
            entered.set()
            release.wait(timeout=5)
            return dict(self.snapshots[team_id], status="running", started_at=100, ends_at=200)
        self.launch_hook = hook
        def attempt():
            try:
                self.service.start(self.host)
                return "started"
            except JointVPError:
                return "rejected"
        try:
            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(lambda _: attempt(), range(2)))
            self.assertCountEqual(results, ["started", "rejected"])
            self.assertTrue(entered.wait(timeout=5), "both independent teams must enter concurrently")
            with self.assertRaises(JointVPError):
                self.service.cancel(self.host)
            with self.assertRaises(JointVPError):
                self.service.leave(self.guest)
            with self.assertRaises(JointVPError):
                self.service.guard_mutation(self.guest)
        finally:
            release.set()
            self.finish_workers()
        self.assertEqual(len(self.launches), 2)

    def test_all_ready_minimum_and_organizer_required(self):
        view = self.service.create(self.host)
        self.ready(self.host)
        with self.assertRaises(JointVPError):
            self.service.start(self.host)
        self.service.join(self.guest, view["room"]["code"])
        with self.assertRaises(JointVPError):
            self.service.start(self.host)
        self.ready(self.guest)
        with self.assertRaises(JointVPError) as caught:
            self.service.start(self.guest)
        self.assertEqual(caught.exception.status, 403)
        self.assertFalse(self.launches)

    def test_ready_is_explicit_and_checks_current_form_and_account(self):
        self.room()
        plan = self.plans[self.host["id"]]
        with self.assertRaises(JointVPError):
            self.service.ready(self.host, plan["fingerprint"], plan["action"]["id"], {})
        self.service.prepare(self.host)
        plan["fingerprint"] = "new-schema"
        with self.assertRaises(JointVPError):
            self.service.ready(self.host, "old-schema", "register_vp", {})
        self.ready(self.host)
        with self.assertRaises(JointVPError):
            self.service.guard_mutation(self.host)
        self.service.unready(self.host)
        self.service.guard_mutation(self.host)
        self.snapshots[self.host["id"]]["mode"] = "official"
        with self.assertRaises(JointVPError):
            self.service.prepare(self.host)
        self.snapshots[self.host["id"]].update(mode="vp", authenticated=False)
        with self.assertRaises(JointVPError):
            self.service.prepare(self.host)
        self.assertFalse(self.launches)

    def test_form_fields_require_explicit_valid_consent(self):
        self.room()
        self.plans[self.host["id"]]["action"]["fields"] = [
            {"name": "rules", "type": "checkbox", "label": "Rules", "required": True, "value": "yes", "default": True},
            {"name": "window", "type": "select", "label": "Window", "required": True,
             "options": [{"value": "now", "label": "Now"}], "default": "now"}]
        plan = self.service.prepare(self.host)["room"]["self"]["plan"]
        for values in ({}, {"rules": True}, {"rules": False, "window": "now"},
                       {"rules": True, "window": "invalid"}, {"rules": "yes", "window": "now"},
                       {"rules": True, "window": "now", "_token": "x"}):
            with self.subTest(values=values), self.assertRaises(JointVPError):
                self.service.ready(self.host, plan["fingerprint"], plan["action"]["id"], values)
        result = self.service.ready(self.host, plan["fingerprint"], plan["action"]["id"], {"rules": True, "window": "now"})
        self.assertEqual(result["room"]["self"]["status"], "ready")
        self.assertFalse(self.launches)

    def test_duplicate_qoj_account_rejected(self):
        self.room()
        self.snapshots[self.guest["id"]]["username"] = self.snapshots[self.host["id"]]["username"].upper()
        self.ready(self.host)
        with self.assertRaises(JointVPError) as caught:
            self.ready(self.guest)
        self.assertEqual(caught.exception.status, 409)
        self.assertFalse(self.launches)

    def test_changed_schema_or_account_never_posts_and_other_team_starts(self):
        for change in ("account", "schema"):
            with self.subTest(change=change):
                if change == "schema":
                    self.service.cancel(self.host)
                    self.service.close()
                    self.service = self.make_service()
                    self.addCleanup(self.service.close)
                    self.snapshots[self.guest["id"]]["status"] = "unregistered"
                    self.launches.clear()
                self.room()
                self.ready(self.host)
                self.ready(self.guest)
                if change == "account":
                    self.snapshots[self.host["id"]]["username"] = "different-user"
                else:
                    self.plans[self.host["id"]]["fingerprint"] = "changed"
                self.service.start(self.host)
                self.finish_workers()
                self.assertEqual([item[0] for item in self.launches], [self.guest["id"]])
                expected = "error" if change == "account" else "needs_confirmation"
                self.assertEqual(self.service.view(self.host, True)["room"]["self"]["status"], expected)

    def test_uncertain_post_never_retries_and_refresh_can_recover(self):
        self.room()
        self.ready(self.host)
        self.ready(self.guest)
        def hook(team_id, grant):
            if team_id == self.host["id"]:
                raise TimeoutError("private-cookie-value-must-not-leak")
            return dict(self.snapshots[team_id], status="running", started_at=101, ends_at=201)
        self.launch_hook = hook
        self.service.start(self.host)
        self.finish_workers()
        view = self.service.view(self.host, True)
        self.assertEqual(view["room"]["self"]["status"], "uncertain")
        self.assertNotIn("private-cookie", json.dumps(view))
        with self.assertRaises(JointVPError):
            self.service.guard_mutation(self.host, start=True)
        self.snapshots[self.host["id"]]["status"] = "unavailable"
        self.service.refresh(self.host)
        with self.assertRaises(JointVPError):
            self.service.guard_mutation(self.host, start=True)
        self.snapshots[self.host["id"]].update(status="unregistered", entry_uncertain=True)
        self.service.refresh(self.host)
        with self.assertRaises(JointVPError):
            self.service.guard_mutation(self.host, start=True)
        self.snapshots[self.host["id"]].update(status="running", started_at=102, ends_at=202)
        recovered = self.service.refresh(self.host)
        self.assertEqual(recovered["room"]["self"]["status"], "started")
        self.assertEqual(recovered["room"]["self"]["started_at"], 102)
        self.assertEqual(len(self.launches), 2)

    def test_confirmed_not_started_allows_manual_entry_only_after_read(self):
        self.room()
        with self.service.connect() as db:
            db.execute("UPDATE joint_vp_members SET status='uncertain' WHERE team_id=?", (self.host["id"],))
            db.execute("UPDATE joint_vp_rooms SET status='started'")
        with self.assertRaises(JointVPError):
            self.service.guard_mutation(self.host, start=True)
        self.service.refresh(self.host)
        self.service.guard_mutation(self.host, start=True)
        self.assertEqual(self.service.view(self.host, True)["room"]["self"]["status"], "error")
        self.assertFalse(self.launches)

    def test_restart_discards_grants_and_does_not_resume_posts(self):
        self.room()
        self.ready(self.host)
        self.ready(self.guest)
        self.service.close()
        with self.service.connect() as db:
            db.execute("UPDATE joint_vp_members SET status='starting' WHERE team_id=?", (self.guest["id"],))
            db.execute("UPDATE joint_vp_rooms SET status='starting'")
        restarted = self.make_service()
        self.addCleanup(restarted.close)
        self.assertEqual(restarted.view(self.host, True)["room"]["self"]["status"], "waiting")
        self.assertEqual(restarted.view(self.guest, True)["room"]["self"]["status"], "uncertain")
        self.assertEqual(restarted.view(self.host, True)["room"]["status"], "started")
        self.assertFalse(self.launches)
        with restarted.connect() as db:
            self.assertTrue(all(r[0] is None for r in db.execute("SELECT grant_json FROM joint_vp_members")))

    def test_cancel_detach_and_team_delete_release_members_without_remote_calls(self):
        self.room()
        self.service.detach(self.guest)
        self.assertIsNone(self.service.view(self.guest)["room"])
        self.service.detach(self.host)
        self.assertIsNone(self.service.view(self.host)["room"])
        self.room()
        self.ready(self.host)
        with self.assertRaises(JointVPError):
            self.service.detach(self.host)
        self.service.unready(self.host)
        # Direct TeamStore deletion remains valid: membership cascades, and an
        # orphaned host room is cleaned on the next projection.
        self.teams.dissolve(self.host["owner_sid"], "qoj:1")
        self.assertIsNone(self.service.view(self.guest)["room"])
        self.assertFalse(self.launches)

    def test_invites_are_contest_scoped_and_team_has_one_room(self):
        room = self.room()
        other_room = self.service.create(self.other)
        with self.assertRaises(JointVPError):
            self.service.join(self.guest, other_room["room"]["code"])
        other_contest_team = self.teams.create("other-contest", "qoj:2", "Other Contest")
        other_service = JointVPService(self.path, "qoj:2", self.observe, self.prepare, self.launch)
        self.addCleanup(other_service.close)
        with self.assertRaises(JointVPError) as caught:
            other_service.join(other_contest_team, room["room"]["code"])
        self.assertEqual(caught.exception.status, 404)

    def test_already_scheduled_native_vp_is_observed_without_new_post(self):
        self.room()
        self.ready(self.host)
        self.ready(self.guest)
        self.snapshots[self.host["id"]].update(status="scheduled", started_at=500, ends_at=1000)
        self.service.start(self.host)
        self.finish_workers()
        self.assertEqual([item[0] for item in self.launches], [self.guest["id"]])
        self.assertEqual(self.service.view(self.host, True)["room"]["self"]["started_at"], 500)

    def test_read_only_refresh_preserves_pending_preparation(self):
        self.room()
        plan = self.service.prepare(self.host)["room"]["self"]["plan"]
        refreshed = self.service.refresh(self.host)
        self.assertEqual(refreshed["room"]["self"]["status"], "needs_confirmation")
        self.assertEqual(refreshed["room"]["self"]["plan"], plan)

    def test_inflight_prepare_cannot_commit_to_a_new_room(self):
        self.room()
        other_room = self.service.create(self.other)["room"]
        original = self.service.prepare_callback
        def move_during_get(team_id, owner_sid):
            self.service.leave(self.guest)
            self.service.join(self.guest, other_room["code"])
            return original(team_id, owner_sid)
        self.service.prepare_callback = move_during_get
        with self.assertRaises(JointVPError):
            self.service.prepare(self.guest)
        view = self.service.view(self.guest, True)["room"]
        self.assertEqual(view["id"], other_room["id"])
        self.assertEqual(view["self"]["status"], "waiting")
        self.assertNotIn("plan", view["self"])

    def test_inflight_refresh_cannot_modify_a_new_room(self):
        self.room()
        other_room = self.service.create(self.other)["room"]
        original = self.service.observe_callback
        def move_during_get(team_id, owner_sid):
            self.service.leave(self.guest)
            self.service.join(self.guest, other_room["code"])
            return dict(original(team_id, owner_sid), status="running", started_at=100, ends_at=200)
        self.service.observe_callback = move_during_get
        view = self.service.refresh(self.guest)["room"]
        self.assertEqual(view["id"], other_room["id"])
        self.assertEqual(view["self"]["status"], "waiting")


if __name__ == "__main__":
    unittest.main()
