"""Getting work to agents.

* **Requests**: ``relay ask <seat> "text"`` queues a message for a seat.
* **Hooks**: agents with a turn-end hook (Claude Code ``Stop``, Grok CLI
  ``Stop``, Cursor ``stop``) run ``relay hook <seat>``. If there is mail, a
  request, or a free task (and the quota allows it), the hook tells the agent
  to keep going with that text instead of stopping.
* **Windows**: desktop apps without hooks get the text typed into their chat
  box by ``scripts/window_paste.ps1``, which checks that it was really sent.

Every delivery respects the economy level of the engine's account: nothing in
``critical``, at most one delivery per ``economy_delivery_gap_min`` in
``economy``. Hooks also have a per-hour cap so an agent can never be kept in
an endless loop.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
import uuid
from typing import Any

from . import board, quota, status
from .config import Config, Engine
from .procs import IS_WINDOWS
from .store import read_json, read_json_lenient, write_json

SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "scripts", "window_paste.ps1")
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


# ---------------------------------------------------------------- requests
def _requests_path(cfg: Config) -> str:
    return cfg.state("requests.json")


def ask(cfg: Config, seat: str, text: str, sender: str = "") -> dict[str, Any]:
    if seat not in cfg.seats:
        raise KeyError(f"unknown seat `{seat}`")
    items = read_json(_requests_path(cfg), default=[])
    item = {"id": uuid.uuid4().hex[:8], "to": seat, "from": sender, "text": text.strip(),
            "at": time.strftime("%Y-%m-%d %H:%M"), "delivered": False}
    items.append(item)
    write_json(_requests_path(cfg), items[-500:])
    return item


def pending(cfg: Config, seat: str) -> list[dict[str, Any]]:
    return [r for r in read_json(_requests_path(cfg), default=[]) if r["to"] == seat and not r["delivered"]]


def mark_delivered(cfg: Config, ids: list[str]) -> None:
    items = read_json(_requests_path(cfg), default=[])
    for r in items:
        if r["id"] in ids:
            r.update(delivered=True, delivered_at=time.strftime("%Y-%m-%d %H:%M"))
    write_json(_requests_path(cfg), items)


def compose(seat: str, items: list[dict[str, Any]]) -> str:
    lines = [f"[agent-relay] {len(items)} request(s) for the {seat} seat:"]
    for r in items:
        lines.append(f"- ({r['from'] or 'team'}) {r['text']}")
    lines.append("Do them, then continue with your board loop.")
    return "\n".join(lines)


def nudge_text(cfg: Config, seat: str, task: dict[str, Any]) -> str:
    relay = f'relay --config "{cfg.path}"'
    return (f"[agent-relay] The {seat} seat has a free task: {task['id']} {task['title']}. "
            f"Run `{relay} claim {seat}`, do it, then `{relay} done {seat} {task['id']} \"what changed + how you tested\"`.")


# ---------------------------------------------------------------- economy
def _deliveries_path(cfg: Config) -> str:
    return cfg.state("deliveries.json")


def allowed(cfg: Config, seat: str, engine: Engine, quota_state: dict[str, Any] | None = None) -> bool:
    lvl = quota.level(cfg, engine.account, quota_state)
    if lvl == "critical":
        return False
    if lvl == "economy":
        last = read_json_lenient(_deliveries_path(cfg), {}).get(seat, 0)
        return time.time() - float(last) >= float(cfg.timing["economy_delivery_gap_min"]) * 60
    return True


def _note_delivery(cfg: Config, seat: str) -> None:
    data = read_json_lenient(_deliveries_path(cfg), {})
    data[seat] = time.time()
    write_json(_deliveries_path(cfg), data)


# ---------------------------------------------------------------- hooks
def _hook_budget_ok(cfg: Config, seat: str) -> bool:
    path = cfg.state("hook_log.json")
    log = read_json_lenient(path, {})
    recent = [t for t in log.get(seat, []) if time.time() - t < 3600]
    if len(recent) >= int(cfg.timing["hook_continue_limit"]):
        return False
    recent.append(time.time())
    log[seat] = recent
    write_json(path, log)
    return True


def continue_payload(fmt: str, text: str) -> dict[str, Any]:
    """The turn-end hook answer that makes each agent keep going with ``text``."""
    if fmt == "cursor":
        return {"followup_message": text}
    return {"decision": "block", "reason": text}  # Claude Code and Grok CLI


def hook(cfg: Config, seat: str, engine: Engine | None = None, mail_inbox=None) -> dict[str, Any] | None:
    """Decide what a hook-driven agent should do when its turn ends.

    Returns the JSON payload to print, or None to let the agent stop.
    """
    from . import seats as seats_mod

    seat_obj = cfg.seats[seat]
    engine = engine or seats_mod.current_engine(cfg, seat_obj)
    if not allowed(cfg, seat, engine):
        return None
    text = ""
    if cfg.mail.get("enabled"):
        try:
            inbox = mail_inbox(seat) if mail_inbox else _mail_inbox(cfg, seat)
        except Exception:  # noqa: BLE001 — mail server down must not block the agent
            inbox = []
        if inbox:
            senders = ", ".join(sorted({str(m.get("from", "?")) for m in inbox}))
            text = (f"[agent-relay] You have {len(inbox)} unread mail message(s) from {senders}. "
                    "Fetch your inbox, do what they ask, reply, and acknowledge them.")
    if not text:
        items = pending(cfg, seat)
        if items:
            mark_delivered(cfg, [r["id"] for r in items])
            text = compose(seat, items)
    if not text and board.claimed_by(cfg, seat) is None:
        free = board.open_for(cfg, seat)
        if free:
            text = nudge_text(cfg, seat, free[0])
    if not text or not _hook_budget_ok(cfg, seat):
        return None
    _note_delivery(cfg, seat)
    status.announce(cfg, seat, engine=engine.name)
    return continue_payload(engine.hook_format, text)


def _mail_inbox(cfg: Config, seat: str) -> list[dict[str, Any]]:
    from . import mail

    return mail.inbox(cfg, seat)


# ---------------------------------------------------------------- windows
def window_paste(cfg: Config, engine: Engine, text: str) -> tuple[bool, str]:
    """Type ``text`` into a desktop agent's chat box and confirm it was sent."""
    if not IS_WINDOWS:
        return False, "window delivery is only available on Windows"
    w = engine.window
    os.makedirs(cfg.state("outbox"), exist_ok=True)
    message = cfg.state("outbox", f"{engine.name}.txt")
    with open(message, "w", encoding="utf-8") as handle:
        handle.write(text)
    args = ["-Process", str(w["process"]), "-File", message,
            "-Title", str(w.get("title", "")),
            "-InputFromBottom", str(int(w.get("input_from_bottom", 80))),
            "-Send", str(w.get("send", "enter"))]
    if w.get("send") == "button":
        args += ["-ButtonFromRight", str(int(w.get("button_from_right", 80))),
                 "-ButtonFromBottom", str(int(w.get("button_from_bottom", 70)))]
    if w.get("executable"):
        args += ["-Executable", str(w["executable"])]
    proc = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", SCRIPT, *args],
                          capture_output=True, text=True, timeout=90, creationflags=_NO_WINDOW)
    out = ((proc.stdout or "") + (proc.stderr or "")).strip()
    return out.startswith("SENT"), out[-300:]


def deliver_windows(cfg: Config, user_idle_s: float, paste=window_paste) -> list[str]:
    """Deliver queued requests and nudges to seats currently filled by a window engine."""
    from . import seats as seats_mod

    if user_idle_s < float(cfg.timing["user_idle_s"]):
        return []
    results = []
    quota_state = quota.state(cfg)
    nudges = read_json_lenient(cfg.state("nudges.json"), {})
    for seat in cfg.seats.values():
        engine = seats_mod.current_engine(cfg, seat)
        if engine.delivery != "window" or not allowed(cfg, seat.name, engine, quota_state):
            continue
        items = pending(cfg, seat.name)
        text = compose(seat.name, items) if items else ""
        if not text and board.claimed_by(cfg, seat.name) is None:
            free = board.open_for(cfg, seat.name)
            quiet = status.quiet_minutes(cfg, seat.name)
            recently = time.time() - float(nudges.get(seat.name, 0)) < float(cfg.timing["nudge_every_min"]) * 60
            if free and quiet >= float(cfg.timing["idle_min"]) and not recently:
                text = nudge_text(cfg, seat.name, free[0])
        if not text:
            continue
        ok, detail = paste(cfg, engine, text)
        if ok:
            if items:
                mark_delivered(cfg, [r["id"] for r in items])
            else:
                nudges[seat.name] = time.time()
            _note_delivery(cfg, seat.name)
        results.append(f"{seat.name} via {engine.name}: {'sent' if ok else 'not sent'} - {detail}")
    write_json(cfg.state("nudges.json"), nudges)
    return results


def hook_install_hint(cfg: Config, seat: str, fmt: str) -> str:
    command = f'relay --config "{cfg.path}" hook {seat}'
    if fmt == "cursor":
        return json.dumps({"version": 1, "hooks": {"stop": [{"command": command, "timeout": 30, "loop_limit": 8}]}},
                          indent=2)
    return json.dumps({"hooks": {"Stop": [{"hooks": [{"type": "command", "command": command, "timeout": 30}]}]}},
                      indent=2)
