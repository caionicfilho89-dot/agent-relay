"""Load and validate ``relay.json``.

The config is found by walking up from the current directory (like git finds
``.git``), or from ``$AGENT_RELAY_CONFIG``. All runtime state lives in
``state_dir`` (default ``.relay/`` next to the config), so one project = one
folder you can delete to reset.
"""

from __future__ import annotations

import copy
import json
import os
from dataclasses import dataclass, field
from typing import Any

CONFIG_NAME = "relay.json"

DEFAULT_TIMING = {
    "tick_s": 30,  # dispatcher loop interval
    "quota_every_s": 180,  # how often quota sources are measured
    "seats_every_s": 300,  # how often seat hand-off is evaluated
    "switch_cooldown_min": 30,  # minimum time between two hand-offs of the same seat
    "idle_min": 10,  # a seat with no status update for this long and a free task gets a nudge
    "nudge_every_min": 30,  # at most one nudge per seat in this window
    "user_idle_s": 3,  # window delivery waits until the keyboard/mouse were idle this long
    "economy_delivery_gap_min": 10,  # in "economy" level, at most one delivery per seat per gap
    "review_every_s": 600,
    "hook_continue_limit": 20,  # max automatic "keep going" answers per seat per hour
}

DEFAULT_GATE = {
    "min_result_chars": 60,
    "require_test_mention": True,
    "test_words": ["test", "tests", "tested", "pytest", "passed", "verified", "checked", "selftest", "ok"],
    "forbidden_paths": [],
    "checks": [],
    "check_timeout_s": 300,
}

LEVELS = ("normal", "economy", "critical")
DELIVERIES = ("hook", "window", "cli", "manual")
HOOK_FORMATS = ("claude", "cursor")


class ConfigError(ValueError):
    """relay.json is missing or invalid."""


@dataclass
class Engine:
    """One way of filling a seat: an agent app, a CLI, or a hook-driven tool."""

    name: str
    account: str | None
    delivery: str
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def hook_format(self) -> str:
        return self.raw.get("hook_format", "claude")

    @property
    def window(self) -> dict[str, Any]:
        return self.raw.get("window", {})

    @property
    def command(self) -> list[str]:
        return list(self.raw.get("command", []))

    @property
    def autonomous_args(self) -> list[str]:
        return list(self.raw.get("autonomous_args", []))


@dataclass
class Seat:
    """A role on the team. Tasks are assigned to seats, not to products."""

    name: str
    role: str
    engines: list[Engine]
    label: str = ""

    def engine(self, name: str) -> Engine | None:
        return next((e for e in self.engines if e.name == name), None)


@dataclass
class Config:
    path: str
    root: str
    project: str
    workdir: str
    state_dir: str
    allow_autonomous: bool
    seats: dict[str, Seat]
    accounts: dict[str, dict[str, Any]]
    gate: dict[str, Any]
    reviewer: dict[str, Any]
    mail: dict[str, Any]
    monitor: dict[str, Any]
    timing: dict[str, Any]
    instructions: str = ""

    def state(self, *parts: str) -> str:
        return os.path.join(self.state_dir, *parts)


def find_config(start: str | None = None) -> str:
    env = os.environ.get("AGENT_RELAY_CONFIG")
    if env:
        if not os.path.isfile(env):
            raise ConfigError(f"AGENT_RELAY_CONFIG points to a missing file: {env}")
        return os.path.abspath(env)
    here = os.path.abspath(start or os.getcwd())
    while True:
        candidate = os.path.join(here, CONFIG_NAME)
        if os.path.isfile(candidate):
            return candidate
        parent = os.path.dirname(here)
        if parent == here:
            raise ConfigError(f"no {CONFIG_NAME} found here or in any parent folder (run `relay init`)")
        here = parent


def load(path: str | None = None) -> Config:
    path = os.path.abspath(path) if path else find_config()
    try:
        with open(path, encoding="utf-8") as handle:
            raw = json.load(handle)
    except (OSError, ValueError) as exc:
        raise ConfigError(f"cannot read {path}: {exc}") from exc
    return parse(raw, path)


def parse(raw: dict[str, Any], path: str) -> Config:
    if not isinstance(raw, dict):
        raise ConfigError("relay.json must be a JSON object")
    root = os.path.dirname(os.path.abspath(path))

    def resolve(p: str) -> str:
        p = os.path.expanduser(os.path.expandvars(p))
        return p if os.path.isabs(p) else os.path.normpath(os.path.join(root, p))

    accounts = raw.get("accounts") or {}
    if not isinstance(accounts, dict):
        raise ConfigError("`accounts` must be an object of name -> source settings")
    for name, acc in accounts.items():
        if not isinstance(acc, dict) or "source" not in acc:
            raise ConfigError(f"account `{name}` needs a `source`")

    seats: dict[str, Seat] = {}
    raw_seats = raw.get("seats") or {}
    if not isinstance(raw_seats, dict) or not raw_seats:
        raise ConfigError("define at least one seat under `seats`")
    for seat_name, s in raw_seats.items():
        if not _is_key(seat_name):
            raise ConfigError(f"seat name `{seat_name}` must be lowercase letters, digits, - or _")
        engines = []
        for e in s.get("engines") or []:
            delivery = e.get("delivery")
            if delivery not in DELIVERIES:
                raise ConfigError(f"seat `{seat_name}`: engine delivery must be one of {DELIVERIES}")
            account = e.get("account")
            if account is not None and account not in accounts:
                raise ConfigError(f"seat `{seat_name}`: engine `{e.get('name')}` uses unknown account `{account}`")
            if delivery == "cli" and not e.get("command"):
                raise ConfigError(f"seat `{seat_name}`: cli engine `{e.get('name')}` needs a `command` list")
            if delivery == "window" and not (e.get("window") or {}).get("process"):
                raise ConfigError(f"seat `{seat_name}`: window engine `{e.get('name')}` needs `window.process`")
            if delivery == "hook" and e.get("hook_format", "claude") not in HOOK_FORMATS:
                raise ConfigError(f"seat `{seat_name}`: hook_format must be one of {HOOK_FORMATS}")
            if not e.get("name"):
                raise ConfigError(f"seat `{seat_name}`: every engine needs a `name`")
            engines.append(Engine(name=e["name"], account=account, delivery=delivery, raw=copy.deepcopy(e)))
        if not engines:
            raise ConfigError(f"seat `{seat_name}` needs at least one engine")
        seats[seat_name] = Seat(name=seat_name, role=s.get("role", ""), engines=engines, label=s.get("label", seat_name))

    reviewer = dict(raw.get("reviewer") or {})
    if reviewer.get("enabled") and not reviewer.get("command"):
        raise ConfigError("`reviewer.command` is required when the reviewer is enabled")
    if reviewer.get("account") and reviewer["account"] not in accounts:
        raise ConfigError(f"reviewer uses unknown account `{reviewer['account']}`")

    timing = dict(DEFAULT_TIMING)
    timing.update(raw.get("timing") or {})
    gate = dict(DEFAULT_GATE)
    gate.update(raw.get("gate") or {})

    state_dir = resolve(raw.get("state_dir", ".relay"))
    return Config(
        path=os.path.abspath(path),
        root=root,
        project=raw.get("project") or os.path.basename(root),
        workdir=resolve(raw.get("workdir", ".")),
        state_dir=state_dir,
        allow_autonomous=bool(raw.get("allow_autonomous", False)),
        seats=seats,
        accounts=accounts,
        gate=gate,
        reviewer=reviewer,
        mail=dict(raw.get("mail") or {}),
        monitor=dict(raw.get("monitor") or {}),
        timing=timing,
        instructions=raw.get("instructions", ""),
    )


def _is_key(name: str) -> bool:
    return bool(name) and all(c.islower() or c.isdigit() or c in "-_" for c in name)


STARTER = {
    "project": "My project",
    "workdir": ".",
    "allow_autonomous": False,
    "instructions": "Read README.md first. Keep changes small and run the tests before finishing a task.",
    "accounts": {
        "claude": {"source": "file", "label": "Claude subscription"},
        "backup": {"source": "none", "label": "Backup agent (not measured)"},
    },
    "seats": {
        "builder": {
            "label": "Builder",
            "role": "Implements tasks from the board.",
            "engines": [
                {"name": "claude-code", "account": "claude", "delivery": "hook", "hook_format": "claude"},
                {
                    "name": "backup-cli",
                    "account": "backup",
                    "delivery": "cli",
                    "command": ["my-agent-cli", "--prompt", "{prompt}"],
                    "autonomous_args": [],
                },
            ],
        }
    },
    "gate": {"checks": [["python", "-m", "pytest", "-q"]], "forbidden_paths": ["dist/", "build/"]},
    "reviewer": {"enabled": False},
    "mail": {"enabled": False},
    "monitor": {"enabled": False},
}
