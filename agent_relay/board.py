"""The shared task board.

Tasks are assigned to *seats* (roles), never to a product, so the board does
not care which AI currently fills a seat. ``who`` lists the seats allowed to
take a task, or ``["any"]``.

Lifecycle: ``open`` -> ``claimed`` (by one seat) -> ``done``. Finishing a task
goes through the completion gate (:mod:`agent_relay.gate`); a refused task
stays claimed with the reason, so the agent fixes it and tries again.
"""

from __future__ import annotations

import time
from typing import Any, Callable

from .config import Config
from .store import read_json, write_json

TIME_FMT = "%Y-%m-%d %H:%M"
OPEN, CLAIMED, DONE = "open", "claimed", "done"


def _path(cfg: Config) -> str:
    return cfg.state("board.json")


def load(cfg: Config) -> list[dict[str, Any]]:
    return read_json(_path(cfg), default=[])


def save(cfg: Config, tasks: list[dict[str, Any]]) -> None:
    write_json(_path(cfg), tasks, guard_shrink=True, backup=True)


def _next_id(tasks: list[dict[str, Any]]) -> str:
    numbers = [int(t["id"][1:]) for t in tasks if str(t.get("id", "")).startswith("T") and t["id"][1:].isdigit()]
    return f"T{1 + max(numbers or [0]):02d}"


def add(cfg: Config, title: str, detail: str = "", who: list[str] | None = None, created_by: str = "") -> dict[str, Any]:
    who = who or ["any"]
    unknown = [w for w in who if w != "any" and w not in cfg.seats]
    if unknown:
        raise ValueError(f"unknown seat(s): {', '.join(unknown)}")
    tasks = load(cfg)
    task = {
        "id": _next_id(tasks),
        "title": title.strip(),
        "detail": detail.strip(),
        "who": who,
        "status": OPEN,
        "by": "",
        "created_by": created_by,
        "at": time.strftime(TIME_FMT),
        "result": "",
        "refusals": [],
    }
    tasks.append(task)
    save(cfg, tasks)
    return task


def fits(task: dict[str, Any], seat: str) -> bool:
    return seat in task.get("who", []) or "any" in task.get("who", [])


def open_for(cfg: Config, seat: str) -> list[dict[str, Any]]:
    return [t for t in load(cfg) if t["status"] == OPEN and fits(t, seat)]


def claimed_by(cfg: Config, seat: str) -> dict[str, Any] | None:
    return next((t for t in load(cfg) if t["status"] == CLAIMED and t["by"] == seat), None)


def _minutes_waiting(task: dict[str, Any]) -> float:
    try:
        return (time.time() - time.mktime(time.strptime(task.get("at", ""), TIME_FMT))) / 60
    except ValueError:
        return 0.0


def claim(cfg: Config, seat: str, headroom: Callable[[str], float] | None = None) -> dict[str, Any] | None:
    """Give ``seat`` its current task, or claim the next open one that fits.

    With ``headroom`` (seat -> % of quota left), a task that another eligible
    seat could do with clearly more quota left (>15 points) is skipped, unless
    it has been waiting for 30+ minutes — seats with less quota do less.
    """
    tasks = load(cfg)
    for t in tasks:
        if t["status"] == CLAIMED and t["by"] == seat:
            return t
    mine = headroom(seat) if headroom else None
    for t in tasks:
        if t["status"] != OPEN or not fits(t, seat):
            continue
        if headroom and _minutes_waiting(t) <= 30:
            others = [s for s in (cfg.seats if "any" in t["who"] else t["who"]) if s != seat]
            if any(headroom(o) > (mine or 0) + 15 for o in others):
                continue
        t.update(status=CLAIMED, by=seat, at=time.strftime(TIME_FMT))
        save(cfg, tasks)
        return t
    return None


def finish(cfg: Config, seat: str, task_id: str, result: str, check: Callable[[dict, str], str] | None = None) -> dict[str, Any]:
    """Mark a task done if it passes ``check`` (the gate). Returns the task, or
    ``{"refused": reason, "id": ...}``. Only the seat holding the task may finish it."""
    task = next((t for t in load(cfg) if t["id"] == task_id), None)
    if task is None:
        raise KeyError(f"no task {task_id}")
    if task["status"] == DONE:
        return task
    if task["status"] == CLAIMED and task["by"] != seat:
        raise PermissionError(f"{task_id} is claimed by {task['by']}, not {seat}")
    reason = check(task, result) if check else ""
    tasks = load(cfg)
    for t in tasks:
        if t["id"] != task_id:
            continue
        if reason:
            t.setdefault("refusals", []).append({"at": time.strftime(TIME_FMT), "reason": reason[:500]})
            t.update(status=CLAIMED, by=seat)
            save(cfg, tasks)
            return {"refused": reason, "id": task_id}
        t.update(status=DONE, by=seat, at=time.strftime(TIME_FMT), result=result.strip()[:4000])
        save(cfg, tasks)
        return t
    raise KeyError(f"no task {task_id}")


def release(cfg: Config, task_id: str) -> dict[str, Any]:
    """Put a claimed task back on the board (e.g. its seat changed engine mid-task and gave up)."""
    tasks = load(cfg)
    for t in tasks:
        if t["id"] == task_id and t["status"] == CLAIMED:
            t.update(status=OPEN, by="", at=time.strftime(TIME_FMT))
            save(cfg, tasks)
            return t
    raise KeyError(f"no claimed task {task_id}")


def summary(cfg: Config, done_limit: int = 5) -> list[str]:
    tasks = load(cfg)
    lines = []
    for status, icon in ((CLAIMED, "[doing]"), (OPEN, "[open] "), (DONE, "[done] ")):
        group = [t for t in tasks if t["status"] == status]
        if status == DONE:
            group = group[-done_limit:]
        for t in group:
            owner = f" ({t['by']})" if t["by"] else f" [for: {', '.join(t['who'])}]"
            lines.append(f"{icon} {t['id']} {t['title']}{owner}")
    return lines
