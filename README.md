# agent-relay

**Run the AI coding agents you already pay for as one team.** When one runs out of quota, the next one takes over its seat automatically, and no task counts as done until it passes your tests.

[Português](README.pt-BR.md) · MIT · Python 3.10+ · no required dependencies

```
$ relay
Quota
  OUT Claude Max: critical | 5h 93%
  OK  Google AI Pro (Antigravity): normal | short 41%
  LOW SuperGrok: economy | period 74% (today 9 of 8 pts)

Seats
  builder      antigravity-cli [normal] doing: T14 Cache the settings file (2 min ago)
  designer     cursor [economy] doing: T15 Empty state for the inbox (6 min ago)
  tester       grok-cli [economy] doing: - (12 min ago)

Board
  [doing] T14 Cache the settings file (builder)
  [doing] T15 Empty state for the inbox (designer)
  [open]  T16 Retry failed uploads [for: builder]
  [done]  T13 Fix the login redirect loop (builder)
```

## Why

Plenty of us pay for more than one AI coding tool: Claude Code, Cursor, Antigravity, Grok, Copilot and others. Each one has its own quota, so in practice you use one until it hits the limit, then copy the context by hand into the next one, and in the meantime the others sit unused.

agent-relay turns those subscriptions into a **team with seats** (builder, designer, tester, and so on):

- **Hand-off by quota.** Each seat lists engines in order of preference. A seat always runs on the first engine whose account still has quota. When Claude runs out, the builder seat moves to Antigravity, keeping the task it was working on. When Claude's quota resets, the seat moves back.
- **A shared task board.** Tasks belong to seats, not to products, so it doesn't matter which AI is in the seat today. When several seats could take a task, the one with more quota left takes it.
- **A completion gate.** "Done" is refused if the agent can't say what changed and how it was checked, if it edited generated folders (`dist/`), or if your test command fails. The refusal reason goes back to the agent.
- **A quota forecast, not only the percentage.** Levels go `normal → economy → critical` based on the current spending pace ("this will run out before the reset") and on a daily budget for weekly limits.
- **An optional AI reviewer and planner.** A cheap model reads each finished task and sends it back as a `Redo` task if it doesn't do what was asked. When the board runs low, it proposes a few small tasks from your `GOALS.md`.
- **Optional extras:** team mail through [MCP Agent Mail](https://github.com/Dicklesworthstone/mcp_agent_mail), and a live view in [Claude Code Agent Monitor](https://github.com/hoangsonww/Claude-Code-Agent-Monitor).

It's a small Python package with no required dependencies. All its state is JSON files in `.relay/` next to your project.

## How agents receive work

| Engine kind | Used for | How it works |
|---|---|---|
| `hook` | Claude Code, Grok CLI, Cursor | When the agent finishes a turn, its stop hook runs `relay hook <seat>`. If there is a request, team mail or a free task, the agent keeps going with it instead of stopping. |
| `cli` | Antigravity CLI (`agy`), any terminal agent | agent-relay starts the command for the seat, with the instructions in a file. It stops the command when the seat hands off and restarts it if it exits or goes silent while there is work. |
| `window` | Desktop apps without hooks (Windows) | The dispatcher types into the app's chat box, but only while you are not typing. It confirms the message was really sent and never presses "send" twice, because in many apps that button turns into "stop". |
| `manual` | You, or anything else | Tracked on the board only. |

## Quick start

```bash
pip install git+https://github.com/caionicfilho89-dot/agent-relay
cd your-project
relay init            # writes relay.json and adds .relay/ to .gitignore
# edit relay.json: your accounts, seats and test command (see examples/relay.json)
relay doctor          # checks commands, paths and the mail server
relay hook-config builder   # prints the stop-hook snippet for Claude Code / Cursor
relay add "Fix the login redirect loop" --for builder
relay run             # the dispatcher: quota, hand-offs, deliveries, reviews
```

Agents work through the same CLI. Each seat's instructions tell them to:

```bash
relay inbox builder                 # requests for this seat
relay claim builder                 # current task, or the next free one
relay status builder --doing "T14 Cache the settings file"
relay done builder T14 "settings.py: cache in memory; pytest 58 passed; startup 1.9s -> 0.6s"
```

## Configuration

`relay.json` is found by walking up from the current folder, or set with `--config` / `AGENT_RELAY_CONFIG`. A full example is in [`examples/relay.json`](examples/relay.json).

**Accounts** are subscriptions. Each one has a `source`:

| source | reads |
|---|---|
| `file` | `.relay/usage/<account>.json`. Set it with `relay quota set claude 5h 82 --resets 2026-01-01T18:00:00Z`. |
| `command` | Any script that prints `{"windows": [{"name": "5h", "used_pct": 82, "resets_at": "..."}]}`. |
| `grok_log` | The `creditUsagePercent` the Grok CLI writes to `~/.grok/logs/unified.jsonl`. |
| `agy` | The per-model quota reported by the Antigravity CLI (one cheap call). |
| `none` | Not measured. The account is always `normal`. |

When a tool prints "quota reached", "usage limit" and similar messages, the account is marked exhausted for `exhausted_hours` (default 24). You can also do it by hand with `relay quota exhausted <account>`.

**Seats** list `engines` in order of preference. A seat hands off to the first engine whose account is not `critical`, and never more than once per `timing.switch_cooldown_min` (default 30), so a quota hovering around a limit can't make it ping-pong.

**Gate:** `gate.checks` holds the commands that must pass, `gate.forbidden_paths` the generated folders, plus `min_result_chars` and `require_test_mention`.

**Timing:** every interval is in `timing` (see `agent_relay/config.py`).

### Autonomous mode is off by default

Many agent CLIs have an "approve everything" flag. agent-relay **only** adds an engine's `autonomous_args` when you set `"allow_autonomous": true`, and `relay doctor` warns when it is on. An agent with auto-approve can run any command your user can run. If you turn it on, use a sandbox, a container or a throwaway clone, and keep secrets out of reach.

## Commands

```
relay                       quota, seats and board at a glance
relay add / board / release task board
relay claim / done / status (agents) take, finish and announce work
relay ask SEAT "text"       queue a request for a seat
relay inbox SEAT            (agents) read requests
relay hook SEAT             stop hook for Claude Code, Grok CLI and Cursor (always exits 0)
relay hook-config SEAT      print the hook snippet to install
relay quota [update|set|force|exhausted|clear]
relay seats [auto|switch SEAT ENGINE|prompt SEAT]
relay run [--once]          dispatcher loop
relay review [--plan]       run the reviewer or the planner once
relay mail setup|inbox|send team mail (pip install "agent-relay[mail]")
relay doctor                check the setup
```

## Design notes

- **Losing state is worse than refusing to write.** If the board file can't be read (another process is mid-write), agent-relay retries and then errors out. It never treats the board as empty, and it refuses to save a board that suddenly shrank by half. This guard exists because that exact bug once wiped a real board of 88 tasks.
- **Hooks can't trap an agent.** `relay hook` always exits 0, respects the quota level, and has a per-hour cap on "keep going" answers.
- **One dispatcher per project**, enforced by a lock file.
- Windows is the main platform, because desktop delivery needs it. Everything except `window` delivery also works on Linux and macOS, and CI runs the tests on both.

## Status

Alpha. The ideas come from a setup that ran for about a week with up to six agents (Claude Code, Cursor, Antigravity, Grok CLI, a Grok desktop app and Gemini) building a real project on Windows. This package is that setup rewritten to be configurable instead of hard-coded. Expect rough edges, and please report them.

## Contributing

Issues and pull requests are welcome, see [CONTRIBUTING.md](CONTRIBUTING.md). Adapters for more quota sources are especially useful (a `command` script for your tool is a good first PR).

## License

MIT, see [LICENSE](LICENSE).
