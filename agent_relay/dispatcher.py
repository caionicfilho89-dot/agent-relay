"""The dispatcher loop (``relay run``).

One small process that keeps the team moving. Each tick it may:

1. measure quota (every ``quota_every_s``);
2. hand seats off to the engine their quota allows (every ``seats_every_s``);
3. keep ``cli`` engines alive while their seat has work;
4. type queued requests / nudges into ``window`` engines (when you are idle);
5. ask the reviewer about finished tasks, and the planner when the board runs low;
6. mirror the board to Claude Code Agent Monitor.

Every step is isolated: one failing step is logged and the loop goes on.
A lock file keeps a second dispatcher from running on the same project.
"""

from __future__ import annotations

import os
import time
import traceback
from typing import Callable

from . import deliver, quota, reviewer, seats
from .config import Config
from .procs import alive, user_idle_seconds
from .store import read_json_lenient, write_json


def log(cfg: Config, text: str) -> None:
    os.makedirs(cfg.state("logs"), exist_ok=True)
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {text}"
    with open(cfg.state("logs", "dispatcher.log"), "a", encoding="utf-8") as handle:
        handle.write(line + "\n")
    print(line, flush=True)


class Dispatcher:
    def __init__(self, cfg: Config, clock: Callable[[], float] = time.time):
        self.cfg = cfg
        self.clock = clock
        self.last: dict[str, float] = {}

    def due(self, name: str, every_s: float) -> bool:
        if self.clock() - self.last.get(name, 0.0) >= every_s:
            self.last[name] = self.clock()
            return True
        return False

    def step(self, name: str, fn: Callable[[], object]) -> object:
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 — never let one step stop the team
            log(self.cfg, f"{name} failed: {exc}")
            if os.environ.get("AGENT_RELAY_DEBUG"):
                traceback.print_exc()
            return None

    def tick(self) -> list[str]:
        cfg, t = self.cfg, cfg_timing(self.cfg)
        notes: list[str] = []
        if self.due("quota", t["quota_every_s"]):
            before = {k: (v or {}).get("level") for k, v in quota.state(cfg).get("accounts", {}).items()}
            new = self.step("quota", lambda: quota.update(cfg)) or {}
            for name, acc in (new.get("accounts") or {}).items():
                if before.get(name) and before[name] != acc.get("level"):
                    notes.append(f"quota: {cfg.accounts[name].get('label', name)} is now {acc.get('level')}")
        if self.due("seats", t["seats_every_s"]):
            notes += self.step("seats", lambda: seats.auto(cfg)) or []
            notes += self.step("keep_alive", lambda: seats.keep_alive(cfg)) or []
        notes += self.step("windows", lambda: deliver.deliver_windows(cfg, user_idle_seconds())) or []
        if cfg.reviewer.get("enabled") and self.due("review", t["review_every_s"]):
            redo = self.step("review", lambda: reviewer.review(cfg)) or []
            notes += [f"reviewer sent {r} back for rework" for r in redo]
            planned = self.step("plan", lambda: reviewer.plan(cfg)) or []
            if planned:
                notes.append(f"planner added {', '.join(planned)}")
        if cfg.monitor.get("enabled") and self.due("monitor", 60):
            from . import monitor

            self.step("monitor", lambda: monitor.sync(cfg, notes=[n for n in notes if not n.startswith("monitor")]))
        for note in notes:
            log(cfg, note)
        return notes

    def run(self, once: bool = False) -> None:
        with Lock(self.cfg):
            log(self.cfg, f"dispatcher started for {self.cfg.project} ({len(self.cfg.seats)} seats)")
            while True:
                self.tick()
                if once:
                    return
                time.sleep(float(self.cfg.timing["tick_s"]))


def cfg_timing(cfg: Config) -> dict[str, float]:
    return {k: float(v) for k, v in cfg.timing.items()}


class Lock:
    """Only one dispatcher per project (a stale lock from a dead process is taken over)."""

    def __init__(self, cfg: Config):
        self.path = cfg.state("dispatcher.lock")

    def __enter__(self) -> "Lock":
        held = read_json_lenient(self.path, {})
        if held.get("pid") and held["pid"] != os.getpid() and alive(held["pid"]):
            raise RuntimeError(f"another dispatcher is already running (pid {held['pid']})")
        write_json(self.path, {"pid": os.getpid(), "since": time.strftime("%Y-%m-%d %H:%M:%S")})
        return self

    def __exit__(self, *exc: object) -> None:
        try:
            if read_json_lenient(self.path, {}).get("pid") == os.getpid():
                os.remove(self.path)
        except OSError:
            pass
