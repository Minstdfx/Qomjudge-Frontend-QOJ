"""Explicit active QOJ participation for legacy resource-focused fixtures.

These fixtures model an upstream contest that has already started. Permission
transitions and native entry actions are covered separately by test_native_api.
"""
import time


class ActiveNativeFixture:
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.native_started_at = time.time() - 60
        self.participation_calls = []

    def participation(self, contest_id, mode="vp"):
        self.participation_calls.append((contest_id, mode))
        return {"mode": mode, "status": "running", "content_available": self.authenticated,
                "can_submit": self.authenticated, "authenticated": self.authenticated,
                "username": self.username, "started_at": self.native_started_at,
                "ends_at": self.native_started_at + 18000, "actions": [], "scoreboard": [],
                "contest": self.contest(contest_id)}

    def enter(self, *args, **kwargs):
        raise AssertionError("Resource tests must not initiate native participation")
