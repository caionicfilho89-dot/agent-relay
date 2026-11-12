"""Store, config, board and gate."""

from __future__ import annotations

import json
import os
import unittest

from agent_relay import board, config, gate, store
from tests.helpers import PY, ProjectCase


class StoreTests(ProjectCase):
    def test_missing_file_returns_default(self):
        self.assertEqual(store.read_json(os.path.join(self.dir, "nope.json"), default=[]), [])

    def test_unreadable_file_is_never_treated_as_empty(self):
        path = os.path.join(self.dir, "board.json")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("{ half-written")
        with self.assertRaises(store.StoreError):
            store.read_json(path, default=[], retries=2, delay=0)

    def test_refuses_to_shrink_a_big_list(self):
        path = os.path.join(self.dir, "list.json")
        store.write_json(path, list(range(20)), guard_shrink=True)
        with self.assertRaises(store.StoreError):
            store.write_json(path, [1], guard_shrink=True)
        store.write_json(path, list(range(15)), guard_shrink=True)  # a normal edit is fine

    def test_backup_keeps_previous_content(self):
        path = os.path.join(self.dir, "x.json")
        store.write_json(path, {"v": 1})
        store.write_json(path, {"v": 2}, backup=True)
        self.assertEqual(store.read_json(path + ".bak"), {"v": 1})


class ConfigTests(ProjectCase):
    def test_loads_seats_and_defaults(self):
        self.assertEqual(list(self.cfg.seats), ["builder", "tester"])
        self.assertEqual(self.cfg.timing["switch_cooldown_min"], 30)
        self.assertFalse(self.cfg.allow_autonomous)
        self.assertTrue(self.cfg.state_dir.endswith(".relay"))

    def test_rejects_unknown_account(self):
        raw = json.loads(json.dumps(self.raw))
        raw["seats"]["builder"]["engines"][0]["account"] = "ghost"
        with self.assertRaises(config.ConfigError):
            config.parse(raw, self.path)

    def test_rejects_cli_engine_without_command(self):
        raw = json.loads(json.dumps(self.raw))
        del raw["seats"]["tester"]["engines"][0]["command"]
        with self.assertRaises(config.ConfigError):
            config.parse(raw, self.path)

    def test_finds_config_in_parent_folder(self):
        nested = os.path.join(self.dir, "a", "b")
        os.makedirs(nested)
        self.assertEqual(config.find_config(nested), os.path.abspath(self.path))

    def test_starter_config_is_valid(self):
        config.parse(config.STARTER, self.path)

    def test_shipped_example_is_valid(self):
        example = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "examples", "relay.json")
        with open(example, encoding="utf-8") as handle:
            cfg = config.parse(json.load(handle), example)
        self.assertEqual(set(cfg.seats), {"builder", "designer", "tester"})
        self.assertFalse(cfg.allow_autonomous)


class BoardTests(ProjectCase):
    def test_lifecycle(self):
        t = board.add(self.cfg, "Fix bug", "details", ["builder"])
        self.assertEqual(t["id"], "T01")
        self.assertIsNone(board.claim(self.cfg, "tester"))
        self.assertEqual(board.claim(self.cfg, "builder")["id"], "T01")
        self.assertEqual(board.claim(self.cfg, "builder")["id"], "T01")  # same task until finished
        done = board.finish(self.cfg, "builder", "T01", "all good")
        self.assertEqual(done["status"], board.DONE)

    def test_unknown_seat_is_rejected(self):
        with self.assertRaises(ValueError):
            board.add(self.cfg, "x", who=["nobody"])

    def test_only_the_holder_can_finish(self):
        board.add(self.cfg, "Fix bug", who=["any"])
        board.claim(self.cfg, "builder")
        with self.assertRaises(PermissionError):
            board.finish(self.cfg, "tester", "T01", "done it")

    def test_refusal_keeps_task_with_seat_and_records_reason(self):
        board.add(self.cfg, "Fix bug", who=["builder"])
        board.claim(self.cfg, "builder")
        result = board.finish(self.cfg, "builder", "T01", "x", check=lambda t, r: "too short")
        self.assertEqual(result["refused"], "too short")
        task = board.claimed_by(self.cfg, "builder")
        self.assertEqual(task["refusals"][-1]["reason"], "too short")

    def test_seat_with_less_quota_leaves_task_to_richer_seat(self):
        board.add(self.cfg, "Shared", who=["builder", "tester"])
        headroom = {"builder": 10.0, "tester": 80.0}.get
        self.assertIsNone(board.claim(self.cfg, "builder", headroom=headroom))
        self.assertEqual(board.claim(self.cfg, "tester", headroom=headroom)["id"], "T01")

    def test_release_reopens(self):
        board.add(self.cfg, "x", who=["any"])
        board.claim(self.cfg, "builder")
        board.release(self.cfg, "T01")
        self.assertEqual(board.open_for(self.cfg, "tester")[0]["id"], "T01")


class GateTests(ProjectCase):
    GOOD = "src/app.py: fixed the crash on empty input; ran pytest, 12 passed, and checked it by hand"

    def test_vague_result_is_refused(self):
        self.assertIn("too vague", gate.check(self.cfg, {}, "done"))

    def test_result_must_say_how_it_was_verified(self):
        text = "src/app.py: rewrote the parser so that empty input no longer crashes the whole application"
        self.assertIn("how you verified", gate.check(self.cfg, {}, text))

    def test_good_result_passes(self):
        self.assertEqual(gate.check(self.cfg, {}, self.GOOD), "")

    def test_forbidden_paths(self):
        cfg = self.variant(gate={"forbidden_paths": ["dist/"]})
        self.assertIn("generated output", gate.check(cfg, {}, self.GOOD + " (edited dist/app.js)"))

    def test_failing_check_command_refuses(self):
        cfg = self.variant(gate={"checks": [[PY, "-c", "import sys; print('boom'); sys.exit(3)"]]})
        reason = gate.check(cfg, {}, self.GOOD)
        self.assertIn("failed (exit 3)", reason)
        self.assertIn("boom", reason)

    def test_passing_check_command(self):
        cfg = self.variant(gate={"checks": [["{python}", "-c", "pass"]]})
        self.assertEqual(gate.check(cfg, {}, self.GOOD), "")

    def test_missing_check_command_is_explained(self):
        cfg = self.variant(gate={"checks": [["definitely-not-a-real-command-xyz"]]})
        self.assertIn("could not start", gate.check(cfg, {}, self.GOOD))


if __name__ == "__main__":
    unittest.main()
