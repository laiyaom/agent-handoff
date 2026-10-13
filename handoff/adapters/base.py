"""Agent Adapter interface.

An adapter turns one AgentRequest into one AgentResult. The orchestrator owns
everything else (state, git diff, tests, verdict parsing, logging), so a new
backend — Codex SDK/App Server, Claude Agent SDK, Gemini — only implements run().
"""

from __future__ import annotations

import os
import subprocess
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path


@dataclass
class AgentRequest:
    task_id: str
    phase: str           # develop | fix | review
    mode: str            # "write" or "read"
    prompt: str
    workdir: Path
    round_dir: Path      # adapter may drop extra artifacts here
    timeout: int


@dataclass
class AgentResult:
    exit_code: int
    output: str          # the agent's final message (handoff text)
    stdout: str = ""
    stderr: str = ""
    duration_s: float = 0.0
    timed_out: bool = False


class AgentAdapter(ABC):
    def __init__(self, name: str, spec: dict):
        self.name = name
        self.spec = spec

    @abstractmethod
    def run(self, req: AgentRequest) -> AgentResult: ...


def run_process(argv: list[str], req: AgentRequest, env_extra: dict | None = None) -> AgentResult:
    """Run a CLI agent with the prompt on stdin, enforcing the timeout."""
    env = dict(os.environ, **(env_extra or {}))
    start = time.monotonic()
    try:
        proc = subprocess.Popen(
            argv, cwd=req.workdir, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace", env=env,
        )
    except FileNotFoundError:
        return AgentResult(127, "", "", f"command not found: {argv[0]}", 0.0)
    try:
        out, err = proc.communicate(req.prompt, timeout=req.timeout)
        timed_out = False
    except subprocess.TimeoutExpired:
        proc.kill()
        out, err = proc.communicate()
        timed_out = True
    code = 124 if timed_out else proc.returncode
    return AgentResult(code, out, out, err, round(time.monotonic() - start, 2), timed_out)
