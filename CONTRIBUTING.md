# Contributing

Thanks for helping! Small, focused pull requests are easiest to review.

## Setup

```bash
git clone https://github.com/caionicfilho89-dot/agent-relay
cd agent-relay
python -m pip install -e .
python -m unittest discover -s tests -t .
```

No third-party packages are needed to run the tests.

## Guidelines

- Keep the core free of required dependencies (optional extras are fine).
- Every behavior change comes with a test. Tests must not launch real agents or touch real windows:
  pass fake `launcher`, `stopper`, `paste` or `ask` functions like the existing tests do.
- State files must stay safe under concurrent access: use `store.read_json` / `store.write_json`.
- Hooks must never fail or block an agent.
- Anything that loosens safety (auto-approve flags, deleting files) stays opt-in and documented.

## Good first contributions

- A quota `command` script for a tool you use (print the `{"windows": [...]}` JSON).
- Hook-format support for another agent.
- Better `relay doctor` checks.
