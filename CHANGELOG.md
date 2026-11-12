# Changelog

## 0.1.0 (unreleased)

First public version.

- Seats with engines in preference order and automatic hand-off by quota (with cooldown).
- Shared task board with safe concurrent writes, quota-aware claiming and a completion gate.
- Quota sources: `file`, `command`, `grok_log`, `agy`, `none`; pace forecast and daily budget; "quota reached" detection.
- Delivery through stop hooks (Claude Code, Grok CLI, Cursor), launched CLI agents, and Windows desktop apps.
- Optional AI reviewer/planner, MCP Agent Mail integration and Claude Code Agent Monitor bridge.
- `relay` CLI: init, show, add, board, claim, done, release, status, ask, inbox, hook, hook-config, quota, seats, run, review, mail, doctor.
