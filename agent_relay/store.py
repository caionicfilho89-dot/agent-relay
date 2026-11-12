"""Safe JSON persistence shared by every agent-relay process.

Several agents (and the dispatcher) read and write the same files at once, so:

* a failed read never turns into "empty" — it retries, then raises. Treating an
  unreadable board as ``[]`` and saving a new task on top of it is how a real
  board of 88 tasks was wiped once;
* writes go to a per-process temp file and are swapped in atomically, retrying
  while Windows reports the target as in use;
* lists can be protected against suddenly shrinking (a classic symptom of the
  bug above) and get a ``.bak`` copy before each write.
"""

from __future__ import annotations

import json
import os
import time
from typing import Any

_MISSING = object()


class StoreError(RuntimeError):
    """A state file exists but could not be read or safely written."""


def read_json(path: str, default: Any = _MISSING, retries: int = 20, delay: float = 0.15) -> Any:
    """Read JSON from ``path``.

    A missing file returns ``default`` (or raises if no default was given). A
    file that exists but cannot be parsed is retried (another process may be
    mid-write) and then raises :class:`StoreError` — never silently ``default``.
    """
    if not os.path.exists(path):
        if default is _MISSING:
            raise StoreError(f"{path} does not exist")
        return default
    error: Exception | None = None
    for _ in range(max(1, retries)):
        try:
            with open(path, encoding="utf-8") as handle:
                return json.load(handle)
        except (OSError, ValueError) as exc:
            error = exc
            time.sleep(delay)
    raise StoreError(f"{path} is unreadable right now; refusing to treat it as empty: {error}")


def read_json_lenient(path: str, default: Any) -> Any:
    """Read JSON for non-critical state (caches, logs): any failure returns ``default``."""
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return default


def write_json(path: str, data: Any, *, guard_shrink: bool = False, backup: bool = False) -> None:
    """Atomically write ``data`` as JSON.

    ``guard_shrink``: refuse to replace a list of 10+ items with one less than
    half its size. ``backup``: keep the previous content in ``<path>.bak``.
    """
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    if (guard_shrink or backup) and os.path.exists(path):
        previous = read_json_lenient(path, None)
        if guard_shrink and isinstance(previous, list) and isinstance(data, list):
            if len(previous) >= 10 and len(data) < len(previous) * 0.5:
                raise StoreError(
                    f"refusing to shrink {os.path.basename(path)} from {len(previous)} to {len(data)} items"
                )
        if backup and previous is not None:
            _atomic_write(path + ".bak", previous)
    _atomic_write(path, data)


def _atomic_write(path: str, data: Any) -> None:
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=1)
    # Windows: os.replace fails with a sharing violation while another process
    # has the target open. Retry for a few seconds before giving up.
    for _ in range(40):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            time.sleep(0.15)
    os.replace(tmp, path)
