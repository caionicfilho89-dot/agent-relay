"""``relay`` command line."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys

from . import __version__, board, deliver, gate, quota, seats, status
from .config import STARTER, Config, ConfigError, load

EPILOG = """examples:
  relay init                                   create relay.json in this folder
  relay add "Fix login bug" --detail "..." --for builder
  relay claim builder                          (agents) take the next task
  relay done builder T03 "auth.py: ...; pytest 41 passed"
  relay hook builder                           (turn-end hook) keep the agent going while there is work
  relay run                                    start the dispatcher loop
"""


def _out(text: str = "") -> None:
    print(text)


def _cfg(args: argparse.Namespace) -> Config:
    return load(args.config)


def cmd_init(args: argparse.Namespace) -> int:
    path = os.path.abspath(args.config or "relay.json")
    if os.path.exists(path) and not args.force:
        _out(f"{path} already exists (use --force to overwrite)")
        return 1
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(STARTER, handle, indent=2)
        handle.write("\n")
    ignore = os.path.join(os.path.dirname(path), ".gitignore")
    lines = open(ignore, encoding="utf-8").read().splitlines() if os.path.exists(ignore) else []
    if ".relay/" not in lines:
        with open(ignore, "a", encoding="utf-8") as handle:
            handle.write(("\n" if lines and lines[-1] else "") + ".relay/\n")
    _out(f"created {path}. Edit the seats and accounts, then run `relay doctor`.")
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    cfg = _cfg(args)
    qs = quota.state(cfg)
    _out(f"agent-relay {__version__} - {cfg.project}")
    _out(f"\nQuota (updated {qs.get('updated', 'never')}" + (f", forced {qs['forced']}" if qs.get("forced") else "") + ")")
    for line in quota.summary(cfg, qs):
        _out("  " + line)
    _out("\nSeats")
    for line in seats.summary(cfg, qs):
        _out("  " + line)
    _out("\nBoard")
    for line in board.summary(cfg) or ["(empty - add tasks with `relay add`)"]:
        _out("  " + line)
    return 0


def cmd_add(args: argparse.Namespace) -> int:
    cfg = _cfg(args)
    who = [w.strip() for w in args.who.split(",")] if args.who else ["any"]
    task = board.add(cfg, args.title, args.detail or "", who, created_by=args.sender or "")
    _out(f"added {task['id']}: {task['title']} [for: {', '.join(task['who'])}]")
    return 0


def cmd_board(args: argparse.Namespace) -> int:
    cfg = _cfg(args)
    if args.json:
        _out(json.dumps(board.load(cfg), ensure_ascii=False, indent=1))
        return 0
    for line in board.summary(cfg, done_limit=args.done) or ["(empty)"]:
        _out(line)
    return 0


def _seat(cfg: Config, name: str) -> str:
    if name not in cfg.seats:
        raise SystemExit(f"unknown seat `{name}` (seats: {', '.join(cfg.seats)})")
    return name


def cmd_claim(args: argparse.Namespace) -> int:
    cfg = _cfg(args)
    seat = _seat(cfg, args.seat)
    qs = quota.state(cfg)
    engine = seats.current_engine(cfg, cfg.seats[seat])
    if quota.level(cfg, engine.account, qs) == "critical":
        _out(f"quota for {engine.account} is critical: finish what you are doing and pause until it recovers.")
        return 2
    task = board.claim(cfg, seat, headroom=lambda s: seats.headroom(cfg, s, qs))
    if not task:
        _out("no task for you right now. Check `relay inbox` and try again in a few minutes.")
        return 3
    status.announce(cfg, seat, doing=f"{task['id']} {task['title']}", engine=engine.name)
    _out(f"{task['id']}: {task['title']}")
    if task.get("detail"):
        _out(task["detail"])
    for r in task.get("refusals", [])[-1:]:
        _out(f"\nLast refusal by the gate: {r['reason']}")
    lvl = quota.level(cfg, engine.account, qs)
    if lvl == "economy":
        _out("\nQuota is getting low: keep it small, avoid reading whole folders, no unnecessary builds.")
    return 0


def cmd_done(args: argparse.Namespace) -> int:
    cfg = _cfg(args)
    seat = _seat(cfg, args.seat)
    result = board.finish(cfg, seat, args.task, args.result, check=lambda t, r: gate.check(cfg, t, r))
    if "refused" in result:
        _out(f"NOT done - the gate refused {args.task}: {result['refused']}")
        return 1
    status.announce(cfg, seat, doing="-", next_up="claim the next task")
    _out(f"{args.task} done.")
    return 0


def cmd_release(args: argparse.Namespace) -> int:
    task = board.release(_cfg(args), args.task)
    _out(f"{task['id']} is open again.")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    cfg = _cfg(args)
    entry = status.announce(cfg, _seat(cfg, args.seat), args.doing or "", getattr(args, "next") or "")
    _out(f"{entry['seat']}: doing {entry['doing'] or '-'}; next {entry['next'] or '-'}")
    return 0


def cmd_ask(args: argparse.Namespace) -> int:
    cfg = _cfg(args)
    item = deliver.ask(cfg, _seat(cfg, args.seat), args.text, args.sender or "")
    _out(f"queued for {item['to']} ({item['id']})")
    return 0


def cmd_inbox(args: argparse.Namespace) -> int:
    cfg = _cfg(args)
    seat = _seat(cfg, args.seat)
    items = deliver.pending(cfg, seat)
    if items:
        deliver.mark_delivered(cfg, [r["id"] for r in items])
        _out(deliver.compose(seat, items))
    else:
        _out("no requests.")
    if cfg.mail.get("enabled"):
        try:
            from . import mail

            unread = mail.inbox(cfg, seat)
            _out(f"team mail: {len(unread)} unread (as {mail.name(cfg, seat)})")
        except Exception as exc:  # noqa: BLE001
            _out(f"team mail unavailable: {exc}")
    return 0


def cmd_hook(args: argparse.Namespace) -> int:
    """Turn-end hook. Always exits 0: a hook must never break the agent."""
    try:
        sys.stdin.read()
    except Exception:  # noqa: BLE001
        pass
    try:
        cfg = _cfg(args)
        seat = _seat(cfg, args.seat)
        engine = seats.current_engine(cfg, cfg.seats[seat])
        if args.format:
            engine.raw["hook_format"] = args.format
        payload = deliver.hook(cfg, seat, engine)
        if payload:
            print(json.dumps(payload, ensure_ascii=False))
    except (Exception, SystemExit):  # noqa: BLE001
        pass
    return 0


def cmd_hook_config(args: argparse.Namespace) -> int:
    cfg = _cfg(args)
    seat = _seat(cfg, args.seat)
    fmt = args.format or seats.current_engine(cfg, cfg.seats[seat]).hook_format
    where = {"claude": "Claude Code: ~/.claude/settings.json (or .claude/settings.json in the project)",
             "cursor": "Cursor: .cursor/hooks.json in the project"}[fmt]
    _out(f"# {where}\n# (Grok CLI: same shape as Claude Code, in ~/.grok/hooks/<name>.json)")
    _out(deliver.hook_install_hint(cfg, seat, fmt))
    return 0


def cmd_quota(args: argparse.Namespace) -> int:
    cfg = _cfg(args)
    action = args.action or "update"
    if action == "update":
        quota.update(cfg)
    elif action == "set":
        if len(args.values) < 3:
            raise SystemExit("usage: relay quota set ACCOUNT WINDOW USED_PCT [--resets ISO]")
        account, window, used = args.values[:3]
        if account not in cfg.accounts:
            raise SystemExit(f"unknown account `{account}`")
        quota.set_usage(cfg, account, window, float(used), args.resets)
        quota.update(cfg)
    elif action == "force":
        level = args.values[0] if args.values else "auto"
        quota.force(cfg, None if level == "auto" else level)
        quota.update(cfg)
    elif action == "exhausted":
        if not args.values:
            raise SystemExit("usage: relay quota exhausted ACCOUNT [--hours N]")
        quota.mark_exhausted(cfg, args.values[0], hours=args.hours)
        quota.update(cfg)
    elif action == "clear":
        if not args.values:
            raise SystemExit("usage: relay quota clear ACCOUNT")
        quota.clear_exhausted(cfg, args.values[0])
        quota.update(cfg)
    for line in quota.summary(cfg):
        _out(line)
    return 0


def cmd_seats(args: argparse.Namespace) -> int:
    cfg = _cfg(args)
    if args.action == "auto":
        for change in seats.auto(cfg) or ["no hand-off needed"]:
            _out(change)
    elif args.action == "switch":
        if len(args.values) != 2:
            raise SystemExit("usage: relay seats switch SEAT ENGINE")
        _out(seats.switch(cfg, _seat(cfg, args.values[0]), args.values[1]))
    elif args.action == "prompt":
        if not args.values:
            raise SystemExit("usage: relay seats prompt SEAT")
        seat = cfg.seats[_seat(cfg, args.values[0])]
        engine = seats.current_engine(cfg, seat)
        _out(seats.prompt_text(cfg, seat, engine))
        if engine.delivery == "cli":
            _out("command: " + " ".join(seats.build_argv(cfg, seat, engine)))
        return 0
    for line in seats.summary(cfg):
        _out(line)
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    from .dispatcher import Dispatcher

    Dispatcher(_cfg(args)).run(once=args.once)
    return 0


def cmd_review(args: argparse.Namespace) -> int:
    from . import reviewer

    cfg = _cfg(args)
    if not cfg.reviewer.get("command"):
        raise SystemExit("configure `reviewer.command` in relay.json first")
    if args.plan:
        _out("planned: " + (", ".join(reviewer.plan(cfg, force=True)) or "nothing"))
    else:
        _out("sent back for rework: " + (", ".join(reviewer.review(cfg, limit=args.limit)) or "nothing"))
    return 0


def cmd_mail(args: argparse.Namespace) -> int:
    from . import mail

    cfg = _cfg(args)
    if args.action == "setup":
        for seat, name in mail.setup(cfg).items():
            _out(f"{seat} = {name}")
    elif args.action == "inbox":
        for m in mail.inbox(cfg, _seat(cfg, args.values[0]), unread_only=False):
            _out(f"- from {m.get('from')}: {m.get('subject')}")
    elif args.action == "send":
        if len(args.values) != 4:
            raise SystemExit("usage: relay mail send FROM TO SUBJECT BODY")
        _out(str(mail.send(cfg, *args.values)))
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    cfg = _cfg(args)
    problems = 0
    _out(f"config: {cfg.path}\nstate:  {cfg.state_dir}\nwork:   {cfg.workdir}")
    if not os.path.isdir(cfg.workdir):
        _out(f"  ! workdir does not exist: {cfg.workdir}")
        problems += 1
    for seat in cfg.seats.values():
        for e in seat.engines:
            line = f"  {seat.name}/{e.name}: {e.delivery}"
            if e.delivery == "cli":
                exe = e.command[0] if e.command else ""
                found = shutil.which(exe) or (os.path.isfile(os.path.expandvars(exe)) and exe)
                line += f" -> {exe} {'found' if found else 'NOT FOUND on PATH'}"
                problems += 0 if found else 1
                if e.autonomous_args:
                    line += " | autonomous args " + ("ON (allow_autonomous=true)" if cfg.allow_autonomous else "off")
            if e.delivery == "window" and sys.platform != "win32":
                line += " -> window delivery only works on Windows"
                problems += 1
            if e.delivery == "hook":
                line += f" ({e.hook_format} format; see `relay hook-config {seat.name}`)"
            _out(line)
    if cfg.allow_autonomous:
        _out("  ! allow_autonomous is ON: cli engines run with their auto-approve flags. Use a sandbox or a "
             "throwaway checkout; agents can run any command your user can.")
    if cfg.mail.get("enabled"):
        try:
            from . import mail

            mail.call(cfg, "ensure_project", human_key=seats.mail_project_key(cfg))
            _out("  mail: server reachable")
        except Exception as exc:  # noqa: BLE001
            _out(f"  ! mail: {exc}")
            problems += 1
    _out("OK" if not problems else f"{problems} problem(s)")
    return 0 if not problems else 1


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="relay", description="Run several AI coding agents as one team.",
                                epilog=EPILOG, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--config", help="path to relay.json (default: search upwards from here)")
    p.add_argument("--version", action="version", version=f"agent-relay {__version__}")
    sub = p.add_subparsers(dest="command")

    s = sub.add_parser("init", help="create a starter relay.json")
    s.add_argument("--force", action="store_true")
    s.set_defaults(fn=cmd_init)
    sub.add_parser("show", help="quota, seats and board at a glance").set_defaults(fn=cmd_show)

    s = sub.add_parser("add", help="add a task to the board")
    s.add_argument("title")
    s.add_argument("--detail", default="")
    s.add_argument("--for", dest="who", help="comma-separated seats (default: any)")
    s.add_argument("--from", dest="sender")
    s.set_defaults(fn=cmd_add)

    s = sub.add_parser("board", help="list tasks")
    s.add_argument("--json", action="store_true")
    s.add_argument("--done", type=int, default=5, help="how many finished tasks to show")
    s.set_defaults(fn=cmd_board)

    s = sub.add_parser("claim", help="(agents) get your current task or claim the next one")
    s.add_argument("seat")
    s.set_defaults(fn=cmd_claim)

    s = sub.add_parser("done", help="(agents) finish a task through the gate")
    s.add_argument("seat")
    s.add_argument("task")
    s.add_argument("result")
    s.set_defaults(fn=cmd_done)

    s = sub.add_parser("release", help="put a claimed task back on the board")
    s.add_argument("task")
    s.set_defaults(fn=cmd_release)

    s = sub.add_parser("status", help="(agents) announce what you are doing")
    s.add_argument("seat")
    s.add_argument("--doing")
    s.add_argument("--next")
    s.set_defaults(fn=cmd_status)

    s = sub.add_parser("ask", help="queue a request for a seat")
    s.add_argument("seat")
    s.add_argument("text")
    s.add_argument("--from", dest="sender")
    s.set_defaults(fn=cmd_ask)

    s = sub.add_parser("inbox", help="(agents) read requests for your seat")
    s.add_argument("seat")
    s.set_defaults(fn=cmd_inbox)

    s = sub.add_parser("hook", help="turn-end hook for Claude Code / Grok CLI / Cursor")
    s.add_argument("seat")
    s.add_argument("--format", choices=["claude", "cursor"])
    s.set_defaults(fn=cmd_hook)

    s = sub.add_parser("hook-config", help="print the hook snippet to install for a seat")
    s.add_argument("seat")
    s.add_argument("--format", choices=["claude", "cursor"])
    s.set_defaults(fn=cmd_hook_config)

    s = sub.add_parser("quota", help="measure or adjust quota (update|set|force|exhausted|clear)")
    s.add_argument("action", nargs="?", choices=["update", "set", "force", "exhausted", "clear"])
    s.add_argument("values", nargs="*")
    s.add_argument("--resets", help="ISO time the window resets (for `set`)")
    s.add_argument("--hours", type=float, default=24, help="for `exhausted` (default 24)")
    s.set_defaults(fn=cmd_quota)

    s = sub.add_parser("seats", help="show seats, or hand off (auto|switch SEAT ENGINE|prompt SEAT)")
    s.add_argument("action", nargs="?", choices=["auto", "switch", "prompt"])
    s.add_argument("values", nargs="*")
    s.set_defaults(fn=cmd_seats)

    s = sub.add_parser("run", help="start the dispatcher loop")
    s.add_argument("--once", action="store_true", help="run a single tick and exit")
    s.set_defaults(fn=cmd_run)

    s = sub.add_parser("review", help="run the AI reviewer (or the planner with --plan) once")
    s.add_argument("--plan", action="store_true")
    s.add_argument("--limit", type=int, default=3)
    s.set_defaults(fn=cmd_review)

    s = sub.add_parser("mail", help="team mail via MCP Agent Mail (setup|inbox SEAT|send FROM TO SUBJECT BODY)")
    s.add_argument("action", choices=["setup", "inbox", "send"])
    s.add_argument("values", nargs="*")
    s.set_defaults(fn=cmd_mail)

    sub.add_parser("doctor", help="check the config and the tools it needs").set_defaults(fn=cmd_doctor)
    return p


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            pass
    parser = build_parser()
    args = parser.parse_args(argv)
    fn = getattr(args, "fn", None) or cmd_show
    try:
        return int(fn(args) or 0)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2
    except (KeyError, PermissionError, ValueError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
