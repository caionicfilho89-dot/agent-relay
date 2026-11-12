"""Requests, turn-end hooks, window delivery, reviewer, monitor bridge, dispatcher and CLI."""

from __future__ import annotations

import contextlib
import io
import json
import os
import unittest

from agent_relay import board, cli, deliver, monitor, quota, reviewer
from agent_relay.dispatcher import Dispatcher, Lock
from tests.helpers import ProjectCase

WINDOW_RAW = {
    "project": "Window project",
    "accounts": {"main": {"source": "file"}},
    "seats": {"designer": {"role": "ui", "engines": [
        {"name": "desk-app", "account": "main", "delivery": "window", "window": {"process": "DeskApp"}}]}},
}


class HookTests(ProjectCase):
    def hook(self, seat="builder"):
        return deliver.hook(self.cfg, seat)

    def test_lets_agent_stop_when_nothing_to_do(self):
        self.assertIsNone(self.hook())

    def test_request_keeps_agent_going_once(self):
        deliver.ask(self.cfg, "builder", "also update the docs", "human")
        payload = self.hook()
        self.assertEqual(payload["decision"], "block")
        self.assertIn("also update the docs", payload["reason"])
        self.assertIsNone(self.hook())  # delivered only once

    def test_free_task_nudges_only_without_current_task(self):
        board.add(self.cfg, "Fix login", who=["builder"])
        self.assertIn("T01", self.hook()["reason"])
        board.claim(self.cfg, "builder")
        self.assertIsNone(self.hook())  # busy with T01: let it stop/continue on its own

    def test_cursor_format(self):
        raw = json.loads(json.dumps(self.raw))
        raw["seats"]["builder"]["engines"][0]["hook_format"] = "cursor"
        self.write_config(raw)
        deliver.ask(self.cfg, "builder", "hi")
        self.assertIn("followup_message", self.hook())

    def test_critical_quota_stops_deliveries(self):
        deliver.ask(self.cfg, "builder", "hi")
        quota.set_usage(self.cfg, "main", "week", 97)
        quota.update(self.cfg)
        self.assertIsNone(self.hook())
        self.assertEqual(len(deliver.pending(self.cfg, "builder")), 1)  # kept for later

    def test_economy_spaces_deliveries(self):
        quota.set_usage(self.cfg, "main", "week", 75)
        quota.update(self.cfg)
        deliver.ask(self.cfg, "builder", "one")
        self.assertIsNotNone(self.hook())
        deliver.ask(self.cfg, "builder", "two")
        self.assertIsNone(self.hook())

    def test_hourly_cap_prevents_endless_loops(self):
        cfg = self.variant(timing={"hook_continue_limit": 2})
        for i in range(3):
            deliver.ask(cfg, "builder", f"r{i}")
            payload = deliver.hook(cfg, "builder")
            if i < 2:
                self.assertIsNotNone(payload)
            else:
                self.assertIsNone(payload)

    def test_mail_comes_first_and_failures_are_ignored(self):
        cfg = self.variant(mail={"enabled": True})
        payload = deliver.hook(cfg, "builder", mail_inbox=lambda seat: [{"from": "BlueLake"}])
        self.assertIn("1 unread mail", payload["reason"])

        def down(seat):
            raise ConnectionError("server down")

        deliver.ask(cfg, "builder", "fallback request")
        self.assertIn("fallback request", deliver.hook(cfg, "builder", mail_inbox=down)["reason"])

    def test_cli_hook_never_fails(self):
        import sys
        from unittest import mock

        out = io.StringIO()
        with contextlib.redirect_stdout(out), mock.patch.object(sys, "stdin", io.StringIO("{}")):
            code = cli.main(["--config", self.path, "hook", "no-such-seat"])
        self.assertEqual(code, 0)
        self.assertEqual(out.getvalue(), "")


class WindowTests(ProjectCase):
    raw = WINDOW_RAW

    def test_delivers_requests_and_nudges_idle_seat(self):
        sent = []

        def paste(cfg, engine, text):
            sent.append((engine.name, text))
            return True, "SENT"

        deliver.ask(self.cfg, "designer", "make the button blue")
        self.assertEqual(deliver.deliver_windows(self.cfg, user_idle_s=0, paste=paste), [])  # user typing
        results = deliver.deliver_windows(self.cfg, user_idle_s=60, paste=paste)
        self.assertIn("sent", results[0])
        self.assertIn("make the button blue", sent[0][1])
        self.assertEqual(deliver.pending(self.cfg, "designer"), [])

        board.add(self.cfg, "New screen", who=["designer"])
        deliver.deliver_windows(self.cfg, user_idle_s=60, paste=paste)
        self.assertIn("T01", sent[-1][1])
        n = len(sent)
        deliver.deliver_windows(self.cfg, user_idle_s=60, paste=paste)
        self.assertEqual(len(sent), n)  # no repeated nudge within nudge_every_min

    def test_failed_paste_keeps_request(self):
        deliver.ask(self.cfg, "designer", "x")
        deliver.deliver_windows(self.cfg, 60, paste=lambda c, e, t: (False, "BUSY"))
        self.assertEqual(len(deliver.pending(self.cfg, "designer")), 1)


class ReviewerTests(ProjectCase):
    def finish(self, title):
        t = board.add(self.cfg, title, "do it", ["builder"])
        board.claim(self.cfg, "builder")
        board.finish(self.cfg, "builder", t["id"], "result text")
        return t

    def test_first_run_does_not_review_history_then_reviews_new_work(self):
        self.finish("Old task")
        self.assertEqual(reviewer.review(self.cfg, ask=lambda c, p: '{"ok": false, "reason": "x"}'), [])
        self.finish("New task")
        redo = reviewer.review(self.cfg, ask=lambda c, p: '```json\n{"ok": false, "reason": "wrong file"}\n```')
        self.assertEqual(len(redo), 1)
        task = next(t for t in board.load(self.cfg) if t["id"] == redo[0])
        self.assertTrue(task["title"].startswith("Redo T02"))
        self.assertIn("wrong file", task["detail"])
        self.assertEqual(task["who"], ["builder"])

    def test_unavailable_model_retries_later(self):
        reviewer.review(self.cfg, ask=lambda c, p: "")
        self.finish("Task")
        self.assertEqual(reviewer.review(self.cfg, ask=lambda c, p: ""), [])
        self.assertEqual(len(reviewer.review(self.cfg, ask=lambda c, p: '{"ok": false, "reason": "r"}')), 1)

    def test_planner(self):
        goals = os.path.join(self.dir, "GOALS.md")
        with open(goals, "w", encoding="utf-8") as handle:
            handle.write("Make the app faster.")
        cfg = self.variant(reviewer={"enabled": True, "command": ["x"], "goals_file": "GOALS.md",
                                     "plan_when_open_below": 2, "plan_max_tasks": 2})
        answer = '[{"title": "Profile startup", "detail": "measure", "seat": "builder"}, ' \
                 '{"title": "Cache config", "seat": "nobody"}, {"title": "Third"}]'
        created = reviewer.plan(cfg, ask=lambda c, p: answer)
        self.assertEqual(len(created), 2)
        tasks = board.load(cfg)
        self.assertEqual(tasks[1]["who"], ["any"])  # unknown seat falls back to any
        self.assertEqual(reviewer.plan(cfg, ask=lambda c, p: answer), [])  # board no longer low

    def test_extract_json(self):
        self.assertEqual(reviewer.extract_json('Sure! {"ok": true} hope that helps'), {"ok": True})
        self.assertIsNone(reviewer.extract_json("no json here"))


class MonitorTests(ProjectCase):
    def test_posts_board_activity_once(self):
        sent = []
        send = lambda cfg, hook, data: sent.append((hook, data))  # noqa: E731
        board.add(self.cfg, "Old", who=["builder"])
        board.claim(self.cfg, "builder")
        board.finish(self.cfg, "builder", "T01", "done")
        monitor.sync(self.cfg, send=send)
        self.assertEqual(sent, [])  # history is not replayed
        board.add(self.cfg, "New", who=["builder"])
        board.claim(self.cfg, "builder")
        monitor.sync(self.cfg, send=send)
        self.assertEqual([h for h, _ in sent], ["SessionStart", "UserPromptSubmit", "PreToolUse"])
        monitor.sync(self.cfg, send=send)
        self.assertEqual(len(sent), 3)
        board.finish(self.cfg, "builder", "T02", "done")
        monitor.sync(self.cfg, send=send, notes=["builder handed off"])
        hooks = [h for h, _ in sent[3:]]
        self.assertEqual(hooks, ["PostToolUse", "Stop", "SessionStart", "Notification"])
        self.assertEqual(sent[-1][1]["message"], "builder handed off")


class DispatcherTests(ProjectCase):
    def test_tick_measures_quota_and_tries_hand_off(self):
        # the backup CLI is not installed in the test environment: the tick must
        # report that and keep the seat, not crash
        quota.set_usage(self.cfg, "main", "week", 96)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            notes = Dispatcher(self.cfg).tick()
        self.assertTrue(any("could not start backup-cli" in n for n in notes), notes)
        self.assertEqual(quota.state(self.cfg)["accounts"]["main"]["level"], "critical")
        with open(self.cfg.state("logs", "dispatcher.log"), encoding="utf-8") as handle:
            self.assertIn("could not start", handle.read())

    def test_lock_blocks_second_dispatcher(self):
        with Lock(self.cfg):
            from agent_relay.store import write_json

            write_json(self.cfg.state("dispatcher.lock"), {"pid": os.getppid()})
            with self.assertRaises(RuntimeError):
                Lock(self.cfg).__enter__()

    def test_due_schedule(self):
        now = [1000.0]
        d = Dispatcher(self.cfg, clock=lambda: now[0])
        self.assertTrue(d.due("x", 60))
        self.assertFalse(d.due("x", 60))
        now[0] += 61
        self.assertTrue(d.due("x", 60))


class CliTests(ProjectCase):
    def run_cli(self, *args):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cli.main(["--config", self.path, *args])
        return code, out.getvalue()

    def test_full_task_flow(self):
        self.assertEqual(self.run_cli("add", "Fix login", "--for", "builder")[0], 0)
        code, out = self.run_cli("claim", "builder")
        self.assertEqual(code, 0)
        self.assertIn("T01: Fix login", out)
        code, out = self.run_cli("done", "builder", "T01", "short")
        self.assertEqual(code, 1)
        self.assertIn("NOT done", out)
        code, out = self.run_cli("done", "builder", "T01",
                                 "src/login.py: handle empty password; ran pytest, 8 passed, checked by hand")
        self.assertEqual(code, 0)
        code, out = self.run_cli("show")
        self.assertIn("[done]  T01 Fix login", out)

    def test_claim_refuses_when_quota_is_critical(self):
        self.run_cli("add", "x", "--for", "builder")
        self.run_cli("quota", "set", "main", "week", "97")
        code, out = self.run_cli("claim", "builder")
        self.assertEqual(code, 2)
        self.assertIn("critical", out)

    def test_init_writes_valid_config_and_gitignore(self):
        target = os.path.join(self.dir, "new", "relay.json")
        os.makedirs(os.path.dirname(target))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(cli.main(["--config", target, "init"]), 0)
        with open(os.path.join(self.dir, "new", ".gitignore"), encoding="utf-8") as handle:
            self.assertIn(".relay/", handle.read())

    def test_unknown_seat_message(self):
        with self.assertRaises(SystemExit):
            self.run_cli("claim", "ghost")

    def test_config_error_exit_code(self):
        bad = os.path.join(self.dir, "bad.json")
        with open(bad, "w", encoding="utf-8") as handle:
            handle.write("{}")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(cli.main(["--config", bad, "show"]), 2)


if __name__ == "__main__":
    unittest.main()
