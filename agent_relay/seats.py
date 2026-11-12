"""Seats and automatic hand-off.

A seat (``builder``, ``reviewer``, ``tester`` ...) is a role on the board. It
lists engines in order of preference, each tied to a quota account. The rule
is simple: **a seat is filled by the first engine whose account is not
critical.** When the current engine's account runs out, the seat hands off to
the next one; when the preferred account recovers, it hands back. The task the
seat was holding stays with the seat, so the new engine picks it up.

Engine kinds:

* ``hook``   – an agent that calls ``relay hook`` when it finishes a turn
               (Claude Code, Grok CLI, Cursor). Nothing to launch.
* ``window`` – a desktop app the dispatcher types into (Windows only).
* ``cli``    – a command agent-relay launches for the seat and stops when the
               seat hands off (e.g. ``agy -i {prompt}``).
* ``manual`` – a human or anything else; agent-relay only tracks it.

A hand-off never happens more often than ``switch_cooldown_min`` per seat, so a
quota hovering around a threshold cannot make a seat ping-pong.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from typing import Any, Callable

from . import board, quota, status
from .config import Config, Engine, Seat
from .procs import IS_WINDOWS, alive, find_pid, kill_tree
from .store import read_json_lenient, write_json

Launcher = Callable[[Config, Seat, Engine], "int | None"]
Stopper = Callable[["int | None"], None]


def _path(cfg: Config) -> str:
    return cfg.state("seats.json")


def load_state(cfg: Config) -> dict[str, Any]:
    return read_json_lenient(_path(cfg), {})


def save_state(cfg: Config, data: dict[str, Any]) -> None:
    write_json(_path(cfg), data)


def current_engine(cfg: Config, seat: Seat, data: dict[str, Any] | None = None) -> Engine:
    data = data if data is not None else load_state(cfg)
    name = (data.get(seat.name) or {}).get("engine")
    return seat.engine(name) or seat.engines[0]


def choose(cfg: Config, seat: Seat, quota_state: dict[str, Any]) -> Engine | None:
    """First engine in preference order whose account is not critical (None if all are)."""
    for engine in seat.engines:
        if quota.level(cfg, engine.account, quota_state) != "critical":
            return engine
    return None


def headroom(cfg: Config, seat_name: str, quota_state: dict[str, Any] | None = None) -> float:
    seat = cfg.seats[seat_name]
    return quota.headroom(cfg, current_engine(cfg, seat).account, quota_state)


# ---------------------------------------------------------------- prompts
def prompt_text(cfg: Config, seat: Seat, engine: Engine) -> str:
    relay = f'"{sys.executable}" -m agent_relay --config "{cfg.path}"'
    lines = [
        f"You hold the `{seat.name}` seat ({seat.label}) on the agent-relay team of project \"{cfg.project}\".",
        f"Your role: {seat.role or 'take tasks from the board and finish them'}",
        f"Work folder: {cfg.workdir}",
        "",
        "Work in a continuous loop:",
        f"1. {relay} inbox {seat.name}        (requests for you; do them first)",
        f"2. {relay} claim {seat.name}        (your current task, or the next free one)",
        f"3. {relay} status {seat.name} --doing \"<task id and title>\"",
        "4. Do the task. Run the project's tests.",
        f"5. {relay} done {seat.name} <ID> \"what changed, in which files, how you tested it\"",
        "   If the gate refuses, read the reason, fix it and run `done` again.",
        "6. Go back to step 1. If there is no task and no request, wait 5 minutes and check again.",
        "",
        "If you already hold a claimed task, continue it: another engine may have started it before a hand-off.",
    ]
    if cfg.mail.get("enabled"):
        lines += [
            "",
            f"Team mail (MCP Agent Mail, project_key \"{mail_project_key(cfg)}\"): check your inbox each loop, "
            "reply, and acknowledge messages you handled.",
        ]
    if cfg.instructions:
        lines += ["", "Project instructions:", cfg.instructions]
    return "\n".join(lines) + "\n"


def mail_project_key(cfg: Config) -> str:
    return cfg.mail.get("project_key") or cfg.workdir.replace("\\", "/")


def prompt_file(cfg: Config, seat: Seat) -> str:
    return cfg.state("seats", f"{seat.name}.md")


def build_argv(cfg: Config, seat: Seat, engine: Engine) -> list[str]:
    path = prompt_file(cfg, seat)
    # The full instructions live in a file: long prompts with quotes or ';' get
    # mangled by terminals. The command line only carries one short sentence.
    short = f"Read the file {path.replace(os.sep, '/')} and follow all of its instructions now, in a continuous loop"
    values = {"prompt": short, "prompt_file": path, "workdir": cfg.workdir, "seat": seat.name, "python": sys.executable}
    argv = [part.format(**values) for part in engine.command]
    if cfg.allow_autonomous and engine.autonomous_args:
        argv += [part.format(**values) for part in engine.autonomous_args]
    return argv


def launch(cfg: Config, seat: Seat, engine: Engine) -> int | None:
    """Start a ``cli`` engine for a seat and return its PID (None if unknown)."""
    path = prompt_file(cfg, seat)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(prompt_text(cfg, seat, engine))
    argv = build_argv(cfg, seat, engine)
    terminal = engine.raw.get("terminal", "console" if IS_WINDOWS else "background")
    os.makedirs(cfg.workdir, exist_ok=True)
    if terminal == "wt" and IS_WINDOWS:
        subprocess.Popen(["wt.exe", "-w", "agent-relay", "new-tab", "--title", f"{seat.name}: {engine.name}",
                          "-d", cfg.workdir, *argv], creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return find_pid(os.path.basename(path))
    if IS_WINDOWS and terminal == "console":
        proc = subprocess.Popen(argv, cwd=cfg.workdir, creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0))
        return proc.pid
    log = open(cfg.state("logs", f"{seat.name}.log"), "ab") if _ensure_logs(cfg) else subprocess.DEVNULL
    proc = subprocess.Popen(argv, cwd=cfg.workdir, stdout=log, stderr=subprocess.STDOUT,
                            start_new_session=not IS_WINDOWS)
    return proc.pid


def _ensure_logs(cfg: Config) -> bool:
    os.makedirs(cfg.state("logs"), exist_ok=True)
    return True


# ---------------------------------------------------------------- hand-off
def _record(data: dict[str, Any], seat: str, engine: Engine, pid: int | None, reason: str) -> None:
    entry = data.setdefault(seat, {})
    history = entry.setdefault("history", [])
    history.append({"at": time.strftime("%Y-%m-%d %H:%M"), "engine": engine.name, "reason": reason})
    entry.update(engine=engine.name, pid=pid, switched_at=time.time(), history=history[-50:])


def start(cfg: Config, seat: Seat, engine: Engine, launcher: Launcher = launch) -> int | None:
    return launcher(cfg, seat, engine) if engine.delivery == "cli" else None


def auto(cfg: Config, quota_state: dict[str, Any] | None = None, launcher: Launcher = launch,
         stopper: Stopper = kill_tree) -> list[str]:
    """Hand each seat to the engine its quota allows. Returns human-readable changes."""
    quota_state = quota_state if quota_state is not None else quota.state(cfg)
    data = load_state(cfg)
    cooldown = float(cfg.timing["switch_cooldown_min"]) * 60
    changes = []
    for seat in cfg.seats.values():
        entry = data.get(seat.name) or {}
        current = current_engine(cfg, seat, data)
        target = choose(cfg, seat, quota_state)
        if target is None or target.name == current.name:
            continue
        if entry.get("switched_at") and time.time() - float(entry["switched_at"]) < cooldown:
            continue
        try:
            pid = start(cfg, seat, target, launcher)
        except OSError as exc:  # e.g. the backup CLI is not installed: keep the seat as it is
            changes.append(f"{seat.name}: could not start {target.name} ({exc}); staying on {current.name}")
            continue
        if current.delivery == "cli":
            stopper(entry.get("pid"))
        reason = (f"{current.account or current.name} is {quota.level(cfg, current.account, quota_state)}"
                  f", {target.account or target.name} is {quota.level(cfg, target.account, quota_state)}")
        _record(data, seat.name, target, pid, reason)
        changes.append(f"{seat.name}: {current.name} -> {target.name} ({reason})")
    if changes:
        save_state(cfg, data)
    return changes


def switch(cfg: Config, seat_name: str, engine_name: str, launcher: Launcher = launch,
           stopper: Stopper = kill_tree) -> str:
    """Manual hand-off (ignores quota and cooldown)."""
    seat = cfg.seats[seat_name]
    target = seat.engine(engine_name)
    if target is None:
        raise KeyError(f"seat `{seat_name}` has no engine `{engine_name}`")
    data = load_state(cfg)
    current = current_engine(cfg, seat, data)
    if current.delivery == "cli":
        stopper((data.get(seat_name) or {}).get("pid"))
    pid = start(cfg, seat, target, launcher)
    _record(data, seat_name, target, pid, "manual")
    save_state(cfg, data)
    return f"{seat_name}: {current.name} -> {target.name} (manual)"


def keep_alive(cfg: Config, launcher: Launcher = launch, stopper: Stopper = kill_tree,
               quota_state: dict[str, Any] | None = None) -> list[str]:
    """Relaunch a seat's ``cli`` engine that exited while there is work, and
    restart one that has been silent for too long (agents sometimes stop and
    wait forever, e.g. after being told to pause for quota). One action per seat."""
    quota_state = quota_state if quota_state is not None else quota.state(cfg)
    data = load_state(cfg)
    stale_after = float(cfg.timing["idle_min"]) * 4
    actions = []
    for seat in cfg.seats.values():
        engine = current_engine(cfg, seat, data)
        if engine.delivery != "cli" or quota.level(cfg, engine.account, quota_state) == "critical":
            continue
        entry = data.setdefault(seat.name, {"engine": engine.name})
        has_work = bool(board.open_for(cfg, seat.name) or board.claimed_by(cfg, seat.name))
        if not has_work:
            continue
        pid = entry.get("pid")
        if not alive(pid):
            try:
                entry["pid"] = launcher(cfg, seat, engine)
            except OSError as exc:
                actions.append(f"{seat.name}: could not start {engine.name} ({exc})")
                continue
            actions.append(f"{seat.name}: {engine.name} was not running and there is work -> started")
            continue
        quiet = status.quiet_minutes(cfg, seat.name)
        recently_woken = time.time() - float(entry.get("woke_at", 0)) < 1800
        if quiet >= stale_after and not recently_woken:
            stopper(pid)
            entry["woke_at"] = time.time()
            try:
                entry["pid"] = launcher(cfg, seat, engine)
            except OSError as exc:
                actions.append(f"{seat.name}: could not restart {engine.name} ({exc})")
                continue
            actions.append(f"{seat.name}: {engine.name} silent for {int(quiet)} min with work waiting -> restarted")
    if actions:
        save_state(cfg, data)
    return actions


def summary(cfg: Config, quota_state: dict[str, Any] | None = None) -> list[str]:
    quota_state = quota_state if quota_state is not None else quota.state(cfg)
    data = load_state(cfg)
    lines = []
    for seat in cfg.seats.values():
        engine = current_engine(cfg, seat, data)
        lvl = quota.level(cfg, engine.account, quota_state)
        st = status.read(cfg, seat.name)
        doing = st.get("doing") or "-"
        quiet = status.quiet_minutes(cfg, seat.name)
        age = "never reported" if quiet > 1e8 else f"{int(quiet)} min ago"
        lines.append(f"{seat.name:<12} {engine.name} [{lvl}] doing: {doing} ({age})")
    return lines
