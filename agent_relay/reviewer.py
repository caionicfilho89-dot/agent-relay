"""AI reviewer and planner (optional).

The gate catches broken tests; it cannot tell that an agent "fixed" something
other than what was asked. The reviewer asks a model, text-only, whether each
finished task really answers its request. A "no" becomes a new ``Redo`` task
for the same seat, with the reason.

When the board runs low, the same model can propose a few small, useful tasks
from a goals file you write (``reviewer.goals_file``).

Configure any CLI that prints a reply, e.g.::

    "reviewer": {"enabled": true, "account": "claude",
                 "command": ["claude", "-p", "{prompt}"]}

The prompt can also go through stdin (``"stdin": true``). The reviewer is
skipped while its account is ``critical``, and a reply saying the quota ran
out marks the account exhausted.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from typing import Any, Callable

from . import board, quota
from .config import Config
from .store import read_json_lenient, write_json

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
TEXT_ONLY = "Do NOT use tools and do NOT read files: everything you need is below. Answer with text only.\n\n"


def ask_model(cfg: Config, prompt: str) -> str:
    spec = cfg.reviewer
    if quota.level(cfg, spec.get("account")) == "critical":
        return ""
    prompt = TEXT_ONLY + prompt
    argv = [part.replace("{prompt}", prompt) for part in spec["command"]]
    try:
        proc = subprocess.run(argv, input=prompt if spec.get("stdin") else None, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=int(spec.get("timeout_s", 240)),
                              cwd=os.path.expanduser("~"), creationflags=_NO_WINDOW)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    quota.detect_exhausted(cfg, spec.get("account"), (proc.stdout or "") + (proc.stderr or ""))
    return (proc.stdout or "").strip()


def extract_json(text: str) -> Any:
    fenced = re.search(r"```(?:json)?\s*(.+?)```", text or "", re.S)
    raw = fenced.group(1) if fenced else text or ""
    starts = [i for i in (raw.find("{"), raw.find("[")) if i >= 0]
    if not starts:
        return None
    raw = raw[min(starts):]
    for end in range(len(raw), 0, -1):
        try:
            return json.loads(raw[:end])
        except ValueError:
            continue
    return None


def _state_path(cfg: Config) -> str:
    return cfg.state("reviewer.json")


def review(cfg: Config, limit: int = 1, ask: Callable[[Config, str], str] = ask_model) -> list[str]:
    """Review up to ``limit`` finished tasks not reviewed yet (newest first). Returns ids of Redo tasks."""
    st = read_json_lenient(_state_path(cfg), {"reviewed": [], "last_plan": 0})
    done = [t for t in board.load(cfg) if t["status"] == board.DONE and t["id"] not in st["reviewed"]
            and not t["title"].startswith("Redo ")]
    if not st["reviewed"] and not st.get("started"):
        # first run: start from now, do not re-review the whole history
        st.update(reviewed=[t["id"] for t in done], started=True)
        write_json(_state_path(cfg), st)
        return []
    redo = []
    for t in sorted(done, key=lambda x: x.get("at", ""), reverse=True)[:limit]:
        answer = ask(cfg, (
            f"You review work on the project \"{cfg.project}\". An agent says it finished a task.\n"
            f"TASK {t['id']}: {t['title']}\nREQUEST: {t.get('detail', '')}\nRESULT CLAIMED: {t.get('result', '')}\n\n"
            "Automated checks already passed. Judge only: does the result really do what was asked? Be fair: a short "
            'but coherent result is fine. Reply with ONLY a JSON object: {"ok": true or false, "reason": '
            '"one sentence saying what is missing, if not ok"}'))
        if not answer:
            break  # model unavailable: try again next time, do not mark as reviewed
        verdict = extract_json(answer) or {}
        st["reviewed"].append(t["id"])
        if verdict.get("ok") is False and verdict.get("reason"):
            seat = t.get("by") if t.get("by") in cfg.seats else None
            new = board.add(cfg, f"Redo {t['id']}: {t['title'][:70]}",
                            f"The reviewer refused {t['id']}: {verdict['reason']} | Original request: {t.get('detail', '')}",
                            [seat] if seat else ["any"], created_by="reviewer")
            redo.append(new["id"])
            _mail_redo(cfg, seat, t, new, verdict["reason"])
    st["reviewed"] = st["reviewed"][-1000:]
    write_json(_state_path(cfg), st)
    return redo


def _mail_redo(cfg: Config, seat: str | None, task: dict, new: dict, reason: str) -> None:
    if not (cfg.mail.get("enabled") and seat):
        return
    try:
        from . import mail

        mail.send(cfg, "reviewer", seat, f"Review of {task['id']}: redo as {new['id']}", reason)
    except Exception:  # noqa: BLE001 — mail is best effort
        pass


def plan(cfg: Config, ask: Callable[[Config, str], str] = ask_model, force: bool = False) -> list[str]:
    """Propose up to ``plan_max_tasks`` small tasks when fewer than ``plan_when_open_below`` are open."""
    spec = cfg.reviewer
    threshold = int(spec.get("plan_when_open_below", 0) or 0)
    goals_file = spec.get("goals_file")
    if not goals_file or (threshold <= 0 and not force):
        return []
    tasks = board.load(cfg)
    open_tasks = [t for t in tasks if t["status"] == board.OPEN]
    st = read_json_lenient(_state_path(cfg), {"reviewed": [], "last_plan": 0})
    if not force and (len(open_tasks) >= threshold or time.time() - float(st.get("last_plan", 0)) < 3600):
        return []
    path = goals_file if os.path.isabs(goals_file) else os.path.join(cfg.root, goals_file)
    try:
        with open(path, encoding="utf-8") as handle:
            goals = handle.read()[:6000]
    except OSError:
        return []
    recent = "\n".join(f"- {t['title']} ({t['status']})" for t in tasks[-40:])
    seats_desc = "\n".join(f"- {s.name}: {s.role}" for s in cfg.seats.values())
    answer = ask(cfg, (
        f"You plan work for the project \"{cfg.project}\".\nGOALS:\n{goals}\n\nSEATS:\n{seats_desc}\n\n"
        f"RECENT TASKS (do not repeat them):\n{recent}\n\n"
        f"Propose at most {int(spec.get('plan_max_tasks', 3))} small, useful tasks that move the goals forward. "
        'Reply with ONLY a JSON list: [{"title": "...", "detail": "what to do and how to check it", "seat": "<seat>"}]'))
    st["last_plan"] = time.time()
    write_json(_state_path(cfg), st)
    created = []
    for item in (extract_json(answer) or [])[: int(spec.get("plan_max_tasks", 3))]:
        if not isinstance(item, dict) or not item.get("title"):
            continue
        seat = item.get("seat") if item.get("seat") in cfg.seats else "any"
        created.append(board.add(cfg, str(item["title"])[:120], str(item.get("detail", ""))[:1500], [seat],
                                 created_by="planner")["id"])
    return created
