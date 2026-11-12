"""What each seat says it is doing.

Agents run ``relay status <seat> --doing "..." --next "..."`` before and after
each task. The dispatcher uses the timestamp to spot seats that went quiet
while there is work to do.
"""

from __future__ import annotations

import time
from typing import Any

from .config import Config
from .store import read_json_lenient, write_json


def _path(cfg: Config, seat: str) -> str:
    return cfg.state("status", f"{seat}.json")


def announce(cfg: Config, seat: str, doing: str = "", next_up: str = "", engine: str = "") -> dict[str, Any]:
    if seat not in cfg.seats:
        raise KeyError(f"unknown seat `{seat}`")
    current = read(cfg, seat)
    entry = {
        "seat": seat,
        "engine": engine or current.get("engine", ""),
        "doing": doing or current.get("doing", ""),
        "next": next_up or current.get("next", ""),
        "epoch": time.time(),
        "at": time.strftime("%Y-%m-%d %H:%M"),
    }
    write_json(_path(cfg, seat), entry)
    return entry


def read(cfg: Config, seat: str) -> dict[str, Any]:
    return read_json_lenient(_path(cfg, seat), {})


def quiet_minutes(cfg: Config, seat: str) -> float:
    """Minutes since the seat last announced anything (very large if never)."""
    epoch = read(cfg, seat).get("epoch")
    return (time.time() - float(epoch)) / 60 if epoch else 1e9
