"""Shared test helpers: a throwaway project with a relay.json."""

from __future__ import annotations

import copy
import json
import os
import shutil
import sys
import tempfile
import unittest

from agent_relay import config

BASE = {
    "project": "Test project",
    "accounts": {
        "main": {"source": "file", "label": "Main"},
        "backup": {"source": "file", "label": "Backup"},
        "free": {"source": "none"},
    },
    "seats": {
        "builder": {
            "role": "builds",
            "engines": [
                {"name": "main-hook", "account": "main", "delivery": "hook", "hook_format": "claude"},
                {"name": "backup-cli", "account": "backup", "delivery": "cli",
                 "command": ["backup-agent", "-i", "{prompt}"], "autonomous_args": ["--yolo"]},
            ],
        },
        "tester": {
            "role": "tests",
            "engines": [{"name": "free-cli", "account": "free", "delivery": "cli", "command": ["free-agent", "{prompt}"]}],
        },
    },
    "gate": {"checks": []},
}


class ProjectCase(unittest.TestCase):
    """Each test gets a fresh folder with relay.json and an empty .relay/."""

    raw: dict = BASE

    def setUp(self) -> None:
        self.dir = tempfile.mkdtemp(prefix="agent-relay-test-")
        self.write_config(self.raw)

    def tearDown(self) -> None:
        shutil.rmtree(self.dir, ignore_errors=True)

    def write_config(self, raw: dict) -> config.Config:
        self.path = os.path.join(self.dir, "relay.json")
        with open(self.path, "w", encoding="utf-8") as handle:
            json.dump(raw, handle)
        self.cfg = config.load(self.path)
        return self.cfg

    def variant(self, **changes) -> config.Config:
        raw = copy.deepcopy(self.raw)
        raw.update(changes)
        return self.write_config(raw)


PY = sys.executable
