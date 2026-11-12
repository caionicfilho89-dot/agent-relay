"""Completion gate: a task only becomes ``done`` if it passes here.

Agents are optimistic about their own work. The gate refuses, with a reason
the agent can act on, when:

1. the result is too vague to review (``min_result_chars``);
2. it does not say how the work was verified (``require_test_mention``);
3. it mentions editing a generated/build folder (``forbidden_paths``) — the
   next build would silently erase that work;
4. any configured check command fails (``checks``, run in ``workdir``).

Returns ``""`` when the task passes.
"""

from __future__ import annotations

import re
import subprocess
import sys
from typing import Any

from .config import Config

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def check(cfg: Config, task: dict[str, Any], result: str) -> str:
    gate = cfg.gate
    text = (result or "").strip()
    minimum = int(gate.get("min_result_chars", 0) or 0)
    if len(text) < minimum:
        return (
            f"result too vague ({len(text)} chars, need {minimum}): say WHAT changed, in WHICH files, "
            "and HOW you verified it (e.g. 'src/app.py: fixed X; pytest 42 passed; checked the screen by hand')."
        )
    if gate.get("require_test_mention", True):
        words = [w for w in gate.get("test_words", []) if w]
        if words and not re.search(r"\b(" + "|".join(re.escape(w) for w in words) + r")\b", text, re.I):
            return "say how you verified the work (tests you ran and their result, or what you checked by hand)."
    for folder in gate.get("forbidden_paths", []):
        pattern = r"(^|[\s\\/'\"`(])" + re.escape(folder.rstrip("/\\")) + r"[\\/]"
        if re.search(pattern, text):
            return (
                f"you edited `{folder}`, which is generated output and gets overwritten. "
                "Make the change in the source files and finish the task again."
            )
    timeout = int(gate.get("check_timeout_s", 300))
    for command in gate.get("checks", []):
        failure = _run(cfg, command, timeout)
        if failure:
            return failure
    return ""


def _run(cfg: Config, command: list[str], timeout: int) -> str:
    argv = [sys.executable if part == "{python}" else part for part in command]
    label = " ".join(argv)
    try:
        proc = subprocess.run(
            argv,
            cwd=cfg.workdir,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            creationflags=_NO_WINDOW,
        )
    except FileNotFoundError:
        return f"check `{label}` could not start (command not found) — fix `gate.checks` in relay.json"
    except subprocess.TimeoutExpired:
        return f"check `{label}` ran longer than {timeout}s (hung?)"
    if proc.returncode != 0:
        tail = (proc.stdout + proc.stderr).strip()[-400:]
        return f"check `{label}` failed (exit {proc.returncode}): ...{tail}"
    return ""
