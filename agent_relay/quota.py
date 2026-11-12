"""Quota tracking per subscription ("account") and the economy level.

Each account in ``relay.json`` has a ``source`` that reports usage windows::

    {"windows": [{"name": "5h", "used_pct": 42, "resets_at": "2026-01-01T18:00:00Z"}]}

Sources:

* ``none``    – not measured (always ``normal``)
* ``file``    – read that JSON from ``path`` (default ``.relay/usage/<account>.json``);
                ``relay quota set`` writes it for you
* ``command`` – run ``command`` and parse that JSON from its stdout
* ``grok_log``– read ``creditUsagePercent`` from the Grok CLI log
* ``agy``     – ask the Antigravity CLI for per-model quota (cheapest model)

Levels per window, worst one wins for the account:

* ``critical`` – used >= 90%, or the current pace hits 100% before the reset
                 and it is already >= 75%; or the account was marked exhausted
* ``economy``  – used >= 70%, or the pace hits 100% before the reset
* ``normal``

Windows that reset more than 36 h away (weekly limits) also get a *daily
budget*: what was left at midnight divided by the days until the reset.
Spending more than the budget today raises the level even if the weekly
percentage still looks fine.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import shutil
import subprocess
from typing import Any, Callable

from .config import LEVELS, Config
from .store import read_json_lenient, write_json

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
LONG_WINDOW_H = 36


def now() -> dt.datetime:
    return dt.datetime.now().astimezone()


def parse_time(value: Any) -> dt.datetime | None:
    if not value:
        return None
    try:
        return dt.datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone()
    except ValueError:
        return None


def worse(a: str, b: str) -> str:
    return a if LEVELS.index(a) >= LEVELS.index(b) else b


# ---------------------------------------------------------------- sources
def _usage_file(cfg: Config, account: str) -> str:
    return cfg.state("usage", f"{account}.json")


def _windows_from(data: Any) -> list[dict[str, Any]]:
    windows = data.get("windows", []) if isinstance(data, dict) else []
    out = []
    for w in windows:
        try:
            out.append({"name": str(w["name"]), "used_pct": float(w["used_pct"]), "resets_at": w.get("resets_at")})
        except (KeyError, TypeError, ValueError):
            continue
    return out


def measure_file(cfg: Config, account: str, spec: dict[str, Any]) -> list[dict[str, Any]]:
    path = spec.get("path") or _usage_file(cfg, account)
    path = os.path.expanduser(os.path.expandvars(path))
    if not os.path.isabs(path):
        path = os.path.join(cfg.root, path)
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as handle:
        return _windows_from(json.load(handle))


def measure_command(cfg: Config, account: str, spec: dict[str, Any]) -> list[dict[str, Any]]:
    proc = subprocess.run(
        spec["command"], cwd=cfg.root, capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=int(spec.get("timeout_s", 60)), creationflags=_NO_WINDOW,
    )
    match = re.search(r"\{.*\}", proc.stdout or "", re.S)
    if proc.returncode != 0 or not match:
        raise RuntimeError(f"usage command failed (exit {proc.returncode})")
    return _windows_from(json.loads(match.group()))


def measure_grok_log(cfg: Config, account: str, spec: dict[str, Any]) -> list[dict[str, Any]]:
    path = os.path.expanduser(spec.get("path", "~/.grok/logs/unified.jsonl"))
    pct = end = None
    with open(path, encoding="utf-8", errors="replace") as handle:
        for line in handle:
            if "creditUsagePercent" not in line:
                continue
            try:
                conf = json.loads(line)["ctx"]["config"]
                pct, end = float(conf["creditUsagePercent"]), (conf.get("currentPeriod") or {}).get("end")
            except (ValueError, KeyError, TypeError):
                continue
    return [] if pct is None else [{"name": spec.get("window", "period"), "used_pct": pct, "resets_at": end}]


AGY_PROMPT = (
    "Use the swarm tool with action models. Reply ONLY with one JSON object mapping each model name to "
    "{quota_remaining, quota_resets}. No other text."
)


def agy_binary(spec: dict[str, Any] | None = None) -> str:
    spec = spec or {}
    if spec.get("agy"):
        return os.path.expanduser(os.path.expandvars(spec["agy"]))
    found = shutil.which("agy")
    if found:
        return found
    return os.path.join(os.environ.get("LOCALAPPDATA", ""), "agy", "bin", "agy.exe")


def agy_models(spec: dict[str, Any] | None = None) -> dict[str, Any]:
    """Per-model quota as reported by the Antigravity CLI (``{model: {quota_remaining, quota_resets}}``)."""
    spec = spec or {}
    proc = subprocess.run(
        [agy_binary(spec), "-p", AGY_PROMPT, "--model", spec.get("probe_model", "gemini-3.8-flash-low"),
         "--print-timeout", "120s"],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=170,
        cwd=os.path.expanduser("~"), creationflags=_NO_WINDOW,
    )
    match = re.search(r"\{.*\}", proc.stdout or "", re.S)
    if not match:
        raise RuntimeError("agy did not return quota JSON")
    return json.loads(match.group())


def _pct(value: Any) -> float:
    return float(str(value).strip().rstrip("%"))


def measure_agy(cfg: Config, account: str, spec: dict[str, Any]) -> list[dict[str, Any]]:
    models = agy_models(spec)
    prefix = spec.get("model_prefix", "")
    chosen = {k: v for k, v in models.items() if k.startswith(prefix)} or models
    remaining = min(_pct(v.get("quota_remaining", 0)) for v in chosen.values())
    resets = next((v.get("quota_resets") for v in chosen.values() if v.get("quota_resets")), None)
    return [{"name": spec.get("window", "short"), "used_pct": round(100 - remaining, 1), "resets_at": resets}]


SOURCES: dict[str, Callable[[Config, str, dict[str, Any]], list[dict[str, Any]]]] = {
    "none": lambda cfg, account, spec: [],
    "file": measure_file,
    "command": measure_command,
    "grok_log": measure_grok_log,
    "agy": measure_agy,
}


# ---------------------------------------------------------------- levels
def forecast(history: list[dict[str, Any]], account: str, window: str, used: float, resets_at: Any):
    """Pace (points/hour over the last 45 min) and when 100% is hit, if before the reset."""
    t_now = now()
    points = []
    for h in history:
        t = parse_time(h.get("t"))
        if h.get("account") == account and h.get("window") == window and t and (t_now - t).total_seconds() <= 2700:
            points.append((t, float(h["pct"])))
    if len(points) < 2:
        return None, None
    (t0, p0), (t1, p1) = points[0], points[-1]
    hours = (t1 - t0).total_seconds() / 3600
    if hours < 0.1 or p1 < p0:  # too little data, or the window reset in between
        return None, None
    pace = (p1 - p0) / hours
    if pace <= 0.05:
        return pace, None
    hits_full = t_now + dt.timedelta(hours=(100 - used) / pace)
    reset = parse_time(resets_at)
    return pace, (hits_full if reset and hits_full < reset else None)


def daily_budget(history: list[dict[str, Any]], account: str, window: str, used: float, resets_at: Any):
    """(used today, budget today) in percentage points for long windows, or (None, None)."""
    reset = parse_time(resets_at)
    if not reset:
        return None, None
    t_now = now()
    if (reset - t_now).total_seconds() < LONG_WINDOW_H * 3600:
        return None, None
    midnight = t_now.replace(hour=0, minute=0, second=0, microsecond=0)
    today = sorted(
        (parse_time(h["t"]), float(h["pct"])) for h in history
        if h.get("account") == account and h.get("window") == window and parse_time(h.get("t"))
        and parse_time(h["t"]) >= midnight
    )
    start = today[0][1] if today else used
    if used < start:  # reset happened today
        start = 0.0
    days = max(1.0, (reset - midnight).total_seconds() / 86400)
    return round(used - start, 1), round((100 - start) / days, 1)


def level_for(used: float, full_at: Any) -> str:
    if used >= 90 or (full_at is not None and used >= 75):
        return "critical"
    if used >= 70 or full_at is not None:
        return "economy"
    return "normal"


def level_for_budget(spent: float | None, budget: float | None) -> str:
    if spent is None or not budget:
        return "normal"
    if spent >= budget * 1.25:
        return "critical"
    if spent >= budget:
        return "economy"
    return "normal"


# ---------------------------------------------------------------- state
def _state_path(cfg: Config) -> str:
    return cfg.state("quota.json")


def _history_path(cfg: Config) -> str:
    return cfg.state("quota_history.json")


def _overrides_path(cfg: Config) -> str:
    return cfg.state("quota_overrides.json")


def state(cfg: Config) -> dict[str, Any]:
    return read_json_lenient(_state_path(cfg), {"accounts": {}})


def update(cfg: Config, sources: dict[str, Callable] | None = None) -> dict[str, Any]:
    """Measure every account, store a history sample and the resulting levels. Never raises."""
    sources = sources or SOURCES
    old = state(cfg)
    forced = old.get("forced")
    history = read_json_lenient(_history_path(cfg), [])
    overrides = read_json_lenient(_overrides_path(cfg), {})
    t_now = now()
    accounts: dict[str, Any] = {}
    for name, spec in cfg.accounts.items():
        measure = sources.get(spec["source"])
        if measure is None:
            accounts[name] = {"level": "normal", "windows": [], "note": f"unknown source `{spec['source']}`"}
            continue
        try:
            windows = measure(cfg, name, spec)
        except Exception as exc:  # noqa: BLE001 — no reading: keep the last known level
            previous = dict(old.get("accounts", {}).get(name) or {"level": "normal", "windows": []})
            previous["note"] = f"no reading now ({str(exc)[:80]})"
            accounts[name] = previous
            continue
        account = {"level": "normal", "windows": []}
        for w in windows:
            history.append({"t": t_now.isoformat(), "account": name, "window": w["name"], "pct": w["used_pct"]})
            pace, full_at = forecast(history, name, w["name"], w["used_pct"], w.get("resets_at"))
            lvl = level_for(w["used_pct"], full_at)
            spent, budget = daily_budget(history, name, w["name"], w["used_pct"], w.get("resets_at"))
            lvl = worse(lvl, level_for_budget(spent, budget))
            w.update(level=lvl, pace_per_h=round(pace, 1) if pace else None,
                     full_at=full_at.isoformat() if full_at else None, spent_today=spent, budget_today=budget)
            account["windows"].append(w)
            account["level"] = worse(account["level"], lvl)
        exhausted = parse_time((overrides.get(name) or {}).get("exhausted_until"))
        if exhausted and exhausted > t_now:
            account["level"] = "critical"
            account["note"] = f"marked exhausted until {exhausted.isoformat(timespec='minutes')}"
        accounts[name] = account
    cutoff = t_now - dt.timedelta(hours=26)
    history = [h for h in history if (parse_time(h.get("t")) or t_now) >= cutoff][-5000:]
    write_json(_history_path(cfg), history)
    if forced in LEVELS:
        for a in accounts.values():
            a["level"] = forced
    new = {"updated": t_now.isoformat(timespec="seconds"), "forced": forced, "accounts": accounts}
    write_json(_state_path(cfg), new)
    return new


def force(cfg: Config, level: str | None) -> None:
    current = state(cfg)
    current["forced"] = level if level in LEVELS else None
    write_json(_state_path(cfg), current)


def set_usage(cfg: Config, account: str, window: str, used_pct: float, resets_at: str | None = None) -> None:
    """Record a usage reading for a ``file`` account (e.g. what an app's settings screen shows)."""
    path = _usage_file(cfg, account)
    data = read_json_lenient(path, {"windows": []})
    data["windows"] = [w for w in data.get("windows", []) if w.get("name") != window]
    data["windows"].append({"name": window, "used_pct": float(used_pct), "resets_at": resets_at})
    write_json(path, data)


def mark_exhausted(cfg: Config, account: str, until: dt.datetime | None = None, hours: float = 24) -> None:
    """Force an account to ``critical`` until a time (e.g. a CLI printed "quota reached")."""
    overrides = read_json_lenient(_overrides_path(cfg), {})
    until = until or now() + dt.timedelta(hours=hours)
    overrides.setdefault(account, {})["exhausted_until"] = until.isoformat()
    write_json(_overrides_path(cfg), overrides)


def clear_exhausted(cfg: Config, account: str) -> None:
    overrides = read_json_lenient(_overrides_path(cfg), {})
    overrides.pop(account, None)
    write_json(_overrides_path(cfg), overrides)


EXHAUSTED_PATTERNS = [r"quota (reached|exceeded|exhausted)", r"rate limit(ed)? reached", r"usage limit"]


def detect_exhausted(cfg: Config, account: str | None, output: str) -> bool:
    """If a tool's output says the quota ran out, mark the account exhausted and return True."""
    if not account or account not in cfg.accounts:
        return False
    patterns = cfg.accounts[account].get("exhausted_patterns", EXHAUSTED_PATTERNS)
    if any(re.search(p, output or "", re.I) for p in patterns):
        mark_exhausted(cfg, account, hours=float(cfg.accounts[account].get("exhausted_hours", 24)))
        return True
    return False


def level(cfg: Config, account: str | None, current: dict[str, Any] | None = None) -> str:
    if not account:
        return "normal"
    current = current if current is not None else state(cfg)
    return (current.get("accounts", {}).get(account) or {}).get("level", "normal")


def headroom(cfg: Config, account: str | None, current: dict[str, Any] | None = None) -> float:
    """Percent left (0-100) of the tightest long window; critical = 0; unmeasured = 50."""
    current = current if current is not None else state(cfg)
    if level(cfg, account, current) == "critical":
        return 0.0
    windows = (current.get("accounts", {}).get(account or "") or {}).get("windows", [])
    if not windows:
        return 50.0
    return max(0.0, 100.0 - max(float(w["used_pct"]) for w in windows))


def summary(cfg: Config, current: dict[str, Any] | None = None) -> list[str]:
    current = current if current is not None else state(cfg)
    icon = {"normal": "OK  ", "economy": "LOW ", "critical": "OUT "}
    lines = []
    for name in cfg.accounts:
        a = current.get("accounts", {}).get(name) or {"level": "normal", "windows": []}
        parts = []
        for w in a.get("windows", []):
            p = f"{w['name']} {float(w['used_pct']):.0f}%"
            if w.get("budget_today"):
                p += f" (today {w['spent_today']:.0f} of {w['budget_today']:.0f} pts)"
            if w.get("full_at"):
                p += " (runs out before reset at this pace)"
            parts.append(p)
        note = f" - {a['note']}" if a.get("note") else ""
        label = cfg.accounts[name].get("label", name)
        lines.append(f"{icon[a.get('level', 'normal')]}{label}: {a.get('level', 'normal')}"
                     + (f" | {', '.join(parts)}" if parts else "") + note)
    return lines
