"""Team mail through MCP Agent Mail (optional).

`MCP Agent Mail <https://github.com/Dicklesworthstone/mcp_agent_mail>`_ gives
agents inboxes, threads and file reservations over MCP. Agents that speak MCP
use it directly; this module lets scripts (the dispatcher, the hook, the
reviewer) read and send mail too. Install with ``pip install agent-relay[mail]``.

Agent Mail requires adjective+noun agent names (``BlueLake``). ``relay mail
setup`` registers one agent per seat and stores the generated names in
``.relay/mail_names.json``; everywhere else you can just use the seat name.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from .config import Config
from .seats import mail_project_key
from .store import read_json_lenient, write_json


def _url(cfg: Config) -> str:
    return cfg.mail.get("url", "http://127.0.0.1:8765/mcp/")


async def _call(url: str, tool: str, args: dict[str, Any]) -> Any:
    try:
        from mcp import ClientSession
        from mcp.client.streamable_http import streamablehttp_client
    except ImportError as exc:  # pragma: no cover - depends on the optional extra
        raise RuntimeError("team mail needs the optional extra: pip install agent-relay[mail]") from exc
    async with streamablehttp_client(url) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool(tool, args)
            text = "\n".join(getattr(c, "text", "") for c in result.content)
            if result.isError:
                raise RuntimeError(f"{tool}: {text[:400]}")
            try:
                return json.loads(text)
            except ValueError:
                return text


def call(cfg: Config, tool: str, **args: Any) -> Any:
    return asyncio.run(_call(_url(cfg), tool, args))


def _names_path(cfg: Config) -> str:
    return cfg.state("mail_names.json")


def name(cfg: Config, seat_or_name: str) -> str:
    return read_json_lenient(_names_path(cfg), {}).get(seat_or_name, seat_or_name)


def setup(cfg: Config) -> dict[str, str]:
    key = mail_project_key(cfg)
    call(cfg, "ensure_project", human_key=key)
    names = read_json_lenient(_names_path(cfg), {})
    from . import seats

    for seat in cfg.seats.values():
        engine = seats.current_engine(cfg, seat)
        args = dict(project_key=key, program=engine.name, model=engine.raw.get("model", engine.name),
                    task_description=f"{seat.label} seat of {cfg.project}")
        if names.get(seat.name):
            args["name"] = names[seat.name]
        result = call(cfg, "register_agent", **args)
        names[seat.name] = (result.get("name") if isinstance(result, dict) else None) or names.get(seat.name, seat.name)
    if cfg.reviewer.get("enabled"):
        result = call(cfg, "register_agent", project_key=key, program="agent-relay-reviewer",
                      model="reviewer", task_description=f"Reviewer of {cfg.project}",
                      **({"name": names["reviewer"]} if names.get("reviewer") else {}))
        names["reviewer"] = (result.get("name") if isinstance(result, dict) else None) or names.get("reviewer", "")
    write_json(_names_path(cfg), names)
    return names


def send(cfg: Config, sender: str, to: list[str] | str, subject: str, body: str, thread: str | None = None) -> Any:
    recipients = [name(cfg, t) for t in ([to] if isinstance(to, str) else to)]
    args = dict(project_key=mail_project_key(cfg), sender_name=name(cfg, sender), to=recipients,
                subject=subject, body_md=body, importance="normal")
    if thread:
        args["thread_id"] = thread
    return call(cfg, "send_message", **args)


def inbox(cfg: Config, seat: str, unread_only: bool = True, limit: int = 20) -> list[dict[str, Any]]:
    """Peek at a seat's inbox WITHOUT marking messages read (the agent does that when it handles them)."""
    result = call(cfg, "fetch_inbox", project_key=mail_project_key(cfg), agent_name=name(cfg, seat),
                  unread_only=unread_only, include_bodies=True, limit=limit, mark_read=False)
    messages = result.get("messages", result.get("result", result)) if isinstance(result, dict) else result
    return messages if isinstance(messages, list) else []
