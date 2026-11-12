"""Quota levels and automatic seat hand-off."""

from __future__ import annotations

import datetime as dt
import time
import unittest

from agent_relay import board, quota, seats, status
from tests.helpers import ProjectCase


def iso(delta_h: float) -> str:
    return (quota.now() + dt.timedelta(hours=delta_h)).isoformat()


class LevelTests(unittest.TestCase):
    def test_thresholds(self):
        self.assertEqual(quota.level_for(10, None), "normal")
        self.assertEqual(quota.level_for(70, None), "economy")
        self.assertEqual(quota.level_for(90, None), "critical")
        self.assertEqual(quota.level_for(50, object()), "economy")  # will run out before reset
        self.assertEqual(quota.level_for(80, object()), "critical")

    def test_forecast_detects_running_out_before_reset(self):
        t0 = quota.now() - dt.timedelta(minutes=30)
        hist = [
            {"t": t0.isoformat(), "account": "a", "window": "5h", "pct": 40},
            {"t": quota.now().isoformat(), "account": "a", "window": "5h", "pct": 60},
        ]
        pace, full_at = quota.forecast(hist, "a", "5h", 60, iso(3))
        self.assertAlmostEqual(pace, 40, delta=1)
        self.assertIsNotNone(full_at)  # 40 pts left at 40/h -> 1h, reset in 3h
        _, full_at = quota.forecast(hist, "a", "5h", 60, iso(0.5))
        self.assertIsNone(full_at)  # resets first

    def test_daily_budget_only_for_long_windows(self):
        self.assertEqual(quota.daily_budget([], "a", "5h", 50, iso(3)), (None, None))
        spent, budget = quota.daily_budget([], "a", "week", 40, iso(24 * 6))
        self.assertEqual(spent, 0)
        self.assertGreater(budget, 0)
        self.assertEqual(quota.level_for_budget(budget * 1.3, budget), "critical")
        self.assertEqual(quota.level_for_budget(budget * 1.0, budget), "economy")


class QuotaStateTests(ProjectCase):
    def test_update_reads_file_sources(self):
        quota.set_usage(self.cfg, "main", "week", 95, iso(48))
        st = quota.update(self.cfg)
        self.assertEqual(st["accounts"]["main"]["level"], "critical")
        self.assertEqual(st["accounts"]["backup"]["level"], "normal")
        self.assertEqual(quota.headroom(self.cfg, "main", st), 0.0)
        self.assertEqual(quota.headroom(self.cfg, "free", st), 50.0)

    def test_failing_source_keeps_last_level(self):
        quota.set_usage(self.cfg, "main", "week", 95)
        quota.update(self.cfg)

        def broken(cfg, account, spec):
            raise RuntimeError("offline")

        st = quota.update(self.cfg, sources={**quota.SOURCES, "file": broken})
        self.assertEqual(st["accounts"]["main"]["level"], "critical")
        self.assertIn("offline", st["accounts"]["main"]["note"])

    def test_exhausted_marker_and_detection(self):
        self.assertTrue(quota.detect_exhausted(self.cfg, "backup", "Error: Individual quota reached"))
        self.assertEqual(quota.update(self.cfg)["accounts"]["backup"]["level"], "critical")
        quota.clear_exhausted(self.cfg, "backup")
        self.assertEqual(quota.update(self.cfg)["accounts"]["backup"]["level"], "normal")
        self.assertFalse(quota.detect_exhausted(self.cfg, "backup", "all fine"))

    def test_force_level(self):
        quota.force(self.cfg, "economy")
        self.assertEqual(quota.update(self.cfg)["accounts"]["main"]["level"], "economy")
        quota.force(self.cfg, None)
        self.assertEqual(quota.update(self.cfg)["accounts"]["main"]["level"], "normal")

    def test_grok_log_source(self):
        import json
        import os

        log = os.path.join(self.dir, "unified.jsonl")
        with open(log, "w", encoding="utf-8") as handle:
            handle.write("noise\n")
            handle.write(json.dumps({"ctx": {"config": {"creditUsagePercent": 33.5,
                                                        "currentPeriod": {"end": iso(30)}}}}) + "\n")
        windows = quota.measure_grok_log(self.cfg, "x", {"path": log})
        self.assertEqual(windows[0]["used_pct"], 33.5)


class HandOffTests(ProjectCase):
    def setUp(self):
        super().setUp()
        self.launched = []
        self.stopped = []

    def launcher(self, cfg, seat, engine):
        self.launched.append((seat.name, engine.name))
        return 4242

    def stopper(self, pid):
        self.stopped.append(pid)

    def auto(self):
        return seats.auto(self.cfg, quota.update(self.cfg), launcher=self.launcher, stopper=self.stopper)

    def test_stays_on_preferred_engine_while_it_has_quota(self):
        self.assertEqual(self.auto(), [])
        self.assertEqual(seats.current_engine(self.cfg, self.cfg.seats["builder"]).name, "main-hook")

    def test_hands_off_when_quota_runs_out_and_back_after_cooldown(self):
        quota.set_usage(self.cfg, "main", "week", 96)
        changes = self.auto()
        self.assertEqual(len(changes), 1)
        self.assertIn("main-hook -> backup-cli", changes[0])
        self.assertEqual(self.launched, [("builder", "backup-cli")])

        quota.set_usage(self.cfg, "main", "week", 5)
        self.assertEqual(self.auto(), [])  # cooldown: no ping-pong

        data = seats.load_state(self.cfg)
        data["builder"]["switched_at"] = time.time() - 3600
        seats.save_state(self.cfg, data)
        changes = self.auto()
        self.assertIn("backup-cli -> main-hook", changes[0])
        self.assertEqual(self.stopped, [4242])  # the substitute CLI was stopped

    def test_claimed_task_stays_with_the_seat(self):
        board.add(self.cfg, "Half done", who=["builder"])
        board.claim(self.cfg, "builder")
        quota.set_usage(self.cfg, "main", "week", 96)
        self.auto()
        self.assertEqual(board.claimed_by(self.cfg, "builder")["id"], "T01")

    def test_all_engines_out_keeps_current(self):
        quota.set_usage(self.cfg, "main", "week", 96)
        quota.set_usage(self.cfg, "backup", "week", 99)
        self.assertEqual(self.auto(), [])

    def test_failed_launch_keeps_the_seat(self):
        def broken(cfg, seat, engine):
            raise FileNotFoundError("backup-agent not installed")

        quota.set_usage(self.cfg, "main", "week", 96)
        changes = seats.auto(self.cfg, quota.update(self.cfg), launcher=broken, stopper=self.stopper)
        self.assertIn("could not start", changes[0])
        self.assertEqual(seats.current_engine(self.cfg, self.cfg.seats["builder"]).name, "main-hook")

    def test_autonomous_args_only_when_allowed(self):
        seat = self.cfg.seats["builder"]
        engine = seat.engine("backup-cli")
        self.assertNotIn("--yolo", seats.build_argv(self.cfg, seat, engine))
        cfg = self.variant(allow_autonomous=True)
        self.assertIn("--yolo", seats.build_argv(cfg, cfg.seats["builder"], cfg.seats["builder"].engine("backup-cli")))

    def test_prompt_lives_in_a_file_and_command_stays_short(self):
        seat = self.cfg.seats["builder"]
        argv = seats.build_argv(self.cfg, seat, seat.engine("backup-cli"))
        self.assertTrue(argv[-1].startswith("Read the file"))
        self.assertNotIn(";", argv[-1])

    def test_keep_alive_starts_cli_seat_with_work(self):
        self.assertEqual(seats.keep_alive(self.cfg, launcher=self.launcher, stopper=self.stopper), [])
        board.add(self.cfg, "Test it", who=["tester"])
        actions = seats.keep_alive(self.cfg, launcher=self.launcher, stopper=self.stopper)
        self.assertIn("started", actions[0])
        self.assertEqual(self.launched, [("tester", "free-cli")])

    def test_keep_alive_restarts_a_silent_seat(self):
        board.add(self.cfg, "Test it", who=["tester"])
        data = {"tester": {"engine": "free-cli", "pid": 1}}
        seats.save_state(self.cfg, data)
        import agent_relay.seats as mod

        original = mod.alive
        mod.alive = lambda pid: True
        try:
            status.announce(self.cfg, "tester", doing="something")
            st = status.read(self.cfg, "tester")
            st["epoch"] = time.time() - 3 * 3600
            from agent_relay.store import write_json

            write_json(self.cfg.state("status", "tester.json"), st)
            actions = seats.keep_alive(self.cfg, launcher=self.launcher, stopper=self.stopper)
        finally:
            mod.alive = original
        self.assertIn("restarted", actions[0])
        self.assertEqual(self.stopped, [1])


if __name__ == "__main__":
    unittest.main()
