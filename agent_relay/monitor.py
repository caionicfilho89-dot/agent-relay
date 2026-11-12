"""Bridge to Claude Code Agent Monitor (optional).

`Claude Code Agent Monitor <https://github.com/hoangsonww/Claude-Code-Agent-Monitor>`_
is a local dashboard that reads Claude Code (and other agents) through hooks.
This bridge posts the team's board activity to it in the same format as its
official hooks, so every seat shows up as a live session — whichever engine
fills it:

* claimed task -> ``UserPromptSubmit`` + ``PreToolUse`` (seat working)
* finished task -> ``PostToolUse`` + ``Stop``
* hand-offs and quota alerts -> ``Notification`` on a "team" session

The dashboard port is read from ``~/.claude/.agent-dashboard.json`` (written by
the monitor) or ``monitor.port`` (default 4820).
"""

from __future__ import annotations

import datetime as dt
import json
import os
import urllib.request
import uuid
from typing import Any, Callable

from . import board
from .config import Config
from .store import read_json_lenient, write_json

Post = Callable[[Config, str, dict[str, Any]], Any]


def port(cfg: Config) -> int:
    if cfg.monitor.get("port"):
        return int(cfg.monitor["port"])
    found = read_json_lenient(os.path.join(os.path.expanduser("~"), ".claude", ".agent-dashboard.json"), {})
    return int(found.get("port") or 4820)


def post(cfg: Config, hook_type: str, data: dict[str, Any]) -> int:
    body = json.dumps({"hook_type": hook_type, "data": data}, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(f"http://127.0.0.1:{port(cfg)}/api/hooks/event", data=body, method="POST",
                                     headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=5) as response:  # noqa: S310 - fixed local URL
        return response.status


def session_id(cfg: Config, name: str) -> str:
    """One monitor session per seat per day."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"agent-relay/{cfg.project}/{name}/{dt.date.today().isoformat()}"))


def _base(cfg: Config, sid: str, event: str, name: str, model: str) -> dict[str, Any]:
    return {"session_id": sid, "hook_event_name": event, "cwd": cfg.workdir.replace("\\", "/"),
            "session_name": name, "model": model}


def sync(cfg: Config, send: Post = post, notes: list[str] | None = None) -> int:
    """Post what changed on the board since the last call. Returns the number of events sent."""
    from . import seats as seats_mod

    path = cfg.state("monitor.json")
    st = read_json_lenient(path, {"sessions": {}, "seen": {}})
    sent = 0

    def ensure(sid: str, name: str, model: str) -> None:
        nonlocal sent
        if sid not in st["sessions"]:
            send(cfg, "SessionStart", dict(_base(cfg, sid, "SessionStart", name, model), source="startup"))
            st["sessions"][sid] = True
            sent += 1

    tasks = board.load(cfg)
    if not st.get("started"):
        # first sync: do not replay the board's whole history into the dashboard
        st["seen"] = {t["id"]: f"{t['id']}:{t['status']}" for t in tasks if t["status"] == board.DONE}
        st["started"] = True
    for task in tasks:
        seat = task.get("by")
        if seat not in cfg.seats or task["status"] == board.OPEN:
            continue
        key = f"{task['id']}:{task['status']}"
        if st["seen"].get(task["id"]) == key:
            continue
        engine = seats_mod.current_engine(cfg, cfg.seats[seat])
        name = f"{cfg.project} - {cfg.seats[seat].label} ({engine.name})"
        model = engine.raw.get("model", engine.name)
        sid = session_id(cfg, seat)
        ensure(sid, name, model)
        label = f"{task['id']} {task['title']}"
        if task["status"] == board.CLAIMED:
            send(cfg, "UserPromptSubmit", dict(_base(cfg, sid, "UserPromptSubmit", name, model), prompt=label))
            send(cfg, "PreToolUse", dict(_base(cfg, sid, "PreToolUse", name, model), tool_name="Task",
                                         tool_input={"description": label}))
            sent += 2
        else:
            send(cfg, "PostToolUse", dict(_base(cfg, sid, "PostToolUse", name, model), tool_name="Task",
                                          tool_input={"description": label},
                                          tool_response={"result": task.get("result", "")[:500]}))
            send(cfg, "Stop", _base(cfg, sid, "Stop", name, model))
            sent += 2
        st["seen"][task["id"]] = key
    if notes:
        sid = session_id(cfg, "team")
        name = f"{cfg.project} - team"
        ensure(sid, name, "agent-relay")
        for note in notes:
            send(cfg, "Notification", dict(_base(cfg, sid, "Notification", name, "agent-relay"), message=note))
            sent += 1
    write_json(path, st)
    return sent
